import asyncio

from runtime.assembler import PromptAssembler
from runtime.context_manager import ContextManager
from runtime.evidence_workspace import build_evidence_working_set
from runtime.loop import AgentLoop
from runtime.plan import PlanQuestion, ResearchPlan
from runtime.state import AgentState, Observation


def _plan(*questions: PlanQuestion) -> ResearchPlan:
    return ResearchPlan(user_goal="分析公司", questions=list(questions))


def test_execution_prompt_uses_compact_contract_not_full_skill_manual():
    state = AgentState(
        session_id="compact", user_message="分析智谱",
        active_skill="deep-research", pinned_skill="deep-research",
    )
    state.context.update({
        "_prompt_stage": "execution",
        "skill_phase": 2,
        "research_plan": _plan(PlanQuestion(
            id="q1", priority="P0", question="现金可支撑多久",
            dimension="financial_health", data_requirement="calc_required",
        )),
    })
    prompt = asyncio.run(PromptAssembler().build(state))

    assert "深度研究执行合同" in prompt.system
    assert "四视角 Evidence Pack 推理结构" not in prompt.system
    assert "## 交付格式（所有 Adapter 共用）" not in prompt.system
    assert len(prompt.system) < 12000


def test_evidence_working_set_selects_relevant_sources_and_deduplicates():
    state = AgentState(session_id="workset", user_message="研究")
    state.context["research_plan"] = _plan(PlanQuestion(
        id="q1", priority="P0", question="现金可支撑多久",
        dimension="financial_health", data_requirement="calc_required",
    ))
    state.observations.extend([
        Observation("web.search", {"results": [{"title": "无关管理层"}]}, evidence_id="e1"),
        Observation("calc.metrics", {"value": 18, "unit": "月"}, evidence_id="e2"),
        Observation("calc.metrics", {"value": 18, "unit": "月"}, evidence_id="e3"),
        Observation("market.get_bundle", {"cash": 100, "raw": "x" * 5000}, evidence_id="e4"),
    ])

    text, breakdown = build_evidence_working_set(state)
    assert "e2" not in text  # 同内容只保留最新证据 id
    assert "e3" in text and "e4" in text
    assert "e1" not in text
    assert "x" * 100 not in text
    assert breakdown["working_set_items"] == 2


def test_research_tool_surface_follows_pending_data_requirements():
    state = AgentState(session_id="tools", user_message="研究")
    state.context["research_plan"] = _plan(PlanQuestion(
        id="q1", priority="P0", question="现金可支撑多久",
        dimension="financial_health", data_requirement="calc_required",
    ))
    names = [
        "market_get_bundle", "calc_metric", "calc_metrics",
        "calc_run_code", "cognition_recall", "cognition_extract", "web_search",
        "web_fetch", "delivery_finish",
    ]
    schemas = [{"function": {"name": name}} for name in names]
    visible = {
        s["function"]["name"] for s in AgentLoop._prune_research_tools(state, schemas)
    }

    assert {"market_get_bundle", "calc_metric", "calc_metrics",
            "cognition_recall", "web_search"} <= visible
    assert "calc_run_code" not in visible
    assert "cognition_extract" not in visible
    assert "delivery_finish" not in visible


def test_context_manager_links_questions_and_traces_selection_reasons():
    state = AgentState(
        session_id="manager", user_message="分析", active_skill="deep-research",
    )
    state.context.update({
        "skill_phase": 2,
        "research_plan": _plan(
            PlanQuestion(
                id="q_cash", priority="P0", question="现金可支撑多久",
                dimension="financial_health", data_requirement="calc_required",
            ),
            PlanQuestion(
                id="q_moat", priority="P0", question="核心护城河是什么",
                dimension="moat", data_requirement="light_evidence",
            ),
        ),
    })
    manager = ContextManager()
    manager.add_evidence(
        state, "e1", "calc.metrics", {"value": 18, "unit": "月", "metric": "cash_runway"},
    )
    manager.add_evidence(
        state, "e2", "web.search", {"results": [{"title": "核心护城河与客户粘性"}]},
    )
    package = manager.prepare(state, [], stage="execution")

    assert set(package.audit["selected_evidence_ids"]) == {"e1", "e2"}
    assert "e1" in package.audit["question_evidence"]["q_cash"]
    assert "e2" in package.audit["question_evidence"]["q_moat"]
    assert package.audit["evidence_tokens_estimate"] > 0


def test_context_manager_drops_low_priority_evidence_when_token_budget_is_full():
    state = AgentState(session_id="packing", user_message="分析", active_skill="deep-research")
    state.context.update({
        "skill_phase": 2,
        "research_plan": _plan(PlanQuestion(
            id="q1", priority="P0", question="增长证据",
            dimension="growth", data_requirement="light_evidence",
        )),
    })
    manager = ContextManager()
    manager.EXECUTION_EVIDENCE_BUDGET = 120
    for i in range(3):
        manager.add_evidence(
            state, f"e{i}", "web.search",
            {"results": [{"title": f"增长证据 {i}", "text": "长" * 300}]},
        )
    package = manager.prepare(state, [], stage="execution")

    assert len(package.audit["selected_evidence_ids"]) < 3
    assert any(item["reason"] == "token_budget" for item in package.audit["dropped_evidence"])


def test_assembler_consumes_context_package_without_rebuilding_dynamic_context():
    state = AgentState(
        session_id="package", user_message="分析", active_skill="deep-research",
    )
    state.context.update({
        "skill_phase": 2,
        "_prompt_stage": "execution",
        "research_plan": _plan(PlanQuestion(
            id="q1", priority="P0", question="增长怎么样",
            dimension="growth", data_requirement="light_evidence",
        )),
    })
    manager = ContextManager()
    manager.add_evidence(state, "e1", "web.search", {"title": "增长证据"})
    package = manager.prepare(state, [], stage="execution")
    prompt = asyncio.run(PromptAssembler().build(state, context_package=package))

    assert prompt.system.count("## 当前研究计划") == 1
    assert prompt.system.count("e1 | web.search") == 1
    assert prompt.breakdown["working_set_items"] == 1


def test_tool_surface_uses_latest_market_result_not_stale_partial_result():
    state = AgentState(
        session_id="latest-market", user_message="分析", active_skill="deep-research",
    )
    state.context.update({
        "skill_phase": 2,
        "research_plan": _plan(PlanQuestion(
            id="q1", priority="P0", question="现金可支撑多久",
            dimension="financial_health", data_requirement="market_data_required",
        )),
    })
    state.observations.extend([
        Observation("market.get_bundle", {
            "fetch_status": "partial", "missing_fields": ["cash"],
        }),
        Observation("market.get_bundle", {
            "fetch_status": "ok", "missing_fields": [], "cash": 100,
        }),
    ])
    schemas = [
        {"function": {"name": "market_get_bundle"}},
        {"function": {"name": "web_search"}},
    ]

    package = ContextManager().prepare(state, schemas, stage="execution")

    assert "market_get_bundle" not in package.audit["tool_names"]
    assert "web_search" in package.audit["tool_names"]


def test_tool_surface_exposes_calc_run_code_only_for_latest_recovery_state():
    state = AgentState(
        session_id="latest-calc", user_message="分析", active_skill="deep-research",
    )
    state.context.update({
        "skill_phase": 2,
        "research_plan": _plan(PlanQuestion(
            id="q1", priority="P0", question="现金可支撑多久",
            dimension="financial_health", data_requirement="calc_required",
        )),
    })
    schemas = [
        {"function": {"name": "calc_metric"}},
        {"function": {"name": "calc_run_code"}},
        {"function": {"name": "web_search"}},
    ]
    state.observations.extend([
        Observation("calc.metric", {"missing_fields": ["cash"]}),
        Observation("calc.metric", {"value": 18, "unit": "month"}),
    ])

    complete = ContextManager().prepare(state, schemas, stage="execution")
    assert "calc_run_code" not in complete.audit["tool_names"]

    state.observations.append(Observation(
        "calc.metric", {"field_recovery_plan": ["fetch annual report"]},
    ))
    recovery = ContextManager().prepare(state, schemas, stage="execution")
    assert "calc_run_code" in recovery.audit["tool_names"]
