"""发布前安全门禁：任一子检查失败即返回非零退出码。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable

CHECKS = (
    ("硬规则绕过测试", [PYTHON, "-m", "pytest", "-q", "evaluation/e2e/scripts/gate_bypass.py"]),
    ("内部信息端到端测试", [PYTHON, "-m", "pytest", "-q", "tests/test_internal_information_guard.py"]),
    ("路由阶段评测", [PYTHON, "evaluation/stage/01_intent/run.py"]),
    ("交付硬规则评测", [PYTHON, "evaluation/stage/08_sufficiency/run.py"]),
    ("研究循环评测", [PYTHON, "evaluation/stage/11_loop/run.py"]),
    ("安全阶段评测", [PYTHON, "evaluation/stage/13_safety/run.py"]),
    ("永久 badcase 回归", [PYTHON, "-m", "evaluation.e2e.scripts.run_regression"]),
    ("报告来源与新鲜度", [PYTHON, "-m", "evaluation.e2e.scripts.verify_report"]),
)


def main() -> int:
    for name, command in CHECKS:
        print(f"\n{'=' * 60}\n▶ {name}\n{'=' * 60}", flush=True)
        code = subprocess.call(command, cwd=PROJECT_ROOT)
        if code:
            print(f"\n安全门禁失败：{name}（exit={code}）")
            return code
    print(f"\n{'=' * 60}\n安全门禁通过：{len(CHECKS)}/{len(CHECKS)}\n{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
