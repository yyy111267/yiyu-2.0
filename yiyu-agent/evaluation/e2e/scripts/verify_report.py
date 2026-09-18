"""校验评测报告确实由当前代码、提示词和用例生成。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from evaluation.e2e.scripts.schema import load_cases
from evaluation.e2e.scripts.run_eval import _source_files

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _fingerprint(paths: list[Path]) -> str:
    return hashlib.sha256(b"".join(path.read_bytes() for path in paths)).hexdigest()


def verify_report(
    report_path: Path,
    cases_dir: Path,
    *,
    max_age_minutes: int = 15,
    required_mode: str = "live",
) -> list[str]:
    errors: list[str] = []
    if not report_path.is_file():
        return [f"报告不存在: {report_path}"]

    report = json.loads(report_path.read_text(encoding="utf-8"))
    run = report.get("run") or {}
    summary = report.get("summary") or {}
    cases = report.get("cases") or []
    current_cases = load_cases(cases_dir)

    try:
        generated_at = datetime.fromisoformat(str(run["generated_at"]))
        age_seconds = (datetime.now(timezone.utc) - generated_at).total_seconds()
        if age_seconds < -60 or age_seconds > max_age_minutes * 60:
            errors.append(f"报告已过期或时间异常: age={age_seconds:.0f}s")
    except (KeyError, TypeError, ValueError):
        errors.append("报告缺少有效 generated_at")

    case_files = sorted(cases_dir.rglob("*.yaml"))
    prompt_files = sorted((PROJECT_ROOT / "prompts").rglob("*.md"))
    expected = {
        "execution_mode": required_mode,
        "case_count": len(current_cases),
        "case_fingerprint": _fingerprint(case_files),
        "prompt_fingerprint": _fingerprint(prompt_files),
        "source_fingerprint": _fingerprint(_source_files(PROJECT_ROOT)),
    }
    for key, value in expected.items():
        if run.get(key) != value:
            errors.append(f"{key} 不匹配: report={run.get(key)!r}, current={value!r}")

    try:
        current_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if run.get("git_sha") != current_sha:
            errors.append(f"git_sha 不匹配: report={run.get('git_sha')!r}, current={current_sha!r}")
    except (OSError, subprocess.SubprocessError):
        errors.append("无法读取当前 git_sha")

    if summary.get("failed") != 0:
        errors.append(f"报告含失败用例: {summary.get('failed')!r}")
    if summary.get("skipped") != 0:
        errors.append(f"报告含跳过用例: {summary.get('skipped')!r}")
    if summary.get("total") != len(current_cases) or len(cases) != len(current_cases):
        errors.append("报告用例数量与当前题库不一致")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="校验安全评测报告的新鲜度和来源")
    parser.add_argument(
        "--report",
        default=str(PROJECT_ROOT / "evaluation/e2e/reports/regression/eval_report.json"),
    )
    parser.add_argument(
        "--cases-dir",
        default=str(PROJECT_ROOT / "evaluation/e2e/datasets/regression"),
    )
    parser.add_argument("--max-age-minutes", type=int, default=15)
    parser.add_argument("--required-mode", default="live")
    args = parser.parse_args()

    errors = verify_report(
        Path(args.report), Path(args.cases_dir),
        max_age_minutes=args.max_age_minutes,
        required_mode=args.required_mode,
    )
    if errors:
        print("安全评测报告校验失败：")
        for error in errors:
            print(f"  - {error}")
        return 2
    print("安全评测报告校验通过：当前代码、提示词、用例与报告一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
