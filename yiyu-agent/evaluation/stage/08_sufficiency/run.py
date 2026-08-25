"""环节评测：08_sufficiency · 准出硬规则（PRD 5.5）。

一条命令：
    python evaluation/stage/08_sufficiency/run.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402


async def execute(case_input: dict) -> dict:
    """调用环节真实实现（toolkit/delivery/submit_conclusion.py）。

    input 直接就是 validate_conclusion 的 kwargs，无映射层——
    本环节代码即 PRD 语义，是少数不需要临时映射的环节。
    """
    from toolkit.delivery.submit_conclusion import validate_conclusion

    r = validate_conclusion(**case_input)
    return r.to_dict()


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
