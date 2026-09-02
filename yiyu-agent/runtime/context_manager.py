"""请求级上下文管理：证据存储、问题关联、工作集装箱与工具面裁剪。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from runtime.evidence_workspace import compact_evidence
from runtime.state import AgentState, Observation
from toolkit.registry import resolve_tool_name


def _estimate_tokens(text: str) -> int:
    """GLM/中英混合 prompt 的保守本地估算；provider 总 usage 仍是精确账本。"""
    return (len(text or "") + 1) // 2


def _terms(text: str) -> set[str]:
    lowered = str(text or "").lower()
    words = set(re.findall(r"[a-z][a-z0-9_.-]{1,}|\d+(?:\.\d+)?", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.update(chinese[i:i + 2] for i in range(max(0, len(chinese) - 1)))
    return words


def _source_supports(source: str, requirement: str) -> bool:
    if source.startswith("calc."):
        return requirement == "calc_required"
    if source.startswith("market."):
        return requirement in {"market_data_required", "calc_required", "memory_verify"}
    if source.startswith("web."):
        # web 也承担 market/calc 缺字段后的官方原文补数。
        return requirement in {
            "light_evidence", "market_data_required", "calc_required", "memory_verify",
        }
    if source.startswith("cognition."):
        return True  # 默认投资框架与个人认知都可为任一 P0 提供反证视角
    return False


@dataclass
class EvidenceRecord:
    evidence_id: str
    source: str
    raw: Any
    summary: str
    question_ids: list[str]
    quality: int
    sequence: int


class EvidenceStore:
    """原始证据与 Prompt 分离存储；Prompt 只消费 summary + evidence_id。"""

    def __init__(self) -> None:
        self._records: list[EvidenceRecord] = []
        self._seen_ids: set[str] = set()

    def reset(self) -> None:
        self._records.clear()
        self._seen_ids.clear()

    @property
    def records(self) -> list[EvidenceRecord]:
        return list(self._records)

    def add(self, state: AgentState, evidence_id: str, source: str, raw: Any) -> None:
        if not evidence_id or evidence_id in self._seen_ids or raw is None:
            return
        source = resolve_tool_name(source)
        compact = compact_evidence(raw)
        summary = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        summary = summary[:1800]
        summary_terms = _terms(summary)
        linked: list[str] = []
        plan = state.context.get("research_plan")
        for question in getattr(plan, "p0_questions", []) if plan is not None else []:
            if question.status in ("answered", "unanswerable"):
                continue
            requirement = str(question.data_requirement or "light_evidence")
            if not _source_supports(source, requirement):
                continue
            # 来源类型是硬相关；词项重合用于后续排序，不作误删闸门。
            if (source.startswith("cognition.")
                    or summary_terms & _terms(question.question)
                    or requirement != "light_evidence"):
                linked.append(question.id)
        quality = 3 if source == "web.fetch" else 2 if source.startswith(("market.", "calc.")) else 1
        self._records.append(EvidenceRecord(
            evidence_id=evidence_id,
            source=source,
            raw=raw,
            summary=summary,
            question_ids=linked,
            quality=quality,
            sequence=len(self._records) + 1,
        ))
        self._seen_ids.add(evidence_id)

    def sync_observations(self, state: AgentState) -> None:
        for obs in state.observations:
            if obs.success and obs.content is not None and obs.evidence_id:
                self.add(state, obs.evidence_id, obs.source, obs.content)


@dataclass
class ContextPackage:
    stage: str
    plan_block: str
    evidence_block: str
    tool_schemas: list[dict]
    evidence_breakdown: dict[str, int]
    audit: dict[str, Any] = field(default_factory=dict)


class ContextManager:
    """单一入口生成当前轮的 Task/Evidence/Tool 工作集。"""

    EXECUTION_EVIDENCE_BUDGET = 2200
    SYNTHESIS_EVIDENCE_BUDGET = 6000

    def __init__(self) -> None:
        self.evidence_store = EvidenceStore()

    def reset(self) -> None:
        self.evidence_store.reset()

    def add_evidence(
        self, state: AgentState, evidence_id: str, source: str, raw: Any,
    ) -> None:
        self.evidence_store.add(state, evidence_id, source, raw)

    def prepare(
        self, state: AgentState, candidate_tools: list[dict], *, stage: str,
        input_token_budget: int | None = None,
    ) -> ContextPackage:
        self.evidence_store.sync_observations(state)
        plan_block = self._plan_block(state, stage=stage)
        tools = [] if stage == "synthesis" else self._tool_surface(state, candidate_tools)
        default_evidence_budget = (
            self.SYNTHESIS_EVIDENCE_BUDGET if stage == "synthesis"
            else self.EXECUTION_EVIDENCE_BUDGET
        )
        if input_token_budget is None:
            evidence_budget = default_evidence_budget
        else:
            tool_chars = len(json.dumps(tools, ensure_ascii=False))
            fixed_allowance = 5000 if stage == "synthesis" else 2500
            available = (
                input_token_budget - _estimate_tokens(plan_block)
                - _estimate_tokens("x" * tool_chars) - fixed_allowance
            )
            evidence_budget = min(default_evidence_budget, max(256, available))
        evidence_block, evidence_meta = self._pack_evidence(
            state, stage=stage, token_budget=evidence_budget,
        )
        audit = {
            "stage": stage,
            "input_token_budget": input_token_budget or 0,
            "evidence_token_budget": evidence_budget,
            "plan_tokens_estimate": _estimate_tokens(plan_block),
            "evidence_tokens_estimate": _estimate_tokens(evidence_block),
            "candidate_evidence": evidence_meta["candidate_count"],
            "selected_evidence_ids": evidence_meta["selected_ids"],
            "dropped_evidence": evidence_meta["dropped"],
            "question_evidence": evidence_meta["question_evidence"],
            "tool_names": [s.get("function", {}).get("name", "") for s in tools],
        }
        return ContextPackage(
            stage=stage,
            plan_block=plan_block,
            evidence_block=evidence_block,
            tool_schemas=tools,
            evidence_breakdown=evidence_meta["breakdown"],
            audit=audit,
        )

    @staticmethod
    def _pending_questions(state: AgentState) -> list[Any]:
        plan = state.context.get("research_plan")
        return [
            q for q in getattr(plan, "p0_questions", [])
            if q.status not in ("answered", "unanswerable")
        ] if plan is not None else []

    def _plan_block(self, state: AgentState, *, stage: str) -> str:
        plan = state.context.get("research_plan")
        if plan is None:
            return ""
        all_questions = list(getattr(plan, "questions", []))
        visible = all_questions if stage == "synthesis" else self._pending_questions(state)
        payload = [
            {
                "id": q.id, "priority": q.priority, "status": q.status,
                "question": q.question, "dimension": q.dimension,
                "data_requirement": q.data_requirement,
                **({"reason": q.reason} if q.status == "unanswerable" and q.reason else {}),
            }
            for q in visible
        ]
        completed = [
            q.id for q in all_questions
            if q.priority == "P0" and q.status in ("answered", "unanswerable")
        ]
        block = "## 当前研究计划\n" + json.dumps(payload, ensure_ascii=False)
        if stage != "synthesis":
            if completed:
                block += "\n已收敛 P0：" + ", ".join(completed)
            block += (
                "\n只处理上述未完成 P0；用 plan.update 批量回写状态。"
                "data_requirement 是硬取证约束，缺对应证据不得 answered。"
            )
        return block

    def _pack_evidence(
        self, state: AgentState, *, stage: str, token_budget: int,
    ) -> tuple[str, dict[str, Any]]:
        pending_ids = {q.id for q in self._pending_questions(state)}
        records = self.evidence_store.records
        scored: list[tuple[int, EvidenceRecord]] = []
        dropped: list[dict[str, str]] = []
        seen_summary: set[str] = set()
        for record in records:
            if record.summary in seen_summary:
                dropped.append({"evidence_id": record.evidence_id, "reason": "duplicate"})
                continue
            seen_summary.add(record.summary)
            linked = len(pending_ids.intersection(record.question_ids))
            if stage != "synthesis" and pending_ids and not linked:
                dropped.append({"evidence_id": record.evidence_id, "reason": "not_relevant_to_active_p0"})
                continue
            score = linked * 100 + record.quality * 10 + record.sequence
            scored.append((score, record))
        scored.sort(key=lambda item: item[0], reverse=True)

        selected: list[EvidenceRecord] = []
        used = 0
        for _, record in scored:
            block = f"[{record.evidence_id} | {record.source}]\n{record.summary}"
            cost = _estimate_tokens(block)
            if used + cost > token_budget:
                dropped.append({"evidence_id": record.evidence_id, "reason": "token_budget"})
                continue
            selected.append(record)
            used += cost
        selected.sort(key=lambda record: record.sequence)
        blocks = [f"[{r.evidence_id} | {r.source}]\n{r.summary}" for r in selected]
        text = "\n\n".join(blocks)
        market_chars = sum(
            len(block) for block, record in zip(blocks, selected)
            if not record.source.startswith("web.")
        )
        web_chars = len(text) - market_chars
        return text, {
            "candidate_count": len(records),
            "selected_ids": [r.evidence_id for r in selected],
            "dropped": dropped,
            "question_evidence": {
                qid: [r.evidence_id for r in records if qid in r.question_ids]
                for qid in sorted({qid for r in records for qid in r.question_ids})
            },
            "breakdown": {
                "history": len(text), "history_market": market_chars,
                "history_web": web_chars, "working_set_items": len(selected),
            },
        }

    def _tool_surface(self, state: AgentState, schemas: list[dict]) -> list[dict]:
        skill = state.active_skill or state.pinned_skill
        if (skill and skill != "deep-research") \
                or int(state.context.get("skill_phase", 2)) != 2:
            return list(schemas)
        pending = self._pending_questions(state)
        requirements = {str(q.data_requirement or "light_evidence") for q in pending}
        successful = {
            resolve_tool_name(obs.source) for obs in state.observations
            if obs.success and obs.content is not None
        }
        latest_market = next((
            obs for obs in reversed(state.observations)
            if obs.success and resolve_tool_name(obs.source).startswith("market.")
        ), None)
        latest_market_data = (
            latest_market.content if latest_market and isinstance(latest_market.content, dict) else {}
        )
        market_needs_retry = latest_market is None or bool(
            latest_market_data.get("missing_fields")
            or str(latest_market_data.get("fetch_status", "")).lower() not in {"", "ok"}
        )
        latest_calc = next((
            obs for obs in reversed(state.observations)
            if obs.success and resolve_tool_name(obs.source).startswith("calc.")
        ), None)
        latest_calc_data = (
            latest_calc.content if latest_calc and isinstance(latest_calc.content, dict) else {}
        )
        calc_recovery_needed = bool(
            latest_calc_data.get("field_recovery_plan") or latest_calc_data.get("missing_fields")
        )
        allowed = {"web.search", "plan.update"}
        if requirements & {"market_data_required", "calc_required", "memory_verify"}:
            if "market.get_bundle" not in successful or market_needs_retry:
                allowed.add("market.get_bundle")
        if "calc_required" in requirements:
            allowed.update({"calc.menu", "calc.metric", "calc.metrics"})
            if calc_recovery_needed:
                allowed.add("calc.run_code")
        if "cognition.recall" not in successful:
            allowed.add("cognition.recall")
        if "web.search" in successful:
            allowed.add("web.fetch")
        plan = state.context.get("research_plan")
        if plan is None or bool(getattr(plan, "is_converged", False)):
            allowed.add("delivery.finish")
        visible = {name.replace(".", "_") for name in allowed}
        return [s for s in schemas if s.get("function", {}).get("name") in visible]
