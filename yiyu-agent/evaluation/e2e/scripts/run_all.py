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

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PY = str(PROJECT_ROOT / ".venv" / "bin" / "python")


def _run(name: str, module: str, extra: list[str] | None = None) -> int:
    print(f"\n{'='*60}\n▶ {name}\n{'='*60}")
    cmd = [PY, "-m", module] + (extra or [])
    return subprocess.call(cmd, cwd=str(PROJECT_ROOT))


def _run_pytest(name: str, path: str) -> int:
    print(f"\n{'='*60}\n▶ {name}\n{'='*60}")
    return subprocess.call(
        [PY, "-m", "pytest", "-q", path],
        cwd=str(PROJECT_ROOT),
    )


def main() -> int:
    p = argparse.ArgumentParser(description="以渔2.0 全量评测")
    p.add_argument("--offline", action="store_true", help="只跑离线判定")
    args = p.parse_args()

    codes = []
    codes.append(_run_pytest("硬规则回归", "evaluation/e2e/scripts/test_hard_rules.py"))
    codes.append(_run_pytest("安全攻击回归", "evaluation/e2e/scripts/gate_bypass.py"))
    if args.offline:
        # benchmark 是场景级用例，不能用同一句手工结论伪装整车路测。
        # 无 LLM 时只跑自带样本与分数阈值的 quality 回归。
        codes.append(_run("质量样本回归(offline)", "evaluation.e2e.scripts.run_quality"))
    else:
        codes.append(_run("评测集(live)", "evaluation.e2e.scripts.run_eval"))

    failed = sum(1 for c in codes if c != 0)
    print(f"\n{'='*60}\n完成：{len(codes) - failed}/{len(codes)} 项通过\n{'='*60}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
