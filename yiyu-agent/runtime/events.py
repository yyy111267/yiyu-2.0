"""
SSE 事件系统

定义所有 Agent 事件的类型和数据结构。
前端通过 SSE 流接收这些事件来实时渲染 UI。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import json


class EventType(str, Enum):
    """事件类型枚举"""
    
    # 生命周期
    START = "start"              # 开始处理
    COMPLETE = "complete"         # 处理完成
    
    # 推理过程
    THOUGHT = "thought"           # AI 思考过程
    DEBUG = "debug"              # 调试信息
    PLAN = "plan"                # 研究执行计划（规划层产出）
    
    # 工具调用
    TOOL_CALL = "tool_call"      # 即将调用工具
    TOOL_RESULT = "tool_result"  # 工具调用结果
    
    # 子 Agent
    SPAWN_AGENT = "spawn_agent"  # 派发子 Agent
    AGENT_DONE = "agent_done"    # 子 Agent 完成
    
    # 最终输出
    FINAL_ANSWER = "final_answer"  # 最终回答
    ANSWER_DELTA = "answer_delta"  # 模型正在生成的可见文本片段（最终答案到达后替换）

    # 对外进度摘要（由可见性层把内部事件归并生成，见 runtime/visibility.py）
    PROGRESS = "progress"
    
    # 异常情况
    ERROR = "error"              # 错误
    WARNING = "warning"          # 警告
    
    # 用户交互
    NEED_INPUT = "need_input"    # 需要用户输入
    ASK_CONFIRMATION = "ask_confirmation"  # 请求确认


@dataclass
class AgentEvent:
    """
    Agent 事件
    
    所有事件都通过 SSE 流发送给前端，
    前端根据 type 字段决定如何渲染。
    """
    type: EventType
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=lambda: __import__("time").time())

    def to_json(self) -> str:
        """序列化为 JSON（用于 SSE）"""
        return json.dumps({
            "type": self.type.value,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }, ensure_ascii=False)

    def to_sse_format(self) -> str:
        """格式化为 SSE 数据行"""
        return f"data: {self.to_json()}\n\n"


# 常用事件工厂方法
def create_thought_event(content: str, step: int = 0) -> AgentEvent:
    """创建思考事件"""
    return AgentEvent(
        type=EventType.THOUGHT,
        content=content,
        metadata={"step": step},
    )


def create_tool_call_event(tool_name: str, arguments: dict) -> AgentEvent:
    """创建工具调用事件"""
    return AgentEvent(
        type=EventType.TOOL_CALL,
        content=f"正在调用工具: {tool_name}",
        metadata={
            "tool_name": tool_name,
            "arguments": arguments,
        },
    )


def create_tool_result_event(tool_name: str, result: Any, success: bool = True) -> AgentEvent:
    """创建工具结果事件"""
    return AgentEvent(
        type=EventType.TOOL_RESULT,
        content=str(result)[:2000] if result else "",  # 截断
        metadata={
            "tool_name": tool_name,
            "success": success,
        },
    )


def create_error_event(error: str, reason: str = "unknown") -> AgentEvent:
    """创建错误事件"""
    return AgentEvent(
        type=EventType.ERROR,
        content=error,
        metadata={"reason": reason},
    )


def create_final_answer_event(answer: str, **kwargs) -> AgentEvent:
    """创建最终回答事件"""
    return AgentEvent(
        type=EventType.FINAL_ANSWER,
        content=answer,
        metadata=kwargs,
    )
