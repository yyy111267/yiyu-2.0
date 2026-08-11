"""
权限管理 - 读/写分离的安全机制

实现简单但有效的权限控制：
- 读操作：自动放行
- 写操作：需要用户确认或预授权
"""

from dataclasses import dataclass
from enum import Enum


class PermissionLevel(str, Enum):
    """权限级别"""
    ALLOW = "allow"       # 允许
    DENY = "deny"         # 拒绝
    CONFIRM = "confirm"   # 需要用户确认


@dataclass
class PermissionResult:
    """权限检查结果"""
    allowed: bool
    reason: str = ""
    level: PermissionLevel = PermissionLevel.ALLOW


class PermissionChecker:
    """
    权限检查器
    
    规则：
    1. 读操作（read_only=True）：直接允许
    2. 写操作：根据配置决定是否需要确认
    3. 危险操作（如提交结论）：强制硬规则校验
    """

    # 需要特殊权限的操作
    SENSITIVE_OPERATIONS = {
        "delivery.submit_conclusion",  # 提交结论（走硬规则）
        "methodology.save",           # 保存方法论（需确认）
        "holdings.delete",           # 删除持仓（危险操作）
    }

    async def check_write_permission(self, tool_call) -> PermissionResult:
        """
        检查写操作权限
        
        Args:
            tool_call: 工具调用请求
            
        Returns:
            PermissionResult: 权限检查结果
        """
        operation = tool_call.name
        
        # 敏感操作需要额外检查
        if operation in self.SENSITIVE_OPERATIONS:
            if operation == "delivery.submit_conclusion":
                # 这个会在 delivery 层做硬规则校验
                return PermissionResult(
                    allowed=True,
                    level=PermissionLevel.CONFIRM,
                    reason="将通过硬规则校验",
                )
            
            elif operation == "methodology.save":
                return PermissionResult(
                    allowed=False,
                    level=PermissionLevel.CONFIRM,
                    reason="保存方法论需要用户明确确认",
                )
            
            elif operation == "holdings.delete":
                return PermissionResult(
                    allowed=False,
                    level=PermissionLevel.DENY,
                    reason="删除操作被禁止",
                )

        # 普通写操作：MVP 版本暂时允许
        # TODO: 后续可以加入更细粒度的权限控制
        return PermissionResult(
            allowed=True,
            level=PermissionLevel.ALLOW,
        )

    async def confirm_operation(self, operation: str, user_response: bool) -> bool:
        """
        处理用户确认响应
        
        Args:
            operation: 操作名称
            user_response: 用户是否同意
            
        Returns:
            bool: 是否允许执行
        """
        return user_response
