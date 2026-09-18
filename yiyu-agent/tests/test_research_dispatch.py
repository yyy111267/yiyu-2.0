"""离线验证研究需求分流、并发、有限搜索和正文失败回退。"""
import asyncio

from runtime.assembler import PromptAssembler
from runtime.loop import AgentLoop, LoopConfig
from runtime.state import AgentState, Observation
from runtime.trace import Trace, LoopTurn
from runtime.plan import validate_plan_payload
from toolkit.base import ToolResult
from toolkit.executor import ToolCall


class Executor:
    def __init__(self):
        self.calls = []

    async def execute(self, call):
        self.calls.append(call)
        if call.name == "web.search":
            return ToolResult(True, {"results": [{"url": "https://xueqiu.com/broken"},
                                                  {"url": "https://finance.sina.com.cn/article"}]})
        if call.name == "web.fetch":
            if "broken" in call.arguments["url"]:
                return ToolResult(True, {"error": "打不开", "text": ""})
            return ToolResult(True, {"url": call.arguments["url"], "text": "新闻正文及日期、信息出处"})
        return ToolResult(True, {"symbol": "600519", "field_evidence": {"price": {"value": 1}}})

    async def execute_batch(self, calls):
        return await asyncio.gather(*(self.execute(call) for call in calls))


def loop(executor=None, **config):
    result = AgentLoop(object(), PromptAssembler(), executor or Executor(), None, LoopConfig(**config))
    result.trace = Trace(session_id="dispatch")
    return result


def request(fields, **extra):
    return ToolCall("market.get_bundle", {"symbol": "600519", "requested_fields": fields, **extra}, "m")


def test_split_does_not_send_unknown_fields_to_market():
    state = AgentState("split", "调研")
    calls = loop()._match_data_capabilities(state, [request(["price", "客户续约意愿"])])
    market = next(c for c in calls if c.name == "market.get_bundle")
    search = next(c for c in calls if c.name == "web.search")
    assert market.arguments["requested_fields"] == ["price"]
    assert search.arguments["gap_id"] == "600519:客户续约意愿"
    assert search.arguments["sources"] == "finance"
    assert state.context["data_needs"][search.arguments["gap_id"]]["status"] == "unregistered"
    assert all(c.name != "market.get_bundle" for c in loop()._match_data_capabilities(
        AgentState("only-gap", "调研"), [request(["客户续约意愿"])]))


def test_structured_and_search_really_start_concurrently():
    async def go():
        started = set()
        both_started = asyncio.Event()
        class ConcurrentExecutor(Executor):
            async def execute(self, call):
                started.add(call.name)
                if {"market.get_bundle", "web.search"} <= started:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), .5)
                return await super().execute(call)
        agent = loop(ConcurrentExecutor())
        state = AgentState("parallel", "调研")
        calls = agent._match_data_capabilities(state, [request(["price", "客户续约意愿"])])
        results, count = await agent._run_data_calls_with_cache(state, calls)
        assert count == 2 and all(r.success for r in results)
    asyncio.run(go())


def test_same_gap_changed_query_stops_after_two_attempts():
    async def go():
        agent = loop(tool_cache_enabled=False)
        state = AgentState("budget", "调研")
        calls = [ToolCall("web.search", {"query": f"措辞{i}", "gap_id": "same-gap"}, str(i)) for i in range(3)]
        results, count = await agent._run_data_calls_with_cache(state, calls)
        assert count == 2 and len(agent.executor.calls) == 2
        assert not results[2].success and "次数" in results[2].error
    asyncio.run(go())


def test_search_budget_reserves_capacity_for_core():
    async def go():
        agent = loop(max_search_calls=2)
        state = AgentState("background", "调研")
        calls = [ToolCall("web.search", {"query": "背景", "importance": "background"}, "bg"),
                 ToolCall("web.search", {"query": "必答", "gap_id": "core"}, "core")]
        results, count = await agent._run_data_calls_with_cache(state, calls)
        assert count == 1 and not results[0].success and results[1].success
    asyncio.run(go())


def test_alternative_evidence_reduces_search_but_explicit_data_still_requested():
    for importance, searches in [("core", 0), ("user_requested", 1)]:
        state = AgentState("other-evidence", "调研")
        state.add_observation(Observation("web.fetch", {"text": "已读正文"}, evidence_id="e1"))
        calls = loop()._match_data_capabilities(state, [request(["客户续约意愿"], data_needs=[{
            "field": "客户续约意愿", "importance": importance, "evidence_ids": ["e1"], "question_id": "q1",
        }])])
        assert sum(c.name == "web.search" for c in calls) == searches


def test_failed_body_tries_alternative_and_preserves_evidence_links():
    async def go():
        agent = loop()
        state = AgentState("body", "调研")
        calls = agent._match_data_capabilities(state, [request(["客户续约意愿"])])
        turn = LoopTurn(1)
        events = [e async for e in agent._execute_data_calls(state, calls, turn)]
        assert len([c for c in agent.executor.calls if c.name == "web.fetch"]) == 2
        assert any(not obs.success and obs.source == "web.fetch" for obs in state.observations)
        need = state.context["data_needs"]["600519:客户续约意愿"]
        assert need["status"] == "body_read_pending_verification"
        assert "search_evidence_id" not in need
        assert len(turn.writeback.evidence_diff) == 1  # 只有成功正文；摘要与失败页不是证据
        assert events
    asyncio.run(go())


def test_search_summary_cannot_close_linked_question():
    state = AgentState("summary", "调研")
    state.context["data_needs"] = {"g": {"field": "需求", "question_id": "q1", "importance": "core",
                                          "status": "search_clues"}}
    error = AgentLoop._missing_data_requirement_evidence(state, None,
                                                        {"op": "advance", "question_id": "q1", "to": "answered"})
    assert "摘要" in error


def test_missing_core_data_cannot_be_downgraded_for_convenience():
    async def go():
        agent = loop()
        plan = validate_plan_payload({"user_goal": "研究", "questions": [{
            "id": "q1", "question": "核心数据是否支持结论", "priority": "P0", "dimension": "growth",
        }, {"id": "q2", "question": "盈利", "priority": "P0", "dimension": "profitability"}]})
        state = AgentState("priority", "调研")
        state.context.update(research_plan=plan, data_needs={"g": {
            "field": "关键数据", "question_id": "q1", "importance": "core", "status": "request_failed",
        }})
        turn = LoopTurn(1)
        await agent._apply_plan_updates(state, [ToolCall("plan.update", {"operations": [{
            "op": "downgrade", "question_id": "q1", "to": "P1", "reason": "数据难取",
        }]}, "p")], turn)
        assert plan.get("q1").priority == "P0"
        assert not state.observations[-1].success
    asyncio.run(go())
