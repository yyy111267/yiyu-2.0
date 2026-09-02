"""
工具注册入口 —— 应用启动时调用 register_all() 把所有 Tool 装入 TOOL_REGISTRY。

集中注册点，避免散落各处。新增工具集只需在此追加 import + extend。
"""

from typing import Optional

from .base import Tool
from .registry import (
    TOOL_REGISTRY,
    SKILL_TOOLS,
    get_all_tools,
    get_tool,
    get_tool_schemas_for_llm,
    get_tools_for_skill,
    list_registered_tools,
    register_tool,
)


def register_all() -> None:
    """注册所有内置工具。幂等：已注册的会跳过。"""
    # 1. delivery（硬规则安全门）—— 最高优先级
    from .delivery.submit_conclusion import SubmitConclusionTool
    _safe_register(SubmitConclusionTool())

    # 1b. delivery.finish（显式终止：信息充分性闸门）
    from .delivery.finish import FinishTool
    _safe_register(FinishTool())

    # 2. market（行情数据）
    from .market.tools import MARKET_TOOLS
    for t in MARKET_TOOLS:
        _safe_register(t)

    # 3. calc（确定性计算）
    from .calc.valuation import CALC_TOOLS
    for t in CALC_TOOLS:
        _safe_register(t)

    # 3b. calc.metric + calc.menu（agent 主导范式：LLM 判断该看什么→按需单点算）
    from .calc.metric import METRIC_TOOLS
    for t in METRIC_TOOLS:
        _safe_register(t)
    from .calc.menu import MENU_TOOLS
    for t in MENU_TOOLS:
        _safe_register(t)

    # 3c. calc.run_code（断网计算沙箱：标准指标之外的非标指标）
    from .calc.run_code import RUN_CODE_TOOLS
    for t in RUN_CODE_TOOLS:
        _safe_register(t)

    # 4. web（搜索/抓取）
    from .web.tools import WEB_TOOLS
    for t in WEB_TOOLS:
        _safe_register(t)

    # 5. entity（实体识别）
    from .entity.tools import ENTITY_TOOLS
    for t in ENTITY_TOOLS:
        _safe_register(t)

    # 6. cognition（认知陪练 RAG：双路检索 + 认知原子抽取）
    from .cognition.tools import COGNITION_TOOLS
    for t in COGNITION_TOOLS:
        _safe_register(t)


def _safe_register(tool: Tool) -> None:
    """幂等注册：已存在则跳过，不抛错。"""
    if tool.schema.name not in TOOL_REGISTRY:
        register_tool(tool)


__all__ = [
    "register_all",
    "TOOL_REGISTRY",
    "SKILL_TOOLS",
    "get_all_tools",
    "get_tool",
    "get_tool_schemas_for_llm",
    "get_tools_for_skill",
    "list_registered_tools",
    "register_tool",
]
