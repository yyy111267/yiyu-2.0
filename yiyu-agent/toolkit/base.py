"""
Tool 基类 - 定义所有工具的契约

每个工具都必须继承这个基类，并实现 execute() 方法。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional, Type


@dataclass
class ToolSchema:
    """工具描述（给 LLM 看的 JSON Schema）"""
    name: str
    description: str
    parameters: dict  # JSON Schema 格式的参数定义
    
    # 元数据
    read_only: bool = True  # 只读工具免权限检查
    max_chars: int = 5000   # 输出截断长度
    timeout_seconds: int = 30  # 超时时间
    max_concurrent: int = 3  # 最大并发数


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    data: Any
    error: Optional[str] = None
    truncated: bool = False


class Tool(ABC):
    """
    工具基类
    
    所有工具必须：
    1. 定义 schema 类属性（描述工具能力）
    2. 实现 execute() 方法（执行具体逻辑）
    3. 遵循权限约定（读/写分离）
    
    示例：
        class MarketDataTool(Tool):
            schema = ToolSchema(
                name="market.get_bundle",
                description="获取标的行情数据包",
                parameters={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "股票代码，如 600519"
                        }
                    },
                    "required": ["symbol"]
                },
                read_only=True,
            )
            
            async def execute(self, symbol: str) -> dict:
                return await self.market_data.bundle(symbol)
    """
    
    schema: ToolSchema

    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        """
        执行工具逻辑
        
        Args:
            **kwargs: 从 schema.parameters 解析出的参数
            
        Returns:
            Any: 工具执行结果（会被自动序列化）
            
        Raises:
            ValueError: 参数校验失败
            RuntimeError: 执行失败
        """
        pass

    def validate_args(self, args: dict) -> bool:
        """简单的参数校验（MVP 版本）"""
        required = self.schema.parameters.get("required", [])
        for param in required:
            if param not in args:
                return False
        return True

    def truncate_result(self, result: Any) -> tuple[Any, bool]:
        """截断过长的结果"""
        result_str = str(result)
        if len(result_str) > self.schema.max_chars:
            return result_str[:self.schema.max_chars] + "\n...(已截断)", True
        return result, False


# 特殊工具标记
class WriteTool(Tool):
    """写操作工具基类（需要额外的权限检查）"""
    schema = ToolSchema(
        name="base.write",
        description="写操作工具基类",
        parameters={},
        read_only=False,  # 标记为非只读
    )


class ReadOnlyTool(Tool):
    """只读工具基类"""
    schema = ToolSchema(
        name="base.read_only",
        description="只读工具基类",
        parameters={},
        read_only=True,
    )
