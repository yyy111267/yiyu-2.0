"""环节评测：06_metrics · 指标计算 / 指标字典（PRD 第6章）。

一条命令：
    python evaluation/stage/06_metrics/run.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bus_router"))  # formulas_core 以目录形式加载

from evaluation.stage.common import run_stage  # noqa: E402


async def execute(case_input: dict) -> dict:
    """调用环节真实实现（bus_router/formulas_core.py 的冻结公式库）。"""
    from formulas_core import REGISTRY

    fn = case_input["fn"]
    if fn not in REGISTRY:
        raise KeyError(f"公式 {fn!r} 不在 REGISTRY，可用: {sorted(REGISTRY)}")
    try:
        result = REGISTRY[fn](**case_input.get("args", {}))
    except TypeError as e:
        # 必填项缺失 → 公式层必须显式抛错（派生结果供断言；NC 语义归调用层）
        return {"raised": True, "error_type": "TypeError", "message": str(e)}
    return result.to_dict() if hasattr(result, "to_dict") else result


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
