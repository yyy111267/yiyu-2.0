"""
工具注册表 - 管理所有可用工具

提供工具注册、查询、按 Skill 裁剪等能力。
"""

from typing import Optional

from .base import Tool, ToolSchema

# 全局注册表
TOOL_REGISTRY: dict[str, Tool] = {}

# Skill → Tools 映射（定义每个 Skill 可以使用哪些工具）
SKILL_TOOLS: dict[str, list[str]] = {
    "deep-research": [
        "entity.resolve",
        "company.classify",
        "market.get_bundle",
        "calc.base_pack",
        "calc.run_code",
        "cognition.recall",
        "cognition.extract",
        "web.search",
        "web.fetch",
        "delivery.finish",
    ],
    "quick-screen": [
        "market.get_snapshot",
        "screen.basic_filter",
    ],
    "private-company": [
        "web.search",
        "web.fetch",
        "calc.cash_runway",
        "delivery.finish",
    ],
    "holdings-track": [
        "holdings.get_list",
        "holdings.update_note",
    ],
    "trade-review": [
        "trade.get_history",
        "market.get_bundle",
        "delivery.propose_methodology",
        "delivery.finish",
    ],
}


def register_tool(tool: Tool):
    """
    注册一个工具
    
    Args:
        tool: Tool 实例
        
    Raises:
        ValueError: 如果工具名称重复
    """
    name = tool.schema.name
    
    if name in TOOL_REGISTRY:
        raise ValueError(f"工具 '{name}' 已注册")
    
    TOOL_REGISTRY[name] = tool
    

def get_tool(name: str) -> Optional[Tool]:
    """
    获取工具实例
    
    Args:
        name: 工具名称
        
    Returns:
        Tool 实例或 None
    """
    return TOOL_REGISTRY.get(name)


def get_tools_for_skill(skill_name: str) -> list[Tool]:
    """
    获取指定 Skill 可用的工具列表
    
    Args:
        skill_name: Skill 名称
        
    Returns:
        该 Skill 可以使用的工具列表
    """
    tool_names = SKILL_TOOLS.get(skill_name, [])
    tools = []
    
    for name in tool_names:
        tool = TOOL_REGISTRY.get(name)
        if tool:
            tools.append(tool)
    
    return tools


def get_all_tools() -> list[Tool]:
    """获取所有已注册的工具"""
    return list(TOOL_REGISTRY.values())


# LLM 可见名 → 注册名 映射（OpenAI 兼容 API 只允许 ^[a-zA-Z0-9_-]+$，故点号转下划线）
_SANITIZED_TO_ORIGINAL: dict[str, str] = {}


def sanitize_tool_name(name: str) -> str:
    """把工具名转换为 LLM 可见的合法函数名（. → _）。"""
    return name.replace(".", "_")


def resolve_tool_name(sanitized: str) -> str:
    """把 LLM 返回的函数名还原为注册表里的原始工具名。"""
    return _SANITIZED_TO_ORIGINAL.get(sanitized, sanitized)


def get_tool_schemas_for_llm(skill_name: Optional[str] = None) -> list[dict]:
    """
    获取工具的 Schema 列表（用于 LLM function calling）
    
    Args:
        skill_name: 如果指定，只返回该 Skill 的工具
        
    Returns:
        JSON Schema 格式的工具描述列表
    """
    # 未命中任何 Skill（纯闲聊/通用问答）→ 返回空工具集，避免 LLM 误用计算工具。
    # 纯对话由 loop 退化为无 function calling 的普通 chat。
    tools = get_tools_for_skill(skill_name) if skill_name else []

    schemas = []
    for tool in tools:
        original = tool.schema.name
        visible = sanitize_tool_name(original)
        _SANITIZED_TO_ORIGINAL[visible] = original
        schemas.append(
            {
                "name": visible,
                "description": tool.schema.description,
                "parameters": tool.schema.parameters,
            }
        )
    return schemas


def list_registered_tools() -> list[str]:
    """列出所有已注册的工具名称"""
    return list(TOOL_REGISTRY.keys())
