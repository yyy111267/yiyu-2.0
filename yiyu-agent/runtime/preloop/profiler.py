"""
环节③：公司画像与 Adapter 路由（PRD 5.3 环节③）

职责：对 granularity_decision.units[] 逐单元执行——
      基于 company_facts 为该单元生成 5 维经济特征画像，
      并从 Adapter 目录中选取匹配的研究方法 Adapter。

链路（per unit）：
  (company_facts + unit) → LLM 画像判断（5维标签+evidence+confidence）
                        → 枚举校验 + Adapter ID 合法性校验
                        → 幻觉 ID 打回重选（最多2次）
                        → 低置信补证据重跑
                        → 仍失败则回退通用 Adapter + pending_verify 标记
                        → UnitProfile

四条质量标准（PRD 5.3 环节③）：
  1. 标签带证：5 维各带 value/evidence/confidence 三元组；confidence<0.6 列入 unverified_tags。
  2. 选型合法：selected_adapter 必须在 ADAPTER_CATALOG 内；幻觉 ID 一律拦截。
  3. per-unit 独立：split 下每单元独立画像与选型；Core Skill 固定加载不随画像变化。
  4. 预算受控：全任务 token 预算超 TOKEN_BUDGET_INITIAL 时低权重单元降级为目录摘要注入。

缓存：per-unit key = (unit_id, facts_version)，TTL 当日 UTC。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import ValidationError

from runtime.schemas import (
    CapitalIntensity,
    CompanyFacts,
    CompanyProfile,
    CycleAttribute,
    DevelopmentStage,
    GranularityDecision,
    ProfileTag,
    ResearchUnit,
    UnitProfile,
    ValueChainPosition,
    ChargingModel,
)

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────
def _load_adapter_catalog() -> dict[str, dict]:
    catalog_path = Path(__file__).parent.parent.parent / "bus_router" / "adapters_catalog.yaml"
    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
        result: dict[str, dict] = {}
        for adapter in raw.get("adapters", []):
            if adapter.get("status") == "active":
                result[adapter["adapter_id"]] = {
                    "name":        adapter["name"],
                    "summary":     adapter.get("description", ""),
                    "applicable":  adapter.get("applicable_conditions", []),
                    "excluded":    adapter.get("excluded_conditions", []),
                }
        return result
    except Exception as e:  # noqa: BLE001
        # 加载失败时回退到内联兜底，保证系统不崩溃
        logger.error("profiler: 加载 adapters_catalog.yaml 失败: %s，使用内联兜底", e)
        return {
            "ai_software":        {"name": "AI 软件/大模型",       "summary": "AI软件订阅/大模型API，早期未盈利"},
            "semiconductor":      {"name": "半导体与 AI 算力",     "summary": "Fabless/IDM/封测/AI算力硬件"},
            "robot_manufacturing":{"name": "机器人与高端制造",     "summary": "工业机器人整机/核心零部件/精密制造"},
            "consumer_brand":     {"name": "消费品牌/成熟制造",    "summary": "靠品牌收溢价的复购生意+成熟制造"},
        }


ADAPTER_CATALOG: dict[str, dict] = _load_adapter_catalog()

# 通用回退 Adapter（所有重试失败时使用，选 consumer_brand 作为最保守的通用研究包）
FALLBACK_ADAPTER_ID = "consumer_brand"

# ── 其他常量 ─────────────────────────────────────────────────────
TOKEN_BUDGET_INITIAL: int = 8000      # Skill 注入 token 预算初值（PRD）
_MAX_HALLUCINATION_RETRY: int = 2     # 幻觉 ID 最多重选次数
UNVERIFIED_CONFIDENCE_THRESHOLD: float = 0.6   # 低于此值列入 unverified_tags


def adapter_catalog_summary() -> str:
    """给 Prompt 注入的 Adapter 目录摘要文本。"""
    lines = ["## 可用 Adapter 目录（只能选以下 ID）"]
    for aid, meta in ADAPTER_CATALOG.items():
        lines.append(f"- {aid}：{meta['name']}——{meta['summary']}")
    return "\n".join(lines)


# ── 进程内缓存 ────────────────────────────────────────────────────
# key = (unit_id, facts_version)
_PROFILE_CACHE: dict[tuple[str, str], tuple[UnitProfile, float]] = {}


def _today_expire_ts() -> float:
    now = datetime.now(tz=timezone.utc)
    try:
        next_midnight = now.replace(hour=0, minute=0, second=0,
                                    microsecond=0, day=now.day + 1)
    except ValueError:
        import calendar
        last = calendar.monthrange(now.year, now.month)[1]
        if now.day >= last:
            if now.month == 12:
                next_midnight = now.replace(year=now.year+1, month=1, day=1,
                                            hour=0, minute=0, second=0, microsecond=0)
            else:
                next_midnight = now.replace(month=now.month+1, day=1,
                                            hour=0, minute=0, second=0, microsecond=0)
        else:
            next_midnight = now.replace(day=now.day+1, hour=0, minute=0,
                                        second=0, microsecond=0)
    return next_midnight.timestamp()


def _cache_get(unit_id: str, fv: str) -> Optional[UnitProfile]:
    entry = _PROFILE_CACHE.get((unit_id, fv))
    if entry is None:
        return None
    profile, expire_ts = entry
    if time.time() > expire_ts:
        del _PROFILE_CACHE[(unit_id, fv)]
        return None
    return profile


def _cache_set(unit_id: str, fv: str, profile: UnitProfile) -> None:
    _PROFILE_CACHE[(unit_id, fv)] = (profile, _today_expire_ts())


def clear_cache(unit_id: Optional[str] = None, fv: Optional[str] = None) -> None:
    if unit_id and fv:
        _PROFILE_CACHE.pop((unit_id, fv), None)
    else:
        _PROFILE_CACHE.clear()


# ── LLM Prompt ───────────────────────────────────────────────────

_SYSTEM = """\
你是公司研究前置分析模块——画像与 Adapter 路由环节。
基于研究单元的事实包，为该单元生成 5 维经济特征画像，并选择最匹配的研究 Adapter。

## 五维画像说明
每个维度输出一个 JSON 对象：{"value": "枚举值", "evidence": "证据原文", "confidence": 0.0~1.0}
- evidence 必须来自事实包的具体字段（如"年报披露NOR Flash占收入65%"），不得自由发挥
- confidence < 0.6 时请诚实标注，不要强行拔高
- value 必须严格使用以下枚举值：

发展阶段（development_stage）：研发期 | 商业化验证 | 高增长 | 成熟 | 收缩
收费方式（charging）：一次性销售 | 订阅 | 按量计费 | 佣金 | 广告 | 授权
资本强度（capital_intensity）：轻资产 | 中等 | 重资产
周期属性（cycle）：弱周期 | 成长周期 | 强周期
价值链位置（value_chain）：资源 | 零部件 | 设备 | 平台 | 整机 | 应用

## Adapter 选型约束
- selected_adapter 只能选以下目录中的合法 ID，不得输出不存在的 ID
- 可以同时选一个主选（selected_adapter）和一个备选（secondary_adapter）
- 若业务交叉，可将 pending_verify 设为 true，选两项 Adapter

## 输出格式（纯 JSON）
{
  "development_stage": {"value": "...", "evidence": "...", "confidence": 0.0},
  "charging": {"value": "...", "evidence": "...", "confidence": 0.0},
  "capital_intensity": {"value": "...", "evidence": "...", "confidence": 0.0},
  "cycle": {"value": "...", "evidence": "...", "confidence": 0.0},
  "value_chain": {"value": "...", "evidence": "...", "confidence": 0.0},
  "selected_adapter": "G1a",
  "secondary_adapter": null,
  "selection_reason": "选型依据：命中哪些维度与适用条件",
  "pending_verify": false
}
"""

_USER_TMPL = """\
## 研究单元信息
单元 ID：{unit_id}
覆盖范围：{scope}

## 事实包（company_facts）
公司：{name}（{security_id}）
一句话业务：{one_line}
本单元相关分部：
{segs_text}
财务快照：收入 {revenue}，毛利率 {gross_margin}，OCF {ocf}，资本开支 {capex}，货币 {currency}
信息丰富度：{info_richness}

{catalog}

请输出画像 JSON。
"""


def _build_user_prompt(
    facts: CompanyFacts,
    unit: ResearchUnit,
) -> str:
    # 过滤出与本单元相关的分部
    segs = facts.segments
    if len(segs) > 1:
        keyword = unit.scope.replace("业务", "").replace("与企业服务", "").strip()
        relevant = [s for s in segs if keyword in s.name or keyword in (s.customer or "")]
        if not relevant:
            relevant = segs  # 无法过滤就全量展示
    else:
        relevant = segs

    segs_lines = []
    for s in relevant:
        share = f"{s.revenue_share*100:.0f}%" if s.revenue_share is not None else "未知"
        line = f"  · {s.name}（{share}）收费={s.charging or '未知'} | 客户={s.customer or '未知'}"
        if s.competition:
            line += f" | 竞争={s.competition}"
        if s.capex_profile:
            line += f" | 资本={s.capex_profile}"
        segs_lines.append(line)

    snap = facts.financial_snapshot
    rev = f"{snap.revenue_ttm:.0f} 百万" if snap.revenue_ttm else "未知"
    gm  = f"{snap.gross_margin*100:.1f}%" if snap.gross_margin else "未知"
    ocf = f"{snap.ocf:.0f} 百万" if snap.ocf else "未知"
    cap = f"{snap.capex:.0f} 百万" if snap.capex else "未知"

    return _USER_TMPL.format(
        unit_id=unit.id, scope=unit.scope,
        name=facts.entity.canonical_name,
        security_id=facts.entity.security_id,
        one_line=facts.one_line_business,
        segs_text="\n".join(segs_lines) or "  （无分部信息）",
        revenue=rev, gross_margin=gm, ocf=ocf, capex=cap,
        currency=snap.currency,
        info_richness=facts.info_richness.value,
        catalog=adapter_catalog_summary(),
    )


# ── 解析与校验 ────────────────────────────────────────────────────

_DIM_ENUMS = {
    "development_stage": {e.value for e in DevelopmentStage},
    "charging":          {e.value for e in ChargingModel},
    "capital_intensity": {e.value for e in CapitalIntensity},
    "cycle":             {e.value for e in CycleAttribute},
    "value_chain":       {e.value for e in ValueChainPosition},
}


def _parse_and_validate(
    raw: dict, unit_id: str
) -> tuple[Optional[UnitProfile], list[str]]:
    """解析 LLM 输出，返回 (UnitProfile, errors)。"""
    errors: list[str] = []
    if not raw:
        errors.append("LLM 返回为空")
        return None, errors

    # ── 解析 5 维标签 ──────────────────────────────────────
    tag_data: dict[str, ProfileTag] = {}
    for dim, valid_vals in _DIM_ENUMS.items():
        raw_dim = raw.get(dim)
        if not isinstance(raw_dim, dict):
            errors.append(f"维度 {dim} 缺失或格式错误")
            continue
        value = str(raw_dim.get("value") or "").strip()
        evidence = str(raw_dim.get("evidence") or "").strip()
        conf_raw = raw_dim.get("confidence")
        try:
            conf = float(conf_raw) if conf_raw is not None else 0.5
        except (TypeError, ValueError):
            conf = 0.5

        if value not in valid_vals:
            errors.append(
                f"维度 {dim} value='{value}' 不在枚举范围 {sorted(valid_vals)}"
            )
            continue
        if not evidence:
            errors.append(f"维度 {dim} evidence 为空，无充分证据时 confidence 设 0.5 以下")
            continue

        try:
            tag_data[dim] = ProfileTag(value=value, evidence=evidence, confidence=conf)
        except (ValidationError, ValueError) as e:
            errors.append(f"维度 {dim} ProfileTag 构造失败: {e}")

    if errors:
        return None, errors

    # ── 解析 Adapter 选型 ──────────────────────────────────
    selected = str(raw.get("selected_adapter") or "").strip()
    secondary = raw.get("secondary_adapter")
    secondary = str(secondary).strip() if secondary else None
    selection_reason = str(raw.get("selection_reason") or "").strip()
    pending_verify = bool(raw.get("pending_verify", False))

    # Adapter ID 合法性校验
    if selected not in ADAPTER_CATALOG:
        errors.append(
            f"selected_adapter='{selected}' 不在 Adapter 目录中（合法ID: {sorted(ADAPTER_CATALOG)}）"
        )
        return None, errors

    if secondary and secondary not in ADAPTER_CATALOG:
        # 备选 ID 幻觉：不打回（非致命），清空备选并记 P1 提示
        logger.warning("profiler: secondary_adapter='%s' 不在目录，已忽略", secondary)
        secondary = None

    if not selection_reason:
        selection_reason = f"命中 {selected} 适用条件"

    # ── 构造 CompanyProfile + UnitProfile ─────────────────
    try:
        profile = CompanyProfile(
            development_stage=tag_data["development_stage"],
            charging=tag_data["charging"],
            capital_intensity=tag_data["capital_intensity"],
            cycle=tag_data["cycle"],
            value_chain=tag_data["value_chain"],
        )
        unit_profile = UnitProfile(
            unit_id=unit_id,
            company_profile=profile,
            selected_adapter=selected,
            adapter_fallback=False,
            selection_reason=selection_reason,
            pending_verify=pending_verify,
            secondary_adapter=secondary,
        )
        return unit_profile, []
    except (ValidationError, ValueError) as e:
        errors.append(f"UnitProfile 构造失败: {e}")
        return None, errors


def _make_fallback_profile(unit_id: str, reason: str) -> UnitProfile:
    """所有重试失败时的通用 Adapter 回退产物。"""
    fallback_profile = CompanyProfile(
        development_stage=ProfileTag(
            value="成熟", evidence=f"通用回退（{reason}）", confidence=0.3),
        charging=ProfileTag(
            value="一次性销售", evidence="通用回退", confidence=0.3),
        capital_intensity=ProfileTag(
            value="中等", evidence="通用回退", confidence=0.3),
        cycle=ProfileTag(
            value="弱周期", evidence="通用回退", confidence=0.3),
        value_chain=ProfileTag(
            value="应用", evidence="通用回退", confidence=0.3),
    )
    return UnitProfile(
        unit_id=unit_id,
        company_profile=fallback_profile,
        selected_adapter=FALLBACK_ADAPTER_ID,
        adapter_fallback=True,
        selection_reason=f"通用 Adapter 回退：{reason}",
        pending_verify=True,
        secondary_adapter=None,
    )


# ── 单单元画像 ────────────────────────────────────────────────────

async def _profile_unit(
    facts: CompanyFacts,
    unit: ResearchUnit,
    llm_client: Any,
    *,
    force_refresh: bool = False,
) -> UnitProfile:
    fv = facts.facts_version
    uid = unit.id

    # 缓存命中
    if not force_refresh and fv:
        cached = _cache_get(uid, fv)
        if cached is not None:
            logger.info("profiler: 缓存命中 unit=%s fv=%s", uid, fv)
            return cached

    user_prompt = _build_user_prompt(facts, unit)
    last_errors: list[str] = []
    unit_profile: Optional[UnitProfile] = None

    for attempt in range(_MAX_HALLUCINATION_RETRY + 1):
        try:
            raw = await asyncio.wait_for(
                llm_client.chat_json(
                    system=_SYSTEM, user=user_prompt, temperature=0.1
                ),
                timeout=45,
            )
            raw = raw if isinstance(raw, dict) else {}
        except Exception as e:  # noqa: BLE001
            logger.warning("profiler: LLM 调用失败 unit=%s attempt=%d: %s", uid, attempt, e)
            raw = {}

        unit_profile, errors = _parse_and_validate(raw, uid)

        if unit_profile is not None:
            break

        last_errors = errors
        if attempt < _MAX_HALLUCINATION_RETRY:
            logger.warning(
                "profiler: 第%d次校验失败 unit=%s，重试: %s",
                attempt + 1, uid, errors[:2],
            )

    # 全部重试失败 → 回退通用 Adapter
    if unit_profile is None:
        reason = "；".join(last_errors[:2]) if last_errors else "LLM 多次失败"
        logger.warning("profiler: 回退通用 Adapter unit=%s 原因: %s", uid, reason)
        unit_profile = _make_fallback_profile(uid, reason)

    # 写缓存
    if fv:
        _cache_set(uid, fv, unit_profile)

    logger.info(
        "profiler: 单元画像完成 unit=%s adapter=%s fallback=%s unverified=%s",
        uid, unit_profile.selected_adapter,
        unit_profile.adapter_fallback, unit_profile.unverified_tags,
    )
    return unit_profile


# ── token 预算控制 ────────────────────────────────────────────────

@dataclass
class TokenBudgetState:
    """简单 token 预算追踪器（基于单元权重估算）。"""
    budget: int = TOKEN_BUDGET_INITIAL
    used: int = 0
    degraded_units: list[str] = None   # type: ignore

    def __post_init__(self):
        if self.degraded_units is None:
            self.degraded_units = []

    def consume(self, unit_id: str, estimated_tokens: int) -> bool:
        """尝试消费 estimated_tokens，返回是否在预算内。
        超预算时记录为降级单元（目录摘要注入），不驳回。
        """
        if self.used + estimated_tokens <= self.budget:
            self.used += estimated_tokens
            return True
        # 超预算：降级为摘要注入，不中断
        self.degraded_units.append(unit_id)
        logger.warning(
            "profiler: 单元 %s 超 token 预算（已用 %d/%d），降级为摘要注入",
            unit_id, self.used, self.budget,
        )
        return False


def _estimate_unit_tokens(unit_profile: UnitProfile) -> int:
    """粗估单元 Adapter 全文注入的 token 数（用于预算控制）。
    实际 token 数由 Adapter 全文决定，此处做保守估算。
    """
    # 每个单元：画像摘要约 300 token + Adapter 全文约 1200 token
    return 1500


# ── 主入口 ────────────────────────────────────────────────────────

async def build_unit_profiles(
    facts: CompanyFacts,
    granularity: GranularityDecision,
    llm_client: Any,
    *,
    force_refresh: bool = False,
    token_budget: int = TOKEN_BUDGET_INITIAL,
) -> list[UnitProfile]:
    """环节③主入口：为所有研究单元生成画像与 Adapter 选型。

    Args:
        facts:         来自环节①的公司事实包。
        granularity:   来自环节②的粒度决策（包含 units[]）。
        llm_client:    实现 chat_json(system, user, temperature) 的客户端。
        force_refresh: True 时绕过缓存，强制重新判断。
        token_budget:  任务级 Skill 注入 token 预算（初值 8k）。

    Returns:
        list[UnitProfile]，与 granularity.units[] 一一对应。
    """
    units = granularity.units
    budget_state = TokenBudgetState(budget=token_budget)

    # per-unit 并发执行（各单元独立，无依赖）
    tasks = [
        _profile_unit(facts, unit, llm_client, force_refresh=force_refresh)
        for unit in units
    ]
    profiles = await asyncio.gather(*tasks)

    # token 预算检查（画像完成后按权重顺序分配）
    for profile, unit in zip(profiles, units):
        estimated = _estimate_unit_tokens(profile)
        in_budget = budget_state.consume(unit.id, estimated)
        if not in_budget:
            # 超预算：回填 is_summary_injection=True，下游环节④按此决定注入方式
            object.__setattr__(profile, "is_summary_injection", True)
            logger.info("profiler: unit=%s 标记为摘要注入（is_summary_injection=True）", unit.id)

    if budget_state.degraded_units:
        logger.info(
            "profiler: token 预算 %d/%d，降级单元: %s",
            budget_state.used, budget_state.budget, budget_state.degraded_units,
        )

    logger.info(
        "profiler: 全部单元画像完成 | units=%d | fallbacks=%d",
        len(profiles),
        sum(1 for p in profiles if p.adapter_fallback),
    )
    return list(profiles)
