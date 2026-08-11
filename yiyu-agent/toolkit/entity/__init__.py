"""实体识别工具集 —— 把用户输入（代码 / 名称 / 别名）解析为唯一标的实体。"""

from .resolver import Entity, EntityResolution, resolve_entity
from .tools import EntityResolveTool, ENTITY_TOOLS

__all__ = [
    "Entity",
    "EntityResolution",
    "resolve_entity",
    "EntityResolveTool",
    "ENTITY_TOOLS",
]
