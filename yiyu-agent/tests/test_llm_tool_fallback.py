import asyncio
from types import SimpleNamespace

from core.llm import LLMClient, _extract_text_tool_calls
import httpx
from runtime.events import EventType
from runtime.loop import AgentLoop, LoopConfig
from runtime.state import AgentState, Observation
from toolkit.base import ToolResult
from toolkit.executor import ToolCall
from toolkit.delivery.submit_conclusion import sanitize_conclusion, validate_conclusion


TOOLS = [{
    "type": "function",
    "function": {
        "name": "delivery_finish",
        "description": "finish",
        "parameters": {"type": "object"},
    },
}]


def test_extracts_dot_named_tool_call_from_plain_json_content():
    content = '{"name":"delivery.finish","arguments":{"conclusion":"ok"}}'
    calls = _extract_text_tool_calls(content, TOOLS)
    assert calls == [{
        "name": "delivery.finish",
        "arguments": {"conclusion": "ok"},
        "id": "text_call_0",
    }]


def test_rejects_unoffered_or_non_tool_json():
    assert _extract_text_tool_calls('{"name":"admin.delete","arguments":{}}', TOOLS) == []
    assert _extract_text_tool_calls('{"answer":"ordinary json"}', TOOLS) == []


def test_accepts_fenced_tool_call_and_string_arguments():
    content = '```json\n{"tool_calls":[{"function":{"name":"delivery_finish","arguments":"{\\"conclusion\\":\\"ok\\"}"}}]}\n```'
    calls = _extract_text_tool_calls(content, TOOLS)
    assert calls[0]["name"] == "delivery_finish"
    assert calls[0]["arguments"] == {"conclusion": "ok"}


def test_qualitative_research_conclusion_still_requires_real_evidence():
    loop = AgentLoop(None, None, None, None)
    state = AgentState(session_id="s", user_message="research", active_skill="deep-research")
    conclusion = "结论为观望，仍需观察后续经营变化。"

    ok, _, _ = loop._validate_conclusion(state, conclusion)
    assert not ok

    state.add_observation(Observation(
        source="entity.resolve", content={"symbol": "600519.SH"}, success=True,
    ))
    ok, _, _ = loop._validate_conclusion(state, conclusion)
    assert not ok

    state.add_observation(Observation(
        source="market.get_bundle", content={"status": "ok"}, success=True,
    ))
    ok, reason, _ = loop._validate_conclusion(state, conclusion)
    assert ok, reason


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("request failed", request=request, response=response)


def test_non_retryable_llm_errors_fail_fast():
    loop = AgentLoop(None, None, None, None)
    assert loop._is_non_retryable_llm_error(_http_error(401))
    assert loop._is_non_retryable_llm_error(_http_error(402))
    assert loop._is_non_retryable_llm_error(_http_error(403))
    assert "额度" in loop._format_llm_error(_http_error(402))


def test_transient_llm_errors_can_retry():
    loop = AgentLoop(None, None, None, None)
    assert not loop._is_non_retryable_llm_error(_http_error(429))
    assert not loop._is_non_retryable_llm_error(_http_error(500))
    assert "超时" in loop._format_llm_error(httpx.ReadTimeout("slow gateway"))


def test_same_batch_duplicate_tool_calls_execute_once():
    class Executor:
        calls = 0

        async def execute_batch(self, calls):
            self.calls += len(calls)
            return [ToolResult(success=True, data={"group": "G1a"}) for _ in calls]

    executor = Executor()
    loop = AgentLoop(None, None, executor, None)
    state = AgentState(session_id="s", user_message="research")
    calls = [
        ToolCall(name="company.classify", arguments={"symbol": "600519.SH"}, call_id="1"),
        ToolCall(name="company.classify", arguments={"symbol": "600519.SH"}, call_id="2"),
    ]

    results, executed = asyncio.run(loop._run_data_calls_with_cache(state, calls))

    assert executor.calls == executed == 1
    assert len(results) == 2
    assert results[0] is results[1]


def test_streaming_tool_call_accumulates_deltas(monkeypatch):
    sent = {}
    lines = [
        'data: {"choices":[{"delta":{"content":"正在"}}]}',
        'data: {"choices":[{"delta":{"content":"取数"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"market_get_bundle","arguments":"{\\"symbol\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"\\"600519.SH\\"}"}}]}}]}',
        "data: [DONE]",
    ]

    class Response:
        is_error = False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def raise_for_status(self): return None
        async def aread(self): return b""
        async def aiter_lines(self):
            for line in lines:
                yield line

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def stream(self, *args, **kwargs):
            sent.update(kwargs.get("json") or {})
            return Response()

    monkeypatch.setattr("core.llm.httpx.AsyncClient", lambda **kwargs: Client())
    settings = SimpleNamespace(
        llm_api_key="key", llm_model="glm-5.3-flash", llm_provider="hunyuan",
        llm_base_url="https://llm.example/v1", llm_thinking_enabled=True,
        llm_reasoning_effort="low",
    )
    client = LLMClient(settings)
    deltas = []

    # 回调必须是 awaitable；局部小函数避免测试依赖 AsyncMock。
    async def run_and_collect():
        async def collect(piece):
            deltas.append(piece)
        return await client.chat_with_tools_stream(
            "system", "user", TOOLS, on_content_delta=collect,
        )

    result = asyncio.run(run_and_collect())
    assert deltas == ["正在", "取数"]
    assert result["tool_calls"][0]["name"] == "market_get_bundle"
    assert result["tool_calls"][0]["arguments"] == {"symbol": "600519.SH"}
    assert sent["stream"] is True
    assert sent["thinking"] == {"type": "enabled"}
    assert sent["reasoning_effort"] == "low"
    assert sent["max_tokens"] == 2048


def test_loop_soft_time_limit_enters_synthesis_without_calling_exploration_llm():
    class NeverCalledLLM:
        async def chat_with_tools(self, **kwargs):
            raise AssertionError("soft limit 后不应再调用探索 LLM")

    question = SimpleNamespace(id="q1", status="pending", reason="")
    plan = SimpleNamespace(questions=[question], is_converged=False)
    loop = AgentLoop(
        NeverCalledLLM(), None, None, None,
        LoopConfig(timeout_seconds=0, max_steps=2),
    )

    async def collect():
        return [evt async for evt in loop.run(
            "研究", "deadline", "deep-research", research_plan=plan,
            initial_context={"skill_phase": 2},
        )]

    events = asyncio.run(collect())
    final = next(evt for evt in events if evt.type == EventType.FINAL_ANSWER)
    assert final.metadata["degraded"] is True
    assert final.metadata["soft_limit_reached"] is True
    assert "时间 soft limit" in final.metadata["soft_limit_reason"]
    assert final.metadata["budget_exhausted"] is False


def test_numeric_source_gate_normalizes_percent_and_yi_units():
    result = validate_conclusion(
        "营收约1500亿元，增速16%，毛利率91%。",
        tool_observations=[{
            "fundamentals": {
                "revenue_ttm": 150_000_000_000,
                "revenue_growth": 0.16,
                "gross_margin": 0.91,
            },
        }],
        require_numeric_sources=True,
    )
    assert result.passed, result.reasons


def test_r6_allows_rounded_human_readable_numbers():
    """观测 15.6%，结论写 16%（取整）不应被 R6 误拦为编数。"""
    result = validate_conclusion(
        "营收增速16%。",
        tool_observations=[{"fundamentals": {"revenue_growth": 0.156}}],
        require_numeric_sources=True,
    )
    assert result.passed, result.reasons


def test_r6_exempts_inference_context_numbers():
    """预测/假设语境的数字（中枢 10%、预期回到 20 倍）不属于对观测的引用，不拦。"""
    result = validate_conclusion(
        "毛利率91%。若未来增速中枢降至10%，估值预期回到20倍则承压。"
        "仍需结合后续经营变化判断。",
        tool_observations=[{"fundamentals": {"gross_margin": 0.91}}],
        require_numeric_sources=True,
    )
    assert result.passed, result.reasons


def test_r4_allows_negative_no_target_price_disclaimer():
    result = validate_conclusion(
        "本报告不提供目标价；现价1204港元仅作市场事实展示。"
        "仍需结合后续经营变化判断。",
        tool_observations=[{"price": 1204}],
        require_numeric_sources=True,
    )
    assert result.passed, result.reasons


def test_r4_still_blocks_actionable_price_in_negative_sentence():
    result = validate_conclusion(
        "不建议在100元买入。",
    )
    assert "R4_no_price_target" in result.violated_rules


def test_sanitize_conclusion_annotates_unsourced_numbers():
    text, replaced = sanitize_conclusion(
        "毛利率91%，增速16%，PE 20倍。",
        [{"fundamentals": {"gross_margin": 0.91, "revenue_growth": 0.16}}],
    )
    assert replaced == ["20"]
    assert "20倍（推断）" in text
    assert "91%" in text and "91%（推断）" not in text
    recheck = validate_conclusion(
        text,
        tool_observations=[{"fundamentals": {
            "gross_margin": 0.91, "revenue_growth": 0.16,
        }}],
        require_numeric_sources=True,
    )
    assert recheck.passed, recheck.reasons


def test_latency_mode_r6_failure_sanitizes_and_delivers():
    """低延迟路径 R6 失败：程序化标注后直接交付，不整篇重生成。"""
    loop = AgentLoop(None, None, None, None)
    state = AgentState(session_id="s", user_message="research", active_skill="deep-research")
    state.context["latency_mode"] = True
    state.add_observation(Observation(
        source="market.get_bundle",
        content={"fundamentals": {"revenue_growth": 0.16, "gross_margin": 0.91}},
        success=True,
    ))
    answer = "毛利率91%，增速16%，PE 20倍。"

    async def collect():
        rejected = {"v": False}
        events = []
        async for evt in loop._handle_final_answer(state, answer, 0.0, 0, rejected):
            events.append(evt)
        return events, rejected

    events, rejected = asyncio.run(collect())
    final = next(evt for evt in events if evt.type == EventType.FINAL_ANSWER)
    assert "（推断）" in final.content
    assert final.metadata["auto_sanitized"] is True
    assert final.metadata["auto_sanitized_numbers"] == ["20"]
    assert rejected["v"] is False


def test_formal_path_r6_first_reject_then_sanitizes():
    """正式（非低延迟）路径：首次 R6 失败仍打回模型修正，第二次标注放行。"""
    loop = AgentLoop(None, None, None, None)
    state = AgentState(session_id="s", user_message="research", active_skill="deep-research")
    state.add_observation(Observation(
        source="market.get_bundle",
        content={"fundamentals": {"gross_margin": 0.91}},
        success=True,
    ))
    answer = "毛利率91%，PE 20倍。"

    async def first_attempt():
        rejected = {"v": False}
        async for _ in loop._handle_final_answer(state, answer, 0.0, 0, rejected):
            pass
        return rejected

    assert asyncio.run(first_attempt())["v"] is True
    assert state.context["r6_rejects"] == 1

    async def second_attempt():
        rejected = {"v": False}
        events = []
        async for evt in loop._handle_final_answer(state, answer, 0.0, 1, rejected):
            events.append(evt)
        return events, rejected

    events, rejected = asyncio.run(second_attempt())
    final = next(evt for evt in events if evt.type == EventType.FINAL_ANSWER)
    assert "（推断）" in final.content
    assert rejected["v"] is False
