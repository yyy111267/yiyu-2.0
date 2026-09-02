"""取数需求计划器：把 metric_ids / field_groups 反推为「该取哪些组件」。

取数层改造 P0 的编排入口（详见改造方案）：
  - 输入 metric_ids 时从 metrics_catalog.yaml 展开 required_fields
  - 输出组件清单（snapshot / fundamentals / news / announcements）
  - 输出每个组件的硬预算秒数与失败上限
  - news 仅在显式要求时取，避免每次都打慢源

铁律：只取必要字段，缺字段由 Metric Service 返回 not_disclosed，
不让 Agent 因取数卡住。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from toolkit.calc.metric_service import _catalog_by_id

logger = logging.getLogger(__name__)


# ── 组件预算（单 symbol 本轮）──────────────────────────────
# 单 provider 硬超时：2-5s（用 settings.market_timeout_seconds 兜底）
# 组件总预算：
#   snapshot：3s
#   fundamentals：8-12s
#   news / announcement：5s
# bundle 总预算：约 15s（各组件预算之和的合理上界）
COMPONENT_BUDGETS: dict[str, float] = {
    "snapshot": 3.0,
    "fundamentals": 10.0,
    "news": 5.0,
    "announcements": 5.0,
}

# 同一 symbol + component 连续失败到该次数后，本轮不再打源，直接转兜底
MAX_FAILURES_PER_COMPONENT: int = 3

# metric_id → 所属组件（用于反推要取哪些组件）
# snapshot 字段：market_cap / pe / pb / price（来自行情快照）
# fundamentals 字段：revenue / net_profit / equity / cash / debt / ocf / capex ...（来自财报）
_FIELD_TO_COMPONENT: dict[str, str] = {
    "market_cap": "snapshot",
    "float_market_cap": "snapshot",
    "pe": "snapshot",
    "pe_dynamic": "snapshot",
    "pe_static": "snapshot",
    "pe_ttm": "snapshot",
    "ps_ttm": "snapshot",
    "pb": "snapshot",
    "price": "snapshot",
    "current_price": "snapshot",
    "industry": "snapshot",
    "listing_date": "snapshot",
    # 其余字段（revenue / net_profit / equity / cash / total_debt /
    # operating_cash_flow / capital_expenditure / ebit / ebitda /
    # total_profit / income_tax / interest_expense / minority_interest /
    # trading_financial_assets / depreciation_amortization ...）
    # 一律归 fundamentals
}


@dataclass(frozen=True)
class FetchPlan:
    """一次取数的计划：要取哪些组件、各自预算、缺字段补齐清单。"""

    components: list[str]               # 要取的组件名（snapshot/fundamentals/news/announcements）
    required_fields: list[str]           # 指标反推出的全部必需字段（用于 evidence 登记 + missing 判定）
    missing_field_groups: list[str]     # 未覆盖的字段组（仅用于诊断）
    budgets: dict[str, float] = field(default_factory=dict)
    max_failures: int = MAX_FAILURES_PER_COMPONENT

    def budget_for(self, component: str) -> float:
        return self.budgets.get(component, COMPONENT_BUDGETS.get(component, 5.0))


def expand_required_fields(metric_ids: list[str] | None) -> list[str]:
    """从 metrics_catalog.yaml 展开 metric_ids 的 required_fields（去重保序）。

    未知 metric_id 直接当作字段名原样保留（兼容 LLM 临时点名要某字段）。
    """
    if not metric_ids:
        return []
    catalog = _catalog_by_id()
    out: list[str] = []
    seen: set[str] = set()
    for mid in metric_ids:
        mdef = catalog.get(str(mid).strip())
        if not mdef:
            # 未知 metric_id：当作裸字段名（兼容 "market_cap" 直传）
            name = str(mid).strip()
            if name and name not in seen:
                out.append(name)
                seen.add(name)
            continue
        for f in mdef.get("required_fields") or []:
            f = str(f).strip()
            if f and f not in seen:
                out.append(f)
                seen.add(f)
    return out


def _fields_to_components(fields: list[str]) -> list[str]:
    """把字段名映射到组件（snapshot / fundamentals）。未在表里的默认 fundamentals。"""
    comps: list[str] = []
    seen: set[str] = set()
    for f in fields:
        c = _FIELD_TO_COMPONENT.get(f, "fundamentals")
        if c not in seen:
            comps.append(c)
            seen.add(c)
    return comps


def plan_fetch(
    *,
    metric_ids: list[str] | None = None,
    field_groups: list[str] | None = None,
    days: int | None = None,
) -> FetchPlan:
    """把需求反推为取数计划。

    优先级：metric_ids > field_groups。
    - 传了 metric_ids：从 catalog 展开 required_fields，只取覆盖这些字段的组件。
      pe_ttm 只需要 market_cap + net_profit → 取 snapshot + fundamentals，不取 news。
    - 传了 field_groups：直接按组取（snapshot/fundamentals/news/announcements）。
    - 都不传：取全量（向后兼容旧 bundle() 调用）。
    """
    components: list[str]
    required_fields: list[str] = []

    if metric_ids:
        required_fields = expand_required_fields(metric_ids)
        components = _fields_to_components(required_fields)
        # metric_ids 没有覆盖到 news 字段 → 不取 news（除非显式要）
        if not components:
            components = ["snapshot", "fundamentals"]
    elif field_groups:
        components = [str(g).strip() for g in field_groups if str(g).strip()]
    else:
        # 向后兼容：旧调用不传任何需求 → 取全量
        components = ["snapshot", "fundamentals", "news"]

    # news / announcements 属于事件类，不在 metric 反推范围，但允许显式点名
    if field_groups:
        for g in field_groups:
            g = str(g).strip()
            if g in ("news", "announcements") and g not in components:
                components.append(g)

    budgets = {c: COMPONENT_BUDGETS.get(c, 5.0) for c in components}
    missing_groups = [g for g in (field_groups or []) if g not in components]
    return FetchPlan(
        components=components,
        required_fields=required_fields,
        missing_field_groups=missing_groups,
        budgets=budgets,
    )


def should_fetch_news(plan: FetchPlan) -> bool:
    """是否要取新闻。metric_ids 驱动时不取，除非显式点名。"""
    return "news" in plan.components or "announcements" in plan.components
