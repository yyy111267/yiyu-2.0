"""
Agent State - 显式状态管理

定义 Agent 的生命周期状态和转换规则。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class AgentPhase(str, Enum):
    """Agent 阶段枚举"""
    
    IDLE = "idle"                       # 空闲
    ORIENTING = "orienting"             # 研究定向
    REASONING = "reasoning"             # 推理中
    CALLING_LLM = "calling_llm"         # 调用模型
    EXECUTING_TOOL = "executing_tool"   # 执行工具
    SPAWNING_AGENT = "spawning_agent"   # 派发子 Agent
    VALIDATING = "validating"           # 硬规则校验
    RESPONDING = "responding"           # 生成回复
    WAITING_USER = "waiting_user"       # 等待用户输入
    ERROR = "error"                     # 错误状态


# 合法的状态转换
VALID_TRANSITIONS: dict[AgentPhase, list[AgentPhase]] = {
    AgentPhase.IDLE: [AgentPhase.ORIENTING, AgentPhase.REASONING],
    AgentPhase.ORIENTING: [AgentPhase.REASONING, AgentPhase.WAITING_USER],
    AgentPhase.REASONING: [AgentPhase.CALLING_LLM, AgentPhase.EXECUTING_TOOL, AgentPhase.SPAWNING_AGENT],
    AgentPhase.CALLING_LLM: [AgentPhase.REASONING, AgentPhase.EXECUTING_TOOL, AgentPhase.SPAWNING_AGENT, AgentPhase.VALIDATING, AgentPhase.RESPONDING, AgentPhase.WAITING_USER, AgentPhase.ERROR],
    AgentPhase.EXECUTING_TOOL: [AgentPhase.REASONING, AgentPhase.ERROR],
    AgentPhase.SPAWNING_AGENT: [AgentPhase.REASONING, AgentPhase.ERROR],
    AgentPhase.VALIDATING: [AgentPhase.RESPONDING, AgentPhase.REASONING, AgentPhase.ERROR],
    AgentPhase.RESPONDING: [AgentPhase.IDLE, AgentPhase.WAITING_USER],
    AgentPhase.WAITING_USER: [AgentPhase.REASONING, AgentPhase.IDLE],
    AgentPhase.ERROR: [AgentPhase.REASONING, AgentPhase.IDLE],
}


@dataclass
class Observation:
    """观察结果（工具调用或子 Agent 的输出）"""
    source: str  # 来源：tool_name 或 agent_name
    content: Any  # 结果内容
    timestamp: datetime = field(default_factory=datetime.now)
    success: bool = True
    error: Optional[str] = None


@dataclass
class AgentState:
    """
    Agent 运行时状态
    
    这个对象会在每轮循环中被传递和更新，
    最终持久化到 store/repos/session_repo.py 以支持中断恢复。
    """
    
    # 基础信息
    session_id: str
    user_message: str
    created_at: datetime = field(default_factory=datetime.now)
    
    # 状态机
    phase: AgentPhase = AgentPhase.IDLE
    finished: bool = False
    
    # 循环计数
    step_count: int = 0
    tokens_used: int = 0
    tool_call_count: int = 0
    
    # 上下文
    context: dict[str, Any] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    
    # Skill 相关
    pinned_skill: Optional[str] = None  # 用户指定的 skill
    active_skill: Optional[str] = None  # 当前激活的 skill
    available_tools: list[str] = field(default_factory=list)  # 当前可用工具列表
    
    # 用户信息（从 store/profile.py 加载）
    user_id: Optional[str] = None
    persona: dict = field(default_factory=dict)  # 用户画像
    methodology: list[dict] = field(default_factory=list)  # 个人方法库
    
    # 中断恢复相关
    checkpoint_data: dict = field(default_factory=dict)  # 断点数据

    def transition_to(self, new_phase: AgentPhase) -> bool:
        """
        尝试状态转换
        
        Returns:
            bool: 是否允许转换
        """
        allowed = VALID_TRANSITIONS.get(self.phase, [])
        
        if new_phase not in allowed:
            logger.warning(
                f"非法状态转换: {self.phase.value} -> {new_phase.value}"
            )
            return False
        
        old_phase = self.phase
        self.phase = new_phase
        
        logger.debug(
            f"状态转换: {old_phase.value} -> {new_phase.value}"
        )
        return True

    def add_observation(self, observation: Observation):
        """添加观察结果"""
        self.observations.append(observation)

    def to_checkpoint(self) -> dict:
        """序列化为检查点（用于中断恢复）"""
        return {
            "session_id": self.session_id,
            "phase": self.phase.value,
            "step_count": self.step_count,
            "tokens_used": self.tokens_used,
            "tool_call_count": self.tool_call_count,
            "context": self.context,
            "observations": [
                {
                    "source": obs.source,
                    "content": obs.content,
                    "timestamp": obs.timestamp.isoformat(),
                    "success": obs.success,
                    "error": obs.error,
                }
                for obs in self.observations[-10:]  # 只保留最近 10 条
            ],
            "pinned_skill": self.pinned_skill,
            "active_skill": self.active_skill,
            "user_id": self.user_id,
            "finished": self.finished,
            "checkpointed_at": datetime.now().isoformat(),
        }

    @classmethod
    def from_checkpoint(cls, data: dict) -> "AgentState":
        """从检查点恢复状态"""
        state = cls(
            session_id=data["session_id"],
            user_message="",  # 恢复时不保留原始消息
            phase=AgentPhase(data["phase"]),
            step_count=data["step_count"],
            tokens_used=data.get("tokens_used", 0),
            tool_call_count=data.get("tool_call_count", 0),
            context=data.get("context", {}),
            pinned_skill=data.get("pinned_skill"),
            active_skill=data.get("active_skill"),
            user_id=data.get("user_id"),
            finished=data.get("finished", False),
        )
        
        # 恢复观察结果
        for obs_data in data.get("observations", []):
            from datetime import datetime as dt
            obs = Observation(
                source=obs_data["source"],
                content=obs_data["content"],
                success=obs_data.get("success", True),
                error=obs_data.get("error"),
                timestamp=dt.fromisoformat(obs_data["timestamp"]),
            )
            state.observations.append(obs)
        
        return state


# 导入 logger（避免循环依赖）
import logging
logger = logging.getLogger(__name__)
