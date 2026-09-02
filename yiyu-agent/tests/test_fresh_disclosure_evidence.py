from __future__ import annotations

import asyncio
from datetime import date

from runtime.loop import AgentLoop
from runtime.plan import validate_plan_payload
from runtime.preloop.facts_builder import _web_search_business
from runtime.schemas import CurrentEntitySchema
from runtime.state import AgentState, Observation
from toolkit.base import ToolResult
from toolkit.executor import ToolCall
from toolkit.web import tools as web_tools


def _entity() -> CurrentEntitySchema:
    return CurrentEntitySchema(
        canonical_name="智谱华章", security_id="02513.HK", code="02513",
        exchange="HK", primary_market="HK", entity_source="explicit", alias_type="fullname",
    )


def _plan(question: str, requirement: str = "market_data_required"):
    return validate_plan_payload({
        "user_goal": "分析智谱",
        "questions": [{
            "id": "q1", "question": question, "priority": "P0",
            "dimension": "financial_health", "data_requirement": requirement,
        }],
    })


def _answer_op() -> dict:
    return {"op": "advance", "question_id": "q1", "to": "answered"}


def test_preloop_queries_current_listing_status_and_reports() -> None:
    queries: list[str] = []

    async def fake_search(query: str, **kwargs):
        queries.append(query)
        return {"results": []}

    asyncio.run(_web_search_business(_entity(), fake_search))
    joined = " ".join(queries)
    assert date.today().isoformat() in joined
    assert "最新上市状态" in joined
    assert "年度报告" in joined
    assert "02513.HK" in joined


def test_finance_search_budget_includes_hkex_official_source() -> None:
    web_tools._whitelist_cache = None
    domains = web_tools._resolve_sources("finance")
    assert domains[:5] == web_tools._resolve_sources("official")
    assert "hkexnews.hk" in domains[:6]


def test_partial_market_call_cannot_answer_market_question() -> None:
    plan = _plan("最新营收和净利润是多少")
    state = AgentState(session_id="partial", user_message="分析智谱")
    state.add_observation(Observation(
        source="market.get_bundle", success=True,
        content={
            "fetch_status": "partial", "missing_fields": ["revenue"],
            "field_evidence": {"net_profit": {"value": -1}},
        },
    ))
    missing = AgentLoop._missing_data_requirement_evidence(state, plan, _answer_op())
    assert "fetch_status=partial" in missing


def test_ok_market_call_still_requires_question_fields() -> None:
    plan = _plan("最新营收是多少")
    state = AgentState(session_id="missing-field", user_message="分析智谱")
    state.add_observation(Observation(
        source="market.get_bundle", success=True,
        content={
            "fetch_status": "ok", "missing_fields": [],
            "field_evidence": {"net_profit": {"value": -1}},
        },
    ))
    missing = AgentLoop._missing_data_requirement_evidence(state, plan, _answer_op())
    assert "revenue" in missing


def test_report_question_requires_official_fetch_not_search_snippet() -> None:
    plan = _plan("最新年度报告披露到哪一期", "light_evidence")
    state = AgentState(session_id="report", user_message="分析智谱")
    state.add_observation(Observation(
        source="web.search", success=True,
        content={"results": [{"url": "https://www1.hkexnews.hk/report.pdf"}]},
    ))
    assert "web.fetch" in AgentLoop._missing_data_requirement_evidence(state, plan, _answer_op())

    state.add_observation(Observation(
        source="web.fetch", success=True,
        content={"url": "https://www1.hkexnews.hk/report.pdf", "text": "Annual report 2025"},
    ))
    assert AgentLoop._missing_data_requirement_evidence(state, plan, _answer_op()) == ""


def test_official_search_result_creates_followup_fetch_call() -> None:
    call = ToolCall(
        name="web.search",
        arguments={"query": "智谱最新上市状态 年度报告"},
        call_id="s1",
    )
    result = ToolResult(success=True, data={
        "results": [{"url": "https://www1.hkexnews.hk/listedco/listconews/report.pdf"}],
    })
    followup = AgentLoop._official_followup_fetch(call, result)
    assert followup is not None
    assert followup.name == "web.fetch"

