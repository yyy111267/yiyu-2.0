"""
批量评测入口：加载用例 → 执行 → 汇总报告 → badcase 入库。

用法：
    # offline（不调 LLM，用给定结论测判定器）
    .venv/bin/python -m evaluation.run_eval --offline --conclusion "..."

    # live（真实调 AgentLoop，需 .env 配好 API Key）
    .venv/bin/python -m evaluation.run_eval

    # 只跑某个 skill / 某个用例
    .venv/bin/python -m evaluation.run_eval --skill deep-research
    .venv/bin/python -m evaluation.run_eval --filter eval17,eval18

    # 并发跑批（大幅提速；每个用例独立 AgentLoop）
    .venv/bin/python -m evaluation.run_eval --concurrency 4

    # 跳过 LLM 行为判定（只看关键词/硬规则，每用例省数秒）
    .venv/bin/python -m evaluation.run_eval --no-behavior

    # 不落库（开发用）
    .venv/bin/python -m evaluation.run_eval --no-store
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CASES_DIR = PROJECT_ROOT / "evaluation" / "e2e" / "datasets" / "benchmark"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="以渔2.0 评测集批量运行器")
    p.add_argument("--offline", action="store_true", help="offline 模式：不调 LLM")
    p.add_argument("--conclusion", default="", help="offline 模式下的结论文本")
    p.add_argument("--skill", default="", help="只跑指定 skill 的用例")
    p.add_argument("--filter", default="", help="只跑指定用例 id，逗号分隔")
    p.add_argument("--cases-dir", default=str(CASES_DIR), help="用例目录")
    p.add_argument("--no-store", action="store_true", help="不写 badcase 库")
    p.add_argument("--db", default="sqlite:///yiyu_agent.db", help="badcase 库连接串")
    p.add_argument(
        "--concurrency", type=int, default=1,
        help="并发跑批的用例数（默认 1 串行）。建议 4-6，过高会撞模型 RPM 限流。",
    )
    p.add_argument(
        "--no-behavior", action="store_true",
        help="跳过 LLM 行为判定（must_do），只做关键词与硬规则判定，显著提速。",
    )
    return p.parse_args()


async def _main() -> None:
    args = _parse_args()

    from evaluation.e2e.scripts.schema import load_cases
    from evaluation.e2e.scripts.runner import EvalRunner, summarize

    cases = load_cases(args.cases_dir)
    if args.skill:
        cases = [c for c in cases if c.skill == args.skill]
    if args.filter:
        wanted = {x.strip() for x in args.filter.split(",") if x.strip()}
        cases = [c for c in cases if c.id in wanted]

    if not cases:
        logger.error(f"无可用用例（dir={args.cases_dir}, skill={args.skill}, filter={args.filter}）")
        sys.exit(1)

    # offline 模式：不需要 LLM
    if args.offline:
        runner = EvalRunner(mode="offline", offline_conclusion=args.conclusion)
    else:
        # live 模式：装配最小闭环（llm/assembler/executor/spawner/loop）
        from core.config import settings
        from core.llm import LLMClient
        from runtime.assembler import PromptAssembler
        from runtime.loop import AgentLoop, LoopConfig
        from toolkit.executor import ToolExecutor
        from toolkit import TOOL_REGISTRY, register_all
        from agents.base import AgentSpawner

        register_all()
        logger.info("live 模式已注册 %d 个工具", len(TOOL_REGISTRY))
        llm_client = LLMClient(settings)
        assembler = PromptAssembler(prompts_dir=str(PROJECT_ROOT / "prompts"))
        prompts_dir = str(PROJECT_ROOT / "prompts")

        def _new_loop():
            """每个用例一个独立 AgentLoop，避免并发下会话态互相污染。

            预算与生产（api/main.py）对齐：评测若走 LoopConfig 默认的
            300s，得出的延迟与降级率无法代表线上表现。
            """
            return AgentLoop(
                llm_client=llm_client,
                assembler=PromptAssembler(prompts_dir=prompts_dir),
                tool_executor=ToolExecutor(),
                agent_spawner=AgentSpawner(),
                config=LoopConfig(
                    timeout_seconds=settings.research_timeout_seconds,
                    llm_timeout_seconds=settings.llm_request_timeout_seconds,
                    finalize_timeout_seconds=settings.finalize_timeout_seconds,
                ),
            )

        runner = EvalRunner(
            mode="live",
            llm_client=llm_client,
            assembler=assembler,
            tool_executor=ToolExecutor(),
            agent_spawner=AgentSpawner(),
            loop=_new_loop() if args.concurrency <= 1 else None,
            loop_factory=_new_loop,
            no_behavior=args.no_behavior,
        )

    reports = await runner.run_all(cases, concurrency=args.concurrency)
    summary = summarize(reports)

    print("\n" + "=" * 70)
    print("评测报告")
    print("=" * 70)
    print(f"用例总数: {summary['total']}  通过: {summary['passed']}  "
          f"失败: {summary['failed']}  跳过: {summary['skipped']}  "
          f"通过率: {summary['pass_rate']:.2%}")
    print(f"硬失败: {len(summary['hard_failures'])} 条  执行错误: {len(summary['errors'])} 条")
    print(f"关键数字来源分布: {summary['source_status_distribution']}")
    if summary.get("quality_avg") is not None:
        print(f"结论质量平均分: {summary['quality_avg']}/100")
    # 轨迹级指标
    trace_sum = summary.get("trace") or {}
    if trace_sum:
        violations = trace_sum.get("state_machine_violations", [])
        print("-" * 70)
        print("轨迹级指标：")
        print(f"  状态机违规用例: {len(violations)} 个 {violations[:5] if violations else ''}")
        print(f"  重复取数总次数: {trace_sum.get('dup_fetch_total', 0)}")
        print(f"  finish 重试总次数: {trace_sum.get('finish_retry_total', 0)}")
    print("-" * 70)
    for r in reports:
        status = "✅" if r.passed else ("⏭️" if r.skipped else "❌")
        detail = ""
        if r.quality:
            detail = f" [质量 {r.quality.total:.1f}/100 {r.quality.grade}]"
        if r.keyword and r.keyword.detail:
            detail += f" [{r.keyword.detail}]"
        elif r.behavior and r.behavior.detail:
            detail += f" [{r.behavior.detail}]"
        elif r.error:
            detail += f" [{r.error}]"
        print(f"  {status} {r.case.id} {r.case.name}{detail}")
    print("=" * 70)

    # badcase 入库
    if not args.no_store:
        from store.repos.badcase_repo import BadcaseRepo
        repo = BadcaseRepo(db_url=args.db)
        stored = 0
        for r in reports:
            if not r.passed and not r.skipped:
                if repo.record_report(r.to_dict()):
                    stored += 1
        print(f"badcase 已入库 {stored} 条（库: {args.db}）")

    # 结果落盘（供 CI/复盘）
    out = PROJECT_ROOT / "evaluation" / "e2e" / "reports" / "benchmark"
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "eval_report.json"
    report_path.write_text(
        json.dumps({
            "summary": summary,
            "cases": [r.to_dict() for r in reports],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"报告已写入: {report_path}")

    # 退出码：有失败则非 0（CI 可用）
    sys.exit(0 if summary["failed"] == 0 else 2)


if __name__ == "__main__":
    asyncio.run(_main())
