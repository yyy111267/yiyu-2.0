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
from typing import Optional

from .state import AgentState

logger = logging.getLogger(__name__)


@dataclass
class AssembledPrompt:
    """组装后的完整 Prompt"""
    system: str  # System message
    user: str    # User message


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

    async def build(self, state: AgentState) -> AssembledPrompt:
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
        
        # 3. 语气分层
        tone = await self._load_tone(state)
        if tone:
            parts.append(tone)
        
        # 4. Skill 工作流（如果有）
        if state.active_skill or state.pinned_skill:
            skill_prompt = await self._load_skill_workflow(state)
            if skill_prompt:
                parts.append(skill_prompt)
        
        # 5. 上下文注入
        context_section = await self._build_context(state)
        if context_section:
            parts.append(context_section)
        
        # 组装 System message
        system = "\n\n---\n\n".join(parts)
        
        # User message 就是原始输入 + 最近的历史
        user = state.user_message
        
        return AssembledPrompt(system=system, user=user)

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
        
        return "\n".join(parts) if parts else ""

    async def _load_tone(self, state: AgentState) -> Optional[str]:
        """根据用户画像加载语气"""
        # TODO: 根据 persona.level 选择 novice/advanced
        return await self._load_prompt("tone/novice.md")

    async def _load_skill_workflow(self, state: AgentState) -> Optional[str]:
        """加载 Skill 工作流定义 + 按 classify 结果动态加载 bus_router/{group} 行业框架（P2）。

        组装顺序：
        1. 加载 skills/{skill_name}/SKILL.md（通用工作流）；
        2. 若 state.context 中已有 business_group（company.classify 执行后写入），
           再动态加载 bus_router/{group}/ 下的行业解读手册，拼到工作流之后。
        这样 LLM 拿到的是「通用流程 + 本行业分析框架」的完整指引。
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

        parts = [skill_path.read_text(encoding="utf-8")]

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

    async def _build_context(self, state: AgentState) -> str:
        """构建上下文部分"""
        sections = []
        
        # 历史观察结果（最近 5 条）
        if state.observations:
            obs_texts = []
            for obs in state.observations[-5:]:
                obs_texts.append(f"[{obs.source}] {'✓' if obs.success else '✗'}\n{str(obs.content)[:500]}")
            
            sections.append("## 历史工具调用结果\n" + "\n\n".join(obs_texts))
        
        # 用户方法论（如果有）
        if state.methodology:
            method_names = [m.get("name", "") for m in state.methodology]
            sections.append(f"## 你的投资方法库\n{', '.join(method_names)}")
        
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
                sections.append("## 当前阶段：锁定标的\n请先调用实体解析工具唯一锁定研究对象（含代码/市场/币种）。")
            elif phase == 1:
                sections.append("## 当前阶段：商业模式识别\n请调用商业模式分类工具确定 group，再进入取数。")
            elif phase == 2:
                sections.append(
                    "## 当前阶段：取数与研究（可多轮）\n"
                    "推荐链路：market.get_bundle 取数 → calc.base_pack 算地基指标 → "
                    "若 base_pack 返回 NC（字段缺失）→ calc.run_code 用数据包相邻字段补算（标 DEGRADED 并注明口径）"
                    "或 web.search/web.fetch 补数 → 交叉验证后撰写结论。\n"
                    "⚠️ 研究要点：\n"
                    "- 地基指标必须由 calc.base_pack 计算，禁止心算；\n"
                    "- base_pack 标 NC 的指标可用 calc.run_code 补算，不要因 NC 就放弃该维度；\n"
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
        
        return "\n\n".join(sections) if sections else ""
