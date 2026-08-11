"""
Agent Loop - ReAct 循环实现

这是整个系统的核心，实现了标准的 Agent 循环：
感知(Perceive) → 推理(Reason) → 行动(Act) → 观察(Observe) → 循环...
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from .state import AgentState, AgentPhase, Observation
from .assembler import PromptAssembler
from .events import AgentEvent, EventType
from .budget import BudgetConfig, BudgetManager
from .breaker import CircuitBreaker
from .planner import Planner
from toolkit.executor import ToolCall, ToolExecutor

logger = logging.getLogger(__name__)


@dataclass
class LoopConfig:
    """循环配置"""
    max_steps: int = 20  # 最大循环次数
    max_tokens: int = 50000  # 最大 token 消耗
    max_tool_calls: int = 30  # 最大工具调用次数
    timeout_seconds: int = 300  # 单次循环超时（秒）
    # 需要显式 delivery.finish 才能结束的 Skill（研究类，保证信息充分才收尾）
    finish_required_skills: tuple = ("deep-research", "private-company", "trade-review")
    # 连续「无取数结论」打回的最大次数；超过则强制放行（防死循环）
    max_content_rejects: int = 3


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
        # phase 2：已分类 → 取数 + 地基计算 + 补算 + 认知检索 + web 补数 + 收尾均可。
        # 这是核心研究阶段，允许多轮循环：
        #   cognition.recall（双路检索，研究开始时调一次）
        #   → market.get_bundle → calc.base_pack → 发现 NC → calc.run_code 补算
        #   → web.search/web.fetch 补数 → 交叉验证 → delivery.finish
        # market.get_bundle / calc.base_pack / calc.run_code / cognition / web 均不推进 phase，
        # 留在本阶段循环，直到 LLM 调 delivery.finish 通过信息自检才进 phase 3。
        2: {
            "entity.resolve", "company.classify",
            "market.get_bundle", "calc.base_pack", "calc.run_code",
            "cognition.recall", "cognition.extract",
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
# 注意：market.get_bundle / calc.base_pack 不再推进 phase——
# 取数和地基计算只是研究阶段的中间步骤，不意味着研究已完成。
# base_pack 标 NC 后还需 calc.run_code 补算或 web 补数，不能一算完就跳收尾。
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
        self.breaker = CircuitBreaker()
        self._reject_count = 0  # 连续「无取数结论」打回次数，防死循环

    async def run(
        self,
        user_message: str,
        session_id: str,
        skill_name: Optional[str] = None,
    ) -> AsyncIterator[AgentEvent]:
        """
        运行 Agent 主循环（多轮 ReAct）。
        
        每轮：
          1. 组装 prompt（含历史 observations）
          2. 调用 LLM（带 tool schemas）
          3. 若返回 tool_calls → 执行 → 回灌 observations → 继续循环
          4. 若无 tool_calls → 视为最终回答 → 经硬规则校验 → 输出
        """
        start_time = time.time()
        
        state = AgentState(
            session_id=session_id,
            user_message=user_message,
            pinned_skill=skill_name,
            active_skill=skill_name,
        )

        logger.info(f"[Session {session_id}] 开始处理: {user_message[:50]}...")
        
        yield AgentEvent(
            type=EventType.START,
            content=f"开始处理您的请求...",
            metadata={"session_id": session_id},
        )

        try:
            # 规划层：研究类任务先产出执行计划（失败降级为无计划执行）。
            # 规划用全量工具列表（plan 需看到所有可用工具），执行时再按 skill_phase 门控。
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

        except Exception as e:
            logger.exception("Agent 循环异常")
            yield AgentEvent(
                type=EventType.ERROR,
                content=f"系统内部错误: {str(e)}",
                metadata={"error": str(e)},
            )

        finally:
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
        for step in range(self.config.max_steps):
            state.step_count = step + 1
            state.phase = AgentPhase.REASONING

            # P1 编排门控：每轮按当前 skill_phase 计算可见工具（phase 随工具执行推进）
            tool_schemas = self._get_tool_schemas(state)

            logger.info(f"[Step {step+1}/{self.config.max_steps}] 推理中")

            # 预算检查
            if self.budget.is_exceeded(state):
                yield AgentEvent(
                    type=EventType.ERROR,
                    content="预算耗尽（token/轮次/工具调用超限）",
                    metadata={"reason": "budget_exceeded"},
                )
                return

            # 熔断检查
            if self.breaker.is_open():
                yield AgentEvent(
                    type=EventType.ERROR,
                    content="系统繁忙，请稍后重试",
                    metadata={"reason": "circuit_breaker_open"},
                )
                return

            # 1. 组装 prompt（assembler 会把历史 observations 注入）
            try:
                prompt = await self.assembler.build(state)
            except Exception as e:
                logger.error(f"Prompt 组装失败: {e}")
                yield AgentEvent(
                    type=EventType.ERROR,
                    content=f"上下文组装失败: {str(e)}",
                    metadata={"error": str(e)},
                )
                return

            # 2. 调用 LLM（带 tools）
            state.phase = AgentPhase.CALLING_LLM
            try:
                response = await self._call_llm(prompt, tool_schemas)
                state.tokens_used += response.get("tokens_used", 0)
            except Exception as e:
                logger.error(f"LLM 调用失败: {e}")
                self.breaker.record_failure()
                yield AgentEvent(
                    type=EventType.ERROR,
                    content=f"模型调用失败: {str(e)}",
                    metadata={"error": str(e)},
                )
                continue

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
                async for evt in self._handle_tool_calls(state, tool_calls, step, start_time):
                    yield evt
                # finish 成功输出结论后，_handle_tool_calls 会在 context 打上
                # _research_done 标记，外层循环据此终止（避免再空转一轮触发预算检查）
                if state.context.get("_research_done"):
                    return
                continue

            # 3b. 无 tool calls：LLM 想直接输出结论（content）
            answer = content or ""
            if self._finish_required(state):
                # 研究类：程序化检查「关键数字必须已取数」，防心算报数。
                # 结论含数字但本次会话无任何成功取数 → 打回继续；否则放行。
                if not self._conclusion_sufficient(state, answer):
                    self._reject_count += 1
                    if self._reject_count >= self.config.max_content_rejects:
                        # 连续多次仍不取数 → 降级放行（不再打回），避免死循环
                        logger.warning(
                            f"连续 {self._reject_count} 轮未取数输出结论，降级放行"
                        )
                        state.context["degraded"] = "未取数结论降级放行"
                    else:
                        yield AgentEvent(
                            type=EventType.WARNING,
                            content=(
                                f"结论含关键数字但未取数（第 {self._reject_count} 次）："
                                "请先调用取数/计算工具（如 market.get_bundle / calc.base_pack）"
                                "拿到数值后再提交，禁止凭记忆报数。"
                            ),
                            metadata={"step": step + 1},
                        )
                        continue
            # 通过充分性检查（或非研究类）：直接进硬规则校验 → 输出
            # 若已降级（取数打回超限），跳过硬规则校验直接输出，避免「放行→拦截→再放行」死循环
            force = bool(state.context.get("degraded"))
            rejected = {"v": False}
            async for evt in self._handle_final_answer(
                state, answer, start_time, step, rejected, force=force
            ):
                yield evt
            if rejected["v"]:
                # 被硬规则拦截：打回 LLM 修正后重试（避免把提示当最终回答）
                continue
            return

        # 达到最大步数
        yield AgentEvent(
            type=EventType.WARNING,
            content=f"达到最大步数限制 ({self.config.max_steps})，强制结束",
            metadata={"reason": "max_steps_reached"},
        )

    async def _call_llm(self, prompt, tool_schemas: list[dict]) -> dict:
        """调用 LLM；无 tools 时退化为普通 chat（兼容无 function calling 的环境）。"""
        if tool_schemas and hasattr(self.llm, "chat_with_tools"):
            return await self.llm.chat_with_tools(
                system=prompt.system,
                user=prompt.user,
                tools=tool_schemas,
                temperature=0.7,
            )
        # 退化路径：无 function calling
        text = await self.llm.chat(
            system=prompt.system,
            user=prompt.user,
            temperature=0.7,
        )
        return {"content": text, "tool_calls": None, "tokens_used": 0}

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
        """
        skill_name = state.active_skill or state.pinned_skill
        all_schemas = self._get_full_tool_schemas(skill_name)
        gates = SKILL_PHASE_TOOLS.get(skill_name or "")
        if not gates:
            return all_schemas

        phase = int(state.context.get("skill_phase", 0))
        allowed = gates.get(phase, set())
        if not allowed:
            # 未知 phase → 全量（容错，不卡死）
            return all_schemas

        # schema 名是下划线形式（entity_resolve），白名单是点号形式（entity.resolve）
        allowed_names = {name.replace(".", "_") for name in allowed}
        return [
            s for s in all_schemas
            if s["function"].get("name") in allowed_names
        ]

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

    def _finish_required(self, state: AgentState) -> bool:
        """当前 Skill 是否要求显式 delivery.finish 才能结束。"""
        return (state.active_skill or "").lower() in self.config.finish_required_skills

    def _conclusion_sufficient(self, state: AgentState, answer: str) -> bool:
        """
        程序化检查结论充分性：结论中的关键数字必须来自工具取数（防心算报数）。

        - 结论无数字 → 充分（知识问答/定性结论）；
        - 结论有数字 → 本会话必须已有成功取数的工具观测，否则不充分。
        """
        if not answer:
            return False
        import re
        # 数字（可带单位：亿/万/元/%/倍 等）
        has_number = re.search(r"\d+(?:\.\d+)?\s*(?:亿|万|%|％|元|块|倍)?", answer) is not None
        if not has_number:
            return True
        # 有数字：要求至少有一次成功的工具取数观测
        for obs in state.observations:
            if obs.success and obs.content is not None and obs.source != "delivery.finish":
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
        self, state: AgentState, tool_calls: list[dict], step: int, start_time: float
    ) -> AsyncIterator[AgentEvent]:
        """执行 LLM 请求的工具调用，回灌 observations。"""
        state.phase = AgentPhase.EXECUTING_TOOL

        call_objs = [
            ToolCall(
                name=tc["name"],
                arguments=tc["arguments"],
                call_id=tc.get("id", f"call_{i}"),
            )
            for i, tc in enumerate(tool_calls)
        ]

        # 分离 delivery.finish（显式终止）与其他工具。
        # 注意：LLM 收到的 schema 名是下划线形式（delivery_finish），
        # 必须用 resolve_tool_name 归一化到注册名（delivery.finish）再比较。
        from toolkit.registry import resolve_tool_name as _rtn_finish
        finish_call = next(
            (tc for tc in call_objs if _rtn_finish(tc.name) == "delivery.finish"), None
        )
        other_calls = [
            tc for tc in call_objs if _rtn_finish(tc.name) != "delivery.finish"
        ]

        # 通知前端即将调用
        for tc in call_objs:
            yield AgentEvent(
                type=EventType.TOOL_CALL,
                content=f"正在调用工具: {tc.name}",
                metadata={"tool_name": tc.name, "arguments": tc.arguments, "step": step + 1},
            )

        # 有 finish 调用：先执行终止闸门
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
                # 通过信息充分性闸门 → 推进到收尾阶段（phase 3）。
                # 之后若硬规则拦截，LLM 只能改文本重试 finish，不能再取数/计算。
                # （market.get_bundle / calc.base_pack 不再推进 phase，研究阶段可多轮循环）
                current_phase = int(state.context.get("skill_phase", 0))
                if current_phase < 3:
                    state.context["skill_phase"] = 3
                # 进入硬规则校验 + 输出链路
                conclusion = data.get("conclusion", "")
                yield AgentEvent(
                    type=EventType.TOOL_RESULT,
                    content="信息自检通过，正在提交研究结论…",
                    metadata={"tool_name": finish_call.name, "success": True, "step": step + 1},
                )
                rejected = {"v": False}
                async for evt in self._handle_final_answer(
                    state, conclusion, start_time, step, rejected
                ):
                    yield evt
                if rejected["v"]:
                    # 硬规则拦截：告知原因，回灌同批其他工具结果后继续循环
                    reason = state.context.get("last_reject_reason", "结论未通过合规校验")
                    yield AgentEvent(
                        type=EventType.WARNING,
                        content=f"结论被硬规则拦截：{reason}。请修正后重新提交 delivery.finish。",
                        metadata={"tool_name": finish_call.name, "step": step + 1, "reason": reason},
                    )
                else:
                    # 正常完成研究：打标记让外层循环终止（避免再空转一轮触发预算检查）
                    state.context["_research_done"] = True
                    return
            else:
                # 信息自检未通过：明确告知原因，回灌同批其他工具结果后继续循环
                reason = data.get("reason") or result.error or "信息自检未通过"
                yield AgentEvent(
                    type=EventType.WARNING,
                    content=f"研究结束被拦截：{reason}。请继续补齐信息后再调用 delivery.finish。",
                    metadata={"tool_name": finish_call.name, "step": step + 1, "reason": reason},
                )

        # 执行其余工具（无 finish 时即全部工具；finish 被拦截时也执行同批其他工具）
        results = await self.executor.execute_batch(other_calls) if other_calls else []
        state.tool_call_count += len(other_calls)

        for tc, result in zip(other_calls, results):
            obs = Observation(
                source=tc.name,
                content=result.data if result.success else None,
                success=result.success,
                error=result.error,
            )
            state.add_observation(obs)

            # P1 编排门控：关键工具成功后推进 skill_phase（未达阶段不暴露后续工具）
            self._advance_skill_phase(state, tc.name, result.success)

            # P2 动态加载行业框架：company.classify 成功后把 group 写入 context，
            # assembler 下一轮组装 prompt 时据此加载 bus_router/{group} 解读手册。
            from toolkit.registry import resolve_tool_name as _rtn
            if result.success and _rtn(tc.name) == "company.classify":
                data = result.data if isinstance(result.data, dict) else {}
                group = data.get("group")
                if group:
                    state.context["business_group"] = group
                    # 同时写入 tier（如 G1a→G1），供 finish 后硬规则校验 R1（G5 禁买入）使用
                    import re as _re
                    m = _re.match(r"(G\d+)", str(group))
                    state.context["tier"] = m.group(1) if m else str(group)

            yield AgentEvent(
                type=EventType.TOOL_RESULT,
                content=(str(result.data)[:2000] if result.success else f"工具失败: {result.error}"),
                metadata={
                    "tool_name": tc.name,
                    "success": result.success,
                    "step": step + 1,
                },
            )
            # 更新研究计划进度：把 action 匹配该工具的步骤 id 标记为已完成
            self._mark_plan_done(state, tc.name)

    async def _handle_final_answer(
        self, state: AgentState, answer: str, start_time: float, step: int,
        rejected: Optional[dict] = None, force: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """处理最终回答：先过硬规则校验，再输出。

        Args:
            rejected: 可变标记，被拦截时置 rejected["v"]=True，供调用方决定是否打回重试。
            force: 降级放行（取数打回超限）时跳过校验直接输出，避免「放行→拦截」死循环。
        """
        state.phase = AgentPhase.VALIDATING

        # 硬规则安全门：若结论需要校验而未通过，拦截并提示
        validated, reason = self._validate_conclusion(state, answer)

        if not validated and not force:
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
        if degraded:
            answer = answer + "\n\n> ⚠️ 降级说明：本结论未经工具取数验证（信息不充分场景），仅作参考。"
        yield AgentEvent(
            type=EventType.FINAL_ANSWER,
            content=answer,
            metadata={
                "steps_taken": step + 1,
                "tokens_used": state.tokens_used,
                "duration_sec": round(time.time() - start_time, 2),
                "validated": not degraded,
                "degraded": degraded,
            },
        )

    def _validate_conclusion(self, state: AgentState, answer: str) -> tuple[bool, str]:
        """调用 delivery 层硬规则校验。无需校验时直接通过。

        tier/verdict 等元数据优先从 state.context 读取（company.classify 成功后写入
        business_group/tier）；LLM 在 finish 里填写的 self_check 不涉及这些字段，
        因此不会出现「LLM 自填元数据导致 R1 误判」的问题。
        """
        ctx = state.context or {}
        needs_validation = ctx.get("is_conclusion", False) or state.active_skill in (
            "deep-research", "private-company", "trade-review"
        )
        if not needs_validation:
            return True, ""

        from toolkit.delivery.submit_conclusion import validate_conclusion
        result = validate_conclusion(
            conclusion=answer,
            tier=ctx.get("tier", ""),
            info_richness=ctx.get("info_richness", ""),
            research_mode=ctx.get("research_mode", ""),
            verdict=ctx.get("verdict"),
            data_status=ctx.get("data_status", "ok"),
        )
        if not result.passed:
            return False, "; ".join(result.reasons) if result.reasons else "违反硬规则"
        return True, ""
