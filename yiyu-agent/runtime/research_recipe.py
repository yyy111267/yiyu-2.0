"""把 Adapter 画像转成可执行的通用研究配方。

研究计划回答“要回答什么”；本模块回答“为回答它至少要取什么、算什么、
如何交付”。它不针对某只股票写分支，而是把行业 Adapter、发展阶段和
事件 Overlay 组合成同一个供 loop 消费的 contract。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml


_BASE_REPORT_TABLES = ["key_metrics", "valuation", "peer_comparison"]

# 只放 Metric Service 当前已实现、可稳定批量计算的指标。其余行业指标保留为
# qualitative_checks，交给官方文件/公告取证，避免配方承诺了不存在的计算能力。
_ADAPTER_RECIPES: dict[str, dict[str, Any]] = {
    "digital_software_platform": {
        "metric_ids": ["ps_ttm", "ev_revenue", "runway", "fcf"],
        "qualitative_checks": [
            "收入质量（商业化/补贴/关联方）", "毛利率趋势", "研发费用率",
            "客户留存或付费客户变化", "股本稀释",
        ],
        "required_documents": ["latest_results", "revenue_breakdown", "customer_concentration"],
        "valuation_methods": ["ps_ttm", "ev_revenue", "reverse_dcf"],
        "invalid_metrics": ["pe_ttm", "roe", "roic", "fcf_yield"],
    },
    "semiconductor": {
        "metric_ids": ["roic", "capex_intensity", "ev_ebitda", "pb"],
        "qualitative_checks": ["产能利用率", "订单出货比", "客户集中度", "技术路线"],
        "required_documents": ["latest_results", "capacity_and_orders"],
        "valuation_methods": ["ev_ebitda", "pb", "ps_ttm"],
        "invalid_metrics": [],
    },
    "advanced_manufacturing": {
        "metric_ids": ["roic", "capex_intensity", "ev_ebitda", "fcf"],
        "qualitative_checks": ["产能利用率", "订单出货比", "客户集中度", "单价压力"],
        "required_documents": ["latest_results", "capacity_and_orders"],
        "valuation_methods": ["ev_ebitda", "pb", "normalized_pe"],
        "invalid_metrics": [],
    },
    "consumer_brand": {
        "metric_ids": ["roic", "fcf", "fcf_yield", "pe_ttm"],
        "qualitative_checks": ["量价拆分", "渠道库存", "合同负债", "资本配置"],
        "required_documents": ["latest_results", "channel_inventory"],
        "valuation_methods": ["dcf", "normalized_pe", "reverse_dcf"],
        "invalid_metrics": [],
    },
}


def _load_packaged_recipes() -> dict[str, dict[str, Any]]:
    """从行业能力包加载配方，包内 YAML 覆盖旧的内联默认值。"""
    root = Path(__file__).resolve().parents[1]
    recipes: dict[str, dict[str, Any]] = {}
    for manifest_path in sorted((root / "skills").glob("*/capability.yaml")):
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        adapter_id = str(manifest.get("adapter_id") or "")
        recipe_path = (manifest.get("resources") or {}).get("research_recipe")
        if not adapter_id or not recipe_path:
            continue
        recipe = yaml.safe_load((root / str(recipe_path)).read_text(encoding="utf-8")) or {}
        if isinstance(recipe, dict):
            recipes[adapter_id] = recipe
    return recipes


_ADAPTER_RECIPES.update(_load_packaged_recipes())


def _load_business_model_overlays() -> dict[str, dict[str, Any]]:
    """按 charging 枚举加载共享商业模式 Overlay。"""
    path = Path(__file__).resolve().parents[1] / "bus_router" / "business_model_overlays.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    overlays: dict[str, dict[str, Any]] = {}
    for model in raw.get("models", []):
        for charging in model.get("charging_values", []):
            overlays[str(charging)] = model
    return overlays


_BUSINESS_MODEL_OVERLAYS = _load_business_model_overlays()

_DEFAULT_RECIPE: dict[str, Any] = {
    "metric_ids": ["roic", "fcf", "pe_ttm"],
    "qualitative_checks": ["商业模式", "竞争格局", "管理层与风险"],
    "required_documents": ["latest_results"],
    "valuation_methods": ["dcf", "pe_ttm"],
    "invalid_metrics": [],
}


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if item))


def _profile_stage(profile: Any) -> str:
    company_profile = getattr(profile, "company_profile", None)
    tag = getattr(company_profile, "development_stage", None)
    return str(getattr(tag, "value", "") or "")


def _profile_charging(profile: Any) -> str:
    company_profile = getattr(profile, "company_profile", None)
    tag = getattr(company_profile, "charging", None)
    return str(getattr(tag, "value", "") or "")


def build_research_recipe(
    profiles: Iterable[Any], *, user_goal: str = "", event_overlays: Iterable[str] | None = None,
) -> dict[str, Any]:
    """根据所有研究单元生成可合并、可审计的执行配方。

    ``new_listing`` 只能由上游已确认的事件 Overlay 传入；不能只因搜索摘要
    提到“上市”就猜测，以免把普通公司误判成新股。
    """
    profiles = list(profiles)
    adapters = _unique(
        getattr(profile, "selected_adapter", "")
        for profile in profiles
        if not bool(getattr(profile, "adapter_fallback", False))
    )
    stages = _unique(_profile_stage(profile) for profile in profiles)
    charging_values = _unique(_profile_charging(profile) for profile in profiles)
    business_models = _unique(
        (_BUSINESS_MODEL_OVERLAYS.get(charging) or {}).get("id", "")
        for charging in charging_values
    )
    overlays = _unique(event_overlays or [])

    selected = [_ADAPTER_RECIPES.get(adapter, _DEFAULT_RECIPE) for adapter in adapters]
    if not selected:
        selected = [_DEFAULT_RECIPE]
    selected += [
        _BUSINESS_MODEL_OVERLAYS[charging]
        for charging in charging_values if charging in _BUSINESS_MODEL_OVERLAYS
    ]

    recipe = {
        "version": "v1",
        "adapters": adapters,
        "stages": stages,
        "business_models": business_models,
        "overlays": overlays,
        "metric_ids": _unique(metric for item in selected for metric in item.get("metric_ids", [])),
        "qualitative_checks": _unique(check for item in selected for check in item.get("qualitative_checks", [])),
        "required_documents": _unique(doc for item in selected for doc in item.get("required_documents", [])),
        "valuation_methods": _unique(method for item in selected for method in item.get("valuation_methods", [])),
        "invalid_metrics": _unique(metric for item in selected for metric in item.get("invalid_metrics", [])),
        "report_tables": list(_BASE_REPORT_TABLES),
        "peer_rule": "仅比较商业模式与发展阶段相近、报告期和币种可比的同行；无法满足时披露不可比。",
        "goal": str(user_goal or ""),
    }

    # 新股不是行业；它只是额外的资料与口径约束，可叠加到任何 Adapter。
    if "new_listing" in overlays:
        recipe["required_documents"] = _unique(recipe["required_documents"] + [
            "prospectus", "listing_announcement", "post_listing_total_shares",
        ])
        recipe["qualitative_checks"] = _unique(recipe["qualitative_checks"] + [
            "上市后总股本、股份类别与稀释", "历史财务口径连续性",
        ])

    # AI 软件的研发/商业化阶段使用收入估值锚，不把“无成熟利润锚”误写成“不估值”。
    if "digital_software_platform" in adapters and any(
        stage in {"研发期", "商业化验证"} for stage in stages
    ):
        recipe["invalid_metrics"] = _unique(
            recipe["invalid_metrics"] + ["pe_ttm", "roe", "roic", "fcf_yield"]
        )
        recipe["valuation_note"] = (
            "当期亏损不使用 PE/ROE/ROIC；以 PS、EV/Revenue 和隐含增长假设作为收入估值锚，"
            "并单列现金跑道与收入质量。"
        )

    return recipe


def recipe_prompt_block(recipe: dict[str, Any]) -> str:
    """渲染给 Agent 的短契约，避免把实现细节散落到多个 Skill 文本。"""
    if not recipe:
        return ""
    lines = [
        "## 本次研究执行配方（系统根据画像生成，必须遵守）",
        f"- Adapter：{'、'.join(recipe.get('adapters') or ['通用'])}",
        f"- 商业模式：{'、'.join(recipe.get('business_models') or []) or '待核验'}",
        f"- 阶段/Overlay：{'、'.join((recipe.get('stages') or []) + (recipe.get('overlays') or [])) or '未标注'}",
        f"- 优先计算：{'、'.join(recipe.get('metric_ids') or [])}",
        f"- 必补资料：{'、'.join(recipe.get('required_documents') or [])}",
        f"- 定性核验：{'；'.join(recipe.get('qualitative_checks') or [])}",
        f"- 估值方法：{'、'.join(recipe.get('valuation_methods') or [])}",
        f"- 禁用/不适用指标：{'、'.join(recipe.get('invalid_metrics') or []) or '无'}",
        "- 取数：market.get_bundle 是唯一结构化行情入口；单项标准指标优先用 calc.metric，多个标准指标优先用 calc.metrics(metric_ids=...)，均复用统一数据包。"
        "关键字段缺失时，按结构化数据→官方原文→权威资料的顺序补齐，搜索摘要只能作线索。",
        "- 交付：必须包含关键数据表、估值与隐含预期表；只有在同行集合已被证据支持时才输出同业比较表。",
        f"- 同业规则：{recipe.get('peer_rule', '')}",
    ]
    note = recipe.get("valuation_note")
    if note:
        lines.append(f"- 估值说明：{note}")
    return "\n".join(lines)
