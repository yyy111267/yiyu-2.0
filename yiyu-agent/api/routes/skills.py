"""
Skills 清单端点 - 返回可用技能列表供前端渲染按钮
"""

from fastapi import APIRouter
import json
from pathlib import Path

router = APIRouter()

# 技能清单文件路径
MANIFEST_PATH = Path(__file__).parent.parent.parent / "skills" / "manifest.json"


@router.get("/skills")
async def list_skills():
    """
    获取可用技能清单
    
    前端根据此接口渲染模块按钮。
    每个 skill 包含名称、描述、图标等信息。
    """
    if not MANIFEST_PATH.exists():
        return {"skills": []}
    
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    
    return manifest


@router.get("/skills/{skill_name}")
async def get_skill_detail(skill_name: str):
    """获取单个技能的详细信息"""
    # TODO: 加载 SKILL.md 文件并解析
    return {"name": skill_name, "detail": "TODO"}
