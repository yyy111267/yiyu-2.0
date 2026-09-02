"""脚本化验证：取数去重 + 连续取数引导（不烧真 hy3 key）。

构造一个「反复调 calc.menu / market.get_bundle、但绝不调 plan.update」的脚本 LLM：
- 断言 1（去重）：executor 的真执行次数 < LLM 要求的 tool_call 次数（相同参数命中会话缓存）。
- 断言 2（引导）：连续取数达到阈值后，注入给 LLM 的 system prompt 含「执行引导」。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from runtime.loop import AgentLoop, LoopConfig
from runtime.assembler import PromptAssembler
from runtime.plan import ResearchPlan
from toolkit.base import ToolResult
from toolkit.executor import ToolCall
from toolkit import register_all
from toolkit.registry import get_tool_schemas_for_llm, TOOL_REGISTRY


# ── 脚本化 LLM：反复取数、不回写 plan ──────────────────────
class ScriptedLLM:
    """前 3 轮只调取数（同参数重复），用于触发去重 + 引导；第 4 轮起调 plan.update 收尾。"""

    def __init__(self):
        self.calls = 0
        self.last_system = ""
        self.responses = self._build_responses()

    def _build_responses(self) -> list[dict]:
        # 注意：loop._handle_tool_calls 期望 tool_calls 为平铺格式 {name, arguments, id}
        data_calls = {
            "tool_calls": [
                {"id": "c1", "name": "calc_menu", "arguments": {"symbol": "600519.SH", "group": "G1a"}},
                {"id": "c2", "name": "market_get_bundle", "arguments": {"symbol": "600519.SH", "days": 30}},
            ]
        }
        advance = {
            "tool_calls": [
                {"id": "p1", "name": "plan_update", "arguments": {
                    "operations": [{"op": "advance", "question_id": "q1",
                                    "to": "in_progress",
                                    "activation": {"falsification": "x",
                                                   "completion_rule": "y",
                                                   "required_evidence": [{"type": "t", "level": "A", "description": "d"}]}}]}}
            ]
        }
        # 前 3 轮 = 取数；第 4 轮 = 推进（触发引导后收敛）；第 5 轮起 = 空（收工，循环自然结束）
        final = {"tool_calls": []}
        return [data_calls, data_calls, data_calls, advance, final]

    async def chat_with_tools(self, system: str = "", user: str = "", **kwargs) -> dict:
        if not hasattr(self, "asked_history"):
            self.asked_history = []
        if not hasattr(self, "all_systems"):
            self.all_systems = []
        self.last_system = system
        self.all_systems.append(system)
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        r = self.responses[idx]
        # 记录 LLM 要求的工具名（下划线形式，与交给 executor 的一致），便于断言
        self.asked_history.append([tc["name"] for tc in r.get("tool_calls", [])])
        return {"content": r.get("content"), "tool_calls": r.get("tool_calls"), "tokens_used": 10}

    async def chat(self, **kwargs) -> str:
        return ""

    async def chat_json(self, **kwargs) -> Any:
        return {}


class ScriptedExecutor:
    def __init__(self, tool_results: dict):
        self.tool_results = tool_results
        self.call_log: list[str] = []  # 记录每次「真执行」的工具名

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        self.call_log.append(tool_call.name)
        r = self.tool_results.get(tool_call.name)
        if r is None:
            return ToolResult(success=False, data=None, error=f"未脚本化工具: {tool_call.name}")
        if isinstance(r, ToolResult):
            return r
        return ToolResult(success=True, data=r, error=None)

    async def execute_batch(self, tool_calls) -> list[ToolResult]:
        return [await self.execute(tc) for tc in tool_calls]


def _make_plan() -> ResearchPlan:
    payload = {
        "version": "1.0",
        "objective": "研究贵州茅台是否值得买入",
        "questions": [
            {"id": "q1", "title": "生意质量", "priority": "P0",
             "status": "pending", "depends_on": [], "evidence": [],
             "answerable": True, "answer": None, "confidence": None},
        ],
        "global_constraints": [], "edges": [],
        "metadata": {"created_by": "verify"},
    }
    return ResearchPlan.from_dict(payload)


async def main():
    register_all()
    # 确保 deep-research 工具 schema 可用（否则 _call_llm 走退化路径，但脚本 LLM 自带行为，无影响）
    get_tool_schemas_for_llm("deep-research")

    llm = ScriptedLLM()
    executor = ScriptedExecutor({
        "calc.menu": {"metrics_count": 23, "metrics": {"pe": 25.3, "roe": 0.34}},
        "market.get_bundle": {"snapshot": {"price": 1680}, "fundamentals": {"name": "贵州茅台"}},
        "company.classify": {"group": "G1a"},
    })

    loop = AgentLoop(
        llm_client=llm,
        assembler=PromptAssembler(prompts_dir=str(ROOT / "prompts")),
        tool_executor=executor,
        agent_spawner=None,
        config=LoopConfig(tool_cache_enabled=True, data_only_rounds_threshold=3),
    )

    plan = _make_plan()
    turns = 0
    async for _evt in loop.run(
        user_message="请研究贵州茅台是否值得买入",
        session_id="verify-001",
        skill_name="deep-research",
        research_plan=plan,
        initial_context={"skill_phase": 2},
    ):
        turns += 1

    # ── 断言 1：去重生效 ──
    # LLM 在前 3 轮要求调 calc_menu 3 次、market_get_bundle 3 次。
    # 去重后 executor 真执行应各只有 1 次（其余命中会话缓存）。
    print("DEBUG: llm.calls=%d, executor.call_log=%s, turns=%d" % (llm.calls, executor.call_log, turns))
    asked_calc = sum(1 for a in llm.asked_history if "calc_menu" in a)
    asked_bundle = sum(1 for a in llm.asked_history if "market_get_bundle" in a)
    calc_real = executor.call_log.count("calc_menu")
    bundle_real = executor.call_log.count("market_get_bundle")
    assert calc_real == 1, f"去重失败: calc_menu 真执行 {calc_real} 次（应=1，LLM 要求 {asked_calc} 次）"
    assert bundle_real == 1, f"去重失败: market_get_bundle 真执行 {bundle_real} 次（应=1，LLM 要求 {asked_bundle} 次）"

    # ── 断言 2：引导注入生效 ──
    # 连续取数达阈值后，某次注入给 LLM 的 system 应含「执行引导」（不限最后一次）。
    guided = any("执行引导" in s for s in getattr(llm, "all_systems", []))
    assert guided, "引导注入失败: 全程未向 LLM 注入『执行引导』"

    print("✅ 去重断言通过: calc_menu 真执行=%d（LLM 要求 3）、market_get_bundle 真执行=%d（LLM 要求 3）"
          % (calc_real, bundle_real))
    print("✅ 引导断言通过: 连续取数达阈值后已注入『执行引导』")
    print(f"   总轮驱动 turns={turns}, LLM 调用次数={llm.calls}, executor 真调用={len(executor.call_log)} 次")


if __name__ == "__main__":
    asyncio.run(main())
