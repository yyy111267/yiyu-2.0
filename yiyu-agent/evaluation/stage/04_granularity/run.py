"""环节评测：04_granularity · 研究粒度决策（PRD 5.3 环节②）。

状态：pending —— 目标实现 runtime/granularity.py 待建，本评测集即接口契约。
一条命令：
    python evaluation/stage/04_granularity/run.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402


def _derive(units: list) -> dict:
    """派生指标：单元拆分理由是否齐备、是否关联差异条件。"""
    reasons = [(u.get("reason") or "").strip() for u in units]
    diff_kw = ("客户", "盈利模式", "收费", "监管", "周期", "货币化", "商业模式")
    return {
        "all_units_have_reason": bool(reasons) and all(reasons),
        "all_units_reasons_valid": bool(reasons) and all(
            any(k in r for k in diff_kw) for r in reasons
        ),
    }


async def execute(case_input: dict) -> dict:
    """调用环节真实实现（改造落地后接通）。"""
    try:
        from runtime.granularity import decide_granularity, validate_granularity
    except ImportError as e:
        raise NotImplementedError(f"runtime/granularity.py 待建: {e}") from e

    # 校验模式：input 带 raw_model_output → 只跑护栏校验器
    if "raw_model_output" in case_input:
        v = validate_granularity(case_input["raw_model_output"])
        return v.to_dict() if hasattr(v, "to_dict") else v

    g = await decide_granularity(case_input["company_facts"])
    d = g.to_dict() if hasattr(g, "to_dict") else g
    d.update(_derive(d.get("units") or []))
    return d


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
