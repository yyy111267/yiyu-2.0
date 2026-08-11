"""
Planner - 研究开始前的任务规划层

在 ReAct 循环之前调用一次轻量 LLM，产出结构化执行计划。
计划不是死流程：只作为执行骨架注入上下文，执行中允许 LLM 按证据偏离。

失败时降级返回 None（无计划执行），不阻塞主流程。
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

PLANNER_SYSTEM = """你是投研研究规划器。根据用户的研究任务，把任务拆成有序的执行计划，输出 JSON。

规划要求：
1. 按依赖排序：实体锁定 → 画像/分类 → 取数 → 指标计算 → 行业/竞争 → 风险 → 估值 → 结论；
2. 每步只做一件事，purpose 写明这一步要回答什么问题；
3. depends_on 标出前置步骤 id（无依赖则为空数组）；
4. 计划允许在执行中被调整：发现信息不足或与预期不符时可修改，不强制按序执行。

只输出 JSON，不要输出任何其他文字。
"""

PLANNER_TASK = """研究任务：{task}

可用工具：{tools}

输出格式：
{{"goal": "本次研究要回答的核心问题",
  "steps": [
    {{"id": "1", "action": "工具名或动作", "purpose": "这一步要回答什么", "depends_on": [], "required": true}}
  ]}}"""


class Planner:
    """规划器：任务开始前生成结构化执行计划。失败降级为 None。"""

    def __init__(self, llm_client) -> None:
        self.llm = llm_client

    async def create(self, task: str, tools: list[str]) -> Optional[dict]:
        try:
            plan = await self.llm.chat_json(
                system=PLANNER_SYSTEM,
                user=PLANNER_TASK.format(task=task, tools=", ".join(tools)),
                temperature=0.2,
            )
            if not isinstance(plan, dict) or not plan.get("steps"):
                logger.warning("规划结果无效（无 steps），降级为无计划执行")
                return None
            # 归一化：确保每步有 id/action/purpose/depends_on
            for i, step in enumerate(plan["steps"]):
                step.setdefault("id", str(i + 1))
                step.setdefault("action", "")
                step.setdefault("purpose", "")
                step.setdefault("depends_on", [])
                step.setdefault("required", True)
            return plan
        except Exception as e:
            logger.warning(f"规划失败，降级为无计划执行: {e}")
            return None
