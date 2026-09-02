"""超时降级交付 + 时间预算回归测试。

对应「preloop 从重研究改为轻量启动器」方案第七项的两个新增用例：

4. test_loop_timeout_with_evidence_returns_degraded_report
   已有证据但收尾组织超时 → 必须输出用户可读的降级报告：
   - 不能把半截 JSON / Evidence Pack 当最终答案；
   - 不能静默什么都不给；
   - 已有证据时不能误报「尚未取得可验证证据」。

5. test_maotai_research_does_not_timeout_at_65s
   研究预算独立完成：research_timeout_seconds 不再被 preloop 重复扣减，
   茅台这类深度问题不会在 65s 左右报「研究已达时间上限」。

离线零 LLM：loop 全部走脚本化 mock。

一条命令：python tests/test_loop_degradation.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation" / "stage" / "11_loop"))

from runtime.loop import AgentLoop, LoopConfig  # noqa: E402
from runtime.plan import validate_plan_payload  # noqa: E402
from runtime.state import AgentState, Observation  # noqa: E402
from toolkit.delivery.submit_conclusion import validate_conclusion  # noqa: E402
from run import ScriptedExecutor, _make_loop  # noqa: E402  (11_loop mocks)

passed = 0
failed = 0


def check(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  ✓ {name}")
        passed += 1
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {name}: {type(e).__name__}: {e}")
        failed += 1


# ── 夹具 ──────────────────────────────────────────────────

class TimeoutAfterEvidenceLLM:
    """第 1 轮取数（产出研究证据），第 2 轮起模拟模型组织答案超时。

    刻意不实现 chat_with_tools_stream，走 _call_llm 的非流式分支，
    这样 streamed_answer 为空——正是「半截内容已流出但没成文」的最坏场景。
    """

    def __init__(self, first_tool_calls: list[dict]):
        self.first_tool_calls = first_tool_calls
        self.calls = 0

    async def chat_with_tools(self, **kwargs) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "先取数",
                "tool_calls": self.first_tool_calls,
                "tokens_used": 10,
            }
        raise asyncio.TimeoutError("模型收尾组织答案超时")


class StreamingToolDraftTimeoutLLM:
    """工具决策轮流出内部草稿后卡住，模拟截图中的真实失败模式。"""

    async def chat_with_tools_stream(self, *, on_content_delta, timeout, **kwargs):
        await on_content_delta("我先锁定研究标的，再准备调用行情和财报工具。")
        await asyncio.sleep(timeout + 1)
        return {"content": None, "tool_calls": None, "tokens_used": 0}


class EvidenceThenConvergeLLM:
    """验证低延迟路径：有证据但计划未收敛时，不能直接进入最终成文。"""

    def __init__(self):
        self.tool_rounds = 0
        self.final_system = ""

    async def chat_with_tools(self, **kwargs):
        self.tool_rounds += 1
        if self.tool_rounds == 1:
            return {
                "content": "",
                "tool_calls": [{
                    "name": "market.get_bundle",
                    "arguments": {"symbol": "600519.SH"},
                }],
                "tokens_used": 10,
            }
        return {
            "content": "",
            "tool_calls": [{
                "name": "web.search",
                "arguments": {"query": "贵州茅台增长驱动", "sources": "finance"},
            }],
            "tokens_used": 10,
        }

    async def chat(self, **kwargs):
        self.final_system = kwargs.get("system", "")
        return (
            "## 投资结论与核心矛盾\n"
            "公司具备长期研究价值，但仍需核对增长证据。\n\n"
            "## 生意本质\n核心业务依靠品牌与复购。\n\n"
            "## 认知陪练\n你最需要判断的是增长放缓后是否仍愿意长期持有。\n\n"
            "AI 置信度：中；本分析不代表投资确定性。"
        )


def _degraded_plan():
    return validate_plan_payload({
        "user_goal": "茅台是否值得买入",
        "questions": [
            {"id": "q1", "question": "增长驱动", "priority": "P0", "dimension": "growth"},
        ],
    })


# ── 测试用例 ──────────────────────────────────────────────

def test_loop_timeout_with_evidence_returns_degraded_report():
    """已有证据但收尾超时 → 用户可读的降级报告，而不是半截 JSON 或空回答。"""
    async def go():
        llm = TimeoutAfterEvidenceLLM([
            {"name": "market.get_bundle", "arguments": {"symbol": "600519.SH"}},
        ])
        executor = ScriptedExecutor({"market.get_bundle": {"revenue_growth": "45.7%"}})
        loop = _make_loop(llm, executor, LoopConfig(
            max_steps=6, max_tool_calls=10,
            timeout_seconds=200, llm_timeout_seconds=5,
            finalize_timeout_seconds=5, max_consecutive_llm_failures=2,
        ))

        events = []
        async for evt in loop.run(
            user_message="分析茅台", session_id="degraded-report",
            skill_name="deep-research", research_plan=_degraded_plan(),
            initial_context={"skill_phase": 2},
        ):
            events.append(evt)

        finals = [e for e in events if e.type.value == "final_answer"]
        assert finals, (
            "已有工具证据却收尾超时时，必须给出降级交付，不能什么都不输出")

        final = finals[-1]
        body = (final.content or "").strip()
        meta = final.metadata or {}

        # 标记为降级交付（前端据此渲染提示条，不当事成结论）
        assert meta.get("degraded") is True, f"应标记 degraded，实际 metadata={meta}"
        assert meta.get("budget_exhausted") is False, "最终模型超时不属于行为预算耗尽"
        assert meta.get("finalize_timeout") is True, "应单独标记最终回答超时"

        # 必须是用户可读正文，不能是中间产物
        assert body, "降级报告正文不能为空"
        assert not body.startswith(("{", "[")) , (
            f"降级报告不能是半截 JSON，实际开头: {body[:60]!r}")
        assert "```json" not in body, "降级报告不能含 JSON 代码块"
        assert "Evidence Pack" not in body, "降级报告不能原样吐出 Evidence Pack"

        # 必须说清停止原因
        assert "时间上限" in body, (
            f"降级报告须说明是时间上限导致停止，实际正文: {body[:200]!r}")

        # 已有证据的场景不能误报「尚未取得可验证证据」
        assert "尚未取得" not in body, (
            "已有工具证据时不得提示尚未取得证据（那是无证据场景的文案）")

        assert loop.trace.final_report is not None, "降级交付也要落最终报告"
        assert loop.trace.final_report.budget_exhausted is False

    asyncio.run(go())


def test_maotai_research_does_not_timeout_at_65s():
    """研究预算独立：不再 research - preloop 双重扣减，65s 中途超时不再出现。"""
    from core.config import settings

    # 三段预算（方案第一节要求值）
    assert settings.research_timeout_seconds >= 200, (
        f"research 预算应 >=200s（茅台类深度问题够用），"
        f"实际 {settings.research_timeout_seconds}s")
    assert settings.preloop_timeout_seconds <= 35, (
        f"preloop 是轻量启动器，上限应 <=35s，"
        f"实际 {settings.preloop_timeout_seconds}s")
    assert settings.finalize_timeout_seconds >= 70, (
        f"finalize 收尾窗口应 >=70s，实际 {settings.finalize_timeout_seconds}s")
    assert settings.research_hard_timeout_seconds >= (
        settings.research_timeout_seconds + settings.finalize_timeout_seconds
    ), "hard deadline 必须覆盖探索 soft limit 和完整 synthesis 窗口"

    # api/main.py 必须把 research_timeout_seconds 原样给 AgentLoop
    main_src = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    forbidden = "research_timeout_seconds - preloop_timeout_seconds"
    assert forbidden not in main_src, (
        f"api/main.py 不应再做 {forbidden!r} 的重复扣减——"
        "preloop 与 research 是两段独立预算")

    # 装配出的 loop 配置即验收值本身
    cfg = LoopConfig(
        max_steps=6, max_tool_calls=36,
        timeout_seconds=max(30, int(settings.research_timeout_seconds)),
        hard_timeout_seconds=max(60, int(settings.research_hard_timeout_seconds)),
        llm_timeout_seconds=settings.llm_request_timeout_seconds,
        finalize_timeout_seconds=settings.finalize_timeout_seconds,
    )
    assert cfg.timeout_seconds == int(settings.research_timeout_seconds), (
        "loop 的研究预算应直接等于 research_timeout_seconds")
    assert cfg.timeout_seconds > 65, (
        f"研究预算 {cfg.timeout_seconds}s 过短——65s 左右会误报「研究已达时间上限」")
    assert cfg.hard_timeout_seconds > cfg.timeout_seconds, (
        "hard deadline 必须大于 research soft limit，为 synthesis 留出窗口")

    from toolkit.calc.run_code import RunCodeTool
    from toolkit.entity.tools import EntityResolveTool, ClassifyCompanyTool
    max_component_timeout = max(
        RunCodeTool.schema.timeout_seconds,
        EntityResolveTool.schema.timeout_seconds,
        ClassifyCompanyTool.schema.timeout_seconds,
    )
    assert max_component_timeout < cfg.timeout_seconds / 2, (
        "组件 timeout 必须显著小于 Agent 探索 soft limit")


def test_token_soft_limit_enters_synthesis_instead_of_budget_failure():
    """token 达到 soft limit 后撤掉工具，基于已有证据形成最终回答。"""
    class LLM:
        def __init__(self):
            self.exploration_calls = 0
            self.synthesis_calls = 0

        async def chat_with_tools(self, **kwargs):
            self.exploration_calls += 1
            return {
                "content": "先取数",
                "tool_calls": [{
                    "name": "market.get_bundle",
                    "arguments": {"symbol": "600519.SH"},
                }],
                "tokens_used": 11,
            }

        async def chat(self, **kwargs):
            self.synthesis_calls += 1
            assert "强制收尾 / synthesis" in kwargs["system"]
            return (
                "## 投资结论与核心矛盾\n现有证据支持继续观察，仍有数据缺口。\n\n"
                "## 认知陪练\n你最需要验证的是增长证据能否持续。\n\n"
                "AI 置信度：中；本分析不代表投资确定性。"
            )

    async def go():
        llm = LLM()
        loop = _make_loop(
            llm,
            ScriptedExecutor({"market.get_bundle": {"status": "ok", "revenue": "已取数"}}),
            LoopConfig(max_steps=6, max_tokens=10, max_tool_calls=10,
                       timeout_seconds=30, hard_timeout_seconds=60),
        )
        events = [event async for event in loop.run(
            "分析茅台", "token-soft", "deep-research",
            research_plan=_degraded_plan(), initial_context={"skill_phase": 2},
        )]
        finals = [e for e in events if e.type.value == "final_answer"]
        assert len(finals) == 1
        assert llm.exploration_calls == 1
        assert llm.synthesis_calls == 1
        assert finals[0].metadata["soft_limit_reached"] is True
        assert "token soft limit" in finals[0].metadata["soft_limit_reason"]
        assert finals[0].metadata.get("budget_exhausted") is not True

    asyncio.run(go())


def test_max_steps_executes_all_rounds_then_synthesizes():
    """max_steps=6 必须完整执行 6 轮，第 7 次模型调用才是无工具 synthesis。"""
    class LLM:
        def __init__(self):
            self.exploration_calls = 0
            self.synthesis_calls = 0

        async def chat_with_tools(self, **kwargs):
            self.exploration_calls += 1
            return {
                "content": "继续取证",
                "tool_calls": [{
                    "name": "web.search",
                    "arguments": {"query": f"证据 {self.exploration_calls}"},
                }],
                "tokens_used": 1,
            }

        async def chat(self, **kwargs):
            self.synthesis_calls += 1
            return (
                "## 投资结论与核心矛盾\n已有公开证据，但关键问题仍需验证。\n\n"
                "## 认知陪练\n哪些反面证据会改变你的判断？\n\n"
                "AI 置信度：中；本分析不代表投资确定性。"
            )

    async def go():
        llm = LLM()
        loop = _make_loop(
            llm,
            ScriptedExecutor({"web.search": {"results": [{"title": "公开资料"}]}}),
            LoopConfig(max_steps=6, max_tokens=1000, max_tool_calls=20,
                       timeout_seconds=30, hard_timeout_seconds=60),
        )
        events = [event async for event in loop.run(
            "分析公司", "six-rounds", "deep-research",
            research_plan=_degraded_plan(), initial_context={"skill_phase": 2},
        )]
        assert llm.exploration_calls == 6
        assert llm.synthesis_calls == 1
        assert any(e.type.value == "final_answer" for e in events)
        complete = next(e for e in events if e.type.value == "complete")
        assert complete.metadata["total_steps"] == 6

    asyncio.run(go())


def test_synthesis_validation_retry_uses_short_repair_prompt():
    """合规修订不得重发行业手册和证据工作集。"""
    class LLM:
        def __init__(self):
            self.exploration_calls = 0
            self.synthesis_prompts = []

        async def chat_with_tools(self, **kwargs):
            self.exploration_calls += 1
            return {
                "content": "先取证",
                "tool_calls": [{
                    "name": "web.search",
                    "arguments": {"query": "公司年报"},
                }],
                "tokens_used": 1,
            }

        async def chat(self, **kwargs):
            self.synthesis_prompts.append((kwargs["system"], kwargs["user"]))
            if len(self.synthesis_prompts) == 1:
                return (
                    "## 投资结论与核心矛盾\n目标价100元，建议买入。\n\n"
                    "AI 置信度：中；本分析不代表投资确定性。"
                )
            return (
                "## 投资结论与核心矛盾\n现有证据不足，建议继续观察。\n\n"
                "## 认知陪练\n什么反面证据会改变判断？\n\n"
                "AI 置信度：中；本分析不代表投资确定性。"
            )

    async def go():
        llm = LLM()
        loop = _make_loop(
            llm,
            ScriptedExecutor({"web.search": {"results": [{"title": "公开资料"}]}}),
            LoopConfig(max_steps=1, max_tokens=1000, max_tool_calls=10,
                       timeout_seconds=30, hard_timeout_seconds=60),
        )
        events = [event async for event in loop.run(
            "分析公司", "repair-prompt", "deep-research",
            research_plan=_degraded_plan(), initial_context={"skill_phase": 2},
        )]
        assert any(event.type.value == "final_answer" for event in events)
        assert len(llm.synthesis_prompts) == 2
        first_system, _ = llm.synthesis_prompts[0]
        second_system, second_user = llm.synthesis_prompts[1]
        assert "强制收尾 / synthesis" in first_system
        assert "投资研报合规编辑" in second_system
        assert "强制收尾 / synthesis" not in second_system
        assert "目标价100元" in second_user
        assert "校验失败原因" in second_user

    asyncio.run(go())


def test_hard_deadline_is_the_request_lifeline():
    """只有请求级 hard deadline 会取消正在运行的整场 Agent。"""
    class HangingLLM:
        async def chat_with_tools(self, **kwargs):
            await asyncio.sleep(1)
            return {"content": "", "tool_calls": None, "tokens_used": 0}

    async def go():
        loop = _make_loop(
            HangingLLM(), ScriptedExecutor({}),
            LoopConfig(max_steps=6, timeout_seconds=10,
                       hard_timeout_seconds=0.02, llm_timeout_seconds=5),
        )
        events = [event async for event in loop.run(
            "分析公司", "hard-deadline", "deep-research",
            research_plan=_degraded_plan(), initial_context={"skill_phase": 2},
        )]
        errors = [e for e in events if e.type.value == "error"]
        assert len(errors) == 1
        assert errors[0].metadata["reason"] == "hard_deadline"
        assert not [e for e in events if e.type.value == "final_answer"]

    asyncio.run(go())


def test_no_progress_stops_exploration_early():
    """连续失败工具没有新增证据时，不等三次打回或 max_steps 才收口。"""
    class LLM:
        def __init__(self):
            self.calls = 0

        async def chat_with_tools(self, **kwargs):
            self.calls += 1
            return {
                "content": "继续尝试",
                "tool_calls": [{
                    "name": "web.search",
                    "arguments": {"query": f"失败查询 {self.calls}"},
                }],
                "tokens_used": 1,
            }

    async def go():
        llm = LLM()
        loop = _make_loop(
            llm, ScriptedExecutor({}),
            LoopConfig(max_steps=6, max_no_progress_rounds=2,
                       timeout_seconds=30, hard_timeout_seconds=60),
        )
        events = [event async for event in loop.run(
            "分析公司", "no-progress", "deep-research",
            research_plan=_degraded_plan(), initial_context={"skill_phase": 2},
        )]
        assert llm.calls == 2
        final = next(e for e in events if e.type.value == "final_answer")
        assert "连续 2 轮没有研究进展" in final.metadata["soft_limit_reason"]
        assert final.metadata["reason"] == "no_evidence_for_synthesis"

    asyncio.run(go())


def test_data_requirement_is_preserved_and_enforced_before_answered():
    """重数据问题不能只在计划里标注，必须阻止无取证的 answered 回写。"""
    plan = validate_plan_payload({
        "user_goal": "茅台估值是否合理",
        "questions": [{
            "id": "q1", "question": "当前估值是否合理", "priority": "P0",
            "dimension": "valuation", "data_requirement": "calc_required",
        }],
    })
    assert plan.get("q1").data_requirement == "calc_required"

    state = AgentState(session_id="data-gate", user_message="分析茅台",
                       active_skill="deep-research")
    operation = {"op": "advance", "question_id": "q1", "to": "answered"}
    missing = AgentLoop._missing_data_requirement_evidence(state, plan, operation)
    assert "calc.*" in missing

    state.add_observation(Observation(
        source="calc.metric", content={"roic": 0.2}, success=True,
    ))
    assert AgentLoop._missing_data_requirement_evidence(state, plan, operation) == ""


def test_memory_verify_requires_fresh_external_evidence():
    """认知召回本身不是验证；必须用本轮公开/行情/计算证据复核。"""
    plan = validate_plan_payload({
        "user_goal": "验证历史判断",
        "questions": [{
            "id": "q1", "question": "云业务是否成为第二增长曲线", "priority": "P0",
            "dimension": "growth", "data_requirement": "memory_verify",
        }],
    })
    state = AgentState(session_id="memory-gate", user_message="验证腾讯",
                       active_skill="deep-research")
    operation = {"op": "advance", "question_id": "q1", "to": "answered"}
    state.add_observation(Observation(
        source="cognition.recall", content={"items": ["旧观点"]}, success=True,
    ))
    assert "本轮公开、行情或计算证据" in AgentLoop._missing_data_requirement_evidence(
        state, plan, operation,
    )
    state.add_observation(Observation(
        source="web.search", content={"results": ["公告"]}, success=True,
    ))
    assert AgentLoop._missing_data_requirement_evidence(state, plan, operation) == ""


def test_streaming_tool_draft_timeout_is_hidden_and_degraded_once():
    """工具轮草稿不外显；首次超时直接安全降级，不再重试到通用 error。"""
    async def go():
        loop = _make_loop(
            StreamingToolDraftTimeoutLLM(), ScriptedExecutor({}),
            LoopConfig(
                max_steps=3, max_tool_calls=5, timeout_seconds=5,
                llm_timeout_seconds=0.05, max_consecutive_llm_failures=2,
            ),
        )
        events = [
            event async for event in loop.run(
                "分析茅台", session_id="stream-timeout", skill_name="deep-research",
                research_plan=_degraded_plan(), initial_context={"skill_phase": 2},
            )
        ]
        assert not [e for e in events if e.type.value == "answer_delta"], (
            "工具决策轮的内部草稿不应作为答案流给前端")
        assert not [e for e in events if e.type.value == "error"], (
            "已有流式草稿时首次超时应安全降级，不应完整重试后报通用错误")
        finals = [e for e in events if e.type.value == "final_answer"]
        assert len(finals) == 1
        assert finals[0].metadata.get("degraded") is True
        assert "我先锁定研究标的" not in finals[0].content
        assert "尚未完成工具调用" in finals[0].content

    asyncio.run(go())


def test_latency_finalize_waits_for_plan_convergence_and_uses_report_contract():
    """任意一条证据不能触发收尾；P0 收敛后才切换为价值投资研报。"""
    async def go():
        llm = EvidenceThenConvergeLLM()
        plan = validate_plan_payload({
            "user_goal": "分析茅台",
            "questions": [{
                "id": "q1", "question": "增长驱动", "priority": "P0",
                "dimension": "growth", "data_requirement": "light_evidence",
            }],
        })
        loop = _make_loop(
            llm,
            ScriptedExecutor({
                "market.get_bundle": {"status": "ok"},
                "web.search": {"results": [{"title": "公司年报"}]},
            }),
            LoopConfig(max_steps=5, max_tool_calls=5, timeout_seconds=30),
        )
        events = [
            event async for event in loop.run(
                "分析茅台", session_id="report-contract", skill_name="deep-research",
                research_plan=plan,
                initial_context={"skill_phase": 2, "latency_mode": True},
            )
        ]
        assert llm.tool_rounds == 2, "取得任意证据后仍应先完成研究计划，不能立即收尾"
        assert "禁止逐题回答" in llm.final_system
        assert "价值投资研报" in llm.final_system
        finals = [e for e in events if e.type.value == "final_answer"]
        assert len(finals) == 1
        assert "逐题回答" not in finals[0].content
        assert "P0" not in finals[0].content

    asyncio.run(go())


def test_info_richness_no_longer_restricts_conclusion():
    """兼容字段可继续传入，但 A/B/C 不再决定能否给出研究倾向。"""
    result = validate_conclusion(
        conclusion="建议关注。AI 置信度中等，但不代表投资确定性。",
        tier="G1", info_richness="C", data_status="ok",
    )
    assert result.passed, result.reasons


if __name__ == "__main__":
    print("超时降级交付 / 时间预算回归")
    check("已有证据收尾超时 → 降级报告",
          test_loop_timeout_with_evidence_returns_degraded_report)
    check("茅台研究预算不再 65s 超时",
          test_maotai_research_does_not_timeout_at_65s)
    check("token soft limit 后进入 synthesis",
          test_token_soft_limit_enters_synthesis_instead_of_budget_failure)
    check("max_steps 完整执行后进入 synthesis",
          test_max_steps_executes_all_rounds_then_synthesizes)
    check("hard deadline 是请求级生命线",
          test_hard_deadline_is_the_request_lifeline)
    check("no-progress 提前停止探索",
          test_no_progress_stops_exploration_early)
    check("数据需求标签在 answered 前强制取证",
          test_data_requirement_is_preserved_and_enforced_before_answered)
    check("历史认知必须由本轮外部证据复核",
          test_memory_verify_requires_fresh_external_evidence)
    check("工具轮流式草稿超时后隐藏并单次降级",
          test_streaming_tool_draft_timeout_is_hidden_and_degraded_once)
    check("低延迟收尾等待计划收敛并使用研报契约",
          test_latency_finalize_waits_for_plan_convergence_and_uses_report_contract)
    check("信息丰富度不再限制结论",
          test_info_richness_no_longer_restricts_conclusion)
    print(f"\n通过 {passed} / 失败 {failed}")
    sys.exit(1 if failed else 0)
