"""
评测执行器。

两种模式：
- live：真实装配 AgentLoop（需 LLM API Key + .env），跑完整链路收集结论与观测。
- offline：不调 LLM，直接用给定的结论文本做判定（用于开发/回归判定器本身）。

产出每个用例的 CaseReport：
    用例元数据 + 关键词判定 + LLM 行为判定 + 三大指标 + 结论摘要 + 错误。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from evaluation.e2e.scripts.metrics import TraceStats, compute_metrics, compute_trace_metrics
from evaluation.e2e.scripts.quality import QualityJudge, QualityReport
from evaluation.e2e.scripts.schema import EvalCase
from evaluation.e2e.scripts.judges import KeywordJudge, LLMBehaviorJudge

logger = logging.getLogger(__name__)


@dataclass
class CaseReport:
    case: EvalCase
    passed: bool = False
    skipped: bool = False
    error: Optional[str] = None
    conclusion: str = ""
    keyword: Any = None          # JudgeResult
    behavior: Any = None         # JudgeResult
    quality: Any = None          # QualityReport（用户侧质量，0-100 分）
    metrics: dict[str, Any] = field(default_factory=dict)
    trace_path: Optional[str] = None  # trace JSONL 文件路径（落盘复盘用）
    execution_mode: str = ""        # live / offline，防止把离线判卷误当真实路测
    degraded: bool = False
    # 用户视角的两个延迟（秒）：
    #   ttft_sec —— 从用户 query 进入，到大模型开始吐第一个字（含 preloop）
    #   total_sec —— 从 query 进入，到最终答案输出完成
    ttft_sec: Optional[float] = None
    total_sec: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.case.id,
            "name": self.case.name,
            "skill": self.case.skill,
            "passed": self.passed,
            "skipped": self.skipped,
            "error": self.error,
            "conclusion": self.conclusion,
            "trace_path": self.trace_path,
            "execution_mode": self.execution_mode,
            "degraded": self.degraded,
            "ttft_sec": self.ttft_sec,
            "total_sec": self.total_sec,
            "keyword": {
                "passed": self.keyword.passed if self.keyword else None,
                "detail": self.keyword.detail if self.keyword else "",
                "hard_fail": self.keyword.hard_fail if self.keyword else [],
            },
            "behavior": {
                "passed": self.behavior.passed if self.behavior else None,
                "skipped": self.behavior.skipped if self.behavior else [],
                "scores": self.behavior.behavior_scores if self.behavior else {},
            },
            "quality": self.quality.to_dict() if self.quality else None,
            "metrics": self.metrics,
        }


class EvalRunner:
    def __init__(
        self,
        mode: str = "live",
        llm_client=None,
        assembler=None,
        tool_executor=None,
        agent_spawner=None,
        loop=None,
        loop_factory=None,
        offline_conclusion: Optional[str] = None,
        no_behavior: bool = False,
    ) -> None:
        self.mode = mode
        self.llm = llm_client
        self.loop = loop
        # 并发跑批时每个用例必须有独立的 AgentLoop：AgentLoop 持有
        # self.trace / breaker / reject_count 等会话态，共享会互相污染
        # （与生产 agent_loop_factory 同模式）。
        self.loop_factory = loop_factory
        # live 模式下 lazy 装配（参考 api/main.py 的 lifespan）
        self.assembler = assembler
        self.executor = tool_executor
        self.spawner = agent_spawner
        self.offline_conclusion = offline_conclusion or ""

        self.keyword_judge = KeywordJudge()
        self.behavior_judge = (
            None if no_behavior
            else (LLMBehaviorJudge(llm_client) if llm_client else None)
        )
        self.quality_judge = QualityJudge()

    # ── 公开接口 ─────────────────────────────────────────────────────

    async def run_case(self, case: EvalCase) -> CaseReport:
        if self.mode == "offline":
            return await self._run_offline(case)
        return await self._run_live(case)

    async def run_all(
        self,
        cases: list[EvalCase],
        concurrency: int = 1,
    ) -> list[CaseReport]:
        """批量执行用例。

        concurrency>1 时并发跑批，用 asyncio.gather 保证返回顺序与入参一致
        （报告可对齐用例）。单个用例异常不影响其余用例。
        """
        n = max(1, int(concurrency or 1))
        if n == 1:
            reports = []
            for case in cases:
                try:
                    reports.append(await self.run_case(case))
                except Exception as e:
                    logger.exception(f"用例执行异常 {case.id}: {e}")
                    reports.append(CaseReport(case=case, error=f"执行异常: {e}"))
            return reports

        sem = asyncio.Semaphore(n)

        async def _one(case: EvalCase) -> CaseReport:
            async with sem:
                try:
                    return await self.run_case(case)
                except Exception as e:
                    logger.exception(f"用例执行异常 {case.id}: {e}")
                    return CaseReport(case=case, error=f"执行异常: {e}")

        return list(await asyncio.gather(*(_one(c) for c in cases)))

    # ── live：真实跑 AgentLoop ───────────────────────────────────────

    async def _run_live(self, case: EvalCase) -> CaseReport:
        from runtime.loop import AgentLoop, LoopConfig
        from runtime.state import AgentState
        from runtime.events import EventType
        from runtime.boundary import OUT_OF_SCOPE, boundary_message
        from runtime.router import route_async
        from runtime.skill_availability import is_skill_available
        from toolkit import register_all

        register_all()
        # 并发跑批时优先用工厂为每个用例建独立 AgentLoop，避免会话态污染。
        loop = (self.loop_factory() if self.loop_factory else self.loop)
        if loop is None:
            loop = self._build_loop()
        stats = TraceStats()
        conclusion = ""
        start = time.time()

        routed = await route_async(
            llm_client=self.llm,
            user_message=case.input_message,
            explicit_skill=case.skill if case.skill else None,
        )
        if routed.route_result == OUT_OF_SCOPE:
            reason = (routed.route_reason.rsplit("：", 1)[-1] or "non_research")
            conclusion = boundary_message(reason)
            report = CaseReport(
                case=case,
                conclusion=conclusion,
                metrics=compute_metrics(TraceStats(conclusion=conclusion)),
                execution_mode="live",
            )
            report.metrics["hard_rules_passed"] = True
            report.keyword = self.keyword_judge.check(case, conclusion)
            report.passed = self._combine(report)
            return report
        skill_name = routed.skill or case.skill
        if not is_skill_available(skill_name):
            conclusion = boundary_message("unsupported_task")
            report = CaseReport(
                case=case,
                conclusion=conclusion,
                metrics=compute_metrics(TraceStats(conclusion=conclusion)),
                execution_mode="live",
            )
            report.metrics["hard_rules_passed"] = True
            report.keyword = self.keyword_judge.check(case, conclusion)
            report.passed = self._combine(report)
            return report

        # 与生产 API 保持一致：deep-research 先跑 preloop 四件套。
        research_plan = None
        initial_context = None
        if skill_name == "deep-research":
            try:
                from core.config import settings
                from runtime.preloop import run_preloop
                from toolkit.market.market import MarketData
                from toolkit.web.tools import WebSearchTool

                preloop = await run_preloop(
                    case.input_message,
                    llm_client=self.llm,
                    market_data=MarketData(settings),
                    web_search_fn=WebSearchTool().execute,
                    candidate=(routed.entity_candidates[0]
                               if routed.entity_candidates else None),
                    user_id=f"eval-{case.id}",
                )
                research_plan = preloop.plan
                initial_context = preloop.initial_context
            except Exception as exc:  # 生产入口同样降级，评测轨迹必须留痕
                logger.warning("%s preloop 失败，降级进入 loop: %s", case.id, exc)
                initial_context = {"preloop_error": str(exc)}

        # 与生产 API 对齐：生产对 deep-research 注入 latency_mode=True 走低延迟
        # 路径（预激活 P0 + 快速收尾）。评测若不开，会跑另一条更慢的路径，
        # 延迟与降级率都无法代表线上。
        if skill_name == "deep-research":
            initial_context = dict(initial_context or {})
            initial_context.setdefault("latency_mode", True)

        events: list[dict[str, Any]] = []
        validated: Optional[bool] = None
        degraded = False
        async for evt in loop.run(
            user_message=case.input_message,
            session_id=f"eval-{case.id}",
            skill_name=skill_name,
            research_plan=research_plan,
            initial_context=initial_context,
            user_id=f"eval-{case.id}",
        ):
            events.append({
                "type": evt.type.value,
                "content": evt.content,
                "metadata": evt.metadata or {},
                "timestamp": evt.timestamp,
            })
            if evt.type == EventType.FINAL_ANSWER:
                conclusion = evt.content or conclusion
                validated = evt.metadata.get("validated")
                degraded = bool(evt.metadata.get("degraded", False))
                stats.tokens_used = evt.metadata.get("tokens_used", stats.tokens_used)
                # 透传 R6 兜底标注（无源数字被标为「推断」），供明细查看与监控
                if evt.metadata.get("auto_sanitized_numbers"):
                    stats.auto_sanitized_numbers = list(
                        evt.metadata["auto_sanitized_numbers"])
            if (evt.type == EventType.TOOL_RESULT
                    and evt.metadata.get("success", True)
                    and evt.metadata.get("tool_name") != "delivery.finish"):
                stats.tool_sources.append(str(evt.metadata.get("tool_name", "")))
            if evt.type == EventType.TOOL_CALL:
                stats.tool_calls += 1

        stats.duration_sec = time.time() - start
        stats.conclusion = conclusion
        stats.events = events
        stats.steps = len([e for e in events if e["type"] == "thought"])
        stats.hard_rules_passed = validated

        # 轨迹落盘：每次 run 写一个 JSONL，一行一个 event（含全字段 metadata）
        trace_dir = Path(__file__).resolve().parent / "traces"
        trace_dir.mkdir(exist_ok=True)
        ts_str = time.strftime("%Y%m%d-%H%M%S")
        trace_file = trace_dir / f"{case.id}-{ts_str}.jsonl"
        trace_file.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
            encoding="utf-8",
        )

        # 汇总指标：结论级 + 轨迹级
        metrics = compute_metrics(stats)
        metrics["trace"] = compute_trace_metrics(events)
        if stats.auto_sanitized_numbers:
            metrics["auto_sanitized_numbers"] = stats.auto_sanitized_numbers

        # 用户视角延迟：TTFT = query 进入 → 第一个 answer_delta；
        # 总耗时 = query 进入 → FINAL_ANSWER。用事件自带的 timestamp 计算，
        # 与 start（含 preloop）同一时基。
        ttft = None
        total = None
        for e in events:
            ts = e.get("timestamp")
            if not isinstance(ts, (int, float)):
                continue
            if ttft is None and e["type"] == "answer_delta":
                ttft = ts - start
            if e["type"] == "final_answer":
                total = ts - start
        if total is None:
            total = time.time() - start

        report = CaseReport(
            case=case,
            conclusion=conclusion,
            metrics=metrics,
            ttft_sec=ttft,
            total_sec=total,
            trace_path=str(trace_file.relative_to(Path(__file__).resolve().parent.parent)),
            execution_mode="live",
            degraded=degraded,
        )
        if not conclusion:
            report.error = "未产出最终回答"
            report.skipped = True
            return report

        # 关键词判定（离线部分）
        report.keyword = self.keyword_judge.check(case, conclusion)
        # LLM 行为判定
        if self.behavior_judge:
            report.behavior = await self.behavior_judge.check(case, conclusion)
        # 用户侧质量打分（质量用例）
        if self._need_quality(case):
            report.quality = self.quality_judge.check(conclusion)

        report.passed = self._combine(report)
        return report

    def _build_loop(self):
        """参照 api/main.py 装配最小闭环。需要 llm_client 等已在外部注入。"""
        from core.config import settings
        from runtime.assembler import PromptAssembler
        from runtime.loop import AgentLoop
        from toolkit.executor import ToolExecutor
        from agents.base import AgentSpawner
        from toolkit import register_all, TOOL_REGISTRY

        register_all()
        if self.llm is None:
            from core.llm import LLMClient
            self.llm = LLMClient(settings)
        if self.assembler is None:
            self.assembler = PromptAssembler(prompts_dir=str(
                Path(__file__).resolve().parents[3] / "prompts"))
        if self.executor is None:
            self.executor = ToolExecutor()
        if self.spawner is None:
            self.spawner = AgentSpawner()
        return AgentLoop(
            llm_client=self.llm,
            assembler=self.assembler,
            tool_executor=self.executor,
            agent_spawner=self.spawner,
        )

    # ── offline：直接判定给定结论 ─────────────────────────────────────

    async def _run_offline(self, case: EvalCase) -> CaseReport:
        # 优先用 --conclusion 传入的文本；质量用例没传时用用例自带的 sample_conclusion
        conclusion = self.offline_conclusion or case.sample_conclusion
        report = CaseReport(case=case, conclusion=conclusion, execution_mode="offline")
        if not conclusion:
            report.error = "offline 模式需提供结论文本（--conclusion）或用例自带 sample_conclusion"
            report.skipped = True
            return report

        report.keyword = self.keyword_judge.check(case, conclusion)
        if self.behavior_judge:
            report.behavior = await self.behavior_judge.check(case, conclusion)
        if self._need_quality(case):
            report.quality = self.quality_judge.check(conclusion)
        report.metrics = compute_metrics(TraceStats(conclusion=conclusion))
        report.passed = self._combine(report)
        return report

    # ── 质量打分辅助 ─────────────────────────────────────────────────

    def _need_quality(self, case: EvalCase) -> bool:
        """该用例是否需要跑用户侧质量打分。"""
        return bool(
            case.sample_conclusion
            or case.quality_min_score is not None
            or case.quality_max_score is not None
        )

    def _quality_ok(self, report: CaseReport) -> bool:
        """质量分是否落在用例要求的区间（min/max 未设置则不约束）。"""
        q = report.quality
        if q is None:
            return True
        c = report.case
        if c.quality_min_score is not None and q.total < c.quality_min_score:
            return False
        if c.quality_max_score is not None and q.total > c.quality_max_score:
            return False
        return True

    # ── 汇总 ──────────────────────────────────────────────────────────

    def _combine(self, report: CaseReport) -> bool:
        """综合判定：关键词通过 + 行为通过（或跳过）+ 质量分达标。"""
        kw_ok = report.keyword.passed if report.keyword else True
        bh_ok = report.behavior.passed if report.behavior is not None else True
        q_ok = self._quality_ok(report)
        delivery_ok = True
        if report.execution_mode == "live":
            delivery_ok = (
                report.metrics.get("hard_rules_passed") is True
                and not report.degraded
            )
        return kw_ok and bh_ok and q_ok and delivery_ok


def summarize(reports: list[CaseReport]) -> dict[str, Any]:
    """汇总通过率 / 硬失败 / 跳过 / 指标均值。"""
    total = len(reports)
    passed = sum(1 for r in reports if r.passed)
    skipped = sum(1 for r in reports if r.skipped)
    hard_failures = [
        (r.case.id, r.keyword.hard_fail)
        for r in reports
        if r.keyword and r.keyword.hard_fail
    ]
    errors = [(r.case.id, r.error) for r in reports if r.error]

    source_status = {}
    for r in reports:
        if r.metrics.get("key_figures"):
            status = r.metrics["key_figures"].get("status")
            source_status[status] = source_status.get(status, 0) + 1

    quality_scores = [r.quality.total for r in reports if r.quality]

    # 轨迹级指标汇总
    trace_violations: list[str] = []        # 状态机违规的用例 id
    dup_fetch_total = 0                      # 重复取数总次数
    finish_retry_total = 0                   # finish 重试总次数
    for r in reports:
        tm = r.metrics.get("trace") or {}
        if tm.get("state_machine_ok") is False:
            trace_violations.append(r.case.id)
        dup_fetch_total += int(tm.get("dup_fetch_count", 0))
        finish_retry_total += max(int(tm.get("finish_attempts", 0)) - 1, 0)

    return {
        "total": total,
        "passed": passed,
        "failed": total - passed - skipped,
        "skipped": skipped,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "hard_failures": hard_failures,
        "errors": errors,
        "source_status_distribution": source_status,
        "quality_avg": (round(sum(quality_scores) / len(quality_scores), 1)
                        if quality_scores else None),
        "trace": {
            "state_machine_violations": trace_violations,
            "dup_fetch_total": dup_fetch_total,
            "finish_retry_total": finish_retry_total,
        },
    }
