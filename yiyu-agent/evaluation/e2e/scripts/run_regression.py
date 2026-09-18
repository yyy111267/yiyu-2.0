"""运行永久回归题库；使用真实路由/循环，安全边界题无需外部模型调用。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from evaluation.e2e.scripts.run_eval import _main


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[3]
    sys.argv.extend([
        "--cases-dir", str(project_root / "evaluation/e2e/datasets/regression"),
        "--report-dir", "regression",
        "--no-store",
        "--no-behavior",
    ])
    asyncio.run(_main())
