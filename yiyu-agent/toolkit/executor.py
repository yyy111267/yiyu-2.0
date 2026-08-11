"""
工具执行器 - 并行执行、校验、截断、错误处理

负责实际执行工具调用，处理各种异常情况。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from .base import Tool, ToolResult
from .permission import PermissionChecker

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """工具调用请求"""
    name: str
    arguments: dict
    call_id: str  # 用于追踪和去重


class ToolExecutor:
    """
    工具执行器
    
    职责：
    1. 权限检查（写操作需要确认）
    2. 参数校验
    3. 并发执行控制
    4. 结果截断
    5. 错误处理和回灌
    """

    def __init__(
        self,
        permission_checker: PermissionChecker = None,
        max_concurrent: int = 3,
    ):
        self.permission = permission_checker or PermissionChecker()
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """
        执行单个工具调用
        
        Args:
            tool_call: 工具调用请求
            
        Returns:
            ToolResult: 执行结果
        """
        from .registry import get_tool, resolve_tool_name
        
        logger.info(f"执行工具: {tool_call.name} (id={tool_call.call_id})")
        
        # 1. 查找工具（LLM 可见名为下划线版，还原为注册名再查）
        tool = get_tool(resolve_tool_name(tool_call.name))
        if not tool:
            return ToolResult(
                success=False,
                data=None,
                error=f"未知工具: {tool_call.name}",
            )

        # 2. 权限检查（写操作）
        if not tool.schema.read_only:
            perm_result = await self.permission.check_write_permission(tool_call)
            if not perm_result.allowed:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"权限拒绝: {perm_result.reason}",
                )

        # 3. 参数校验
        if not tool.validate_args(tool_call.arguments):
            return ToolResult(
                success=False,
                data=None,
                error=f"参数校验失败: 缺少必要参数",
            )

        # 4. 并发控制 + 执行
        async with self._semaphore:
            try:
                # 带超时执行
                result = await asyncio.wait_for(
                    tool.execute(**tool_call.arguments),
                    timeout=tool.schema.timeout_seconds,
                )
                
                # 5. 截断过长结果
                final_result, truncated = tool.truncate_result(result)
                
                return ToolResult(
                    success=True,
                    data=final_result,
                    truncated=truncated,
                )

            except asyncio.TimeoutError:
                logger.error(f"工具超时: {tool_call.name}")
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"工具执行超时 ({tool.schema.timeout_seconds}s)",
                )

            except Exception as e:
                logger.exception(f"工具执行异常: {tool_call.name}")
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"执行错误: {str(e)}",
                )

    async def execute_batch(
        self,
        tool_calls: list[ToolCall],
    ) -> list[ToolResult]:
        """
        并行执行多个工具调用
        
        Args:
            tool_calls: 工具调用列表
            
        Returns:
            结果列表（顺序与输入一致）
        """
        tasks = [self.execute(tc) for tc in tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed_results.append(ToolResult(
                    success=False,
                    data=None,
                    error=str(result),
                ))
            else:
                processed_results.append(result)
        
        return processed_results
