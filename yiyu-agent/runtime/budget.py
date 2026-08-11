"""
预算管理器 - 控制 Token/轮次/工具调用消耗

防止 Agent 无限循环或消耗过多资源。
"""

from dataclasses import dataclass


@dataclass
class BudgetConfig:
    """预算配置"""
    max_tokens: int = 50000      # 最大 token 数
    max_tool_calls: int = 30     # 最大工具调用次数
    max_steps: int = 20          # 最大步数
    warning_threshold: float = 0.8  # 警告阈值（80% 时警告）


@dataclass
class BudgetUsage:
    """当前使用量"""
    tokens_used: int = 0
    tool_calls: int = 0
    steps: int = 0


class BudgetManager:
    """
    预算管理器
    
    在每轮循环开始前检查是否超限，
    并在接近上限时发出警告。
    """

    def __init__(self, config: BudgetConfig = None):
        self.config = config or BudgetConfig()
        self.usage = BudgetUsage()

    def record_token_usage(self, tokens: int):
        """记录 token 消耗"""
        self.usage.tokens_used += tokens

    def record_tool_call(self):
        """记录一次工具调用"""
        self.usage.tool_calls += 1

    def is_exceeded(self, state=None) -> bool:
        """
        检查是否超出预算
        
        Args:
            state: AgentState（可选，用于获取实时数据）
            
        Returns:
            bool: 是否超限
        """
        # 使用传入的 state 或内部记录
        steps = state.step_count if state else self.usage.steps
        tokens = state.tokens_used if state else self.usage.tokens_used
        tool_calls = state.tool_call_count if state else self.usage.tool_calls
        
        return (
            steps >= self.config.max_steps
            or tokens >= self.config.max_tokens
            or tool_calls >= self.config.max_tool_calls
        )

    def get_warning(self, state=None) -> str | None:
        """
        获取警告信息（如果在阈值附近）
        
        Returns:
            str | None: 警告消息或 None
        """
        tokens = state.tokens_used if state else self.usage.tokens_used
        tool_calls = state.tool_call_count if state else self.usage.tool_calls
        steps = state.step_count if state else self.usage.steps
        
        warnings = []
        
        if tokens >= self.config.max_tokens * self.config.warning_threshold:
            pct = (tokens / self.config.max_tokens) * 100
            warnings.append(f"Token 使用已达 {pct:.0f}%")
        
        if tool_calls >= self.config.max_tool_calls * self.config.warning_threshold:
            pct = (tool_calls / self.config.max_tool_calls) * 100
            warnings.append(f"工具调用已达 {pct:.0f}%")
        
        if steps >= self.config.max_steps * self.config.warning_threshold:
            pct = (steps / self.config.max_steps) * 100
            warnings.append(f"步数已达 {pct:.0f}%")
        
        return "；".join(warnings) if warnings else None

    def reset(self):
        """重置预算"""
        self.usage = BudgetUsage()

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.config.max_tokens - self.usage.tokens_used)

    @property
    def remaining_tool_calls(self) -> int:
        return max(0, self.config.max_tool_calls - self.usage.tool_calls)

    @property
    def remaining_steps(self) -> int:
        return max(0, self.config.max_steps - self.usage.steps)

    def to_dict(self) -> dict:
        """序列化为字典（用于监控）"""
        return {
            "usage": {
                "tokens": self.usage.tokens_used,
                "tool_calls": self.usage.tool_calls,
                "steps": self.usage.steps,
            },
            "limits": {
                "max_tokens": self.config.max_tokens,
                "max_tool_calls": self.config.max_tool_calls,
                "max_steps": self.config.max_steps,
            },
            "remaining": {
                "tokens": self.remaining_tokens,
                "tool_calls": self.remaining_tool_calls,
                "steps": self.remaining_steps,
            },
        }
