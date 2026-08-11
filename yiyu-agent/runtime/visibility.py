"""
SSE 字段可见性过滤 - 按 Skill 控制前端可见的事件类型。

不同 Skill 对事件可见性有不同要求：
- deep-research：完整暴露思考过程（thought/tool_call/tool_result）
- quick-screen：只暴露最终结果，隐藏中间思考（减少噪音）
- holdings-track：敏感字段（持仓成本）需脱敏

设计为纯函数，在 loop emit 事件后、发送给前端前过滤。
"""

from __future__ import annotations

import copy
from typing import Optional

from runtime.events import AgentEvent, EventType


# 每个 Skill 默认可见的事件类型
SKILL_VISIBILITY: dict[str, set[EventType]] = {
    "deep-research": {
        EventType.START, EventType.THOUGHT, EventType.DEBUG,
        EventType.TOOL_CALL, EventType.TOOL_RESULT,
        EventType.SPAWN_AGENT, EventType.AGENT_DONE,
        EventType.FINAL_ANSWER, EventType.WARNING, EventType.COMPLETE, EventType.ERROR,
    },
    "quick-screen": {
        # 快筛：隐藏中间思考，只给结果
        EventType.START, EventType.TOOL_CALL,
        EventType.FINAL_ANSWER, EventType.WARNING, EventType.COMPLETE, EventType.ERROR,
    },
    "private-company": {
        EventType.START, EventType.THOUGHT,
        EventType.TOOL_CALL, EventType.TOOL_RESULT,
        EventType.FINAL_ANSWER, EventType.WARNING, EventType.COMPLETE, EventType.ERROR,
    },
    "holdings-track": {
        EventType.START, EventType.FINAL_ANSWER, EventType.WARNING, EventType.COMPLETE, EventType.ERROR,
    },
    "trade-review": {
        EventType.START, EventType.THOUGHT,
        EventType.TOOL_CALL, EventType.TOOL_RESULT,
        EventType.FINAL_ANSWER, EventType.WARNING, EventType.COMPLETE, EventType.ERROR,
    },
}

# 默认可见集（闲聊 / 未知 skill）
DEFAULT_VISIBLE: set[EventType] = {
    EventType.START, EventType.THOUGHT,
    EventType.FINAL_ANSWER, EventType.WARNING, EventType.COMPLETE, EventType.ERROR,
}

# 需脱敏的字段名（出现在 metadata 中时遮蔽）
SENSITIVE_META_KEYS = {"buy_price", "cost", "amount", "quantity"}


def filter_event(event: AgentEvent, skill: Optional[str]) -> Optional[AgentEvent]:
    """
    按 Skill 过滤事件可见性。

    Args:
        event: 原始事件
        skill: 当前 skill（可能为 None 表示闲聊）

    Returns:
        过滤后的 AgentEvent；若该事件不应可见则返回 None（前端不收到）。
    """
    visible_set = SKILL_VISIBILITY.get(skill, DEFAULT_VISIBLE) if skill else DEFAULT_VISIBLE

    if event.type not in visible_set:
        return None

    # 脱敏：holdings-track 等场景下遮蔽敏感字段
    if skill in ("holdings-track",) and event.metadata:
        event = copy.deepcopy(event)
        for k in list(event.metadata.keys()):
            if k in SENSITIVE_META_KEYS:
                event.metadata[k] = "***"
            # 嵌套 arguments 里的敏感字段
            args = event.metadata.get("arguments")
            if isinstance(args, dict):
                for ak in list(args.keys()):
                    if ak in SENSITIVE_META_KEYS:
                        args[ak] = "***"

    return event


def is_visible(event_type: EventType, skill: Optional[str]) -> bool:
    """快速判断某事件类型在指定 skill 下是否可见。"""
    visible_set = SKILL_VISIBILITY.get(skill, DEFAULT_VISIBLE) if skill else DEFAULT_VISIBLE
    return event_type in visible_set
