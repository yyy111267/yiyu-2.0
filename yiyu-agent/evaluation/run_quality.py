"""
研报结论「用户侧质量」评测入口。

两种用法：
    # 1) 直接给一段结论文本打分（开发/临时最常用）
    .venv/bin/python -m evaluation.run_quality --conclusion "结论全文……"

    # 2) 批量跑 quality 回归用例（cases/quality 下的 YAML）
    .venv/bin/python -m evaluation.run_quality
    .venv/bin/python -m evaluation.run_quality --case quality01
    .venv/bin/python -m evaluation.run_quality --cases-dir path/to/cases

输出：
    - 终端：打分卡片（总分 / 评级 / 每项得分与理由）
    - 批量模式额外输出：通过率 + 平均分 + JSON 落盘（evaluation/reports/quality_report.json）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from evaluation.quality import QualityJudge, QualityReport

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_DIR = PROJECT_ROOT / "evaluation" / "cases" / "quality"
REPORT_PATH = PROJECT_ROOT / "evaluation" / "reports" / "quality_report.json"

# 每项指标显示用的图标
_ICON = {10.0: "✅", 7.0: "🟢", 5.0: "🟡", 3.0: "🟠", 0.0: "🔴"}


def _icon_for(score: float) -> str:
    if score >= 8:
        return "✅"
    if score >= 6:
        return "🟢"
    if score >= 4:
        return "🟡"
    if score >= 2:
        return "🟠"
    return "🔴"


def print_card(report: QualityReport, title: str = "结论质量评分") -> None:
    """打印打分卡片。"""
    line = "═" * 56
    print(f"\n{line}")
    print(f"  {title}  总分 {report.total:.1f} / 100   评级 {report.grade}")
    print(line)
    for it in report.items:
        cat = "质量" if it.category == "质量" else "友好"
        print(f"  {_icon_for(it.score)} [{cat}] {it.key} {it.name:<8s} "
              f"{it.score:.1f}/10  {it.reason}")
    print(line)


def load_quality_cases(cases_dir: str | Path) -> list:
    """只加载带 sample_conclusion 的质量用例。"""
    from evaluation.schema import load_cases

    all_cases = load_cases(cases_dir)
    return [c for c in all_cases if c.sample_conclusion]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="研报结论用户侧质量评测（100 分制）")
    p.add_argument("--conclusion", default="", help="待打分的结论文本（与批量互斥）")
    p.add_argument("--cases-dir", default=str(DEFAULT_CASES_DIR), help="质量用例目录")
    p.add_argument("--case", default="", help="只跑指定用例 id")
    p.add_argument("--no-store", action="store_true", help="不写 JSON 报告")
    return p.parse_args()


def _run_single(conclusion: str) -> None:
    if not conclusion.strip():
        logger.error("--conclusion 不能为空")
        sys.exit(1)
    report = QualityJudge().check(conclusion)
    print_card(report)


def _run_batch(args: argparse.Namespace) -> None:
    cases = load_quality_cases(args.cases_dir)
    if args.case:
        cases = [c for c in cases if c.id == args.case]
    if not cases:
        logger.error(f"无可用质量用例（dir={args.cases_dir}, case={args.case}）")
        sys.exit(1)

    results = []
    passed = 0
    total_score = 0.0
    for case in cases:
        report = QualityJudge().check(case.sample_conclusion)
        ok = True
        if case.quality_min_score is not None and report.total < case.quality_min_score:
            ok = False
        if case.quality_max_score is not None and report.total > case.quality_max_score:
            ok = False
        if ok:
            passed += 1
        total_score += report.total
        results.append({
            "id": case.id,
            "name": case.name,
            "min_score": case.quality_min_score,
            "max_score": case.quality_max_score,
            "passed": ok,
            "report": report.to_dict(),
        })
        print_card(report, title=f"{case.id} {case.name}")

    avg = total_score / len(results)
    print(f"\n{'=' * 56}")
    print(f"  共 {len(results)} 个用例   通过 {passed}   失败 {len(results) - passed}"
          f"   平均分 {avg:.1f}")
    print(f"{'=' * 56}")

    if not args.no_store:
        REPORT_PATH.parent.mkdir(exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps({"avg_score": round(avg, 1), "results": results},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"报告已写入: {REPORT_PATH}")

    sys.exit(0 if passed == len(results) else 2)


def main() -> None:
    args = _parse_args()
    if args.conclusion:
        _run_single(args.conclusion)
    else:
        _run_batch(args)


if __name__ == "__main__":
    main()
