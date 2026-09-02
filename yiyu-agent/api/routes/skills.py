"""
Skills 清单端点 - 返回可用技能列表供前端渲染按钮
"""

from fastapi import APIRouter, HTTPException
import json
from pathlib import Path

from runtime.skill_availability import is_skill_available

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

    manifest["skills"] = [
        dict(s, available=is_skill_available(s.get("id")))
        for s in manifest.get("skills", [])
    ]
    
    return manifest


@router.get("/skills/{skill_name}")
async def get_skill_detail(skill_name: str):
    """获取单个技能的详细信息"""
    if not MANIFEST_PATH.exists():
        raise HTTPException(status_code=404, detail="skill_not_found")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    item = next((s for s in manifest.get("skills", []) if s.get("id") == skill_name), None)
    if item is None:
        raise HTTPException(status_code=404, detail="skill_not_found")

    # skill_name 只从 manifest 的精确 id 命中，不直接拼接用户输入。
    skill_file = MANIFEST_PATH.parent / skill_name / "SKILL.md"
    detail = skill_file.read_text(encoding="utf-8") if skill_file.is_file() else ""
    return {
        **item,
        "available": is_skill_available(skill_name),
        "detail": detail,
    }
