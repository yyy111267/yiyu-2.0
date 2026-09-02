"""
环节②：研究粒度决策（PRD 5.3 环节②）

职责：输入 company_facts，判断本次研究应整体研究还是拆分为多个研究单元，
      产出 GranularityDecision（mode + units[] + confidence + facts_version）。

链路：
  company_facts
      → 缓存查询（facts_version 命中 → 直接复用，不调模型）
      → LLM 结构化判断（Prompt + CompanyFacts）
      → 确定性护栏校验（split 逻辑矛盾 / 空泛理由 / 数量上限 / 差异条件）
      → 置信度检查（< CONFIDENCE_MIN → 补证据重判；仍不足 → 回退 whole）
      → 缓存写入（绑定 facts_version）
      → GranularityDecision

四条质量标准（PRD 5.3 环节② / 评测集 D 类）：
  1. 默认整体：产品/地区/渠道分类不构成拆分理由。
  2. 拆必有据：split 须命中四项差异条件之一 + 事实依据，reason 不接受空泛表述。
  3. 判得一致：与 facts_version 绑定缓存，同版本不重判。
  4. 兜得住：校验失败重试一次，仍不通过回退 mode=whole。

提速设计：
  · 进程内缓存（_GRANULARITY_CACHE），key = facts_version，TTL = 当日 UTC。
  · 回退 whole 是最后兜底，保证下游 per-unit 路由始终有合法输入。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import ValidationError

from runtime.schemas import (
    CompanyFacts,
    DifferentialDimension,
    GranularityDecision,
    GranularityMode,
    ResearchUnit,
)

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────
CONFIDENCE_MIN: float = 0.6       # 低于此值先补证据重判
UNIT_WARN_LIMIT: int = 4          # 研究单元参考上限（超限告警不拦截）
_MAX_RETRIES: int = 1             # 护栏不通过最多重试次数（+1 = 最多 2 次调用）

# ── 进程内缓存 ────────────────────────────────────────────────────
_GRANULARITY_CACHE: dict[str, tuple[GranularityDecision, float]] = {}

# 回退产物的短命 TTL（秒）：5 分钟后允许重试
# 正常产物使用全日 TTL（当日 UTC 00:00 重置）
_FALLBACK_TTL_SECONDS: int = 300


def _today_expire_ts() -> float:
    now = datetime.now(tz=timezone.utc)
    try:
        next_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0,
                                    day=now.day + 1)
    except ValueError:
        # 月末
        import calendar
        last_day = calendar.monthrange(now.year, now.month)[1]
        if now.day >= last_day:
            if now.month == 12:
                next_midnight = now.replace(year=now.year + 1, month=1, day=1,
                                            hour=0, minute=0, second=0, microsecond=0)
            else:
                next_midnight = now.replace(month=now.month + 1, day=1,
                                            hour=0, minute=0, second=0, microsecond=0)
        else:
            next_midnight = now.replace(day=now.day + 1, hour=0, minute=0,
                                        second=0, microsecond=0)
    return next_midnight.timestamp()


def _cache_get(facts_version: str) -> Optional[GranularityDecision]:
    entry = _GRANULARITY_CACHE.get(facts_version)
    if entry is None:
        return None
    decision, expire_ts = entry
    if time.time() > expire_ts:
        del _GRANULARITY_CACHE[facts_version]
        return None
    return decision


def _cache_set(facts_version: str, decision: GranularityDecision) -> None:
    """写缓存。is_fallback=True 的回退产物只写短命 TTL，防止一次失败锁死版本。"""
    if decision.is_fallback:
        expire_ts = time.time() + _FALLBACK_TTL_SECONDS
        logger.debug(
            "granularity: 回退产物写短命缓存 %ds，fv=%s", _FALLBACK_TTL_SECONDS, facts_version
        )
    else:
        expire_ts = _today_expire_ts()
    _GRANULARITY_CACHE[facts_version] = (decision, expire_ts)


def clear_cache(facts_version: Optional[str] = None) -> None:
    """测试/强制刷新时调用。"""
    if facts_version:
        _GRANULARITY_CACHE.pop(facts_version, None)
    else:
        _GRANULARITY_CACHE.clear()


# ── LLM Prompt ───────────────────────────────────────────────────

_SYSTEM = """\
你是公司研究前置分析模块。基于公司事实包判断本次研究应采用的粒度。

## 判断规则（必须遵守）
1. 默认整体研究（mode=whole）。产品/地区/渠道分类仅作分析维度，不因披露多个分类就拆分。
2. 仅当满足以下两个条件时才可拆分（mode=split）：
   a. 部分业务在「盈利模式 / 护城河来源 / 资本投入 / 估值逻辑」中至少一项存在显著差异；
   b. 且不单独研究会显著影响最终判断。
3. 若判断 split，每个 unit 的 reason 必须明确指出命中了 2.a 中的哪一项差异维度，并给出事实依据；
   不接受「业务较多」「比较复杂」「不好说」「综合考虑」等空泛理由。
4. 若证据不足以支撑 split，宁可保守判定 whole，并将不确定性写入 open_questions。
5. 研究单元参考上限 4 个。

## 差异条件枚举（reason 和 differential_dimensions 只能使用以下值）
- profit_model：盈利模式
- moat_source：护城河来源
- capital_input：资本投入
- valuation_logic：估值逻辑

## 输出格式（纯 JSON，不加任何其他文字）
{
  "mode": "whole" | "split",
  "units": [
    {
      "id": "u_<名称缩写，小写下划线>",
      "scope": "覆盖范围描述",
      "reason": "拆分理由（须命中差异条件并给事实依据）；整体研究时写「整体研究，各业务盈利模式/护城河/资本结构高度相近」",
      "differential_dimensions": ["profit_model"] // split 时必填至少一项；whole 时填 []
    }
  ],
  "confidence": 0.0~1.0,
  "open_questions": ["..."]
}
"""

_USER_TMPL = """\
## 公司事实包
公司：{name}（{security_id}）
一句话业务：{one_line}
分部数量：{seg_count}
分部详情（名称/占比/收费/客户/竞争/资本属性）：
{segments_text}
财务快照：收入 {revenue}，毛利率 {gross_margin}，货币 {currency}
open_questions（事实包遗留缺口）：{open_questions}

## 差异判断要点
请逐一对照四项差异条件评估各分部：
1. 盈利模式（profit_model）：各分部的收费方式和利润来源是否存在根本性差异？
2. 护城河来源（moat_source）：各分部的竞争壁垒是否来自不同类型的资产（技术/流量/品牌/规模/牌照）？
3. 资本投入（capital_input）：各分部的资本消耗模式是否显著不同（轻资产软件 vs 重资产制造；无监管合规成本 vs 高监管成本）？
4. 估值逻辑（valuation_logic）：各分部适用的估值方法是否不同（如 PS/流水驱动 vs PE/现金流驱动 vs 监管折价驱动）？

请判断粒度并输出 JSON。
"""


def _build_user_prompt(facts: CompanyFacts) -> str:
    segs = []
    for s in facts.segments:
        share = f"{s.revenue_share*100:.0f}%" if s.revenue_share is not None else "未知"
        parts = [f"  · {s.name}（{share}）"]
        if s.charging:
            parts.append(f"收费={s.charging}")
        if s.customer:
            parts.append(f"客户={s.customer}")
        if s.competition:
            parts.append(f"竞争={s.competition}")
        if s.capex_profile:
            parts.append(f"资本={s.capex_profile}")
        segs.append(" | ".join(parts))
    snap = facts.financial_snapshot
    revenue_str = (f"{snap.revenue_ttm:.0f} 百万{snap.currency}"
                   if snap.revenue_ttm else "未知")
    gm_str = f"{snap.gross_margin*100:.1f}%" if snap.gross_margin else "未知"
    return _USER_TMPL.format(
        name=facts.entity.canonical_name,
        security_id=facts.entity.security_id,
        one_line=facts.one_line_business,
        seg_count=len(facts.segments),
        segments_text="\n".join(segs) or "  （无分部信息）",
        revenue=revenue_str,
        gross_margin=gm_str,
        currency=snap.currency,
        open_questions="; ".join(facts.open_questions) or "无",
    )


# ── LLM 调用 ─────────────────────────────────────────────────────

async def _call_llm(facts: CompanyFacts, llm_client: Any) -> dict:
    user = _build_user_prompt(facts)
    try:
        data = await asyncio.wait_for(
            llm_client.chat_json(system=_SYSTEM, user=user, temperature=0.1),
            timeout=12,
        )
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("granularity: LLM 调用失败: %s", e)
        return {}


# ── 确定性护栏：解析并校验 LLM 输出 ─────────────────────────────

def _parse_and_validate(raw: dict, facts_version: str) -> tuple[
    Optional[GranularityDecision], list[str]
]:
    """把 LLM 输出解析为 GranularityDecision，返回 (decision, errors)。

    errors 非空说明校验不通过，需要打回重试。
    """
    errors: list[str] = []
    if not raw:
        errors.append("LLM 返回为空")
        return None, errors

    mode_raw = raw.get("mode", "")
    if mode_raw not in ("whole", "split"):
        errors.append(f"mode='{mode_raw}' 不合法，必须为 whole 或 split")
        return None, errors

    units_raw: list[dict] = raw.get("units") or []
    confidence = raw.get("confidence")
    open_qs = [str(q) for q in (raw.get("open_questions") or [])]

    # 解析 units
    units: list[ResearchUnit] = []
    for i, u in enumerate(units_raw):
        uid = str(u.get("id") or f"u_{i}").strip()
        scope = str(u.get("scope") or "").strip()
        reason = str(u.get("reason") or "").strip()
        dims_raw: list[str] = u.get("differential_dimensions") or []

        if not uid or not scope or not reason:
            errors.append(f"units[{i}] 缺少必填字段（id/scope/reason）")
            continue

        # 差异条件枚举校验
        valid_dims = {d.value for d in DifferentialDimension}
        bad_dims = [d for d in dims_raw if d not in valid_dims]
        if bad_dims:
            errors.append(
                f"units[{i}].differential_dimensions 含非法值 {bad_dims}，"
                f"合法值：{sorted(valid_dims)}"
            )
            continue

        dims = [DifferentialDimension(d) for d in dims_raw if d in valid_dims]
        try:
            unit = ResearchUnit(
                id=uid, scope=scope, reason=reason,
                differential_dimensions=dims,
            )
            units.append(unit)
        except (ValidationError, ValueError) as e:
            errors.append(f"units[{i}] 构造失败: {e}")

    if errors:
        return None, errors

    # 遥测告警（不阻塞）
    if len(units) > UNIT_WARN_LIMIT:
        logger.warning(
            "granularity: 研究单元数量 %d 超参考上限 %d，请确认是否过度拆分",
            len(units), UNIT_WARN_LIMIT,
        )

    # 构造 GranularityDecision（内部还有 model_validator 的逻辑校验）
    source = raw.get("source") or "LLM 基于事实包判断"
    conf = float(confidence) if confidence is not None else 0.5

    try:
        decision = GranularityDecision(
            mode=GranularityMode(mode_raw),
            units=units,
            source=source,
            confidence=conf,
            facts_version=facts_version,
            open_questions=open_qs,
        )
    except (ValidationError, ValueError) as e:
        errors.append(f"GranularityDecision 构造失败: {e}")
        return None, errors

    return decision, []


# ── 置信度检查与 whole 回退 ────────────────────────────────────────

def _make_whole_fallback(
    facts: CompanyFacts,
    reason: str = "置信度不足或校验失败，安全回退整体研究",
) -> GranularityDecision:
    """产出合法的 whole 单一单元作为最终降级。

    is_fallback=True 标记此结果为降级产物，调用方写短命缓存（5分钟），
    避免一次失败锁死同 facts_version 的未来请求。
    confidence=0.0 如实反映"没有充分证据支撑判断"，不伪装成高置信。
    """
    return GranularityDecision(
        mode=GranularityMode.WHOLE,
        units=[ResearchUnit(
            id="u_company",
            scope=f"{facts.entity.canonical_name} 整体",
            reason=reason,
            differential_dimensions=[],
        )],
        source="安全回退（护栏降级）",
        confidence=0.0,    # 如实反映降级，不伪装高置信
        is_fallback=True,  # 触发短命缓存，防毒化
        facts_version=facts.facts_version,
        open_questions=[f"粒度决策回退原因：{reason}"],
    )


# ── 主入口 ────────────────────────────────────────────────────────

async def decide_granularity(
    facts: CompanyFacts,
    llm_client: Any,
    *,
    force_refresh: bool = False,
) -> GranularityDecision:
    """环节②主入口：基于 company_facts 决定研究粒度。

    Args:
        facts:         来自环节① 的公司事实包（必须有合法 facts_version）。
        llm_client:    实现 chat_json(system, user, temperature) 的客户端。
        force_refresh: True 时绕过缓存，强制重新判断。

    Returns:
        GranularityDecision（绑定 facts_version，已通过全部确定性校验）。
    """
    fv = facts.facts_version
    if not fv:
        logger.warning("granularity: facts_version 为空，无法缓存，直接判断")

    # ── 缓存命中 ────────────────────────────────────────────────
    if not force_refresh and fv:
        cached = _cache_get(fv)
        if cached is not None:
            logger.info("granularity: 缓存命中 facts_version=%s", fv)
            return cached

    logger.info("granularity: 开始粒度决策 %s", facts.entity.security_id)

    # ── LLM 调用 + 护栏，最多 _MAX_RETRIES+1 次 ─────────────────
    decision: Optional[GranularityDecision] = None
    last_errors: list[str] = []

    for attempt in range(_MAX_RETRIES + 1):
        raw = await _call_llm(facts, llm_client)
        decision, errors = _parse_and_validate(raw, fv)

        if decision is not None and not errors:
            # 置信度检查
            if decision.confidence < CONFIDENCE_MIN:
                logger.warning(
                    "granularity: 第%d次判断置信度 %.2f < %.2f，补证据重判",
                    attempt + 1, decision.confidence, CONFIDENCE_MIN,
                )
                last_errors = [
                    f"置信度 {decision.confidence:.2f} 低于阈值 {CONFIDENCE_MIN}，需补充证据"
                ]
                decision = None
                # 重试时会再次调用 LLM（循环下一次）
                continue
            break
        else:
            last_errors = errors
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "granularity: 第%d次护栏校验不通过，重试: %s",
                    attempt + 1, errors,
                )

    # ── 最终兜底：回退 whole ──────────────────────────────────────
    if decision is None:
        fallback_reason = "；".join(last_errors) if last_errors else "LLM 多次失败"
        logger.warning("granularity: 所有尝试失败，回退 whole。原因: %s", fallback_reason)
        decision = _make_whole_fallback(facts, reason=fallback_reason)

    # ── 写缓存 ───────────────────────────────────────────────────
    if fv:
        _cache_set(fv, decision)

    logger.info(
        "granularity: 决策完成 %s | mode=%s | units=%d | conf=%.2f | fv=%s",
        facts.entity.security_id,
        decision.mode.value, len(decision.units),
        decision.confidence, decision.facts_version,
    )
    return decision
