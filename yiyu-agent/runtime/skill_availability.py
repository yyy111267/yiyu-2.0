"""Skill 可用性检查。

manifest 里会提前注册后续版本能力；真正进入 AgentLoop 前必须确认该 skill
已有可执行说明，避免半成品能力被路由后产生承诺。
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SKILLS_DIR = _ROOT / "skills"

# light answer 不依赖 SKILL.md；SOTP 是 deep-research 的补充框架。
ALWAYS_AVAILABLE = {"knowledge_qa", "sotp-multi-business"}


def is_skill_available(skill_id: str | None) -> bool:
    if not skill_id:
        return True
    if skill_id in ALWAYS_AVAILABLE:
        return True
    return (_SKILLS_DIR / skill_id / "SKILL.md").exists()


def available_skill_ids() -> set[str]:
    ids = {p.parent.name for p in _SKILLS_DIR.glob("*/SKILL.md")}
    return ids | ALWAYS_AVAILABLE

