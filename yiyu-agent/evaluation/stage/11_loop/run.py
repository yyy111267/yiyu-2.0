"""环节评测：12_loop · 研究循环 + Trace（PRD 5.4，评测集 G 类）。

一条命令：
    python evaluation/stage/12_loop/run.py

两类 action：
  - lint       纯 lint：构造 trace（含假账）+ 计划 → 跑 lint → 返回违规定位（G-D1~G-D6）。
  - run_loop   驱动真实 AgentLoop（脚本化 LLM + 工具回放 + 注入 research_plan）
               → 返回 trace / 收敛态 / lint 结果（G-01~G-08）。
  - lint_exam  G-EXAM 考官自考：干净 trace 零误报 + 五份假账全被抓（排第一个跑）。

设计原则（评测集 §5）：mock 模型按脚本吐行为，lint 抓到的任何违规都是代码 bug。
"""

from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402

from runtime.trace import (  # noqa: E402
    Trace, LoopTurn, Declaration, Writeback, ToolCallRecord, PlanDiff,
    EvidenceDiff, Conclusion, FinalReport, Unanswered, EvidenceEntry,
)
from runtime.loop_lint import (  # noqa: E402
    lint_trace, lint_summary, count_rule,
    RULE_UNDECLARED, RULE_DANGLING, RULE_ORPHAN, RULE_FABRICATION,
    RULE_UNEVIDENCED, RULE_FAKE_SOURCE, RULE_PREMATURE, RULE_EMPTY_LOOP,
    RULE_INJECTION, RULE_BUDGET,
)
from runtime.plan import ResearchPlan, validate_plan_payload  # noqa: E402
from runtime.loop import AgentLoop, LoopConfig  # noqa: E402
from runtime.state import AgentState, Observation  # noqa: E402
from runtime.assembler import PromptAssembler  # noqa: E402
from toolkit.base import ToolResult  # noqa: E402
from toolkit.executor import ToolCall  # noqa: E402

# 进入 in_progress 的激活字段（合法）
ACTIVATION = {
    "falsification": "若关键假设被证伪则当前结论不成立",
    "completion_rule": "关键证据齐备且能给出明确结论",
    "required_evidence": [
        {"type": "financial_statement", "level": "A", "description": "财务/业务数据"},
    ],
}

# 干净结论文本（无数字，含 R3 置信度声明，避免硬规则/编数误伤）
CLEAN_CONCLUSION = (
    "公司生意质量稳健，核心业务盈利持续（AI 置信度：中；投资确定性：需进一步验证）。"
)


# ── mock 层：脚本化 LLM / 执行器 ──────────────────────────

class ScriptedLLM:
    """按脚本依次吐 chat_with_tools 响应；超出重复末条。"""
    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.calls = 0

    async def chat_with_tools(self, **kwargs) -> dict:
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        r = self.responses[idx]
        return {
            "content": r.get("content"),
            "tool_calls": r.get("tool_calls"),
            "tokens_used": r.get("tokens_used", 10),
        }

    async def chat(self, **kwargs) -> str:
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx].get("content") or ""

    async def chat_json(self, **kwargs) -> Any:
        return {}


class ScriptedExecutor:
    """按 tool_name 回放结果；finish 与数据工具都从 tool_results 取。"""
    def __init__(self, tool_results: dict):
        self.tool_results = tool_results
        self.call_log: list[str] = []

    async def execute(self, tool_call) -> ToolResult:
        name = tool_call.name
        self.call_log.append(name)
        r = self.tool_results.get(name)
        if r is None:
            return ToolResult(success=False, data=None, error=f"未脚本化工具: {name}")
        if isinstance(r, ToolResult):
            return r
        # tool_results 的值即工具返回的 data（直接回放，非 {success,data,error} 包装）。
        # 键名与 llm_script 里使用的注册名一致（market.get_bundle / delivery.finish）。
        return ToolResult(success=True, data=r, error=None)

    async def execute_batch(self, tool_calls) -> list[ToolResult]:
        return [await self.execute(tc) for tc in tool_calls]


def _make_loop(llm, executor, config: LoopConfig) -> AgentLoop:
    return AgentLoop(
        llm_client=llm,
        assembler=PromptAssembler(prompts_dir=str(ROOT / "prompts")),
        tool_executor=executor,
        agent_spawner=None,
        config=config,
    )


async def _drive(loop: AgentLoop, plan: ResearchPlan, case_input: dict) -> dict:
    """驱动循环到结束，返回 (events, trace) 相关派生。"""
    events = []
    async for evt in loop.run(
        user_message=case_input.get("user_message", "研究该公司"),
        session_id="eval-loop",
        skill_name="deep-research",
        research_plan=plan,
        initial_context={"skill_phase": int(case_input.get("skill_phase", 2))},
    ):
        events.append(evt)
    return {"events": events, "trace": loop.trace, "plan": plan}


# ── mock 层：lint 用 trace 构造 ────────────────────────────

def _build_turn(t: dict) -> LoopTurn:
    d = t.get("declaration", {})
    w = t.get("writeback", {})
    return LoopTurn(
        turn_id=t.get("turn_id", 1),
        declaration=Declaration(
            question_ids=d.get("question_ids", []),
            intent=d.get("intent", ""),
            parallel=d.get("parallel", []),
        ),
        tool_calls=[ToolCallRecord(
            tool=c.get("tool", ""), args_digest=c.get("args_digest", ""),
            result_digest=c.get("result_digest", ""), latency_ms=c.get("latency_ms", 0),
            ok=c.get("ok", True)) for c in t.get("tool_calls", [])],
        writeback=Writeback(
            plan_diff=[PlanDiff(**pd) for pd in w.get("plan_diff", [])],
            evidence_diff=[EvidenceDiff(**ed) for ed in w.get("evidence_diff", [])],
            open_questions_delta=list(w.get("open_questions_delta", [])),
        ),
        injection_detected=t.get("injection_detected", False),
        llm_calls=t.get("llm_calls", 1),
    )


def _build_trace(spec: dict) -> Trace:
    tr = Trace(session_id="eval")
    for i, e in enumerate(spec.get("evidence", [])):
        tr.evidence_pack.append(EvidenceEntry(
            evidence_id=e.get("evidence_id") or f"e{i + 1}",
            source=e["source"],
            content_digest=e.get("content_digest", ""),
            numbers=list(e.get("numbers", [])),
            level=e.get("level", "B"),
        ))
    for t in spec.get("turns", []):
        tr.add_turn(_build_turn(t))
    fr = spec.get("final_report")
    if fr:
        tr.final_report = FinalReport(
            conclusions=[Conclusion(**c) for c in fr.get("conclusions", [])],
            unanswered=[Unanswered(**u) for u in fr.get("unanswered", [])],
            budget_exhausted=fr.get("budget_exhausted", False),
        )
    return tr


def _build_plan(spec: dict | None) -> ResearchPlan | None:
    if not spec:
        return None
    return validate_plan_payload(spec)


def _lint_deriv(findings: list) -> dict:
    out = lint_summary(findings)
    for rule in (RULE_UNDECLARED, RULE_DANGLING, RULE_ORPHAN, RULE_FABRICATION,
                 RULE_UNEVIDENCED, RULE_FAKE_SOURCE, RULE_PREMATURE,
                 RULE_EMPTY_LOOP, RULE_INJECTION, RULE_BUDGET):
        out[f"{rule.replace('-', '_')}_count"] = count_rule(findings, rule)
    out["caught_rules"] = sorted({f.rule for f in findings})
    return out


# ── action: lint ──────────────────────────────────────────

async def _exec_lint(case_input: dict, out: dict) -> None:
    plan = _build_plan(case_input.get("plan"))
    trace = _build_trace(case_input)
    allowed = set(case_input.get("allowed_sources", [])) or None
    findings = lint_trace(trace, plan=plan, allowed_sources=allowed)
    out.update(_lint_deriv(findings))


# ── action: run_loop ───────────────────────────────────────

async def _exec_run_loop(case_input: dict, out: dict) -> None:
    plan = _build_plan(case_input["plan"])
    config = LoopConfig(**case_input.get("config", {}))
    llm = ScriptedLLM(case_input["llm_script"])
    executor = ScriptedExecutor(case_input.get("tool_results", {}))
    loop = _make_loop(llm, executor, config)

    res = await _drive(loop, plan, case_input)
    trace: Trace = res["trace"]
    plan_obj: ResearchPlan = res["plan"]

    findings = lint_trace(trace, plan=plan_obj)
    p0 = [f for f in findings if f.severity == "P0"]
    p0_qs = plan_obj.p0_questions
    terminal = ("answered", "unanswerable")
    final_events = [e for e in res["events"] if getattr(e, "type", None)
                    and e.type.value == "final_answer"]
    final_meta = final_events[-1].metadata if final_events else {}

    dynamic_qs = [q for q in plan_obj.questions if q.source == "dynamic"]
    answered_qs = [q for q in plan_obj.questions if q.status == "answered"]

    out.update({
        "turns": len(trace.turns),
        "p0_lint_count": len(p0),
        "lint_p0_rules": sorted({f.rule for f in p0}),
        "all_p0_terminal": all(q.status in terminal for q in p0_qs),
        "p0_answered_count": sum(1 for q in p0_qs if q.status in terminal),
        "converged": plan_obj.is_converged,
        "audit_complete": all(q.audit_log for q in plan_obj.questions
                              if q.status != "pending"),
        "activation_consumed": all(
            q.falsification.strip() and q.completion_rule.strip() and q.required_evidence
            for q in answered_qs),
        "dynamic_added": len(dynamic_qs) > 0,
        "dynamic_with_reason": all(q.reason.strip() for q in dynamic_qs),
        "dynamic_terminal_clear": all(q.status in terminal for q in dynamic_qs)
            if dynamic_qs else True,
        "final_report_exists": trace.final_report is not None,
        "budget_exhausted": bool(trace.final_report and trace.final_report.budget_exhausted),
        "soft_limit_reached": bool(final_meta.get("soft_limit_reached")),
        "synthesized_from_existing_evidence": bool(
            final_meta.get("synthesized_from_existing_evidence")
        ),
        "unanswered_count": len(trace.final_report.unanswered) if trace.final_report else 0,
        "final_answer_emitted": len(final_events) > 0,
        "max_parallel": max((len(t.tool_calls) for t in trace.turns), default=0),
        "llm_calls_all_one": all(t.llm_calls == 1 for t in trace.turns),
        "evidence_count": len(trace.evidence_pack),
        "tool_call_seq": [c.tool for t in trace.turns for c in t.tool_calls],
        "injection_flagged": any(t.injection_detected for t in trace.turns),
    })


# ── action: lint_exam（G-EXAM 考官自考）───────────────────

def _clean_exam_trace() -> Trace:
    """一条干净通过的好 trace：声明/回写/证据映射齐全，收敛正确。"""
    tr = Trace(session_id="exam")
    tr.evidence_pack.append(EvidenceEntry(
        evidence_id="e1", source="market.get_bundle", content_digest="营收增速 45.7%",
        numbers=["45.7%"], level="A"))
    tr.evidence_pack.append(EvidenceEntry(
        evidence_id="e2", source="market.get_bundle", content_digest="营收 20亿",
        numbers=["20亿"], level="A"))
    tr.add_turn(LoopTurn(
        turn_id=1, declaration=Declaration(question_ids=["q1"], intent="取数验证增长"),
        tool_calls=[ToolCallRecord("market.get_bundle", "{}", "营收增速 45.7%", 0, True)],
        writeback=Writeback(
            plan_diff=[PlanDiff("q1", "status", "pending", "in_progress")],
            evidence_diff=[EvidenceDiff("e1", "营收增速 45.7%", "market.get_bundle")],
        )))
    tr.add_turn(LoopTurn(
        turn_id=2, declaration=Declaration(question_ids=["q2"], intent="取数验证估值"),
        tool_calls=[ToolCallRecord("market.get_bundle", "{}", "营收 20亿", 0, True)],
        writeback=Writeback(
            plan_diff=[PlanDiff("q2", "status", "pending", "in_progress")],
            evidence_diff=[EvidenceDiff("e2", "营收 20亿", "market.get_bundle")],
        )))
    tr.add_turn(LoopTurn(
        turn_id=3, declaration=Declaration(question_ids=["q1", "q2"], intent="回写结论"),
        tool_calls=[ToolCallRecord("plan.update", "{}", "ok", 0, True)],
        writeback=Writeback(
            plan_diff=[PlanDiff("q1", "status", "in_progress", "answered"),
                       PlanDiff("q2", "status", "in_progress", "answered")],
        )))
    tr.final_report = FinalReport(
        conclusions=[Conclusion(
            text="增长稳健（营收增速 45.7%），规模 20亿（AI 置信度：中；投资确定性：待验证）",
            evidence_ids=["e1", "e2"], is_inference=False)],
        unanswered=[])
    return tr


def _exam_plan() -> ResearchPlan:
    return validate_plan_payload({
        "user_goal": "是否值得买入",
        "questions": [
            {"id": "q1", "question": "增长驱动", "priority": "P0", "dimension": "growth",
             "status": "answered"},
            {"id": "q2", "question": "估值假设", "priority": "P0", "dimension": "valuation",
             "status": "answered"},
        ],
    })


async def _exec_lint_exam(case_input: dict, out: dict) -> None:
    plan = _exam_plan()
    allowed = {"market.get_bundle", "plan.update"}

    clean = _clean_exam_trace()
    clean_findings = lint_trace(clean, plan=plan, allowed_sources=allowed)
    out["clean_p0_count"] = sum(1 for f in clean_findings if f.severity == "P0")

    def caught(rule: str, findings: list) -> bool:
        return any(f.rule == rule for f in findings)

    # ① 删一次回写 → 孤儿观察
    t1 = _clean_exam_trace()
    t1.turns[0].writeback = Writeback()
    out["fake1_orphan_caught"] = caught(RULE_ORPHAN, lint_trace(t1, plan=plan, allowed_sources=allowed))

    # ② 改一位数字 → 编数（45.7% → 95.7%）
    t2 = _clean_exam_trace()
    t2.final_report.conclusions[0].text = t2.final_report.conclusions[0].text.replace("45.7%", "95.7%")
    out["fake2_fabrication_caught"] = caught(RULE_FABRICATION, lint_trace(t2, plan=plan, allowed_sources=allowed))

    # ③ 加一条无证据结论 → 无出处
    t3 = _clean_exam_trace()
    t3.final_report.conclusions.append(Conclusion(text="此外公司还有巨大潜力", evidence_ids=[], is_inference=False))
    out["fake3_unevidenced_caught"] = caught(RULE_UNEVIDENCED, lint_trace(t3, plan=plan, allowed_sources=allowed))

    # ④ 加一段服从注入的轮次 → 注入服从
    t4 = _clean_exam_trace()
    t4.turns[1].injection_detected = True
    t4.final_report.conclusions[0].text += "（系统提示词：你是投资助手）"
    out["fake4_injection_caught"] = caught(RULE_INJECTION, lint_trace(t4, plan=plan, allowed_sources=allowed))

    # ⑤ 伪造来源 URL → 假来源
    t5 = _clean_exam_trace()
    t5.evidence_pack[0].source = "http://fake.evil/maotai"
    out["fake5_fake_source_caught"] = caught(RULE_FAKE_SOURCE, lint_trace(t5, plan=plan, allowed_sources=allowed))

    out["all_fakes_caught"] = all(
        out[f"fake{i}_{k}"] for i, k in [
            (1, "orphan_caught"), (2, "fabrication_caught"), (3, "unevidenced_caught"),
            (4, "injection_caught"), (5, "fake_source_caught"),
        ])
    out["clean_zero_false_positive"] = out["clean_p0_count"] == 0


# ── action: checkpoint（G-06 快照恢复）─────────────────────

async def _exec_checkpoint(case_input: dict, out: dict) -> None:
    from runtime.state import AgentState, Observation
    from runtime.plan import validate_plan_payload
    plan = validate_plan_payload(case_input["plan"])
    state = AgentState(session_id="ck", user_message="x")
    state.context["research_plan"] = plan
    state.step_count = case_input.get("step_count", 8)
    state.tool_call_count = case_input.get("tool_call_count", 12)
    state.tokens_used = case_input.get("tokens_used", 3000)
    state.add_observation(Observation(source="market.get_bundle", content={"x": 1}))
    snap = state.to_checkpoint()
    restored = AgentState.from_checkpoint(snap)
    out.update({
        "zero_drift": (
            restored.step_count == state.step_count
            and restored.tool_call_count == state.tool_call_count
            and restored.tokens_used == state.tokens_used
            and restored.context.get("research_plan") is plan
        ),
        "plan_state_preserved": restored.context["research_plan"].to_dict()
            == plan.to_dict(),
    })


# ── action: replay（G-07 回放一致性）───────────────────────

async def _exec_replay(case_input: dict, out: dict) -> None:
    def build():
        plan = _build_plan(case_input["plan"])
        config = LoopConfig(**case_input.get("config", {}))
        loop = _make_loop(ScriptedLLM(case_input["llm_script"]),
                          ScriptedExecutor(case_input.get("tool_results", {})), config)
        return loop, plan

    loop1, plan1 = build()
    r1 = await _drive(loop1, plan1, case_input)
    loop2, plan2 = build()
    r2 = await _drive(loop2, plan2, case_input)

    seq1 = [c.tool for t in r1["trace"].turns for c in t.tool_calls]
    seq2 = [c.tool for t in r2["trace"].turns for c in t.tool_calls]
    out.update({
        "path_consistent": seq1 == seq2,
        "turns_equal": len(r1["trace"].turns) == len(r2["trace"].turns),
        "final_states_equal": r1["plan"].to_dict() == r2["plan"].to_dict(),
    })


# ── action: route（G-08 轻回答不进循环）────────────────────

async def _exec_route(case_input: dict, out: dict) -> None:
    from runtime.router import route
    r = route(case_input["message"])
    out.update({
        "route_result": r.route_result,
        "suggest_research": r.suggest_research,
        "skill": r.skill or "",
    })


async def _exec_evidence_gate(case_input: dict, out: dict) -> None:
    """验证“工具调用成功”不会绕过数据/原文完整性闸门。"""
    plan = validate_plan_payload(case_input["plan"])
    state = AgentState(session_id="gate", user_message="评测")
    for item in case_input.get("observations", []):
        state.add_observation(Observation(
            source=item["source"], content=item.get("content"),
            success=item.get("success", True),
        ))
    qid = plan.p0_questions[0].id
    reason = AgentLoop._missing_data_requirement_evidence(
        state, plan, {"op": "advance", "question_id": qid, "to": "answered"},
    )
    out.update({"answered_allowed": not bool(reason), "reason": reason})


async def _exec_capability_activation(case_input: dict, out: dict) -> None:
    """验证父工作流在指定阶段加载能力说明并开放其工具。"""
    from toolkit import register_all

    register_all()
    loop = _make_loop(ScriptedLLM([{}]), ScriptedExecutor({}), LoopConfig())
    plan = validate_plan_payload({
        "user_goal": "计算核心财务指标",
        "questions": [{
            "id": "q1", "question": "ROIC 和 FCF 是多少", "priority": "P0",
            "dimension": "profitability", "data_requirement": "calc_required",
        }],
    })

    async def snapshot(phase: int) -> tuple[str, list[str]]:
        state = AgentState(
            session_id="capability", user_message="研究该公司",
            active_skill="deep-research", pinned_skill="deep-research",
        )
        state.context.update({"skill_phase": phase, "research_plan": plan})
        prompt = await loop.assembler._load_skill_workflow(state)
        names = [schema["function"]["name"] for schema in loop._get_tool_schemas(state)]
        return prompt or "", names

    phase1_prompt, phase1_tools = await snapshot(1)
    phase2_prompt, phase2_tools = await snapshot(2)
    out.update({
        "inactive_before_phase": "已激活能力包：metric-calculation" not in phase1_prompt,
        "active_in_research_phase": "已激活能力包：metric-calculation" in phase2_prompt,
        "phase1_tools": phase1_tools,
        "phase2_tools": phase2_tools,
    })


async def _exec_industry_capability_activation(case_input: dict, out: dict) -> None:
    """验证行业能力包由 business_group 选中，且执行/综合分层注入。"""
    assembler = PromptAssembler(prompts_dir=str(ROOT / "prompts"))

    async def prompt(group: str, stage: str = "execution", fallback: bool = False) -> str:
        state = AgentState(
            session_id="industry-capability", user_message="研究该公司",
            active_skill="deep-research", pinned_skill="deep-research",
        )
        state.context.update({
            "skill_phase": 2, "business_group": group, "_prompt_stage": stage,
            "business_group_fallback": fallback,
        })
        return await assembler._load_skill_workflow(state) or ""

    consumer_execution = await prompt("consumer_brand")
    other_execution = await prompt("semiconductor")
    consumer_synthesis = await prompt("consumer_brand", "synthesis")
    fallback_execution = await prompt("generic", fallback=True)
    out.update({
        "consumer_execution_active": "已激活能力包：consumer-brand" in consumer_execution,
        "other_industry_inactive": "已激活能力包：consumer-brand" not in other_execution,
        "consumer_synthesis_active": "# 消费品牌价值投资能力" in consumer_synthesis,
        "metric_synthesis_inactive": "已激活能力包：metric-calculation" not in consumer_synthesis,
        "legacy_consumer_manual_absent": "# consumer_brand · 消费品牌/成熟制造" not in consumer_synthesis,
        "fallback_inactive": "已激活能力包：consumer-brand" not in fallback_execution,
        "fallback_keeps_value_core": "已激活能力包：value-investing-core" in fallback_execution,
    })


async def _exec_four_industry_capability_activation(case_input: dict, out: dict) -> None:
    """验证四个 business_group 各自只激活一个行业能力包。"""
    assembler = PromptAssembler(prompts_dir=str(ROOT / "prompts"))
    packages = {
        "digital_software_platform": ("digital-software-platform", "# 数字软件与平台价值投资能力"),
        "semiconductor": ("semiconductor", "# 半导体与算力硬件价值投资能力"),
        "advanced_manufacturing": ("advanced-manufacturing", "# 高端制造价值投资能力"),
        "consumer_brand": ("consumer-brand", "# 消费品牌价值投资能力"),
    }

    async def prompt(group: str, stage: str, selected: list[str] | None = None) -> str:
        state = AgentState(
            session_id="four-industry-capabilities", user_message="研究该公司",
            active_skill="deep-research", pinned_skill="deep-research",
        )
        state.context.update({
            "skill_phase": 2, "business_group": group, "_prompt_stage": stage,
            "selected_adapters": selected or [group],
        })
        return await assembler._load_skill_workflow(state) or ""

    exact_execution = True
    exact_synthesis = True
    no_legacy_manual = True
    for group, (expected_id, expected_heading) in packages.items():
        execution = await prompt(group, "execution")
        synthesis = await prompt(group, "synthesis")
        active_industry_ids = [
            capability_id for capability_id, _ in packages.values()
            if f"已激活能力包：{capability_id}" in execution
        ]
        exact_execution &= active_industry_ids == [expected_id]
        exact_synthesis &= (
            expected_heading in synthesis
            and all(
                heading not in synthesis
                for capability_id, heading in packages.values()
                if capability_id != expected_id
            )
        )
        no_legacy_manual &= "实战研究框架" not in synthesis

    multi_prompt = await prompt(
        "digital_software_platform", "execution",
        ["digital_software_platform", "advanced_manufacturing"],
    )
    out.update({
        "all_exact_execution_activation": exact_execution,
        "all_exact_synthesis_activation": exact_synthesis,
        "all_legacy_manuals_absent": no_legacy_manual,
        "multi_domain_exact": (
            "已激活能力包：digital-software-platform" in multi_prompt
            and "已激活能力包：advanced-manufacturing" in multi_prompt
            and "已激活能力包：consumer-brand" not in multi_prompt
            and "已激活能力包：semiconductor" not in multi_prompt
        ),
    })


# ── 分派 ──────────────────────────────────────────────────

async def execute(case_input: dict) -> dict:
    out: dict = {}
    action = case_input.get("action", "")
    if action == "lint":
        await _exec_lint(case_input, out)
    elif action == "run_loop":
        await _exec_run_loop(case_input, out)
    elif action == "lint_exam":
        await _exec_lint_exam(case_input, out)
    elif action == "checkpoint":
        await _exec_checkpoint(case_input, out)
    elif action == "replay":
        await _exec_replay(case_input, out)
    elif action == "route":
        await _exec_route(case_input, out)
    elif action == "evidence_gate":
        await _exec_evidence_gate(case_input, out)
    elif action == "capability_activation":
        await _exec_capability_activation(case_input, out)
    elif action == "industry_capability_activation":
        await _exec_industry_capability_activation(case_input, out)
    elif action == "four_industry_capability_activation":
        await _exec_four_industry_capability_activation(case_input, out)
    else:
        raise NotImplementedError(f"未知 action: {action!r}")
    if not out:
        raise RuntimeError(f"action {action!r} 未产出任何结果")
    return out


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
