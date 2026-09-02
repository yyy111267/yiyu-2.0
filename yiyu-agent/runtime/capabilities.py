"""可组合能力包加载。

工作流 Skill 在 frontmatter 中声明 ``capabilities``；运行时按
``capability.yaml`` 的激活条件加载能力说明。能力包不直接路由用户，
也不启动第二个 Agent；它为父工作流补充决策规则和工具合同。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


_CAPABILITY_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def _frontmatter(markdown: str) -> dict[str, Any]:
    """读取 Markdown 顶部 YAML frontmatter；无或非对象时返回空字典。"""
    if not markdown.startswith("---\n"):
        return {}
    end = markdown.find("\n---\n", 4)
    if end < 0:
        return {}
    data = yaml.safe_load(markdown[4:end]) or {}
    return data if isinstance(data, dict) else {}


def declared_capabilities(skill_markdown: str) -> list[str]:
    """返回父 Skill 声明的能力包，保序去重。"""
    raw = _frontmatter(skill_markdown).get("capabilities") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(item) for item in raw if item))


def load_capability_manifest(
    project_root: Path, capability_id: str, *, phase: int,
) -> tuple[Path, dict[str, Any]] | None:
    """若能力包存在且已到激活阶段，返回目录和清单。"""
    if not _CAPABILITY_ID.fullmatch(capability_id):
        return None
    capability_dir = project_root / "skills" / capability_id
    manifest_path = capability_dir / "capability.yaml"
    if not manifest_path.is_file():
        return None

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(manifest, dict) or manifest.get("kind") != "capability":
        return None
    min_phase = int((manifest.get("activation") or {}).get("min_skill_phase", 0))
    if phase < min_phase:
        return None
    return capability_dir, manifest


def load_capability(
    project_root: Path, capability_id: str, *, phase: int, prompt_stage: str = "execution",
) -> str | None:
    """若能力包已激活，返回其紧凑运行时入口。"""
    loaded = load_capability_manifest(project_root, capability_id, phase=phase)
    if not loaded:
        return None
    capability_dir, manifest = loaded

    entrypoints = manifest.get("entrypoints") or {}
    if isinstance(entrypoints, dict):
        entrypoint = entrypoints.get(prompt_stage)
    else:
        entrypoint = None
    entrypoint = str(entrypoint or manifest.get("entrypoint") or "")
    if not entrypoint:
        return None
    entry_path = (capability_dir / entrypoint).resolve()
    if capability_dir.resolve() not in entry_path.parents or not entry_path.is_file():
        return None
    return entry_path.read_text(encoding="utf-8")


def load_declared_capabilities(
    project_root: Path, skill_markdown: str, *, phase: int,
    prompt_stage: str = "execution",
) -> list[tuple[str, str]]:
    """加载当前阶段已激活的 ``(id, instructions)``。"""
    loaded = []
    for capability_id in declared_capabilities(skill_markdown):
        instructions = load_capability(
            project_root, capability_id, phase=phase, prompt_stage=prompt_stage,
        )
        if instructions:
            loaded.append((capability_id, instructions))
    return loaded


def capability_ids_for_skill(
    project_root: Path,
    skill_id: str,
    skill_markdown: str,
    *,
    phase: int,
    business_group: Any = "",
    allow_dynamic: bool = True,
) -> list[str]:
    """组合父 Skill 显式依赖与分类结果动态选中的能力包。"""
    selected = declared_capabilities(skill_markdown)
    selected_groups = {
        str(group) for group in (
            [business_group] if isinstance(business_group, str) else business_group or []
        ) if group
    }
    manifest_paths = sorted((project_root / "skills").glob("*/capability.yaml")) \
        if allow_dynamic else []
    for manifest_path in manifest_paths:
        capability_id = manifest_path.parent.name
        loaded = load_capability_manifest(project_root, capability_id, phase=phase)
        if not loaded:
            continue
        _, manifest = loaded
        activation = manifest.get("activation") or {}
        workflows = activation.get("workflows") or []
        groups = activation.get("business_groups") or []
        if skill_id in workflows and selected_groups.intersection(str(group) for group in groups):
            selected.append(capability_id)

    resolved: list[str] = []
    visiting: set[str] = set()

    def add_with_dependencies(capability_id: str) -> None:
        if capability_id in resolved or capability_id in visiting:
            return
        loaded = load_capability_manifest(project_root, capability_id, phase=phase)
        if not loaded:
            return
        visiting.add(capability_id)
        _, manifest = loaded
        for dependency in manifest.get("depends_on") or []:
            add_with_dependencies(str(dependency))
        visiting.remove(capability_id)
        resolved.append(capability_id)

    for capability_id in selected:
        add_with_dependencies(capability_id)
    return resolved


def load_capabilities_for_skill(
    project_root: Path,
    skill_id: str,
    skill_markdown: str,
    *,
    phase: int,
    business_group: Any = "",
    prompt_stage: str = "execution",
    allow_dynamic: bool = True,
) -> list[tuple[str, str]]:
    """加载父 Skill 当前阶段和业务分类选中的能力说明。"""
    loaded = []
    for capability_id in capability_ids_for_skill(
        project_root, skill_id, skill_markdown,
        phase=phase, business_group=business_group, allow_dynamic=allow_dynamic,
    ):
        instructions = load_capability(
            project_root, capability_id, phase=phase, prompt_stage=prompt_stage,
        )
        if instructions:
            loaded.append((capability_id, instructions))
    return loaded


def capability_tools_for_skill(
    project_root: Path, skill_id: str, *, phase: int, business_group: Any = "",
    allow_dynamic: bool = True,
) -> set[str]:
    """返回父 Skill 当前阶段已激活能力包声明的工具。"""
    skill_path = project_root / "skills" / skill_id / "SKILL.md"
    if not skill_path.is_file():
        return set()
    tools: set[str] = set()
    skill_markdown = skill_path.read_text(encoding="utf-8")
    for capability_id in capability_ids_for_skill(
        project_root, skill_id, skill_markdown,
        phase=phase, business_group=business_group, allow_dynamic=allow_dynamic,
    ):
        loaded = load_capability_manifest(project_root, capability_id, phase=phase)
        if not loaded:
            continue
        _, manifest = loaded
        raw_tools = manifest.get("tools") or []
        if isinstance(raw_tools, list):
            tools.update(str(tool) for tool in raw_tools if tool)
    return tools
