"""
Prompt 组装器 - 将各层 Prompt 片段组装成完整的 System/User 消息

组装顺序（从外到内）：
1. Constitution（宪法）
2. Reminder（矫正层：定向/反偏见/纪律）
3. Tone（语气分层）
4. Skill 工作流（如果指定了 skill）
5. Context（记忆/行情/历史观察）
"""

import logging
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from .state import AgentState

logger = logging.getLogger(__name__)


@dataclass
class AssembledPrompt:
    """组装后的完整 Prompt"""
    system: str  # System message
    user: str    # User message
    # 按层字符数分解（token 优化的度量依据；各段与 system 中实际内容一一对应）
    breakdown: dict = None

    def __post_init__(self):
        if self.breakdown is None:
            self.breakdown = {}


class PromptAssembler:
    """
    Prompt 组装器
    
    职责：
    1. 按优先级加载各层 prompt 片段
    2. 注入变量和上下文
    3. 控制总长度（避免超 token 限制）
    """

    def __init__(self, prompts_dir: str = "prompts"):
        self.prompts_dir = prompts_dir
        
        # 缓存已加载的 prompt 片段
        self._cache: dict[str, str] = {}

    async def build(self, state: AgentState, context_package: Any = None) -> AssembledPrompt:
        """
        组装完整 Prompt
        
        Args:
            state: Agent 当前状态
            
        Returns:
            AssembledPrompt: 组装好的 system/user 消息
        """
        parts = []
        
        # 1. 宪法（~500 tokens）
        constitution = await self._load_prompt("constitution.md")
        if constitution:
            parts.append(constitution)
        
        # 2. 矫正层注入
        reminder = await self._build_reminder(state)
        if reminder:
            parts.append(reminder)
        
        # 工具决策轮不需要每次重读长篇写作语气；只在最终综合注入。
        prompt_stage = str(state.context.get("_prompt_stage", "execution"))
        tone = None if prompt_stage == "execution" else await self._load_tone(state)
        if tone:
            parts.append(tone)
        
        # 4. Skill 工作流（如果有）
        if state.active_skill or state.pinned_skill:
            skill_prompt = await self._load_skill_workflow(state)
            if skill_prompt:
                parts.append(skill_prompt)
        
        # 5. 上下文注入
        context_section, context_breakdown = await self._build_context(
            state, context_package=context_package,
        )
        if context_section:
            parts.append(context_section)
        
        # 组装 System message
        system = "\n\n---\n\n".join(parts)
        
        # User message 就是原始输入 + 最近的历史
        user = state.user_message

        breakdown = {
            # 固定层：宪法/矫正/语气/工作流/行业手册，每轮内容不变
            "system_base": sum(len(p) for p in parts[:-1]) if context_section else len(system),
            "context": len(context_section or ""),
            "user": len(user),
            **context_breakdown,
        }
        return AssembledPrompt(system=system, user=user, breakdown=breakdown)

    async def _load_prompt(self, filename: str) -> Optional[str]:
        """加载 prompt 文件"""
        if filename in self._cache:
            return self._cache[filename]
        
        try:
            from pathlib import Path
            path = Path(self.prompts_dir) / filename
            
            if path.exists():
                content = path.read_text(encoding="utf-8")
                self._cache[filename] = content
                return content
                
        except Exception as e:
            logger.error(f"加载 prompt 失败 {filename}: {e}")
        
        return None

    async def _build_reminder(self, state: AgentState) -> str:
        """构建矫正层提示"""
        parts = []
        
        # 定向召回
        orientation = await self._load_prompt("reminders/orientation_recall.md")
        if orientation and state.user_id:
            parts.append(orientation.format(user_id=state.user_id))
        
        # 反偏见
        anti_bias = await self._load_prompt("reminders/anti_bias.md")
        if anti_bias:
            parts.append(anti_bias)
        
        # 纪律召回
        discipline = await self._load_prompt("reminders/discipline_recall.md")
        if discipline and state.methodology:
            methods = [m.get("name", "") for m in state.methodology[:3]]
            parts.append(discipline.format(methods=", ".join(methods)))

        # 取数纪律（消掉「反复拉同一份数据」的空转）：同工具 + 同参数会命中会话缓存，
        # 重复调用既浪费预算也不产生新信息。取证必须回写研究计划。
        parts.append(
            "【取数纪律】\n"
            "1) 同一份数据不要重复取：相同参数的取数工具会被会话缓存直接复用，"
            "重复调用不会产生新信息，只会消耗预算。\n"
            "2) 默认批处理：首轮用一次 plan.update 激活全部互相独立的 P0，同时并行调用"
            "取数/检索工具；拿到证据后用一次 plan.update 批量回写 answered/unanswerable。\n"
            "3) 只有问题存在真实前置依赖时才逐题串行；不要为了展示过程把四个独立问题"
            "拆成四次模型往返。所有 P0 收敛后立即调用 delivery.finish。"
        )

        return "\n".join(parts) if parts else ""

    async def _load_tone(self, state: AgentState) -> Optional[str]:
        """根据用户画像加载语气"""
        # TODO: 根据 persona.level 选择 novice/advanced
        return await self._load_prompt("tone/novice.md")

    async def _load_skill_workflow(self, state: AgentState) -> Optional[str]:
        """加载 Skill 工作流定义 + 按领域分类动态加载能力包。

        组装顺序：
        1. 加载 skills/{skill_name}/SKILL.md（通用工作流）；
        2. 始终加载父 Skill 声明的通用价值投资和指标能力；
        3. 若 state.context 已有 business_group，再动态加载对应领域能力包。
        fallback 时不猜测行业手册，只保留通用能力。
        """
        from pathlib import Path
        skill_name = state.active_skill or state.pinned_skill
        if not skill_name:
            return None

        # skills 目录相对项目根（prompts_dir 的上一级）
        project_root = Path(self.prompts_dir).parent
        skill_path = project_root / "skills" / skill_name / "SKILL.md"
        if not skill_path.exists():
            logger.warning(f"Skill 文件不存在: {skill_path}")
            return None
        skill_markdown = skill_path.read_text(encoding="utf-8")

        from runtime.capabilities import load_capabilities_for_skill
        phase = int(state.context.get("skill_phase", 0))
        prompt_stage = str(state.context.get("_prompt_stage", "execution"))
        selected_groups = state.context.get("selected_adapters") or str(
            state.context.get("business_group") or ""
        )
        capability_parts = [
            f"## 已激活能力包：{capability_id}\n\n{instructions}"
            for capability_id, instructions in load_capabilities_for_skill(
                project_root, skill_name, skill_markdown,
                phase=phase,
                business_group=selected_groups,
                prompt_stage=prompt_stage,
                allow_dynamic=not bool(state.context.get("business_group_fallback")),
            )
        ]

        # 完整 SKILL.md / Evidence Pack 是设计文档，不是每轮运行时手册。
        # deep-research 只下发当前阶段的紧凑执行合同；行业手册只在
        # synthesis 出现一次，避免固定上下文 × 轮数。
        if skill_name == "deep-research":
            stage = prompt_stage
            contract = self._deep_research_contract(stage)
            if stage == "synthesis":
                group = state.context.get("business_group")
                group_set = {
                    str(item) for item in (
                        selected_groups if isinstance(selected_groups, list) else [selected_groups]
                    ) if item
                }
                packaged_group = any(
                    capability_id.replace("-", "_") in group_set
                    for capability_id, _ in load_capabilities_for_skill(
                        project_root, skill_name, skill_markdown,
                        phase=phase, business_group=selected_groups,
                        prompt_stage=stage,
                        allow_dynamic=not bool(state.context.get("business_group_fallback")),
                    )
                )
                router_doc = self._load_bus_router(group) if group and not packaged_group else None
                if router_doc:
                    contract += f"\n\n## 行业分析框架（{group}，仅本轮综合使用）\n{router_doc}"
            return "\n\n---\n\n".join([contract, *capability_parts])

        parts = [skill_markdown]

        # 父工作流显式声明的能力包，在其激活阶段自动组合进 prompt。
        # 能力包不是第二个 Agent：模型仍在当前工作流内调用包声明的工具。
        parts.extend(capability_parts)

        # P2b：若 skill 目录下有 evidence_pack.md（四视角中间推理结构），
        # 拼在 SKILL.md 之后、行业框架之前——组织分析步骤会引用它。
        evidence_path = project_root / "skills" / skill_name / "evidence_pack.md"
        if evidence_path.exists():
            parts.append(
                "## 四视角 Evidence Pack 推理结构（组织分析 6a/6b/6c 的强制 JSON 定义）\n\n"
                + evidence_path.read_text(encoding="utf-8")
            )

        # P2：按 classify 结果动态加载行业框架
        group = state.context.get("business_group")
        if group:
            router_doc = self._load_bus_router(group)
            if router_doc:
                parts.append(
                    f"## 行业分析框架（business group: {group}）\n\n{router_doc}"
                )

        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _deep_research_contract(stage: str) -> str:
        if stage == "synthesis":
            return (
                "# 深度研究综合合同\n"
                "探索已结束，不得调用工具或要求补检索。只使用已注入的证据工作集；"
                "数字必须可回溯到 evidence_id，缺失就明示缺口。\n"
                "综合为连续研报：结论与核心矛盾、生意本质、护城河、财务与资本配置、"
                "管理层、行业趋势、估值与安全边际、逆向风险、证伪条件与跟踪指标。"
                "区分事实/估算/判断，结尾包含 AI 置信度与不代表投资确定性。"
            )
        return (
            "# 深度研究执行合同\n"
            "preloop 已完成实体、研究计划和执行配方；不要重新规划。"
            "仅围绕当前未完成 P0 的 data_requirement 取证。\n"
            "工具决策轮 content 保持为空；独立调用并行批处理。数字只能来自 market/calc/web 证据，"
            "不得心算或凭记忆补数。搜索摘要只是线索，上市状态、公告和定期报告须打开官方原文。\n"
            "取证后批量用 plan.update 回写 answered/unanswerable；已有证据不重复取。"
            "所有 P0 收敛后立即停止探索，由系统进入最终综合。"
        )

    def _load_bus_router(self, group: str) -> Optional[str]:
        """加载 bus_router/{group}/ 下的行业解读手册。

        兼容三种布局：
        - bus_router/{group}/skill.md（如 G1a：解读手册）
        - bus_router/{group}/{group}.yaml（如 G5/G6：分区配置）
        - bus_router/{group}.yaml（平铺布局，如有）
        找不到时返回 None（不阻塞主流程，LLM 仍可按 SKILL.md 通用流程分析）。
        """
        from pathlib import Path
        project_root = Path(self.prompts_dir).parent
        base = project_root / "bus_router"
        candidates = [
            base / group / "skill.md",
            base / group / f"{group}.yaml",
            base / f"{group}.yaml",
        ]
        for p in candidates:
            if p.exists():
                return p.read_text(encoding="utf-8")
        return None

    async def _build_context(self, state: AgentState, context_package: Any = None) -> tuple[str, dict]:
        """构建上下文部分。

        返回 (拼接后的上下文段, 字符数分解)。分解用于 token 埋点：
        history 为重发的历史工具结果，其中再按来源拆 market/calc/cognition
        （结构化取数）与 web（网页正文）两类，二者是 context 膨胀的主要来源。
        """
        sections = []

        recipe = state.context.get("research_recipe")
        if isinstance(recipe, dict):
            from runtime.research_recipe import recipe_prompt_block
            sections.append(recipe_prompt_block(recipe))

        current_entity = state.context.get("current_entity")
        if current_entity:
            sections.append(
                "## 当前日期与已锁定证券实体\n"
                f"当前日期：{date.today().isoformat()}\n"
                + json.dumps(current_entity, ensure_ascii=False)
                + "\nsecurity_id/交易所是本地证券主数据已锁定的实体信息；"
                  "不得根据旧搜索摘要将已有证券代码的标的写成‘尚未上市’。"
                  "如需判断最新上市状态或定期报告，必须检索并打开交易所原文。"
            )
        
        # 原始 observation 不直接回灌；只注入与当前未完成问题相关的证据工作集。
        if context_package is not None:
            workset = context_package.evidence_block
            workset_breakdown = dict(context_package.evidence_breakdown)
        else:
            from runtime.evidence_workspace import build_evidence_working_set
            workset, workset_breakdown = build_evidence_working_set(
                state, synthesis=str(state.context.get("_prompt_stage")) == "synthesis",
            )
        history_chars = workset_breakdown["history"]
        history_market_chars = workset_breakdown["history_market"]
        history_web_chars = workset_breakdown["history_web"]
        if workset:
            sections.append("## 当前证据工作集（按未完成问题选取）\n" + workset)
        
        # 用户方法论（如果有）
        if state.methodology:
            method_names = [m.get("name", "") for m in state.methodology]
            sections.append(f"## 你的投资方法库\n{', '.join(method_names)}")

        # PRD 6.2：偏好只影响表达与研究范围；认知目录提供待验证假设，不能当结论。
        preferences = state.context.get("memory_preferences") or []
        if preferences:
            preference_lines = [
                str(p.get("statement", "")) for p in preferences if p.get("statement")
            ]
            if preference_lines:
                sections.append("## 用户偏好（仅影响表达和研究范围；本次明确指令优先）\n- "
                                + "\n- ".join(preference_lines))
        cognition_directory = state.context.get("cognition_directory") or []
        if cognition_directory:
            lines = []
            for item in cognition_directory:
                hard = " [投资底线]" if item.get("is_hard_constraint") else ""
                scope = " [本标的历史判断]" if item.get("subject_scope") == "company" else ""
                lines.append(f"- [{item.get('id', '')}] {item.get('statement', '')}{scope}{hard}")
            sections.append("## 已确认认知目录（只能作为待验证假设；引用须保留 id）\n" + "\n".join(lines))
        hard_constraints = state.context.get("hard_constraints") or []
        if hard_constraints:
            labels = "\n".join(
                f"- [{item.get('id', '')}] {item.get('statement', '')}"
                for item in hard_constraints
            )
            sections.append(
                "## 投资底线（代码会在正向结论时强制校验）\n" + labels
                + "\n若结论出现买入/配置等正向倾向，必须逐条写出「投资底线对比」：原前提、本次证据、"
                  "前提是否变化、模型倾向；不得静默绕过。"
            )
        
        # 动态研究计划（PRD 5.4：每轮开头注入计划当前版——问题清单 + 状态）。
        # 这是研究循环的执行清单；plan.update 是状态迁移的唯一出口。
        research_plan = state.context.get("research_plan")
        plan_chars = 0
        if research_plan is not None and context_package is not None:
            if context_package.plan_block:
                sections.append(context_package.plan_block)
                plan_chars = len(context_package.plan_block)
        elif research_plan is not None:
            all_questions = list(getattr(research_plan, "questions", []))
            visible_questions = all_questions
            if str(state.context.get("_prompt_stage")) != "synthesis":
                visible_questions = [
                    q for q in all_questions
                    if q.priority == "P0" and q.status not in ("answered", "unanswerable")
                ]
            qs = [
                {"id": q.id, "priority": q.priority, "status": q.status,
                 "question": q.question, "dimension": q.dimension,
                 "data_requirement": getattr(q, "data_requirement", "")}
                for q in visible_questions
            ]
            completed_ids = [
                q.id for q in all_questions
                if q.priority == "P0" and q.status in ("answered", "unanswerable")
            ]
            sections.append(
                "## 当前研究计划（仅未完成问题）\n"
                + json.dumps(qs, ensure_ascii=False)
                + (f"\n已收敛 P0：{', '.join(completed_ids)}" if completed_ids else "")
                + "\n\n⚠️ 用 plan.update 的结构化调用表达问题意图（指向问题 id），"
                "不要在 content 中叙述‘我先……/接下来……’。观察后必须用 plan.update "
                "回写状态（advance / add_question / downgrade）；所有 P0 进入 answered "
                "或 unanswerable（附 reason）后方可 delivery.finish 收工。"
                "\ndata_requirement 是取证约束：market_data_required 必须有行情/财报工具证据，"
                "calc_required 必须有计算工具证据，memory_verify 必须用本轮公开或市场证据复核；"
                "缺少对应证据时不得标记 answered。"
            )
            plan_chars = len(sections[-1])

        # 研究计划与进度（规划层产出；剩余步骤供 LLM 参考，可调整）
        # 注意：只暴露 purpose（自然语言目的），不暴露 action 工具名——
        # 避免 LLM 拿着 plan 里的工具名去"硬凑"当前尚未解锁的工具。
        # 实际可用的工具以本次 function calling 提供的 schema 为准（P1 门控）。
        plan = state.context.get("plan")
        if plan:
            done = set(state.context.get("plan_progress", []))
            steps = plan.get("steps", [])
            remaining = [
                {
                    "id": s.get("id"),
                    "purpose": s.get("purpose", ""),
                    "depends_on": s.get("depends_on", []),
                }
                for s in steps if s.get("id") not in done
            ]
            sections.append(
                "## 本次研究计划（剩余步骤，执行时可调整顺序）\n"
                + json.dumps(remaining, ensure_ascii=False)
                + "\n\n⚠️ 以上计划仅描述研究目的，具体工具请以 function calling 提供的可用工具为准，"
                "不要尝试调用未提供的工具名。"
            )

        # P1 阶段提示：研究类 skill 按 skill_phase 门控解锁工具，
        # 在关键阶段给 LLM 明确的收尾信号，避免"数据齐了却继续空转"。
        phase = int(state.context.get("skill_phase", 0))
        skill_name = state.active_skill or state.pinned_skill
        if skill_name == "deep-research":
            if phase == 0:
                sections.append(
                    "## 当前阶段：校验研究标的\n"
                    "preloop 已提供实体候选；直接调用当前开放的实体工具完成结构化校验。"
                    "不要输出执行计划或过程说明，工具调用前 content 保持为空。"
                )
            elif phase == 1:
                sections.append(
                    "## 当前阶段：校验商业模式\n"
                    "直接调用当前开放的分类工具确定 group。"
                    "不要输出执行计划或过程说明，工具调用前 content 保持为空。"
                )
            elif phase == 2:
                sections.append(
                    "## 当前阶段：取数与研究（可多轮）\n"
                    "本阶段只执行当前研究计划，不要重述或重新规划全流程。"
                    "需要工具时直接返回 tool calls，工具调用前 content 保持为空。\n"
                    "低延迟推荐链路：首轮批量激活全部独立 P0，并行调用 market.get_bundle、"
                    "cognition.recall 和必要计算；第二轮批量回写全部问题并立即收尾。"
                    "market.get_bundle 是结构化行情的唯一取数入口；calc.metric(s)/calc.run_code "
                    "只复用它生成的统一数据包，不得自行重拉行情。同轮声明行情与计算即可，"
                    "系统会按依赖顺序执行，同时保留其他工具并行。"
                    "推荐工具链：按执行配方用 calc.metrics(metric_ids=...) 批量计算标准指标，"
                    "单项才用 calc.metric；calc.run_code 只算非标情景（须带 symbol）。"
                    "若 calc.metric(s) 返回 field_recovery_plan，必须按其中的 documents / validation "
                    "补一手资料；市值缺失须先核对价格与总股本的股份类别和时点，再用 calc.run_code "
                    "按给出的公式派生，不得只凭搜索摘要补数。\n"
                    "⚠️ 研究要点：\n"
                    "- web.search 的标题/摘要只用于发现线索；上市状态、招股书、"
                    "上市公告和定期报告必须继续用 web.fetch 打开交易所/官方原文；\n"
                    "- market.get_bundle 的 success 只表示调用未异常。fetch_status 不是 ok "
                    "或 missing_fields 包含所需字段时，须继续用官方原文补齐；\n"
                    "- 标准指标必须由 calc.metric(s) 的冻结口径计算，禁止心算；\n"
                    "- 字段缺失时先补一手资料；calc.run_code 只计算非标情景或经披露来源补齐后的派生值；\n"
                    "- 信息充分后调用 delivery.finish 提交结论（通过信息自检后进入收尾阶段）。"
                )
            elif phase >= 3:
                sections.append(
                    "## 当前阶段：撰写结论并收尾\n"
                    "信息自检已通过，核心数据已齐备（实体已锁定、分类已完成、关键数值已取数）。"
                    "请基于取数与计算结果撰写完整研究结论，并调用 delivery.finish 结束研究。\n"
                    "⚠️ 收尾规则：\n"
                    "- delivery.finish 是唯一收尾出口，硬规则校验由系统在提交后自动执行；\n"
                    "- 结论必须包含「AI 置信度」（模型对结论的信心）与「投资确定性」"
                    "（标的未来走势的确定性）的区分声明；\n"
                    "- 结论引用工具证据时使用 evidence_id（如 e1/e2），不要手写 URL；"
                    "系统会按 evidence_id 展开白名单搜索原文链接或指标字段来源；\n"
                    "- 若结论被拦截，按返回原因修正结论文本后重试 delivery.finish，"
                    "不要调用取数/分类工具，也不要追加新的估值计算。"
                )

        # 上次硬规则拦截原因（打回修正提示）
        reject_reason = state.context.get("last_reject_reason")
        if reject_reason:
            sections.append(
                "## 上次结论被拦截（必须修正后再提交）\n"
                f"原因：{reject_reason}\n"
                "⚠️ 请直接修正结论文本后重新调用 delivery.finish。"
                "取数与分类已完成，不要重复调用任何取数/分类/搜索工具，"
                "也不要从头重跑研究流程。"
            )
        
        breakdown = {
            "history": history_chars,
            "history_market": history_market_chars,
            "history_web": history_web_chars,
            "plan": plan_chars,
            "working_set_items": workset_breakdown["working_set_items"],
        }
        return ("\n\n".join(sections) if sections else "", breakdown)
