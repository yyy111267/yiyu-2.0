"""环节评测：07_cognition · 认知抽取（PRD 记忆管理）。

一条命令：
    python evaluation/stage/07_cognition/run.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402


async def execute(case_input: dict) -> dict:
    """调用环节真实实现（store/cognition_store.py::extract_atoms）。"""
    from store.cognition_store import CognitionStore

    store = CognitionStore()  # 抽取为纯函数，不触库
    atoms = store.extract_atoms(
        case_input["text"],
        user_id=case_input.get("user_id", "eval_user"),
        symbol=case_input.get("symbol", ""),
    )
    # 派生指标：条数（断言引擎不做 list 聚合，聚合放 execute）
    return {"atoms": atoms, "count": len(atoms)}


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
