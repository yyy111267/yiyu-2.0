"""证据工作集：原始工具结果留在 state，每轮只向模型投影当前问题需要的摘要。"""

from __future__ import annotations

import json
from typing import Any

from runtime.state import AgentState, Observation
from toolkit.registry import resolve_tool_name

_DROP_KEYS = {
    "raw", "debug", "prompt", "prompt_block", "fallback_results", "request",
    "traceback", "html", "markdown", "reasoning", "tool_schema",
}
_PRIORITY_KEYS = (
    "status", "success", "fetch_status", "symbol", "name", "title", "url", "as_of",
    "period", "value", "unit", "currency", "revenue", "revenue_growth", "net_income",
    "cash", "cash_flow", "rd_expense", "market_cap", "price", "pe", "pb", "ps",
    "missing_fields", "field_evidence", "citations", "results", "text", "error", "reason",
)


def compact_evidence(value: Any, *, depth: int = 0) -> Any:
    if depth >= 3:
        return str(value)[:240]
    if isinstance(value, dict):
        keys = [k for k in _PRIORITY_KEYS if k in value]
        keys.extend(k for k in value if k not in keys and k not in _DROP_KEYS)
        return {str(k): compact_evidence(value[k], depth=depth + 1) for k in keys[:24]}
    if isinstance(value, (list, tuple)):
        return [compact_evidence(v, depth=depth + 1) for v in value[:6]]
    if isinstance(value, str):
        return value[:800]
    return value


def _requirements(state: AgentState) -> set[str]:
    plan = state.context.get("research_plan")
    if plan is None:
        return {"light_evidence", "market_data_required", "calc_required", "memory_verify"}
    pending = [
        q for q in getattr(plan, "p0_questions", [])
        if getattr(q, "status", "") not in ("answered", "unanswerable")
    ]
    return {str(getattr(q, "data_requirement", "") or "light_evidence") for q in pending}


def _relevant(source: str, requirements: set[str], *, synthesis: bool) -> bool:
    if synthesis:
        return source.startswith(("market.", "calc.", "web.", "cognition."))
    if source.startswith("calc."):
        return "calc_required" in requirements
    if source.startswith("market."):
        return bool(requirements & {"market_data_required", "calc_required", "memory_verify"})
    if source.startswith("web."):
        return bool(requirements & {"light_evidence", "market_data_required", "memory_verify"})
    if source.startswith("cognition."):
        return "memory_verify" in requirements or not requirements
    return False


def build_evidence_working_set(
    state: AgentState, *, synthesis: bool = False,
) -> tuple[str, dict[str, int]]:
    """返回当前证据工作集及字符分解；不修改原始 observations。"""
    requirements = _requirements(state)
    limit = 8 if synthesis else 5
    chosen: list[tuple[Observation, str, str]] = []
    seen: set[str] = set()
    for obs in reversed(state.observations):
        source = resolve_tool_name(obs.source)
        if not obs.success or obs.content is None or not _relevant(
            source, requirements, synthesis=synthesis,
        ):
            continue
        compact = json.dumps(compact_evidence(obs.content), ensure_ascii=False, separators=(",", ":"))
        compact = compact[:1400 if synthesis else 1000]
        dedup = f"{source}:{compact}"
        if dedup in seen:
            continue
        seen.add(dedup)
        chosen.append((obs, source, compact))
        if len(chosen) >= limit:
            break
    chosen.reverse()

    blocks: list[str] = []
    market_chars = web_chars = 0
    for obs, source, compact in chosen:
        evidence_id = obs.evidence_id or "unindexed"
        block = f"[{evidence_id} | {source}]\n{compact}"
        blocks.append(block)
        if source.startswith("web."):
            web_chars += len(block)
        else:
            market_chars += len(block)
    text = "\n\n".join(blocks)
    return text, {
        "history": len(text),
        "history_market": market_chars,
        "history_web": web_chars,
        "working_set_items": len(chosen),
    }
