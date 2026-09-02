"""
逐条明细评测：输出每条用例的「用户 query / 模型输出 / 首字延迟 / 总耗时」。

与 run_eval.py 的区别：
  - run_eval.py 侧重通过率与判定（关键词 + LLM 行为打分）；
  - 本脚本侧重「看效果」：完整打印模型输出全文，并给出用户视角的两个延迟
    （TTFT 首字延迟、总耗时），可直接人工查看模型输出质量。

用法：
    PYTHONPATH=. .venv/bin/python evaluation/e2e/scripts/run_eval_detail.py \
        --cases-dir evaluation/e2e/datasets/e2e_benchmark \
        --concurrency 4 \
        --out /tmp/eval_detail.md

输出：Markdown 明细（每条一节）+ JSON（机器可读，含完整字段）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CASES_DIR = PROJECT_ROOT / "evaluation" / "e2e" / "datasets" / "e2e_benchmark"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="逐条明细评测（看输出效果 + 延迟）")
    p.add_argument("--cases-dir", default=str(CASES_DIR), help="用例目录")
    p.add_argument("--skill", default="", help="只跑指定 skill")
    p.add_argument("--filter", default="", help="只跑指定用例 id，逗号分隔")
    p.add_argument("--concurrency", type=int, default=4, help="并发数（建议 4-6）")
    p.add_argument("--no-behavior", action="store_true", help="跳过 LLM 行为判定，提速")
    p.add_argument("--max-chars", type=int, default=0,
                   help="单条输出打印的最大字符数（0=不截断）")
    p.add_argument("--out", default="", help="Markdown 输出路径（默认 stdout）")
    p.add_argument("--json-out", default="", help="JSON 输出路径")
    return p.parse_args()


def _fmt(v, nd: int = 1) -> str:
    """格式化秒数；None 显示 n/a。"""
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "n/a"


def _stats(values: list[float]) -> str:
    """给出 min/中位数/max，便于看长尾。"""
    if not values:
        return "n/a"
    s = sorted(values)
    mid = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
    return f"min {s[0]:.1f} / 中位 {mid:.1f} / max {s[-1]:.1f}"


def render_markdown(reports, wall_sec: float, max_chars: int) -> str:
    lines: list[str] = []
    lines.append("# 端到端评测明细（逐条）\n")
    lines.append(f"- 用例数：{len(reports)}")
    lines.append(f"- 墙钟总耗时：{wall_sec:.1f}s（并发执行，非串行累加）")
    lines.append("")

    # 延迟总览表
    ttfts = [r.ttft_sec for r in reports if r.ttft_sec is not None]
    totals = [r.total_sec for r in reports if r.total_sec is not None]
    lines.append("## 延迟总览\n")
    lines.append("| 指标 | min | 中位 | max |")
    lines.append("|---|---|---|---|")
    for name, vals in (("首字延迟 TTFT (s)", ttfts), ("总耗时 (s)", totals)):
        if not vals:
            lines.append(f"| {name} | - | - | - |")
            continue
        s = sorted(vals)
        mid = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
        lines.append(f"| {name} | {s[0]:.1f} | {mid:.1f} | {s[-1]:.1f} |")
    lines.append("")
    lines.append(f"- 首字延迟分布：{_stats(ttfts)}")
    lines.append(f"- 总耗时分布：{_stats(totals)}")
    lines.append("")

    # 逐条明细
    lines.append("## 逐条明细\n")
    for i, r in enumerate(reports, 1):
        c = r.case
        lines.append(f"### {i}. {c.id} — {c.name}\n")
        meta = c.input_metadata or {}
        lines.append(f"- skill：`{c.skill}`　阶段/类型：`{meta.get('stage','')}/{meta.get('case_type','')}`")
        lines.append(f"- **首字延迟（TTFT）**：`{_fmt(r.ttft_sec)}s`")
        lines.append(f"- **总耗时**：`{_fmt(r.total_sec)}s`")
        lines.append(f"- 判定：{'✅ 通过' if r.passed else '❌ 未通过'}"
                     f"　降级：`{r.degraded}`　硬规则：`{r.metrics.get('hard_rules_passed')}`")
        if r.error:
            lines.append(f"- ⚠️ 错误：`{r.error}`")

        lines.append("\n**用户 query：**\n")
        lines.append("```")
        lines.append(c.input_message or "(空)")
        lines.append("```")

        out = r.conclusion or ""
        if max_chars and len(out) > max_chars:
            out = out[:max_chars] + f"\n…(共 {len(r.conclusion)} 字，已截断)"
        lines.append("\n**模型输出：**\n")
        lines.append("```")
        lines.append(out if out else "(无输出)")
        lines.append("```")

        # 判定细节（辅助定位为什么没通过）
        if r.keyword and not r.keyword.passed:
            lines.append(f"\n- 关键词判定：{r.keyword.detail}")
        if r.behavior is not None and r.behavior.behavior_scores:
            avg = sum(v["score"] for v in r.behavior.behavior_scores.values()) / len(
                r.behavior.behavior_scores)
            lines.append(f"- 行为判定（must_do）平均分：`{avg:.2f}/4`")
        if r.metrics.get("auto_sanitized_numbers"):
            lines.append(f"- 标注为推断的数字：`{r.metrics['auto_sanitized_numbers']}`")
        lines.append("")

    return "\n".join(lines)


async def _main() -> int:
    args = _parse_args()
    sys.path.insert(0, str(PROJECT_ROOT))

    from core.config import settings
    from core.llm import LLMClient
    from runtime.assembler import PromptAssembler
    from runtime.loop import AgentLoop, LoopConfig
    from toolkit.executor import ToolExecutor
    from toolkit import TOOL_REGISTRY, register_all
    from agents.base import AgentSpawner
    from evaluation.e2e.scripts.schema import load_cases
    from evaluation.e2e.scripts.runner import EvalRunner

    register_all()
    logger.info("已注册 %d 个工具", len(TOOL_REGISTRY))

    cases = load_cases(args.cases_dir)
    if args.skill:
        cases = [c for c in cases if c.skill == args.skill]
    if args.filter:
        wanted = {x.strip() for x in args.filter.split(",") if x.strip()}
        cases = [c for c in cases if c.id in wanted]
    if not cases:
        logger.error("无可用用例")
        return 1

    llm_client = LLMClient(settings)
    prompts_dir = str(PROJECT_ROOT / "prompts")

    def _new_loop():
        # 每个用例独立 AgentLoop：并发下共享会污染 breaker / trace 会话态。
        # 预算必须与生产（api/main.py）对齐，否则评测跑的是另一条时间尺度，
        # 结果无法代表线上表现。
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
        assembler=PromptAssembler(prompts_dir=prompts_dir),
        tool_executor=ToolExecutor(),
        agent_spawner=AgentSpawner(),
        loop_factory=_new_loop,
        no_behavior=args.no_behavior,
    )

    logger.info("开始执行 %d 个用例（并发 %d）…", len(cases), args.concurrency)
    t0 = time.time()
    reports = await runner.run_all(cases, concurrency=args.concurrency)
    wall = time.time() - t0
    logger.info("执行完成，墙钟 %.1fs", wall)

    md = render_markdown(reports, wall, args.max_chars)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        logger.info("Markdown 已写入 %s", args.out)
    else:
        print(md)

    if args.json_out:
        payload = {
            "wall_sec": round(wall, 2),
            "concurrency": args.concurrency,
            "reports": [r.to_dict() for r in reports],
        }
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("JSON 已写入 %s", args.json_out)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
