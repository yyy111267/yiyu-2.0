"""一键评测入口：hard_rules 回归 + 安全攻击 + 评测集，并打印 badcase 概览。

用法：
    .venv/bin/python -m evaluation.run_all            # 全部
    .venv/bin/python -m evaluation.run_all --offline  # 只跑离线判定
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PY = str(PROJECT_ROOT / ".venv" / "bin" / "python")


def _run(name: str, module: str, extra: list[str] | None = None) -> int:
    print(f"\n{'='*60}\n▶ {name}\n{'='*60}")
    cmd = [PY, "-m", module] + (extra or [])
    return subprocess.call(cmd, cwd=str(PROJECT_ROOT))


def main() -> int:
    p = argparse.ArgumentParser(description="以渔2.0 全量评测")
    p.add_argument("--offline", action="store_true", help="只跑离线判定")
    args = p.parse_args()

    codes = []
    codes.append(_run("硬规则回归", "evaluation.test_hard_rules"))
    codes.append(_run("安全攻击回归", "evaluation.gate_bypass"))
    if args.offline:
        codes.append(_run("评测集(offline)", "evaluation.run_eval", ["--offline", "--conclusion",
            "研究结论：该公司 ROE 15% 来自财报，估值区间 20-25 元，存在现金流风险（来源：年报）。"]))
    else:
        codes.append(_run("评测集(live)", "evaluation.run_eval"))

    failed = sum(1 for c in codes if c != 0)
    print(f"\n{'='*60}\n完成：{len(codes) - failed}/{len(codes)} 项通过\n{'='*60}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
