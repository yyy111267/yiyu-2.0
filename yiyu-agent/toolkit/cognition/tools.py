"""认知陪练工具 —— 双路检索 + 认知正文展开 + 候选卡生成。

cognition.recall  （只读）双路检索：默认投资框架 vs 用户个人认知 → 一致/冲突/盲区
cognition.extract （只读）从研究结论生成候选确认卡，不写库

设计：
- 默认框架库内置、版本化、不可被用户数据覆盖；
- 用户认知严格按 user_id 租户隔离；
- 混合检索（向量 + 关键词）。
"""

from __future__ import annotations

import logging
from typing import Any

from toolkit.base import ReadOnlyTool, ToolSchema

logger = logging.getLogger(__name__)

# 复用单例（SQLite 连接 + 向量索引懒加载）
_store = None


def _get_store():
    global _store
    if _store is None:
        from store.cognition_store import CognitionStore
        # 从 config 读 database_url，同步引擎需去掉 +aiosqlite
        try:
            from core.config import get_config
            db_url = get_config().database_url.replace("+aiosqlite", "")
        except Exception:
            db_url = "sqlite:///./invest_coach.db"
        _store = CognitionStore(db_url)
    return _store


class CognitionRecallTool(ReadOnlyTool):
    """双路认知检索：默认投资框架 + 用户个人认知 → 差异分析。"""

    schema = ToolSchema(
        name="cognition.recall",
        description=(
            "认知陪练双路检索：同时检索「默认投资框架」和「用户个人认知库」，"
            "输出一致点、冲突点和用户潜在盲区，供组织双路回答。\n"
            "用法：研究开始时用研究主题/标的特征作为 query 检索一次，"
            "收尾时按返回的一致/冲突/盲区组织「默认框架怎么看 vs 你的个人框架怎么看」。\n"
            "user_id 取当前会话用户（由上层注入或从对话上下文获取）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索意图，如「茅台护城河」「新能源估值」「管理层资本配置」",
                },
                "user_id": {
                    "type": "string",
                    "description": "当前会话用户 ID（租户隔离用，取对话上下文中的用户）",
                },
                "top_k": {
                    "type": "integer",
                    "description": "每路检索返回条数，默认 5",
                    "default": 5,
                },
            },
            "required": ["query", "user_id"],
        },
        read_only=True,
        max_chars=6000,
    )

    async def execute(self, query: str, user_id: str,
                      top_k: int = 5, **kwargs: Any) -> dict:
        if not query or not user_id:
            return {"success": False, "error": "query 和 user_id 必填"}
        store = _get_store()
        result = store.recall(query, user_id=user_id, top_k=top_k)
        # 渲染为 LLM 友好的文本块
        text = _render_recall(result)
        return {
            "success": True,
            "query": query,
            "default_count": len(result["default_framework"]),
            "user_count": len(result["user_cognition"]),
            "agreement_count": len(result["agreement"]),
            "conflict_count": len(result["conflict"]),
            "blind_spot_count": len(result["blind_spots"]),
            "recall_block": text,
        }


class CognitionGetTool(ReadOnlyTool):
    """按 ID 展开一条 active 认知的完整正文。"""

    schema = ToolSchema(
        name="cognition.get",
        description="按认知 id 展开完整正文。只可读取当前用户的 active/confirmed 条目。",
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "认知目录中的 id"},
                "user_id": {"type": "string", "description": "当前会话用户 ID"},
            },
            "required": ["id", "user_id"],
        },
        read_only=True,
    )

    async def execute(self, id: str, user_id: str, **kwargs: Any) -> dict:
        item = _get_store().repo.get_atom(id, user_id=user_id)
        if not item or item.get("status") not in ("active", "confirmed"):
            return {"success": False, "error": "认知不存在、未生效或无权访问"}
        return {"success": True, "item": item}


class CognitionExtractTool(ReadOnlyTool):
    """从研究结论生成候选确认卡，不产生任何持久化写入。"""

    schema = ToolSchema(
        name="cognition.extract",
        description=(
            "从研究结论文本中生成候选认知确认卡，不会写入用户认知库。\n"
            "候选须由用户在界面中确认或编辑后确认，才会成为 active 记忆。\n"
            "user_id 取当前会话用户。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "research_summary": {
                    "type": "string",
                    "description": "研究结论文本（含用户观点/判断的段落）",
                },
                "user_id": {
                    "type": "string",
                    "description": "当前会话用户 ID",
                },
                "symbol": {
                    "type": "string",
                    "description": "标的代码（标注认知来源）",
                    "default": "",
                },
            },
            "required": ["research_summary", "user_id"],
        },
        read_only=True,
        max_chars=4000,
    )

    async def execute(self, research_summary: str, user_id: str,
                      symbol: str = "", **kwargs: Any) -> dict:
        if not research_summary or not user_id:
            return {"success": False, "error": "research_summary 和 user_id 必填"}
        store = _get_store()
        atoms = store.extract_atoms(research_summary, user_id=user_id, symbol=symbol)
        if not atoms:
            return {"success": True, "extracted": 0, "cards": [],
                    "note": "未从文本中识别到候选认知原子"}
        return {
            "success": True,
            "extracted": len(atoms),
            "cards": [{"candidate": a} for a in atoms],
            "note": "候选仅供确认，不会自动写入认知库",
        }


def _render_recall(result: dict) -> str:
    """把双路检索结果渲染为 LLM 友好的文本块。"""
    lines = ["## 认知双路检索结果", f"检索意图：{result['query']}", ""]

    lines.append("### 默认投资框架（相关条目）")
    for item in result["default_framework"]:
        lines.append(f"- [{item.get('category', '')}] {item['statement']}")
        if item.get("counter_example"):
            lines.append(f"  反例：{item['counter_example']}")
    if not result["default_framework"]:
        lines.append("-（无命中）")

    lines.append("\n### 你的个人认知库（相关条目）")
    for item in result["user_cognition"]:
        rev = " [已修正]" if item.get("is_revised") else ""
        lines.append(f"- [{item.get('category', '')}] {item['statement']}{rev}")
        if item.get("counter_evidence"):
            lines.append(f"  反证：{item['counter_evidence']}")
    if not result["user_cognition"]:
        lines.append("-（暂无个人认知记录）")

    if result["agreement"]:
        lines.append("\n### 一致点（默认框架与你的认知趋同）")
        for a in result["agreement"]:
            lines.append(f"- {a.get('user_view', '')}")
            if a.get("framework_view"):
                lines.append(f"  ← 框架：{a['framework_view']}")

    if result["conflict"]:
        lines.append("\n### 冲突点（你的认知与默认框架存在张力）")
        for c in result["conflict"]:
            lines.append(f"- 你：{c.get('user_view', '')}")
            lines.append(f"  框架：{c.get('framework_view', '')}")
            lines.append(f"  提示：{c.get('note', '')}")

    if result["blind_spots"]:
        lines.append("\n### 潜在盲区（默认框架强调但你尚未体现的认知）")
        for b in result["blind_spots"]:
            lines.append(f"- [{b.get('category', '')}] {b.get('framework_view', '')}")

    lines.append("\n⚠️ 组织最终回答时，请按「默认框架怎么看 / 你的个人框架怎么看 / "
                 "一致点 / 冲突点 / 潜在盲区 / 反思问题」结构输出。")
    return "\n".join(lines)


COGNITION_TOOLS = [CognitionRecallTool(), CognitionGetTool(), CognitionExtractTool()]
