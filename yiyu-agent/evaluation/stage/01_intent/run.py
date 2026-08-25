"""环节评测：01_intent · 意图识别与路由（PRD 5.1）。

一条命令：
    python evaluation/stage/01_intent/run.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402


async def execute(case_input: dict) -> dict:
    """调用环节真实实现（runtime/router.py，已对齐 PRD 5.1 两路径结构）。"""
    from runtime.router import route

    r = route(
        case_input["message"],
        current_entity=case_input.get("current_entity"),
        query_streak=case_input.get("query_streak", 0),
    )
    return {
        "route": r.route_result,
        "entity_candidates": r.entity_candidates,
        "route_reason": r.route_reason,
        "query_streak": r.query_streak,
        "suggest_research": r.suggest_research,
        "matched_by": r.matched_by,
        "intent": r.intent,      # 兼容字段，供排查
        "skill": r.skill,
        "confidence": r.confidence,
    }


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
