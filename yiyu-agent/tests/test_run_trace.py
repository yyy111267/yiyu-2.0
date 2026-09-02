import json

from runtime.events import AgentEvent, EventType
from runtime.run_trace import RunTraceRecorder


def test_run_trace_records_stage_and_internal_error_without_answer_text(tmp_path):
    recorder = RunTraceRecorder("session/unsafe", trace_dir=str(tmp_path))
    recorder.mark("routing_finished", duration_sec=1.2, skill="deep-research")
    recorder.record_event(AgentEvent(
        type=EventType.ANSWER_DELTA,
        content="不应落盘的正文",
        metadata={"step": 1},
    ))
    recorder.record_event(AgentEvent(
        type=EventType.ERROR,
        content="LLM 请求超时",
        metadata={"reason": "llm_retry_exhausted", "error": "TimeoutError(60s)"},
    ))
    recorder.record_event(AgentEvent(
        type=EventType.FINAL_ANSWER,
        content="不应落盘的最终正文",
        metadata={
            "soft_limit_reached": True,
            "soft_limit_reason": "达到 token soft limit（50000）",
            "synthesized_from_existing_evidence": True,
        },
    ))
    recorder.record_event(AgentEvent(
        type=EventType.DEBUG,
        content="",
        metadata={
            "step": 2,
            "context_usage": {
                "selected_evidence_ids": ["e1"],
                "dropped_evidence": [{"evidence_id": "e2", "reason": "token_budget"}],
            },
        },
    ))
    recorder.mark("request_closed", answer_chars=recorder.answer_chars)

    rows = [json.loads(line) for line in recorder.path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["name"] == "routing_finished"
    assert any(row["details"].get("reason") == "llm_retry_exhausted" for row in rows)
    assert any(row["details"].get("soft_limit_reason") == "达到 token soft limit（50000）"
               for row in rows)
    text = recorder.path.read_text(encoding="utf-8")
    assert "不应落盘的正文" not in text
    assert "不应落盘的最终正文" not in text
    assert "TimeoutError(60s)" in text
    assert any(
        row["details"].get("context_usage", {}).get("selected_evidence_ids") == ["e1"]
        for row in rows
    )


def test_run_trace_aggregates_exact_provider_usage_by_stage(tmp_path):
    recorder = RunTraceRecorder("ledger", trace_dir=str(tmp_path))
    recorder.record_llm_usage({
        "stage": "preloop", "input_tokens": 100, "output_tokens": 10,
        "total_tokens": 110, "cached_input_tokens": 20,
        "chars": {"system": 200, "user": 100, "tool_schemas": 2},
    })
    recorder.record_llm_usage({
        "stage": "research", "input_tokens": 300, "output_tokens": 30,
        "total_tokens": 330, "cached_input_tokens": 0,
        "chars": {"system": 500, "user": 100, "tool_schemas": 400},
    })

    ledger = recorder.token_ledger()
    assert ledger["calls"] == 2
    assert ledger["input_tokens"] == 400
    assert ledger["output_tokens"] == 40
    assert ledger["total_tokens"] == 440
    assert ledger["cached_input_tokens"] == 20
    assert ledger["by_stage"]["preloop"]["total_tokens"] == 110
    assert ledger["by_stage"]["research"]["total_tokens"] == 330


def test_usage_breakdown_never_produces_negative_components():
    from runtime.assembler import AssembledPrompt
    from runtime.loop import AgentLoop

    prompt = AssembledPrompt(
        system="s" * 100, user="u" * 20,
        breakdown={
            "system_base": 9999, "context": 80, "user": 20,
            "history_market": 40, "history_web": 10, "plan": 20,
        },
    )
    detail = AgentLoop._llm_usage_detail(100, 5, prompt, [])
    assert detail["input_tokens"] == 100
    assert detail["attribution_method"] == "proportional_chars_estimate"
    assert detail["chars"]["system_base"] == 20
    assert all(value >= 0 for value in detail["est_tokens"].values())
