from __future__ import annotations

import asyncio

from runtime.state import AgentState
from runtime.trace import Trace
from runtime.loop import AgentLoop


def test_web_search_evidence_keeps_original_links() -> None:
    trace = Trace(session_id="cite")

    entry = trace.add_evidence(
        "web.search",
        {
            "results": [
                {
                    "title": "Annual report",
                    "url": "https://www.cninfo.com.cn/report.html",
                    "snippet": "Revenue was 20 billion.",
                    "source_level": "S",
                }
            ],
            "sources_verified": True,
        },
    )

    payload = entry.to_dict()
    assert payload["evidence_id"] == "e1"
    assert payload["url"] == "https://www.cninfo.com.cn/report.html"
    assert payload["source_level"] == "S"
    assert payload["citations"][0]["url"] == "https://www.cninfo.com.cn/report.html"
    assert payload["citations"][0]["snippet"] == "Revenue was 20 billion."


def test_metric_evidence_keeps_field_provenance() -> None:
    trace = Trace(session_id="cite")

    entry = trace.add_evidence(
        "calc.metric",
        {
            "metric_id": "pe_ttm",
            "value": 20.0,
            "unit": "x",
            "caliber_version": "v1.0",
            "period": "2025",
            "source_level": "A",
            "as_of": "2025-12-31",
            "fields": [
                {
                    "field": "market_cap",
                    "period": "current",
                    "source": "westock",
                    "source_level": "A",
                    "as_of": "2026-08-27 10:00:00",
                }
            ],
        },
    )

    payload = entry.to_dict()
    assert payload["metric_id"] == "pe_ttm"
    assert payload["caliber_version"] == "v1.0"
    assert payload["citations"][0]["kind"] == "metric"
    assert payload["citations"][0]["fields"][0]["field"] == "market_cap"


def test_loop_final_citations_are_keyed_by_evidence_id() -> None:
    loop = AgentLoop(
        llm_client=None,
        assembler=None,
        tool_executor=None,
        agent_spawner=None,
    )
    loop.trace = Trace(session_id="cite")
    loop.trace.add_evidence(
        "web.search",
        {"results": [{"title": "Source", "url": "https://example.com/a"}]},
    )

    citations = loop._final_citations()

    assert citations == [{
        "kind": "web",
        "title": "Source",
        "url": "https://example.com/a",
        "source_level": "B",
        "as_of": "",
        "snippet": "",
        "evidence_id": "e1",
    }]


def test_research_answer_normalizer_adds_product_guardrails() -> None:
    loop = AgentLoop(
        llm_client=None,
        assembler=None,
        tool_executor=None,
        agent_spawner=None,
    )
    loop.trace = Trace(session_id="cite")
    loop.trace.add_evidence(
        "web.search",
        {"results": [{"title": "Annual report", "url": "https://example.com/a"}]},
    )
    state = AgentState(session_id="s1", user_message="研究一下", active_skill="deep-research")
    state.context["data_status"] = "partial"

    answer = loop._normalize_research_answer(state, "结论：暂时观望。")

    assert "AI 置信度" in answer
    assert "投资确定性" in answer
    assert "一手验证" in answer
    assert "风险与反证" in answer
    assert "来源与证据" in answer
    assert "https://example.com/a" in answer


def test_timeout_degraded_answer_replaces_intermediate_json() -> None:
    loop = AgentLoop(
        llm_client=None,
        assembler=None,
        tool_executor=None,
        agent_spawner=None,
    )
    loop.trace = Trace(session_id="timeout")
    loop.trace.add_evidence(
        "web.search",
        {"results": [{"title": "Source", "url": "https://example.com/source"}]},
    )
    state = AgentState(session_id="s1", user_message="分析茅台", active_skill="deep-research")

    async def collect():
        return [
            evt async for evt in loop._emit_timeout_degraded_answer(
                state,
                "```json\n[{\"master\":\"巴菲特\"",
                0,
                1,
            )
        ]

    events = asyncio.run(collect())
    answer = events[0].content

    assert events[0].type.value == "final_answer"
    assert events[0].metadata["degraded"] is True
    assert "```json" not in answer
    assert "最终组织答案时达到时间上限" in answer
    assert "https://example.com/source" in answer
