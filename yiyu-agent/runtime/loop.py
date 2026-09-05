"""
Agent Loop - ReAct 循环实现

这是整个系统的核心，实现了标准的 Agent 循环：
感知(Perceive) → 推理(Reason) → 行动(Act) → 观察(Observe) → 循环...
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from .state import AgentState, AgentPhase, Observation
from .assembler import AssembledPrompt, PromptAssembler
from .budget import BudgetConfig, BudgetManager
from .events import AgentEvent, EventType
from .breaker import CircuitBreaker, CircuitBreakerConfig
from .planner import Planner
from .trace import (
    Trace, LoopTurn, Declaration, Writeback, PlanDiff, EvidenceDiff,
    ToolCallRecord, BudgetSnapshot, Conclusion, Unanswered, FinalReport,
    digest, extract_numbers,
)
from .plan import apply_plan_update, _plan_update_schema
from toolkit.executor import ToolCall, ToolExecutor

logger = logging.getLogger(__name__)


# ── 前端 citations 收口（docs/frontend-research-report-handoff.md P1）───
# 内部工具前缀 → 用户可读 source_type 标签
_SOURCE_TYPE_LABEL = {
    "market.": "行情数据",
    "calc.": "计算结果",
    "web.": "公开网页",
    "cognition.": "认知库",
}
# 纯流程信号源：整条 evidence 不算研究证据；防御性兜底，正常不会进 evidence_pack
_FLOW_ONLY_EVIDENCE_SOURCES = frozenset({
    "entity_resolver", "company_classify", "plan_update",
    "preloop_orchestrator", "preloop_facts_builder",
    "preloop_granularity", "preloop_profiler",
})

_PRIMARY_DOCUMENT_KEYWORDS = (
    "上市状态", "上市日期", "上市公告", "招股书",
    "年度报告", "年报", "中期报告", "定期报告",
)
_MARKET_FIELD_KEYWORDS = (
    (("营收", "收入", "revenue"), "revenue"),
    (("毛利", "gross margin"), "gross_margin"),
    (("净利润", "净利", "盈利", "profit"), "net_profit"),
    (("现金流", "cash flow"), "operating_cash_flow"),
    (("市值", "market cap"), "market_cap"),
    (("股价", "现价", "price"), "price"),
)


def _sanitize_citations_for_frontend(citations: list[dict]) -> list[dict]:
    """把 internal 工具名换成 user-friendly source_type，handoff 文档要求。

    规则：
    1) 子项的 `source` 字段若归一化后是已知内部工具前缀 → 替换为可读标签；
    2) URL 缺失的引用按 kind 给出结构化标签，不回退为工具名；
    3) 其他无可读类型项 → "结构化数据"，至少让前端不暴露内部标识。
    """
    from toolkit.registry import resolve_tool_name
    out: list[dict] = []
    for c in citations:
        sub_src = str(c.get("source") or "")
        if sub_src:
            norm = resolve_tool_name(sub_src).replace("_", ".")
            # _SOURCE_TYPE_LABEL 的 key 是前缀（如 "market."），用 startswith 匹配
            label = next(
                (lbl for prefix, lbl in _SOURCE_TYPE_LABEL.items() if norm.startswith(prefix)),
                None,
            )
            if label:
                c["source_type"] = label
                c.pop("source", None)
            else:
                c.setdefault("source_type", "其他来源")
        if not c.get("url"):
            kind = c.get("kind", "")
            if kind == "metric":
                c["source_type"] = "结构化计算结果"
            elif kind == "field":
                c["source_type"] = "结构化字段引用"
            elif not c.get("source_type"):
                c["source_type"] = "结构化数据"
        out.append(c)
    return out


@dataclass
class LoopConfig:
    """循环配置"""
    max_steps: int = 6  # 最大探索轮数；完整执行后进入 synthesis
    max_tokens: int = 50000  # token soft limit
    max_tool_calls: int = 36  # 工具调用 soft limit
    timeout_seconds: int = 200  # 探索 soft limit；达到后进入 synthesis
    hard_timeout_seconds: int = 300  # 整个 Agent run 的系统级 hard deadline
    llm_timeout_seconds: float = 75.0  # 单次模型请求上限，避免一次请求吃完整体预算
    max_consecutive_llm_failures: int = 2  # 短暂故障最多重试次数；超过即明确失败
    # 需要显式 delivery.finish 才能结束的 Skill（研究类，保证信息充分才收尾）
    finish_required_skills: tuple = ("deep-research", "private-company", "trade-review")
    # 连续「无取数结论」打回的最大次数；超过则输出安全失败说明
    max_content_rejects: int = 3
    # 取数去重：同一工具 + 同一参数在本会话内命中缓存则直接复用结果，不真跑 executor
    tool_cache_enabled: bool = True
    # 连续「只取数、不回写 plan」达到该轮数后，下一轮注入引导语（默认 3 轮）
    data_only_rounds_threshold: int = 3
    max_no_progress_rounds: int = 2
    breaker_failure_threshold: int = 5
    breaker_recovery_seconds: float = 30.0
    # 低延迟最终综合轮（fast_finalize）的单次模型时限。收尾要一次生成整篇
    # 正文，通常需要比中间轮更长的窗口，因此单独配置；为 None 时沿用
    # llm_timeout_seconds（不再静默抬高，避免配置的超时形同虚设）。
    finalize_timeout_seconds: float | None = None
    synthesis_token_reserve: int = 10000  # 探索不得吃掉的最终回答预留额


# ── P1 编排顺序强制（skill_phase 状态机）──────────────────────────────
# 每个 skill 定义「阶段 → 允许暴露的工具」。未达阶段的工具不发给 LLM，
# 从工具暴露层面杜绝「跳过 classify 直接取数 / 未取数直接收尾」。
# 仅对需要严格顺序的研究类 skill 配置；其他 skill 不做门控（全量暴露）。
SKILL_PHASE_TOOLS: dict[str, dict[int, set[str]]] = {
    "deep-research": {
        # phase 0：未锁定实体 → 只能解析标的
        0: {"entity.resolve"},
        # phase 1：已解析实体 → 可做商业模式分类
        1: {"entity.resolve", "company.classify"},
        # phase 2：已分类 → 取数 + 按需计算 + 补算 + 认知检索 + web 补数 + 收尾均可。
        # 这是核心研究阶段，允许多轮循环：
        #   cognition.recall（双路检索，研究开始时调一次）
        #   → market.get_bundle → calc.metric(s) → 发现 NC → calc.run_code 补算
        #   → web.search/web.fetch 补数 → 交叉验证 → delivery.finish
        # market.get_bundle / calc.* / cognition / web 均不推进 phase，
        # 留在本阶段循环，直到 LLM 调 delivery.finish 通过信息自检才进 phase 3。
        # 注：calc.menu / calc.metric / calc.metrics / calc.run_code 由
        # metric-calculation 能力包在本阶段激活后注入（capability_tools_for_skill）。
        2: {
            "market.get_bundle", "calc.run_code",
            "cognition.recall", "cognition.get", "cognition.extract",
            "web.search", "web.fetch",
            "delivery.finish",
        },
        # phase 3：finish 已通过信息自检 → 只允许收尾（收尾唯一出口 = delivery.finish）。
        # 取数/计算/补算工具全部收回：被硬规则拦截后 LLM 无法绕回重取数，
        # 只能修正结论文本重试 finish。硬规则合规校验由 loop 在 finish 通过后自动执行。
        3: {
            "delivery.finish",
        },
    },
}

# 各工具执行成功后推进到的 phase（仅对配置了门控的 skill 生效）。
# 注意：market.get_bundle / calc.* 不再推进 phase——
# 取数和指标计算只是研究阶段的中间步骤，不意味着研究已完成。
# 指标标 NC 后还需 calc.run_code 补算或 web 补数，不能一算完就跳收尾。
# phase 2 → phase 3 的推进由 delivery.finish 通过信息自检时触发（见 _handle_tool_calls）。
_PHASE_ADVANCE_ON_TOOL: dict[str, int] = {
    "entity.resolve": 1,
    "company.classify": 2,
}


class AgentLoop:
    """
    Agent 主循环
    
    职责：
    1. 维护 Agent 状态机
    2. 组装 Prompt 并调用 LLM（含 function calling）
    3. 解析 LLM 响应并执行动作（tool / sub-agent / 回复）
    4. 发送 SSE 事件给前端
    5. 管理预算和熔断
    """

    def __init__(
        self,
        llm_client,  # LLMClient 实例
        assembler: PromptAssembler,
        tool_executor: ToolExecutor,
        agent_spawner,  # AgentSpawner 实例（可选）
        config: LoopConfig = None,
    ):
        self.llm = llm_client
        self.assembler = assembler
        self.executor = tool_executor
        self.spawner = agent_spawner
        self.config = config or LoopConfig()
        self.budget = BudgetManager(BudgetConfig(
            max_tokens=self.config.max_tokens,
            max_tool_calls=self.config.max_tool_calls,
            max_steps=self.config.max_steps,
        ))
        self.breaker = CircuitBreaker(CircuitBreakerConfig(
            failure_threshold=self.config.breaker_failure_threshold,
            recovery_timeout=self.config.breaker_recovery_seconds,
        ))
        self._reject_count = 0  # 连续「无取数结论」打回次数，防死循环
        self.trace: Optional[Trace] = None  # 本轮会话 trace（run 时重建）
        from runtime.context_manager import ContextManager
        self.context_manager = ContextManager()

    async def run(
        self,
        user_message: str,
        session_id: str,
        skill_name: Optional[str] = None,
        *,
        research_plan=None,
        initial_context: Optional[dict] = None,
        user_id: Optional[str] = None,
    ) -> AsyncIterator[AgentEvent]:
        """
        运行 Agent 主循环（多轮 ReAct）。

        每轮：
          1. 组装 prompt（含历史 observations）
          2. 调用 LLM（带 tool schemas）
          3. 若返回 tool_calls → 执行 → 回灌 observations → 继续循环
          4. 若无 tool_calls → 视为最终回答 → 经硬规则校验 → 输出

        新增参数：
          research_plan: 上游四件套注入的 ResearchPlan；提供时循环收敛按 P0 状态判定，
                         不再走轻量 Planner，也不允许 LLM 自评完成即收工。
          initial_context: 初始 context（如 skill_phase），供评测夹具与上游 preloop 注入。
        """
        start_time = time.time()
        self._reject_count = 0
        self.context_manager.reset()
        from toolkit.market.research_data import set_research_data_scope
        research_data_token = set_research_data_scope(session_id)

        state = AgentState(
            session_id=session_id,
            user_message=user_message,
            pinned_skill=skill_name,
            active_skill=skill_name,
            user_id=user_id,
        )
        if initial_context:
            state.context.update(initial_context)

        # 本轮会话 trace（逐轮落盘，lint 与仪表盘的数据源）
        self.trace = Trace(session_id=session_id)

        logger.info(f"[Session {session_id}] 开始处理: {user_message[:50]}...")

        yield AgentEvent(
            type=EventType.START,
            content=f"开始处理您的请求...",
            metadata={"session_id": session_id},
        )

        try:
            # hard deadline 是整份请求唯一的时间生命线；soft limit 由循环内部
            # 转成 synthesis，绝不从这里杀掉研究。
            request_deadline = state.context.get("_request_hard_deadline_monotonic")
            hard_window = self.config.hard_timeout_seconds
            if request_deadline is not None:
                hard_window = max(0.001, float(request_deadline) - time.monotonic())
            async with asyncio.timeout(hard_window):
                if research_plan is not None:
                    # 上游已注入研究计划：直接进入循环，跳过轻量规划层。
                    state.context["research_plan"] = research_plan
                    if state.context.get("latency_mode"):
                        self._activate_p0_for_latency(research_plan)
                else:
                    # 规划层：研究类任务先产出执行计划（失败降级为无计划执行）。
                    full_tool_schemas = self._get_full_tool_schemas(skill_name)
                    plan = await self._make_plan(state, full_tool_schemas)
                    if plan:
                        state.context["plan"] = plan
                        state.context["plan_progress"] = []
                        yield AgentEvent(
                            type=EventType.PLAN,
                            content=json.dumps(plan, ensure_ascii=False),
                            metadata={"session_id": session_id},
                        )

                async for evt in self._react_loop(state, start_time):
                    yield evt

        except TimeoutError:
            logger.error("Agent hard deadline reached after %.1fs", time.time() - start_time)
            async for evt in self._emit_hard_deadline_report(state, start_time):
                yield evt
        except Exception as e:
            logger.exception("Agent 循环异常")
            yield AgentEvent(
                type=EventType.ERROR,
                content=f"系统内部错误: {str(e)}",
                metadata={"error": str(e)},
            )

        finally:
            from toolkit.market.research_data import reset_research_data_scope
            reset_research_data_scope(research_data_token)
            state.finished = True
            duration = time.time() - start_time
            
            logger.info(
                f"[Session {session_id}] 完成 - "
                f"步骤: {state.step_count}, "
                f"Token: {state.tokens_used}, "
                f"耗时: {duration:.2f}s"
            )
            
            yield AgentEvent(
                type=EventType.COMPLETE,
                content="",
                metadata={
                    "total_steps": state.step_count,
                    "total_tokens": state.tokens_used,
                    "duration_sec": round(duration, 2),
                },
            )

    async def _react_loop(
        self, state: AgentState, start_time: float
    ) -> AsyncIterator[AgentEvent]:
        """ReAct 主循环：推理 → 行动 → 观察 → 循环。"""
        consecutive_llm_failures = 0
        for step in range(self.config.max_steps):
            state.phase = AgentPhase.REASONING

            elapsed = time.time() - start_time
            soft_reason = self._soft_limit_reason(state, elapsed)
            if soft_reason:
                async for evt in self._synthesize_and_finish(
                    state, start_time, soft_reason,
                ):
                    yield evt
                return

            # 预算检查发生在新一轮计数前，因此 max_steps=6 会完整执行 6 轮；
            # 第 6 轮结束后由循环出口进入 synthesis。
            state.step_count = step + 1

            llm_timeout = max(
                1.0,
                min(self.config.llm_timeout_seconds,
                    self.config.timeout_seconds - elapsed),
            )

            # 候选工具先过 phase 门控，再由 ContextManager 按当前 P0 缺口精裁。
            candidate_tool_schemas = self._get_tool_schemas(state)
            fast_finalize = bool(
                state.context.get("latency_mode")
                and state.context.get("research_plan") is not None
                and self._has_research_evidence(state)
                and self._plan_converged(state)
            )
            prompt_stage = "synthesis" if fast_finalize else "execution"
            state.context["_prompt_stage"] = prompt_stage
            context_package = self.context_manager.prepare(
                state, candidate_tool_schemas, stage=prompt_stage,
                input_token_budget=max(
                    2000,
                    min(12000, self.config.max_tokens - state.tokens_used
                        - self.config.synthesis_token_reserve),
                ),
            )
            tool_schemas = context_package.tool_schemas
            yield AgentEvent(
                type=EventType.DEBUG,
                content="",
                metadata={"step": step + 1, "context_usage": context_package.audit},
            )
            if self._finish_required(state) and not tool_schemas and not fast_finalize:
                yield AgentEvent(
                    type=EventType.ERROR,
                    content=(
                        "研究类 Agent 未拿到任何工具 schema，疑似工具注册或 skill 装配失败。"
                        "请确认 live 入口已调用 toolkit.register_all()。"
                    ),
                    metadata={
                        "reason": "missing_tool_schemas",
                        "skill": state.active_skill,
                    },
                )
                return

            logger.info(f"[Step {step+1}/{self.config.max_steps}] 推理中")

            # 熔断检查
            if self.breaker.is_open():
                yield AgentEvent(
                    type=EventType.ERROR,
                    content="系统繁忙，请稍后重试",
                    metadata={"reason": "circuit_breaker_open"},
                )
                return

            # 1. 组装当前阶段的 prompt
            try:
                prompt = await self.assembler.build(state, context_package=context_package)
            except Exception as e:
                logger.error(f"Prompt 组装失败: {e}")
                yield AgentEvent(
                    type=EventType.ERROR,
                    content=f"上下文组装失败: {str(e)}",
                    metadata={"error": str(e)},
                )
                return

            if fast_finalize:
                # 已有证据后直接进入正文综合：不再把长报告塞进 delivery.finish
                # 的 JSON 参数，正文可逐 token 流出，完成后再由代码做硬规则校验。
                tool_schemas = []
                # G5 或第一性原理模式下「买入」类结论属硬规则拦截（R1），
                # 且无法像 R6 那样用标注兜底——必须从生成源头避免，否则会
                # 整篇打回重生成并可能耗尽总预算。这里按当前分级动态注入。
                tier = str(state.context.get("tier", "")).upper()
                research_mode = str(state.context.get("research_mode", ""))
                restricted = (
                    tier == "G5" or research_mode in ("g5", "first_principles")
                )
                restriction = (
                    "3) 当前标的为 G5/第一性原理模式：结论只能是"
                    "「观望 / 不参与 / 证据不足」，禁止出现买入、建仓、加仓等"
                    "任何买入倾向表述；\n"
                    "4) 数据不完整时必须写明「需一手验证……」的具体验证指引。"
                    if restricted else
                    "3) 未取得充分证据时，结论应为「观望 / 待验证」，"
                    "不得给出买入倾向；\n"
                    "4) 数据不完整时须写明具体的「一手验证」指引。"
                )
                prompt.system += (
                    "\n\n【最终研报交付】研究计划已在后台完成，本轮禁止继续调用工具。"
                    "研究计划只是内部防漏项清单，不是给用户看的目录。禁止逐题回答，"
                    "禁止出现 P0/P1/P2、Q1/Q2、data_requirement、工具名或内部状态。\n"
                    "请把已完成问题综合成一份深入浅出的投研陪练报告，按用户决策而非研究流程成文："
                    "①小渔的结论与核心矛盾；②生意与关键变化；③市场分歧；"
                    "④对持有者/准备买入者的含义；⑤什么会改变判断；"
                    "⑥使用“这次值得留下的投资认知”与“留给你的问题”两个二级标题，"
                    "分别沉淀一条原则和一个具体问题。"
                    "只保留与本次判断相关的部分；无证据的章节直接省略。"
                    "数据不足只在阻碍核心判断时自然说明“缺什么、影响什么、后续看什么”。"
                    "事实、推断和边界通过句式区分；术语首次出现时用一句白话解释。"
                    "禁止展示工具名、内部状态、字段名、口径标签、取数过程、AI 置信度或研究计划。\n"
                    "【硬性禁令】违反会整篇打回重生成并重燃预算，务必遵守：\n"
                    "1) 可以给有假设前提的内在价值或估值情景区间，但禁止包装成"
                    "确定性目标价、买卖点位、止损止盈价或交易指令；\n"
                    "2) 所有关键数字必须直接引用上方工具观测返回值，禁止心算、"
                    "外推或凭记忆补数；确需给出预测/假设时，必须写成"
                    "「预计/约 X」形式以标明其为推断；\n"
                    + restriction
                )
                # 收尾轮时限：优先用显式的 finalize_timeout_seconds；未配置则
                # 沿用 llm_timeout_seconds（不再静默抬高到 45s，避免配置项失效）。
                # 两种情况都受「本轮剩余总预算」封顶。
                finalize_timeout = (
                    self.config.finalize_timeout_seconds
                    if self.config.finalize_timeout_seconds is not None
                    else llm_timeout
                )
                llm_timeout = min(
                    finalize_timeout,
                    max(1.0, self.config.timeout_seconds - elapsed),
                )

            # 发送前预测：有证据后才启用，并为 synthesis 预留额度。
            # 中文/英文 schema 混合时 2 chars/token 是保守估计。
            predicted_input = (
                len(prompt.system) + len(prompt.user)
                + len(json.dumps(tool_schemas, ensure_ascii=False)) + 1
            ) // 2
            reserve = (
                self.config.synthesis_token_reserve
                if 0 < self.config.synthesis_token_reserve < self.config.max_tokens else 0
            )
            if (reserve and not fast_finalize and self._has_research_evidence(state)
                    and state.tokens_used + predicted_input > self.config.max_tokens - reserve):
                async for evt in self._synthesize_and_finish(
                    state, start_time,
                    f"下一轮预计将超过 token soft limit（已为最终回答预留 {reserve}）",
                ):
                    yield evt
                return

            # 2. 调用 LLM（带 tools）
            state.phase = AgentPhase.CALLING_LLM

            # 引导：连续多轮只在取数、不回写研究计划 → 注入强引导，把模型拉回「逐题推进」。
            if self.config.data_only_rounds_threshold > 0 and \
                    state.consecutive_data_only_rounds >= self.config.data_only_rounds_threshold:
                guide = (
                    "【执行引导】你已连续多轮重复取数却未推进研究计划。请立即：\n"
                    "1) 停止重复调用已调过的取数工具（相同参数会直接复用缓存，无需再调）；\n"
                    "2) 调用 plan.update 将当前 P0 问题激活为 in_progress，"
                    "并给出 falsification / completion_rule / required_evidence；\n"
                    "3) 针对已激活的问题取证，取证后再次调用 plan.update 把该题标记为 answered 并附结论；\n"
                    "4) 所有 P0 都 answered/unanswerable 后，再调用 delivery.finish 收工。"
                )
                prompt.system = prompt.system + "\n\n" + guide
                logger.info(
                    f"[Step {step+1}] 注入取数引导（连续 {state.consecutive_data_only_rounds} 轮只取数）")
                state.consecutive_data_only_rounds = 0  # 注入后即清零，避免反复刷屏

            try:
                llm_started = time.monotonic()
                streamed_answer = ""
                from core.llm import llm_usage_stage
                usage_scope = llm_usage_stage("synthesis" if fast_finalize else "research")
                usage_scope.__enter__()
                # 工具决策轮的 content 常是“我先取数……”一类内部草稿，不能当
                # 最终答案实时展示；仅无工具的最终综合轮允许向前端流式输出。
                stream_is_public = not bool(tool_schemas)
                if hasattr(self.llm, "chat_with_tools_stream"):
                    delta_queue: asyncio.Queue[str] = asyncio.Queue()

                    async def _on_delta(piece: str) -> None:
                        await delta_queue.put(piece)

                    llm_task = asyncio.create_task(
                        self.llm.chat_with_tools_stream(
                            system=prompt.system,
                            user=prompt.user,
                            tools=tool_schemas,
                            on_content_delta=_on_delta,
                            temperature=0.7,
                            timeout=llm_timeout,
                        )
                    )
                    while not llm_task.done():
                        if time.monotonic() - llm_started >= llm_timeout:
                            llm_task.cancel()
                            try:
                                await llm_task
                            except asyncio.CancelledError:
                                pass
                            raise TimeoutError(
                                f"streaming LLM exceeded {llm_timeout:.1f}s wall-clock limit"
                            )
                        get_delta = asyncio.create_task(delta_queue.get())
                        remaining_call = max(
                            0.1, llm_timeout - (time.monotonic() - llm_started),
                        )
                        done, _ = await asyncio.wait(
                            {llm_task, get_delta},
                            timeout=min(8, remaining_call),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if get_delta in done:
                            piece = get_delta.result()
                            streamed_answer += piece
                            state.context["_streamed_draft"] = (
                                str(state.context.get("_streamed_draft", "")) + piece
                            )[-20000:]
                            if stream_is_public:
                                yield AgentEvent(
                                    type=EventType.ANSWER_DELTA,
                                    content=piece,
                                    metadata={"step": step + 1, "provisional": True},
                                )
                        else:
                            get_delta.cancel()
                            try:
                                await get_delta
                            except asyncio.CancelledError:
                                pass
                        if not done:
                            yield AgentEvent(
                                type=EventType.DEBUG,
                                content="模型正在生成回复",
                                metadata={"step": step + 1},
                            )
                    response = await llm_task
                    while not delta_queue.empty():
                        piece = delta_queue.get_nowait()
                        streamed_answer += piece
                        state.context["_streamed_draft"] = (
                            str(state.context.get("_streamed_draft", "")) + piece
                        )[-20000:]
                        if stream_is_public:
                            yield AgentEvent(
                                type=EventType.ANSWER_DELTA,
                                content=piece,
                                metadata={"step": step + 1, "provisional": True},
                            )
                else:
                    response = await self._call_llm(
                        prompt, tool_schemas, timeout=llm_timeout,
                    )
                logger.info(
                    "[Step %d] LLM 返回，耗时 %.1fs，prompt_chars=%d，tools=%d",
                    step + 1,
                    time.monotonic() - llm_started,
                    len(prompt.system) + len(prompt.user),
                    len(tool_schemas),
                )
                input_tokens = int(response.get("prompt_tokens", 0) or 0)
                output_tokens = int(response.get("completion_tokens", 0) or 0)
                state.tokens_used += int(response.get("tokens_used", 0) or 0) or (
                    input_tokens + output_tokens)
                # step 级 token 埋点：input/output 取模型 usage（精确），
                # 结构占比按字符分摊（估算）。落到 run-trace 的 llm_usage 字段。
                yield AgentEvent(
                    type=EventType.DEBUG,
                    content="",
                    metadata={
                        "step": step + 1,
                        "llm_usage": self._llm_usage_detail(
                            input_tokens, output_tokens, prompt, tool_schemas),
                    },
                )
                consecutive_llm_failures = 0
                self.breaker.record_success()
            except Exception as e:
                detail = self._format_llm_error(e)
                elapsed = time.monotonic() - llm_started
                logger.error("LLM 调用失败（%.1fs）: %s", elapsed, detail)
                timeout_draft = streamed_answer.strip() or str(
                    state.context.get("_streamed_draft", "")
                ).strip()
                if self._is_non_retryable_llm_error(e):
                    yield AgentEvent(
                        type=EventType.ERROR,
                        content=detail,
                        metadata={
                            "error": str(e),
                            "reason": "llm_non_retryable_error",
                            "http_status": self._http_status(e),
                        },
                    )
                    return
                consecutive_llm_failures += 1
                self.breaker.record_failure()
                if consecutive_llm_failures >= self.config.max_consecutive_llm_failures:
                    if self._has_research_evidence(state):
                        async for evt in self._synthesize_and_finish(
                            state, start_time,
                            f"连续 LLM 请求失败（{consecutive_llm_failures} 次）",
                            max_attempts=1,
                        ):
                            yield evt
                        return
                    if timeout_draft:
                        async for evt in self._emit_synthesis_fallback(
                            state, start_time,
                            "模型连续请求超时，且尚未完成工具调用、取得可验证证据。",
                            reason="llm_retry_exhausted",
                        ):
                            yield evt
                        return
                    yield AgentEvent(
                        type=EventType.ERROR,
                        content=(f"{detail}；连续失败 {consecutive_llm_failures} 次，"
                                 "本次研究已停止，请稍后重试。"),
                        metadata={"error": repr(e), "reason": "llm_retry_exhausted"},
                    )
                    return
                yield AgentEvent(
                    type=EventType.WARNING,
                    content=(f"{detail}；正在重试 "
                             f"({consecutive_llm_failures}/"
                             f"{self.config.max_consecutive_llm_failures})。"),
                    metadata={"error": repr(e), "reason": "llm_retrying"},
                )
                continue
            finally:
                usage_scope.__exit__(None, None, None)

            # 3. 解析响应
            tool_calls = response.get("tool_calls")
            content = response.get("content")

            # 有思考内容先流给前端
            if content:
                yield AgentEvent(
                    type=EventType.THOUGHT,
                    content=content[:2000],
                    metadata={"step": step + 1},
                )

            # 3a. 有 tool calls → 执行 → 回灌 → 继续
            if tool_calls:
                turn = self._new_turn(state, step, content, tool_calls)
                async for evt in self._handle_tool_calls(state, tool_calls, step, start_time, turn):
                    yield evt
                self.trace.add_turn(turn)
                # finish 成功输出结论后，_handle_tool_calls 会在 context 打上
                # _research_done 标记，外层循环据此终止（避免再空转一轮触发预算检查）
                if state.context.get("_research_done"):
                    return
                continue

            # 3b. 无 tool calls：LLM 想直接输出结论（content）
            answer = content or ""
            if self._light_data_required(state) and not self._has_light_data_evidence(state):
                self._reject_count += 1
                gap = self._light_data_evidence_gap(state)
                logger.warning("light-data 未取数即作答（第 %s 次）：%s",
                               self._reject_count, gap)
                if self._reject_count >= self.config.max_content_rejects:
                    answer = (
                        "本次未能取得可验证的行情或财务数据，因此无法给出该指标。"
                        "请稍后重试；系统不会用模型记忆中的旧数据代替实时取数。"
                    )
                else:
                    yield AgentEvent(
                        type=EventType.WARNING,
                        content=(f"尚未取得可用数据（{gap}）。"
                                 "请先调用 market.* 或 calc.metric 拿到数值再回答，"
                                 "不要用模型记忆里的数字。"),
                        metadata={"step": step + 1, "reason": "light_data_missing"},
                    )
                    continue
            if self._finish_required(state):
                # 研究类：程序化检查「关键数字必须已取数」，防心算报数。
                # 结论含数字但本次会话无任何成功取数 → 打回继续；否则放行。
                if not self._conclusion_sufficient(state, answer):
                    self._reject_count += 1
                    if self._reject_count >= self.config.max_content_rejects:
                        # 连续多次仍不取数：丢弃模型的未验证结论，只返回
                        # 确定性的安全失败说明。安全门拒绝的原文绝不得「降级放行」。
                        logger.warning(
                            f"连续 {self._reject_count} 轮未取数输出结论，改为安全失败说明"
                        )
                        state.context["degraded"] = "未获得可验证的工具数据"
                        answer = (
                            "本次研究未能获得可验证的工具数据，因此无法形成可交付的"
                            "投资结论，也不会输出未经取数核对的数字或建议。\n\n"
                            "请稍后重试，或提供明确的公司代码与公开披露材料供进一步确认。"
                        )
                    else:
                        yield AgentEvent(
                            type=EventType.WARNING,
                            content=(
                                f"结论含关键数字但未取数（第 {self._reject_count} 次）："
                                "请先调用取数/计算工具（如 market.get_bundle / calc.metric）"
                                "拿到数值后再提交，禁止凭记忆报数。"
                            ),
                            metadata={"step": step + 1},
                        )
                        continue
            # 收敛判据硬（PRD 5.4 设计原则三 / 评测集 G-D3）：研究类且注入 research_plan 时，
            # 无 tool_calls 直接输出结论也必须先过 P0 清空校验，否则打回继续。
            if self._finish_required(state) and state.context.get("research_plan") is not None \
                    and not self._plan_converged(state):
                yield AgentEvent(
                    type=EventType.WARNING,
                    content=("研究尚未收敛：仍有 P0 问题未 answered/unanswerable，"
                             "请通过 plan.update 推进问题状态后再提交结论（禁止过早收工）。"),
                    metadata={"step": step + 1, "reason": "premature_convergence"},
                )
                continue

            # 通过充分性检查（或已替换为安全失败说明）后仍须过硬规则。
            rejected = {"v": False}
            async for evt in self._handle_final_answer(
                state, answer, start_time, step, rejected
            ):
                yield evt
            if rejected["v"]:
                # 被硬规则拦截：打回 LLM 修正后重试（避免把提示当最终回答）
                continue
            return

        # 完整执行 max_steps 轮后停止探索，使用已有证据合成最终回答。
        async for evt in self._synthesize_and_finish(
            state, start_time, f"达到最大研究轮数（{self.config.max_steps}）",
        ):
            yield evt

    @staticmethod
    def _llm_usage_detail(input_tokens: int, output_tokens: int,
                           prompt, tool_schemas) -> dict:
        """step 级 token 明细（token 预算优化的度量依据）。

        input/output 直接取模型 usage（精确值）；结构占比没有 tokenizer
        可用，按字符占比分摊成估算值。字符口径与实际发送内容一致：
        system + user + 工具 schema。
        """
        try:
            schema_chars = len(json.dumps(tool_schemas, ensure_ascii=False))
        except (TypeError, ValueError):
            schema_chars = 0
        source_bd = dict(getattr(prompt, "breakdown", None) or {})
        context_chars = min(
            max(0, int(source_bd.get("context", 0) or 0)), len(prompt.system),
        )
        bd = {
            **source_bd,
            "system_base": len(prompt.system) - context_chars,
            "context": context_chars,
            "user": len(prompt.user),
            "tool_schemas": schema_chars,
        }
        actual_chars = len(prompt.system) + len(prompt.user) + schema_chars
        if input_tokens > 0 and actual_chars > 0:
            ratio = input_tokens / actual_chars
            est = {k: max(0, int(round(float(v or 0) * ratio))) for k, v in bd.items()}
        else:
            est = {}
        history_market = est.get("history_market", 0)
        history_web = est.get("history_web", 0)
        evidence = max(
            0,
            est.get("context", 0) - est.get("plan", 0) - history_market - history_web,
        )
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "system_prompt_tokens": est.get("system_base", 0),
            "history_tokens": 0,
            "tool_result_tokens": history_market,
            "evidence_tokens": evidence,
            "web_content_tokens": history_web,
            "plan_tokens": est.get("plan", 0),
            "tool_schema_tokens": est.get("tool_schemas", 0),
            "attribution_method": "proportional_chars_estimate",
            "chars": bd,
            "est_tokens": est,
        }

    def _soft_limit_reason(self, state: AgentState, elapsed: float) -> str:
        """返回停止扩展研究的原因；soft limit 永远不直接终止请求。"""
        if elapsed >= self.config.timeout_seconds:
            return f"达到研究时间 soft limit（{self.config.timeout_seconds}s）"
        budget_reason = self.budget.soft_limit_reason(state)
        if budget_reason:
            return budget_reason
        if state.consecutive_no_progress_rounds >= self.config.max_no_progress_rounds:
            return f"连续 {state.consecutive_no_progress_rounds} 轮没有研究进展"
        return ""

    async def _synthesize_and_finish(
        self, state: AgentState, start_time: float, reason: str, *, max_attempts: int = 2,
    ) -> AsyncIterator[AgentEvent]:
        """停止工具扩展，基于已有证据单独执行 synthesis。

        synthesis 不计入探索轮数，也不再暴露任何工具。最终模型失败时重试一次；
        仍失败则交付明确的收尾失败状态，不把行为预算伪装成研究失败。
        """
        state.context["soft_limit_reason"] = reason
        state.context["forced_synthesis"] = True
        yield AgentEvent(
            type=EventType.WARNING,
            content="研究范围已收口，正在基于已有证据生成最终回答。",
            metadata={"reason": "soft_limit", "soft_limit_reason": reason},
        )

        if not self._has_research_evidence(state):
            async for evt in self._emit_synthesis_fallback(
                state, start_time,
                "达到研究行为上限时仍未取得可验证证据，无法形成投资结论。",
                reason="no_evidence_for_synthesis",
            ):
                yield evt
            return

        state.context["_prompt_stage"] = "synthesis"
        context_package = self.context_manager.prepare(
            state, [], stage="synthesis",
            input_token_budget=max(
                3000, min(16000, self.config.max_tokens - state.tokens_used),
            ),
        )
        yield AgentEvent(
            type=EventType.DEBUG,
            content="",
            metadata={
                "step": state.step_count + 1,
                "context_usage": context_package.audit,
            },
        )
        try:
            prompt = await self.assembler.build(state, context_package=context_package)
        except Exception as exc:
            logger.exception("synthesis prompt 组装失败")
            async for evt in self._emit_synthesis_fallback(
                state, start_time, f"最终回答上下文组装失败：{exc}",
                reason="synthesis_context_failed",
            ):
                yield evt
            return

        prompt.system += (
            "\n\n【强制收尾 / synthesis】探索阶段已经结束，禁止调用任何工具，"
            "也不得要求继续检索。请仅基于现有工具观测生成一份可独立阅读的最终回答。"
            "自然区分已证实结论、合理推断和必要的数据限制；只披露会影响用户决策的缺口，"
            "不要把 soft limit、token、轮次、工具预算、工具名、状态码或 AI 置信度写给用户。"
        )

        finalize_timeout = self.config.finalize_timeout_seconds or self.config.llm_timeout_seconds
        last_error: Exception | None = None
        call_prompt = prompt
        for attempt in range(max_attempts):
            try:
                from core.llm import llm_usage_stage
                with llm_usage_stage("synthesis"):
                    if hasattr(self.llm, "chat_with_usage"):
                        response = await asyncio.wait_for(
                            self.llm.chat_with_usage(
                                system=call_prompt.system,
                                user=call_prompt.user,
                                temperature=0.3,
                                timeout=finalize_timeout,
                            ),
                            timeout=finalize_timeout,
                        )
                        answer = response.get("content", "")
                        input_tokens = int(response.get("prompt_tokens", 0) or 0)
                        output_tokens = int(response.get("completion_tokens", 0) or 0)
                        state.tokens_used += int(response.get("tokens_used", 0) or 0) or (
                            input_tokens + output_tokens
                        )
                    elif hasattr(self.llm, "chat"):
                        answer = await asyncio.wait_for(
                            self.llm.chat(
                                system=call_prompt.system,
                                user=call_prompt.user,
                                temperature=0.3,
                                timeout=finalize_timeout,
                            ),
                            timeout=finalize_timeout,
                        )
                        input_tokens = output_tokens = 0
                    else:
                        response = await asyncio.wait_for(
                            self.llm.chat_with_tools(
                                system=call_prompt.system,
                                user=call_prompt.user,
                                tools=[],
                                temperature=0.3,
                                timeout=finalize_timeout,
                            ),
                            timeout=finalize_timeout,
                        )
                        answer = response.get("content", "")
                        input_tokens = int(response.get("prompt_tokens", 0) or 0)
                        output_tokens = int(response.get("completion_tokens", 0) or 0)
                        state.tokens_used += int(response.get("tokens_used", 0) or 0) or (
                            input_tokens + output_tokens
                        )

                if input_tokens or output_tokens:
                    yield AgentEvent(
                        type=EventType.DEBUG,
                        content="",
                        metadata={
                            "step": state.step_count + 1,
                            "llm_usage": self._llm_usage_detail(
                                input_tokens, output_tokens, call_prompt, []
                            ),
                        },
                    )

                rejected = {"v": False}
                async for evt in self._handle_final_answer(
                    state, str(answer or ""), start_time,
                    max(0, state.step_count - 1), rejected,
                ):
                    yield evt
                if not rejected["v"]:
                    return
                # 安全修订不再重发整套行业手册和证据工作集。只给“编辑合同
                # + 上轮草稿 + 拦截原因”，不引入新事实，避免 synthesis 重试翻倍成本。
                call_prompt = AssembledPrompt(
                    system=(
                        "你是投资研报合规编辑。只修改用户提供的草稿，不得新增任何"
                        "数字、事实或来源。删除目标价、买卖点、止盈止损和交易指令；"
                        "无法核对的数字删除或明确标为推断。保留报告结构与已有 evidence_id。"
                        "修订稿不要复述被删除的禁用词，也不要写合规辩解。"
                        "只输出修订后的完整报告。"
                    ),
                    user=(
                        "校验失败原因："
                        + str(state.context.get("last_reject_reason", "未知"))
                        + "\n\n待修订草稿：\n" + str(answer or "")
                    ),
                    breakdown={"system_base": 0, "context": 0, "user": 0},
                )
            except Exception as exc:  # 单次最终模型失败：重试一次
                last_error = exc
                logger.warning("synthesis 第 %d 次失败: %s", attempt + 1, exc)
                if attempt + 1 < max_attempts:
                    continue

        if isinstance(last_error, (TimeoutError, asyncio.TimeoutError)):
            async for evt in self._emit_timeout_degraded_answer(
                state, "", start_time, max(0, state.step_count - 1),
            ):
                yield evt
            return
        async for evt in self._emit_synthesis_fallback(
            state, start_time,
            "最终回答生成连续失败，已停止本次请求；已取得的证据仍保留在来源列表中。",
            reason="synthesis_failed",
        ):
            yield evt

    async def _emit_synthesis_fallback(
        self, state: AgentState, start_time: float, message: str, *, reason: str,
    ) -> AsyncIterator[AgentEvent]:
        body = (
            f"{message}\n\n"
            "本次没有输出未经证据支持的投资判断。你可以基于已保留的来源继续研究。"
        )
        if self.trace is not None:
            self.trace.final_report = FinalReport(
                conclusions=[Conclusion(
                    text=body,
                    evidence_ids=[e.evidence_id for e in self.trace.evidence_pack],
                )],
                unanswered=[],
                budget_exhausted=False,
            )
        yield AgentEvent(
            type=EventType.FINAL_ANSWER,
            content=body,
            metadata={
                "steps_taken": state.step_count,
                "tokens_used": state.tokens_used,
                "duration_sec": round(time.time() - start_time, 2),
                "validated": True,
                "degraded": True,
                "research_complete": False,
                "budget_exhausted": False,
                "soft_limit_reached": bool(state.context.get("soft_limit_reason")),
                "soft_limit_reason": state.context.get("soft_limit_reason", ""),
                "reason": reason,
                "citations": self._final_citations(),
            },
        )

    async def _emit_hard_deadline_report(
        self, state: AgentState, start_time: float,
    ) -> AsyncIterator[AgentEvent]:
        body = (
            "本次请求达到系统级最长运行时间，后台工作已被强制停止。"
            "系统已保留此前取得的来源与证据，本次不提供投资判断。"
        )
        if self.trace is not None:
            self.trace.final_report = FinalReport(
                conclusions=[Conclusion(
                    text=body,
                    evidence_ids=[e.evidence_id for e in self.trace.evidence_pack],
                )],
                unanswered=[], budget_exhausted=False,
            )
        yield AgentEvent(
            type=EventType.ERROR,
            content=body,
            metadata={
                "reason": "hard_deadline",
                "hard_deadline_seconds": self.config.hard_timeout_seconds,
                "duration_sec": round(time.time() - start_time, 2),
                "citations": self._final_citations(),
            },
        )

    async def _call_llm(
        self, prompt, tool_schemas: list[dict], *, timeout: Optional[float] = None,
    ) -> dict:
        """调用 LLM；无 tools 时退化为普通 chat（兼容无 function calling 的环境）。"""
        request_timeout = timeout or self.config.llm_timeout_seconds
        if tool_schemas and hasattr(self.llm, "chat_with_tools"):
            return await self.llm.chat_with_tools(
                system=prompt.system,
                user=prompt.user,
                tools=tool_schemas,
                temperature=0.7,
                timeout=request_timeout,
            )
        # 退化路径：无 function calling
        if hasattr(self.llm, "chat_with_usage"):
            return await self.llm.chat_with_usage(
                system=prompt.system,
                user=prompt.user,
                temperature=0.7,
                timeout=request_timeout,
            )
        text = await self.llm.chat(
            system=prompt.system,
            user=prompt.user,
            temperature=0.7,
            timeout=request_timeout,
        )
        return {"content": text, "tool_calls": None, "tokens_used": 0}

    @staticmethod
    def _http_status(exc: Exception) -> Optional[int]:
        """从 httpx/OpenAI 兼容异常中提取 HTTP 状态码。"""
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
        status = getattr(exc, "status_code", None)
        return status if isinstance(status, int) else None

    @classmethod
    def _is_non_retryable_llm_error(cls, exc: Exception) -> bool:
        """认证、付费、参数和权限类 4xx 重试不会自愈，应立即失败。"""
        status = cls._http_status(exc)
        return status is not None and 400 <= status < 500 and status not in (408, 409, 429)

    @classmethod
    def _format_llm_error(cls, exc: Exception) -> str:
        status = cls._http_status(exc)
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or \
                exc.__class__.__name__.endswith("Timeout"):
            return "LLM 请求超时：模型网关未在时限内返回"
        if status == 401:
            return "LLM 认证失败（401）：请检查 IC_LLM_API_KEY 是否正确且未过期。"
        if status == 402:
            return (
                "LLM 服务拒绝请求（402 Payment Required）："
                "当前 TokenHub 账户额度、套餐或计费授权不可用，"
                "请充值/开通 hy3 后重试，或切换到可用的 LLM 端点。"
            )
        if status == 403:
            return "LLM 权限不足（403）：当前凭证无权访问所配置模型。"
        if status is not None:
            return f"LLM 请求被拒绝（HTTP {status}）：{exc}"
        return f"LLM 调用失败：{exc}"

    def _get_full_tool_schemas(self, skill_name: Optional[str]) -> list[dict]:
        """获取当前 Skill 的全量工具 schema（OpenAI 格式），不做 phase 门控。"""
        from toolkit.registry import get_tool_schemas_for_llm
        raw = get_tool_schemas_for_llm(skill_name)
        return [
            {"type": "function", "function": s}
            for s in raw
        ]

    def _get_tool_schemas(self, state: AgentState) -> list[dict]:
        """获取当前 Skill 可用工具 schema，并按 skill_phase 编排门控过滤（P1）。

        未配置门控的 skill（或 phase 未定义）返回全量；研究类 skill 按
        SKILL_PHASE_TOOLS 的阶段白名单过滤，未达阶段的工具不暴露给 LLM。
        注入 research_plan 后，额外下发 plan.update（循环内状态迁移的唯一出口）。
        """
        skill_name = state.active_skill or state.pinned_skill
        all_schemas = self._get_full_tool_schemas(skill_name)
        gates = SKILL_PHASE_TOOLS.get(skill_name or "")

        if not gates:
            result = all_schemas
        else:
            phase = int(state.context.get("skill_phase", 0))
            allowed = set(gates.get(phase, set()))
            from runtime.capabilities import capability_tools_for_skill
            allowed.update(capability_tools_for_skill(
                Path(__file__).resolve().parents[1], skill_name or "", phase=phase,
                business_group=state.context.get("selected_adapters")
                or str(state.context.get("business_group") or ""),
                allow_dynamic=not bool(state.context.get("business_group_fallback")),
            ))
            if not allowed:
                # 未知 phase → 全量（容错，不卡死）
                result = all_schemas
            else:
                # schema 名是下划线形式（entity_resolve），白名单是点号形式（entity.resolve）
                allowed_names = {name.replace(".", "_") for name in allowed}
                result = [s for s in all_schemas
                          if s["function"].get("name") in allowed_names]

        # 注入研究计划后下发 plan.update（供 LLM 驱动问题状态迁移）
        if state.context.get("research_plan") is not None:
            result = list(result) + [self._plan_update_tool_schema()]
        return result

    @staticmethod
    def _prune_research_tools(state: AgentState, schemas: list[dict]) -> list[dict]:
        """兼容评测入口；生产路径由 ContextManager 统一生成工具面。"""
        from runtime.context_manager import ContextManager
        return ContextManager().prepare(
            state, schemas, stage="execution",
        ).tool_schemas

    def _advance_skill_phase(self, state: AgentState, tool_name: str, success: bool) -> None:
        """工具执行后推进 skill_phase（P1 编排门控）。

        仅推进、不倒退。成功执行关键工具后，把 phase 提到该工具对应的最小阶段。
        注意：LLM 返回的工具名可能是点号（company.classify）或下划线（company_classify）
        两种形式，须用 resolve_tool_name 归一化到注册名再匹配 _PHASE_ADVANCE_ON_TOOL。
        """
        gates = SKILL_PHASE_TOOLS.get(state.active_skill or state.pinned_skill or "")
        if not gates:
            return
        if not success:
            return
        from toolkit.registry import resolve_tool_name
        target = _PHASE_ADVANCE_ON_TOOL.get(resolve_tool_name(tool_name))
        if target is None:
            return
        current = int(state.context.get("skill_phase", 0))
        if target > current:
            state.context["skill_phase"] = target

    # ── Trace / 不变量落盘 ──────────────────────────────────

    def _plan_converged(self, state: AgentState) -> bool:
        """研究计划是否收敛：P0 全 answered 或 unanswerable（带 reason）。"""
        rp = state.context.get("research_plan")
        if rp is None:
            return True
        return bool(getattr(rp, "is_converged", True))

    @staticmethod
    def _activate_p0_for_latency(research_plan) -> None:
        """低延迟模式由代码一次性激活 P0，避免模型生成大段 plan.update JSON。"""
        for question in getattr(research_plan, "p0_questions", []):
            if question.status != "pending":
                continue
            research_plan.advance(
                question.id,
                "in_progress",
                activation={
                    "falsification": f"若工具证据与「{question.question}」的核心假设相反，则证伪",
                    "completion_rule": "取得可引用的取数、计算或检索证据并形成明确判断",
                    "required_evidence": [{
                        "type": "tool_evidence",
                        "level": "A",
                        "description": "本轮工具返回的可追溯证据",
                    }],
                },
            )

    def _plan_question_ids(self, state: AgentState) -> set[str]:
        rp = state.context.get("research_plan")
        if rp is None:
            return set()
        return {q.id for q in rp.questions}

    def _extract_question_ids(self, content: str, state: AgentState) -> list[str]:
        """从 LLM 思考文本中抽取声明的目标问题 id（须真实存在于研究计划）。"""
        ids = re.findall(r"\bq\d+\b", content or "")
        valid = self._plan_question_ids(state)
        seen: list[str] = []
        for qid in ids:
            if qid in valid and qid not in seen:
                seen.append(qid)
        return seen

    def _new_turn(self, state: AgentState, step: int, content: str,
                  tool_calls: list[dict]) -> LoopTurn:
        """构建本轮 LoopTurn：declaration（不变量①落点）+ 预算快照。"""
        parallel = [tc.get("name", "") for tc in tool_calls]
        remaining = max(0, self.config.max_tool_calls - state.tool_call_count)
        intent = (content or "").strip()[:500]
        if not intent:
            # 流式 function calling 下模型经常不输出任何文本直接发工具调用。
            # 不变量①的落点由代码兜底：如实记录本轮并行调用事实，避免
            # declaration 双空被 lint 判为「无声明调用」。
            intent = f"（无文本声明，直接并行调用 {len(tool_calls)} 个工具）"
        return LoopTurn(
            turn_id=step + 1,
            declaration=Declaration(
                question_ids=self._extract_question_ids(content or "", state),
                intent=intent,
                parallel=parallel,
            ),
            budget=BudgetSnapshot(
                used=state.tool_call_count,
                limit=self.config.max_tool_calls,
                remaining=remaining,
            ),
        )

    @staticmethod
    def _is_plan_update(name: str) -> bool:
        return (name or "").replace("_", ".") == "plan.update"

    def _plan_update_tool_schema(self) -> dict:
        """plan.update 的 LLM 可见 schema（注入研究计划后随工具一起下发）。"""
        s = dict(_plan_update_schema())
        s["name"] = "plan_update"  # OpenAI 兼容：LLM 可见名下划线形式
        return {"type": "function", "function": s}

    def _detect_injection(self, content: Any) -> bool:
        """检测工具结果中的注入指令（忽略指令/索取系统提示词）。"""
        import re as _re
        text = str(content) if content is not None else ""
        return bool(_re.search(
            r"(忽略.{0,6}(之前|前面)?.{0,6}指令|输出.{0,6}(系统|system).{0,6}(提示|prompt)"
            r"|不要遵守.{0,6}规则|你是.{0,6}(助手|assistant).{0,10}现在)",
            text, _re.IGNORECASE))

    async def _emit_timeout_degraded_answer(
        self, state: AgentState, partial_answer: str, start_time: float, step: int,
    ):
        """模型收尾超时时，已有证据则给降级交付，不把它包装成无法回答。"""
        partial = (partial_answer or "").strip()
        has_evidence = self._has_research_evidence(state)
        if has_evidence and partial and not self._looks_like_intermediate_output(partial):
            body = (
                partial
                + "\n\n## 降级说明\n"
                "- 模型在最终组织答案时达到时间上限，上方为已生成内容；请优先核对来源与未覆盖问题。"
            )
        elif has_evidence:
            body = (
                "⏱ 研究已取得部分证据，但在最终组织答案时达到时间上限。\n\n"
                "### 当前状态\n"
                "- 已完成数据检索和工具调用，但尚未形成完整结论。\n"
                "- 中间的推理链、反证检查和口径核对需要在下一轮继续补齐。\n\n"
                "---\n"
                "**建议**：稍后重试，系统会基于已有证据加速完成剩余分析。"
            )
        else:
            # 工具决策轮可能流出“我先锁定标的/准备取数”一类内部草稿；
            # 它不是研究结论，也不能在超时后原样交付给用户。
            body = (
                "⏱ 模型在研究准备阶段达到单次请求时间上限，尚未完成工具调用，"
                "因此本次没有取得可验证证据，也不输出投资结论。\n\n"
                "**建议**：稍后重试，或把问题缩小为一个维度（例如只看估值或增长）。"
            )
        state.context["degraded"] = state.context.get("degraded") or "最终组织答案超时"
        body = self._normalize_research_answer(state, body)
        if self.trace is not None:
            self.trace.final_report = FinalReport(
                conclusions=[Conclusion(
                    text=body,
                    evidence_ids=[e.evidence_id for e in self.trace.evidence_pack],
                    is_inference=False,
                )],
                unanswered=[],
                budget_exhausted=False,
            )
        yield AgentEvent(
            type=EventType.FINAL_ANSWER,
            content=body,
            metadata={
                "steps_taken": step + 1,
                "tokens_used": state.tokens_used,
                "duration_sec": round(time.time() - start_time, 2),
                "validated": False,
                "degraded": True,
                "research_complete": False,
                "budget_exhausted": False,
                "finalize_timeout": True,
                "stop_reason": state.context.get("degraded"),
                "citations": self._final_citations(),
            },
        )

    @staticmethod
    def _looks_like_intermediate_output(text: str) -> bool:
        stripped = text.strip()
        return (
            "```json" in stripped
            or stripped.startswith("[")
            or stripped.startswith("{")
            or "Evidence Pack" in stripped
        )

    def _finish_required(self, state: AgentState) -> bool:
        """当前 Skill 是否要求显式 delivery.finish 才能结束。"""
        return (state.active_skill or "").lower() in self.config.finish_required_skills

    @staticmethod
    def _light_data_required(state: AgentState) -> bool:
        return (state.active_skill or "").lower() == "light-data"

    @staticmethod
    def _has_light_data_evidence(state: AgentState) -> bool:
        return not AgentLoop._light_data_evidence_gap(state)

    @staticmethod
    def _light_data_evidence_gap(state: AgentState) -> str:
        """返回「为什么还不算已取数」的说明；空串表示已有可用证据。

        必须把原因说出来：只回一句「请先取数」，已经取过数的模型不知道该改什么，
        只会把同一个工具再调一遍，直到打回次数用尽变成安全失败。
        """
        from toolkit.registry import resolve_tool_name
        gap = ""
        for obs in state.observations:
            if not obs.success:
                continue
            if not isinstance(obs.content, dict):
                gap = (f"{obs.source} 返回了非结构化结果（结果超长被截断成文本），"
                       "请换更窄的参数重取")
                continue
            source = resolve_tool_name(obs.source)
            if source.startswith("calc."):
                if obs.content.get("success", True) is False:
                    gap = f"{source} 调用失败：{obs.content.get('error', '未知错误')}"
                elif obs.content.get("value") is None:
                    gap = (f"{source} 未算出数值"
                           f"（{obs.content.get('reason') or obs.content.get('status') or '缺数据'}），"
                           "可改调 market.get_snapshot 取现价/市盈率")
                else:
                    return ""
            elif source == "market.get_snapshot":
                if any(obs.content.get(k) is not None
                       for k in ("price", "pe", "pb", "market_cap")):
                    return ""
                gap = "market.get_snapshot 没带回现价/市盈率/市值等有效字段"
            elif source == "market.get_fundamentals":
                if obs.content.get("years"):
                    return ""
                gap = "market.get_fundamentals 没带回任何年度财务数据"
        return gap or "本次会话还没有任何成功返回有效数值的 market.* / calc.* 工具结果"

    def _conclusion_sufficient(self, state: AgentState, answer: str) -> bool:
        """
        程序化检查结论充分性。研究类结论无论定量还是定性，都必须至少
        有一条真实取数/检索/计算观测，防止模型靠删掉数字绕过闸门。
        """
        if not answer:
            return False
        return self._has_research_evidence(state)

    def _has_research_evidence(self, state: AgentState) -> bool:
        """实体解析、分类和 plan.update 是流程信号，不是研究证据。"""
        from toolkit.registry import resolve_tool_name

        evidence_prefixes = ("market.", "calc.", "web.", "cognition.")
        for obs in state.observations:
            source = resolve_tool_name(obs.source)
            if (obs.success and obs.content is not None
                    and source.startswith(evidence_prefixes)):
                return True
        if self.trace is not None:
            for evidence in self.trace.evidence_pack:
                source = resolve_tool_name(evidence.source)
                if source.startswith(evidence_prefixes):
                    return True
        return False

    def _mark_plan_done(self, state: AgentState, tool_name: str) -> None:
        """把计划中 action 匹配该工具名的步骤标记为已完成。"""
        plan = state.context.get("plan")
        if not plan:
            return
        plan_progress = state.context.setdefault("plan_progress", [])
        norm = lambda s: (s or "").replace(".", "_").lower()
        target = norm(tool_name)
        for step in plan.get("steps", []):
            if norm(step.get("action")) == target and step.get("id") not in plan_progress:
                plan_progress.append(step.get("id"))

    async def _make_plan(self, state: AgentState, tool_schemas: list[dict]) -> Optional[dict]:
        """规划层：研究类任务在 ReAct 循环前先产出执行计划。失败降级为 None。"""
        if not self._finish_required(state):
            return None  # 非研究类任务不做规划，保持轻量
        try:
            tools_desc = [
                s.get("function", {}).get("name", "") for s in tool_schemas
            ]
            planner = Planner(self.llm)
            return await planner.create(state.user_message, tools_desc)
        except Exception as e:
            logger.warning(f"规划层异常，降级为无计划执行: {e}")
            return None

    async def _handle_tool_calls(
        self, state: AgentState, tool_calls: list[dict], step: int, start_time: float,
        turn: LoopTurn,
    ) -> AsyncIterator[AgentEvent]:
        """执行 LLM 请求的工具调用，回灌 observations，并落本轮 LoopTurn。

        分工：
          - plan.update  → 确定性应用研究计划状态迁移（不变量②回写 + 动态新增）
          - 取数/计算    → 经 executor 执行，结果进证据包（不变量② evidence_diff）
          - delivery.finish → 收敛校验（P0 清空）后才准出（不变量④ / 过早收敛拦截）
        """
        state.phase = AgentPhase.EXECUTING_TOOL

        call_objs = [
            ToolCall(
                name=tc["name"],
                arguments=tc["arguments"],
                call_id=tc.get("id", f"call_{i}"),
            )
            for i, tc in enumerate(tool_calls)
        ]

        # 模型偶尔会在同一响应中发出两条完全相同的 function call。
        # 在发事件和执行前合并，避免前端显示重复调用，执行层仍保留二次防线。
        if self.config.tool_cache_enabled:
            unique_calls: list[ToolCall] = []
            seen_call_keys: set[str] = set()
            from toolkit.registry import resolve_tool_name as _rtn_dedup
            for tc in call_objs:
                key = self._tool_cache_key(_rtn_dedup(tc.name), tc.arguments)
                if key in seen_call_keys:
                    continue
                seen_call_keys.add(key)
                unique_calls.append(tc)
            call_objs = unique_calls

        # 分离三类调用：plan.update（状态迁移）/ delivery.finish（终止闸门）/ 其他（取数计算）。
        from toolkit.registry import resolve_tool_name as _rtn_finish
        plan_update_calls = [tc for tc in call_objs if self._is_plan_update(tc.name)]
        finish_call = next(
            (tc for tc in call_objs if _rtn_finish(tc.name) == "delivery.finish"), None
        )
        other_calls = [
            tc for tc in call_objs
            if _rtn_finish(tc.name) != "delivery.finish" and not self._is_plan_update(tc.name)
        ]

        # 通知前端即将调用
        for tc in call_objs:
            yield AgentEvent(
                type=EventType.TOOL_CALL,
                content=f"正在调用工具: {tc.name}",
                metadata={"tool_name": tc.name, "arguments": tc.arguments, "step": step + 1},
            )

        # ① plan.update：确定性应用到研究计划，回写 plan_diff / open_questions_delta。
        if plan_update_calls:
            await self._apply_plan_updates(state, plan_update_calls, turn)

        # ② 取数/计算：并行执行，结果进证据包与 writeback.evidence_diff。
        if other_calls:
            async for evt in self._execute_data_calls(state, other_calls, turn):
                yield evt
            self._auto_converge_latency_plan(state, turn)

        # ③ delivery.finish：先过收敛校验（P0 清空），再走硬规则 + 输出链路。
        if finish_call:
            result = await self.executor.execute(finish_call)
            data = result.data if isinstance(result.data, dict) else {}
            state.add_observation(Observation(
                source=finish_call.name,
                content=result.data,
                success=result.success,
                error=result.error,
            ))
            if result.success and data.get("finish_allowed"):
                # 收敛判据硬：research_plan 存在且 P0 未清空 → 拦截收工（过早收敛）。
                if (
                    state.context.get("research_plan") is not None
                    and not self._plan_converged(state)
                ):
                    reason = "仍有 P0 问题未 answered/unanswerable，禁止收工"
                    state.context["last_reject_reason"] = reason
                    yield AgentEvent(
                        type=EventType.WARNING,
                        content=f"研究结束被拦截：{reason}。请通过 plan.update 推进问题状态后再提交。",
                        metadata={"tool_name": finish_call.name, "step": step + 1,
                                  "reason": reason},
                    )
                    return
                current_phase = int(state.context.get("skill_phase", 0))
                if current_phase < 3:
                    state.context["skill_phase"] = 3
                conclusion = data.get("conclusion", "")
                yield AgentEvent(
                    type=EventType.TOOL_RESULT,
                    content="信息自检通过，正在提交研究结论…",
                    metadata={
                        "tool_name": finish_call.name,
                        "success": True,
                        "step": step + 1,
                    },
                )
                rejected = {"v": False}
                async for evt in self._handle_final_answer(
                    state, conclusion, start_time, step, rejected
                ):
                    yield evt
                if rejected["v"]:
                    reason = state.context.get("last_reject_reason", "结论未通过合规校验")
                    yield AgentEvent(
                        type=EventType.WARNING,
                        content=f"结论被硬规则拦截：{reason}。请修正后重新提交 delivery.finish。",
                        metadata={
                            "tool_name": finish_call.name,
                            "step": step + 1,
                            "reason": reason,
                        },
                    )
                else:
                    state.context["_research_done"] = True
                    return
            else:
                reason = data.get("reason") or result.error or "信息自检未通过"
                yield AgentEvent(
                    type=EventType.WARNING,
                    content=f"研究结束被拦截：{reason}。请继续补齐信息后再调用 delivery.finish。",
                    metadata={"tool_name": finish_call.name, "step": step + 1, "reason": reason},
                )

        # 连续「只取数、不回写 plan」计数：本轮调了取数类工具但没调 plan.update → +1，
        # 否则归零。供 _react_loop 在达到阈值时注入引导，把模型从取数打转拉回逐题推进。
        if other_calls and not plan_update_calls:
            state.consecutive_data_only_rounds += 1
        else:
            state.consecutive_data_only_rounds = 0

        made_progress = bool(
            turn.writeback.plan_diff
            or turn.writeback.evidence_diff
            or state.context.get("_research_done")
        )
        if made_progress:
            state.consecutive_no_progress_rounds = 0
        else:
            state.consecutive_no_progress_rounds += 1

    async def _apply_plan_updates(self, state: AgentState, plan_update_calls: list,
                                  turn: LoopTurn) -> None:
        """把 plan.update 操作确定性应用到研究计划，回写 writeback + 观测。"""
        rp = state.context.get("research_plan")
        if rp is None:
            return
        ops: list[dict] = []
        for tc in plan_update_calls:
            args = tc.arguments or {}
            ops.extend(args.get("operations") or [])
        # 计划里的 data_requirement 不能只停留在提示词中。每个问题要进入
        # answered 前，在代码层检查本轮会话是否已有对应类型的成功证据。
        # 这样模型无法把估值/指标问题伪装成已完成来绕过正式 loop 取数。
        results: list[dict] = []
        for op in ops:
            missing = self._missing_data_requirement_evidence(state, rp, op or {})
            if missing:
                qid = str((op or {}).get("question_id", "") or "")
                results.append({
                    "op": (op or {}).get("op", ""), "ok": False,
                    "question_id": qid, "field": "status", "old": "in_progress",
                    "new": "answered", "error": missing,
                })
                continue
            results.extend(apply_plan_update(rp, [op]))
        for r in results:
            turn.writeback.plan_diff.append(PlanDiff(
                question_id=r.get("question_id", ""),
                field=r.get("field", ""),
                old=str(r.get("old", "")),
                new=str(r.get("new", "")),
            ))
            if r.get("ok") and r.get("field") == "status" and r.get("new") == "unanswerable":
                turn.writeback.open_questions_delta.append(
                    f"{r.get('question_id')}: {r.get('reason', '')}")
        state.add_observation(Observation(
            source="plan.update",
            content={"operations": results},
            success=all(r.get("ok") for r in results),
            error="; ".join(r.get("error", "") for r in results if not r.get("ok")),
        ))

    @staticmethod
    def _missing_data_requirement_evidence(state: AgentState, research_plan: Any,
                                           operation: dict) -> str:
        """返回 answered 操作缺失的证据说明；满足或非 answered 操作返回空串。"""
        if operation.get("op") != "advance" or operation.get("to") != "answered":
            return ""
        qid = str(operation.get("question_id", "") or "")
        try:
            question = research_plan.get(qid)
        except Exception:  # 不吞掉原有的“不存在问题”校验错误
            return ""
        requirement = str(getattr(question, "data_requirement", "") or "")
        if not requirement:
            return ""

        question_text = str(getattr(question, "question", "") or "")
        if AgentLoop._requires_primary_document(question_text) and not AgentLoop._has_official_fetch(state):
            return f"{qid} 涉及上市状态或定期报告，须用 web.fetch 打开交易所/官方原文"

        from toolkit.registry import resolve_tool_name
        sources = {
            resolve_tool_name(obs.source)
            for obs in state.observations
            if obs.success and obs.content is not None
        }
        has_prefix = lambda prefixes: any(
            source.startswith(prefixes) for source in sources
        )

        if requirement == "market_data_required":
            complete, detail = AgentLoop._has_complete_market_evidence(state, question_text)
            if not complete:
                return f"{qid} 需要完整行情/财报证据：{detail}"
        if requirement == "calc_required" and not has_prefix(("calc.",)):
            return f"{qid} 需要指标计算证据（calc.*）后才能标记 answered"
        if requirement == "memory_verify" and not has_prefix(("market.", "calc.", "web.")):
            return f"{qid} 是历史认知复核问题，须用本轮公开、行情或计算证据验证后才能标记 answered"
        if requirement == "light_evidence":
            # 只有 preloop 拿到过真实公开链接（带 url 的 source_trace），
            # 或本轮已经过白名单 web 检索，才算已收公开证据。
            # "实体解析"等纯结构化兜底无 url，不能据此销项（避免研究过早收尾）。
            facts = state.context.get("company_facts")
            has_open_preloop = False
            if facts is not None:
                for tr in (getattr(facts, "source_trace", None) or []):
                    if getattr(tr, "url", None):
                        has_open_preloop = True
                        break
            if not has_open_preloop and not has_prefix(("web.",)):
                return f"{qid} 需要白名单检索或 preloop 事实包后才能标记 answered"
        return ""

    @staticmethod
    def _requires_primary_document(text: str) -> bool:
        lowered = (text or "").lower()
        return any(keyword.lower() in lowered for keyword in _PRIMARY_DOCUMENT_KEYWORDS)

    @staticmethod
    def _has_official_fetch(state: AgentState) -> bool:
        from toolkit.registry import resolve_tool_name
        from toolkit.web.tools import is_official_finance_url

        for obs in reversed(state.observations):
            if not obs.success or resolve_tool_name(obs.source) != "web.fetch":
                continue
            data = obs.content if isinstance(obs.content, dict) else {}
            if is_official_finance_url(str(data.get("url", ""))) and str(data.get("text", "")).strip():
                return True
        return False

    @staticmethod
    def _has_complete_market_evidence(state: AgentState, question_text: str) -> tuple[bool, str]:
        """验证市场工具的语义完整性，不把 ToolResult.success 当成数据齐备。"""
        from toolkit.registry import resolve_tool_name

        lowered = (question_text or "").lower()
        required = {
            field_name
            for keywords, field_name in _MARKET_FIELD_KEYWORDS
            if any(keyword.lower() in lowered for keyword in keywords)
        }
        saw_market = False
        last_detail = "未调用 market.*"
        for obs in reversed(state.observations):
            if not obs.success or not resolve_tool_name(obs.source).startswith("market."):
                continue
            saw_market = True
            data = obs.content if isinstance(obs.content, dict) else {}
            fetch_status = str(data.get("fetch_status", "") or "").lower()
            if fetch_status and fetch_status != "ok":
                last_detail = f"fetch_status={fetch_status}"
                continue
            missing = {str(f) for f in (data.get("missing_fields") or [])}
            evidence = data.get("field_evidence") if isinstance(data.get("field_evidence"), dict) else {}
            available = set(evidence)
            available.update(k for k, value in data.items() if value is not None and k not in {
                "status", "fetch_status", "missing_fields", "field_evidence", "fallback_results", "prompt_block",
            })
            absent = sorted(field for field in required if field in missing or field not in available)
            if absent:
                last_detail = "所需字段缺失：" + ", ".join(absent)
                continue
            # 旧工具返回没有 fetch_status 时，只允许“问题需要的具体字段”
            # 已实际返回的结果通过；宽泛问题不再凭一个 success 自动销项。
            if not fetch_status and not required:
                last_detail = "返回缺少 fetch_status/field_evidence 完整性信息"
                continue
            if fetch_status == "ok" and (available or not required):
                return True, ""
            if required and not absent:
                return True, ""
        return False, last_detail if saw_market else "未调用 market.*"

    def _auto_converge_latency_plan(self, state: AgentState, turn: LoopTurn) -> None:
        """低延迟模式只做计划记账：对应证据类型已取得后自动销项。

        工具选择仍由模型完成；这里只避免模型为维护内部问题清单额外生成
        大段 plan.update JSON。最终研报仍须披露工具结果中的字段缺失。
        """
        if not state.context.get("latency_mode"):
            return
        rp = state.context.get("research_plan")
        if rp is None:
            return
        for question in getattr(rp, "p0_questions", []):
            if question.status != "in_progress":
                continue
            op = {"op": "advance", "question_id": question.id, "to": "answered"}
            if self._missing_data_requirement_evidence(state, rp, op):
                continue
            old = question.status
            rp.advance(question.id, "answered")
            turn.writeback.plan_diff.append(PlanDiff(
                question_id=question.id,
                field="status",
                old=old,
                new="answered(auto_evidence_gate)",
            ))

    async def _execute_data_calls(self, state: AgentState, other_calls: list,
                                  turn: LoopTurn) -> None:
        """并行执行取数/计算工具：记录 ToolCallRecord + 证据条目 + 注入检测。

        取数去重：同一（解析后的工具名 + 归一化参数）在本会话内若已执行过，
        直接复用上次结果，不再真跑 executor（消掉 hy3 反复拉同一份数据的空转）。
        """
        results, executed = await self._run_data_calls_with_cache(state, other_calls)
        state.tool_call_count += executed

        for tc, result in zip(other_calls, results):
            turn.tool_calls.append(ToolCallRecord(
                tool=tc.name,
                args_digest=digest(tc.arguments),
                result_digest=digest(result.data if result.success else result.error),
                latency_ms=0,
                ok=result.success,
            ))
            obs = Observation(
                source=tc.name,
                content=result.data if result.success else None,
                success=result.success,
                error=result.error,
            )
            state.add_observation(obs)

            # 证据包：成功取数/计算结果进证据包（不变量② evidence_diff）
            if result.success and result.data is not None:
                entry = self.trace.add_evidence(source=tc.name, content=result.data)
                obs.evidence_id = entry.evidence_id
                self.context_manager.add_evidence(
                    state, entry.evidence_id, tc.name, result.data,
                )
                turn.writeback.evidence_diff.append(EvidenceDiff(
                    evidence_id=entry.evidence_id,
                    content_digest=entry.content_digest,
                    source=entry.source,
                ))

            # 注入检测：工具结果里埋了指令 → 标记该轮（不执行、留痕）
            if self._detect_injection(result.data):
                turn.injection_detected = True
                logger.warning(f"[Step] 检测到工具结果注入指令，已标记（未执行）")

            self._advance_skill_phase(state, tc.name, result.success)

            from toolkit.registry import resolve_tool_name as _rtn
            if result.success and _rtn(tc.name) == "company.classify":
                data = result.data if isinstance(result.data, dict) else {}
                group = data.get("group")
                if group:
                    state.context["business_group"] = group
                    m = re.match(r"(G\d+)", str(group))
                    state.context["tier"] = m.group(1) if m else str(group)

            yield AgentEvent(
                type=EventType.TOOL_RESULT,
                content=(str(result.data)[:2000] if result.success else f"工具失败: {result.error}"),
                metadata={
                    "tool_name": tc.name,
                    "success": result.success,
                    "step": state.step_count,
                },
            )
            self._mark_plan_done(state, tc.name)

            # 搜索摘要只是线索。对上市状态/定期报告查询，若结果中已有
            # 交易所原文，编排层直接打开首条，不再把“模型记得调 web.fetch”
            # 当成可靠性条件。每次 search 最多追一条，避免扩大延迟。
            fetch_call = self._official_followup_fetch(tc, result)
            if fetch_call is not None and state.tool_call_count < self.config.max_tool_calls:
                yield AgentEvent(
                    type=EventType.TOOL_CALL,
                    content="正在打开交易所/官方原文",
                    metadata={"tool_name": "web.fetch", "arguments": fetch_call.arguments,
                              "step": state.step_count},
                )
                fetched = await self.executor.execute(fetch_call)
                state.tool_call_count += 1
                turn.tool_calls.append(ToolCallRecord(
                    tool="web.fetch", args_digest=digest(fetch_call.arguments),
                    result_digest=digest(fetched.data if fetched.success else fetched.error),
                    latency_ms=0, ok=fetched.success,
                ))
                fetched_obs = Observation(
                    source="web.fetch", content=fetched.data if fetched.success else None,
                    success=fetched.success, error=fetched.error,
                )
                state.add_observation(fetched_obs)
                if fetched.success and fetched.data is not None:
                    entry = self.trace.add_evidence(source="web.fetch", content=fetched.data)
                    fetched_obs.evidence_id = entry.evidence_id
                    self.context_manager.add_evidence(
                        state, entry.evidence_id, "web.fetch", fetched.data,
                    )
                    turn.writeback.evidence_diff.append(EvidenceDiff(
                        evidence_id=entry.evidence_id, content_digest=entry.content_digest,
                        source=entry.source,
                    ))
                if self._detect_injection(fetched.data):
                    turn.injection_detected = True
                yield AgentEvent(
                    type=EventType.TOOL_RESULT,
                    content=(str(fetched.data)[:2000] if fetched.success else f"工具失败: {fetched.error}"),
                    metadata={"tool_name": "web.fetch", "success": fetched.success,
                              "step": state.step_count},
                )

    @staticmethod
    def _official_followup_fetch(search_call: ToolCall, result: Any) -> ToolCall | None:
        from toolkit.registry import resolve_tool_name
        from toolkit.web.tools import is_official_finance_url

        if resolve_tool_name(search_call.name) != "web.search" or not result.success:
            return None
        query = str((search_call.arguments or {}).get("query", ""))
        if not AgentLoop._requires_primary_document(query):
            return None
        data = result.data if isinstance(result.data, dict) else {}
        for item in data.get("results") or []:
            url = str(item.get("url", "")) if isinstance(item, dict) else ""
            if url and is_official_finance_url(url):
                return ToolCall(name="web.fetch", arguments={"url": url, "max_chars": 8000},
                                call_id="auto_official_fetch")
        return None

    async def _run_data_calls_with_cache(
        self, state: AgentState, other_calls: list,
    ) -> tuple[list, int]:
        """带会话内去重的取数执行：返回 (按 other_calls 对齐的 results, 真执行次数)。

        同一（解析后的工具名 + 归一化参数）若已在 state.tool_cache 中，
        直接复用结果；否则真跑 execute_batch 并把结果写回缓存。
        """
        from toolkit.registry import resolve_tool_name as _rtn_resolve

        cached: list = [None] * len(other_calls)
        to_run: list = []
        run_index: list = []
        pending_by_key: dict[str, int] = {}
        duplicate_of: dict[int, int] = {}
        for i, tc in enumerate(other_calls):
            if self.config.tool_cache_enabled:
                key = self._tool_cache_key(_rtn_resolve(tc.name), tc.arguments)
                if key in state.tool_cache:
                    cached[i] = state.tool_cache[key]
                    continue
                # 同一个模型响应里也可能重复发出完全相同的调用。此时缓存尚未
                # 回写，必须在 batch 内合并，否则仍会真的执行两次。
                if key in pending_by_key:
                    duplicate_of[i] = pending_by_key[key]
                    continue
                pending_by_key[key] = i
            to_run.append(tc)
            run_index.append(i)

        ran = await self._execute_data_batch(to_run) if to_run else []
        for slot, res in zip(run_index, ran):
            cached[slot] = res
            if self.config.tool_cache_enabled:
                tc = other_calls[slot]
                key = self._tool_cache_key(_rtn_resolve(tc.name), tc.arguments)
                state.tool_cache[key] = res

        for duplicate_slot, original_slot in duplicate_of.items():
            cached[duplicate_slot] = cached[original_slot]

        return cached, len(to_run)

    async def _execute_data_batch(self, calls: list[ToolCall]) -> list:
        """保留无依赖并行，同时保证统一行情包先于计算消费者就绪。

        模型可以在同一轮并行声明 ``market.get_bundle`` 与 ``calc.*``。
        行情、搜索、认知等任务会立即并行启动；只有读取行情包的计算任务
        等待本轮行情完成，避免计算工具为同一标的再次访问 provider。
        """
        from toolkit.registry import resolve_tool_name

        market_slots = [
            i for i, call in enumerate(calls)
            if resolve_tool_name(call.name) == "market.get_bundle"
        ]
        calc_slots = [
            i for i, call in enumerate(calls)
            if resolve_tool_name(call.name) in {"calc.metric", "calc.metrics"}
        ]
        if not market_slots or not calc_slots:
            return await self.executor.execute_batch(calls)

        results: list[Any] = [None] * len(calls)
        independent_slots = [i for i in range(len(calls)) if i not in calc_slots]
        independent_tasks = {
            i: asyncio.create_task(self.executor.execute(calls[i]))
            for i in independent_slots
        }

        market_results = await asyncio.gather(*(independent_tasks[i] for i in market_slots))
        for slot, result in zip(market_slots, market_results):
            results[slot] = result

        calc_tasks = {
            i: asyncio.create_task(self.executor.execute(calls[i]))
            for i in calc_slots
        }
        remaining_slots = [i for i in independent_slots if i not in market_slots]
        remaining_tasks = [independent_tasks[i] for i in remaining_slots]
        completed = await asyncio.gather(
            *remaining_tasks,
            *(calc_tasks[i] for i in calc_slots),
        )
        split = len(remaining_slots)
        for slot, result in zip(remaining_slots, completed[:split]):
            results[slot] = result
        for slot, result in zip(calc_slots, completed[split:]):
            results[slot] = result
        return results

    @staticmethod
    def _tool_cache_key(tool_name: str, arguments: dict) -> str:
        """取数去重键：解析后的工具名 + 归一化参数（参数排序后序列化）。"""
        try:
            norm = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            norm = str(arguments)
        return f"{tool_name}|{norm}"

    async def _handle_final_answer(
        self, state: AgentState, answer: str, start_time: float, step: int,
        rejected: Optional[dict] = None, force: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """处理最终回答：先过硬规则校验，再输出。

        Args:
            rejected: 可变标记，被拦截时置 rejected["v"]=True，供调用方决定是否打回重试。
            force: 仅为旧调用兼容保留；不再允许绕过任何安全校验。
        """
        state.phase = AgentPhase.VALIDATING

        hard_constraints_ok, constraint_reason = self._validate_hard_constraints(state, answer)
        if not hard_constraints_ok:
            state.context["last_reject_reason"] = constraint_reason
            if rejected is not None:
                rejected["v"] = True
            yield AgentEvent(
                type=EventType.WARNING,
                content=f"结论触发投资底线校验：{constraint_reason}（已拦截，请完成显式对比后重试）",
                metadata={"step": step + 1, "reason": constraint_reason},
            )
            return

        answer = self._normalize_research_answer(state, answer)

        # 硬规则安全门：若结论需要校验而未通过，拦截并提示
        validated, reason, violated_rules = self._validate_conclusion(state, answer)
        auto_sanitized_numbers: list[str] = []

        if not validated and "R6_numeric_source_required" in violated_rules:
            # R6（关键数字无源）程序化兜底：把无源数字标注为「（推断）」后复检。
            # 打回让模型整篇重生成一次要 20s+，且重写常引入新的无源数字，
            # 在总预算内形成昂贵的重试循环（正文此前已流式推送，重生成还会
            # 造成前端内容重复）。低延迟路径直接采纳标注版；正式路径首次仍
            # 打回让模型修正，第二次仍失败才采纳标注版。
            from toolkit.delivery.submit_conclusion import sanitize_conclusion
            sanitized, replaced = sanitize_conclusion(
                answer, self._tool_observation_payloads(state),
            )
            if replaced:
                ok2, _reason2, _violated2 = self._validate_conclusion(state, sanitized)
                if ok2:
                    prior_r6_rejects = int(state.context.get("r6_rejects", 0))
                    if bool(state.context.get("latency_mode")) or prior_r6_rejects >= 1:
                        answer = sanitized
                        auto_sanitized_numbers = replaced
                        validated = True
                        logger.info(
                            "R6 兜底：%d 个无源数字已标注为推断后放行: %s",
                            len(replaced), ", ".join(replaced[:8]),
                        )

        if not validated:
            if "R6_numeric_source_required" in violated_rules:
                state.context["r6_rejects"] = int(state.context.get("r6_rejects", 0)) + 1
            # 硬规则拦截：不把提示当最终回答，而是打回 LLM 修正后重试
            state.context["last_reject_reason"] = reason
            if rejected is not None:
                rejected["v"] = True
            yield AgentEvent(
                type=EventType.WARNING,
                content=f"结论未通过硬规则校验：{reason}（已拦截，请修正后重试）",
                metadata={"step": step + 1, "reason": reason},
            )
            return

        state.phase = AgentPhase.RESPONDING
        degraded = bool(state.context.get("degraded"))
        rp = state.context.get("research_plan")
        research_complete = not degraded and (
            rp is None or self._plan_converged(state)
        )
        if degraded:
            answer = answer + "\n\n> ⚠️ 降级说明：本结论未经工具取数验证（信息不充分场景），仅作参考。"

        # 落最终报告（不变量③：结论映射到会话证据包；未答 P0 清单）
        if self.trace is not None and self.trace.final_report is None:
            unanswered = []
            if rp is not None:
                unanswered = [
                    Unanswered(question_id=q.id, reason=q.reason or "未处理")
                    for q in rp.questions
                    if q.status not in ("answered", "unanswerable")
                ]
            self.trace.final_report = FinalReport(
                conclusions=[Conclusion(
                    text=answer,
                    evidence_ids=[e.evidence_id for e in self.trace.evidence_pack],
                    is_inference=False,
                )],
                unanswered=unanswered,
                budget_exhausted=False,
            )
        citations = self._final_citations()
        coach = self._extract_coach_payload(answer)

        yield AgentEvent(
            type=EventType.FINAL_ANSWER,
            content=answer,
            metadata={
                "steps_taken": step + 1,
                "tokens_used": state.tokens_used,
                "duration_sec": round(time.time() - start_time, 2),
                "validated": not degraded,
                "degraded": degraded,
                "research_complete": research_complete,
                "soft_limit_reached": bool(state.context.get("soft_limit_reason")),
                "soft_limit_reason": state.context.get("soft_limit_reason", ""),
                "synthesized_from_existing_evidence": bool(
                    state.context.get("forced_synthesis")
                ),
                "safety_validated": validated,
                "auto_sanitized": bool(auto_sanitized_numbers),
                "auto_sanitized_numbers": auto_sanitized_numbers,
                "citations": citations,
                "coach": coach,
            },
        )

        # 研究交付后只提出候选卡，绝不自动写入长期认知库。
        # 抽取输入必须含用户本轮原话（PRD 六类识别信号的载体），不能只喂 AI 结论。
        if state.active_skill == "deep-research" and state.user_id:
            try:
                from store.cognition_store import CognitionStore
                from core.config import get_config
                from core.llm import llm_usage_stage
                store = CognitionStore(get_config().database_url.replace("+aiosqlite", ""))
                with llm_usage_stage("postprocess"):
                    candidates = await store.extract_candidates(
                        answer, user_id=state.user_id,
                        symbol=state.context.get("memory_symbol", ""),
                        source_task_id=state.session_id,
                        user_message=state.user_message,
                        company_name=state.context.get("memory_company", ""),
                        llm_client=self.llm,
                    )
                if candidates:
                    cards = [
                        {"card_id": f"{state.session_id}:memory:{i + 1}", "candidate": candidate}
                        for i, candidate in enumerate(candidates[:3])
                    ]
                    state.context["pending_memory_cards"] = cards
                    yield AgentEvent(
                        type=EventType.ASK_CONFIRMATION,
                        content="研究已完成。以下是候选认知，请确认、编辑后确认或拒绝；未确认不会入库。",
                        metadata={"cards": cards, "source_task_id": state.session_id},
                    )
            except Exception as exc:  # 候选卡失败不影响已经通过的研究结论
                logger.warning("候选认知卡生成失败: %s", exc)

        # 偏好走独立静默通道：不弹卡、不参与判断链，也不影响已交付的结论。
        if state.user_id:
            try:
                from store.cognition_store import CognitionStore
                from core.config import get_config
                store = CognitionStore(get_config().database_url.replace("+aiosqlite", ""))
                preferences = store.extract_preferences(
                    state.user_message, user_id=state.user_id, source_task_id=state.session_id,
                )
                store.merge_preferences(preferences, user_id=state.user_id)
            except Exception as exc:
                logger.warning("偏好静默提取失败: %s", exc)

    def _validate_conclusion(self, state: AgentState, answer: str) -> tuple[bool, str, list[str]]:
        """调用 delivery 层硬规则校验。无需校验时直接通过。

        tier/verdict 等元数据优先从 state.context 读取（company.classify 成功后写入
        business_group/tier）；LLM 在 finish 里填写的 self_check 不涉及这些字段，
        因此不会出现「LLM 自填元数据导致 R1 误判」的问题。

        Returns:
            (是否通过, 人类可读原因, 触发的硬规则 id 列表)
        """
        ctx = state.context or {}
        needs_validation = ctx.get("is_conclusion", False) or state.active_skill in (
            "deep-research", "private-company", "trade-review"
        )
        if not needs_validation:
            return True, "", []

        if not ctx.get("degraded") and not self._has_research_evidence(state):
            return False, "未获得可支撑研究结论的取数、检索或计算证据", ["evidence_missing"]

        from toolkit.delivery.submit_conclusion import validate_conclusion
        result = validate_conclusion(
            conclusion=answer,
            tier=ctx.get("tier", ""),
            research_mode=ctx.get("research_mode", ""),
            verdict=ctx.get("verdict"),
            data_status=ctx.get("data_status", "ok"),
            tool_observations=self._tool_observation_payloads(state),
            require_numeric_sources=True,
        )
        if not result.passed:
            return False, "; ".join(result.reasons) if result.reasons else "违反硬规则", result.violated_rules
        return True, "", []

    def _normalize_research_answer(self, state: AgentState, answer: str) -> str:
        """Add deterministic product-format guardrails without inventing facts."""
        if state.active_skill not in ("deep-research", "private-company", "trade-review"):
            return answer
        text = (answer or "").strip()
        if not text:
            return text

        additions: list[str] = []

        data_status = str(state.context.get("data_status", "ok"))
        degraded = bool(state.context.get("degraded")) or data_status in ("partial", "degraded", "unknown")
        if degraded and not re.search(r"一手验证|进一步确认|查阅|核对|待验证", text):
            additions.append(
                "## 还需要确认\n"
                "- 目前部分关键信息不完整，需结合公告、年报或交易所披露进一步确认后再判断。"
            )

        if not re.search(r"风险|反证|不利因素|证伪", text):
            additions.append(
                "## 风险与反证\n"
                "- 正文未充分展开反证项；上线展示时应把未答问题、数据缺口和不利证据单独列出。"
            )

        if not additions:
            return text
        return text + "\n\n" + "\n\n".join(additions)

    @staticmethod
    def _extract_coach_payload(answer: str) -> Optional[dict[str, str]]:
        """从用户可见研报提取陪练卡；不额外调用模型，也不猜测缺失内容。"""
        text = str(answer or "")

        def section(*headings: str) -> str:
            names = "|".join(re.escape(h) for h in headings)
            match = re.search(
                rf"^##+\s*(?:{names})\s*$\n(?P<body>.*?)(?=^##+\s|\Z)",
                text,
                re.MULTILINE | re.DOTALL,
            )
            return match.group("body") if match else ""

        def clean(value: str, limit: int = 240) -> str:
            value = re.sub(r"\[e?\d+\]", "", value, flags=re.IGNORECASE)
            value = re.sub(r"[*_`#>]", "", value)
            value = re.sub(r"^\s*[-+•]\s*", "", value, flags=re.MULTILINE)
            value = re.sub(r"\s+", " ", value).strip()
            return value[:limit].rstrip("，、；;。")

        conflict = clean(section("市场分歧", "市场在交易什么", "多空分歧"))
        takeaway = clean(section("这次值得留下的投资认知"))
        question_section = clean(section("留给你的问题"), limit=300)
        question_matches = re.findall(r"[^。！？?]{4,120}[？?]", question_section)
        question = question_matches[-1].strip() if question_matches else ""

        if not (conflict and takeaway and question):
            return None
        return {
            "core_conflict": conflict,
            "takeaway": takeaway,
            "reflection_question": question,
        }

    @staticmethod
    def _tool_observation_payloads(state: AgentState) -> list[dict]:
        """R6 数字溯源用的观测载荷（成功观测，剔除流程信号 delivery.finish）。"""
        return [
            {"source": obs.source, "content": obs.content}
            for obs in state.observations
            if obs.success and obs.source != "delivery.finish"
        ]

    def _final_citations(self) -> list[dict]:
        """Return display-ready citations keyed by evidence_id.

        收口清洗：handoff 文档要求前端只看到用户可读类型，不暴露内部工具名。
        1) 内部工具名（market.get.bundle 等）→ 用户可读 source_type；
        2) 纯流程信号（entity_resolver / company_classify / plan_update 等）整条丢弃；
        3) URL 缺失的引用给可读结构化标签，不回退为工具名。
        """
        if self.trace is None:
            return []
        from toolkit.registry import resolve_tool_name
        citations: list[dict] = []
        for entry in self.trace.evidence_pack:
            # 整条 evidence 是纯流程信号 → 跳过（防御性，正常不会进 evidence_pack）
            entry_src = resolve_tool_name(entry.source)
            if entry_src in _FLOW_ONLY_EVIDENCE_SOURCES:
                continue
            data = entry.to_dict()
            nested = data.pop("citations", []) or []
            items = nested if nested else [data]
            for item in items:
                merged = dict(item)
                merged["evidence_id"] = entry.evidence_id
                # 来源面板只展示可核对的联网信源（web.search/fetch、公告新闻等带 url 的）；
                # 结构化取数字段（kind=field）与指标计算（kind=metric）没有网页原文，不下发。
                if not merged.get("url"):
                    continue
                citations.append(merged)
        return _sanitize_citations_for_frontend(citations)

    def _validate_hard_constraints(self, state: AgentState, answer: str) -> tuple[bool, str]:
        """底线冲突的确定性闸门。

        当前底线多为自然语言，不能把语义是否冲突伪装成可靠的正则判断。因此采用
        保守策略：存在 active hard constraint 且结论给出正向行动倾向时，必须完成
        固定四段对比。未来带 metric/operator/threshold 的条目可在这里直接比较事实包。
        """
        constraints = state.context.get("hard_constraints") or []
        if not constraints:
            return True, ""
        positive = re.search(
            r"(建议|推荐|值得).{0,8}(买入|建仓|配置|持有)|(?<!不)买入|(?<!不)建仓|增持",
            answer,
        )
        if not positive:
            return True, ""
        required = ("投资底线对比", "原前提", "本次证据", "模型倾向")
        if all(token in answer for token in required):
            return True, ""
        labels = "；".join(
            f"[{item.get('id', '')}] {item.get('statement', '')}"
            for item in constraints
        )
        return False, (
            f"检测到正向结论，必须针对以下投资底线补充「投资底线对比 / 原前提 / 本次证据 / 模型倾向」四段论证：{labels}"
        )
