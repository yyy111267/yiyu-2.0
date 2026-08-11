"""
评测执行器。

两种模式：
- live：真实装配 AgentLoop（需 LLM API Key + .env），跑完整链路收集结论与观测。
- offline：不调 LLM，直接用给定的结论文本做判定（用于开发/回归判定器本身）。

产出每个用例的 CaseReport：
    用例元数据 + 关键词判定 + LLM 行为判定 + 三大指标 + 结论摘要 + 错误。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .metrics import TraceStats, compute_metrics
from .quality import QualityJudge, QualityReport
from .schema import EvalCase
from .judges import KeywordJudge, LLMBehaviorJudge

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

    def to_dict(self) -> dict:
        return {
            "id": self.case.id,
            "name": self.case.name,
            "skill": self.case.skill,
            "passed": self.passed,
            "skipped": self.skipped,
            "error": self.error,
            "conclusion": self.conclusion[:500],
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
        offline_conclusion: Optional[str] = None,
    ) -> None:
        self.mode = mode
        self.llm = llm_client
        self.loop = loop
        # live 模式下 lazy 装配（参考 api/main.py 的 lifespan）
        self.assembler = assembler
        self.executor = tool_executor
        self.spawner = agent_spawner
        self.offline_conclusion = offline_conclusion or ""

        self.keyword_judge = KeywordJudge()
        self.behavior_judge = LLMBehaviorJudge(llm_client) if llm_client else None
        self.quality_judge = QualityJudge()

    # ── 公开接口 ─────────────────────────────────────────────────────

    async def run_case(self, case: EvalCase) -> CaseReport:
        if self.mode == "offline":
            return await self._run_offline(case)
        return await self._run_live(case)

    async def run_all(self, cases: list[EvalCase]) -> list[CaseReport]:
        reports = []
        for case in cases:
            try:
                reports.append(await self.run_case(case))
            except Exception as e:
                logger.exception(f"用例执行异常 {case.id}: {e}")
                reports.append(CaseReport(case=case, error=f"执行异常: {e}"))
        return reports

    # ── live：真实跑 AgentLoop ───────────────────────────────────────

    async def _run_live(self, case: EvalCase) -> CaseReport:
        from runtime.loop import AgentLoop, LoopConfig
        from runtime.state import AgentState
        from runtime.events import EventType

        loop = self.loop or self._build_loop()
        stats = TraceStats()
        conclusion = ""
        start = time.time()

        events: list[dict[str, Any]] = []
        validated: Optional[bool] = None
        async for evt in loop.run(
            user_message=case.input_message,
            session_id=f"eval-{case.id}",
            skill_name=case.skill,
        ):
            events.append({"type": evt.type.value, "content": evt.content})
            if evt.type == EventType.FINAL_ANSWER:
                conclusion = evt.content or conclusion
                validated = evt.metadata.get("validated")
                stats.tokens_used = evt.metadata.get("tokens_used", stats.tokens_used)
            if evt.type == EventType.TOOL_RESULT:
                stats.tool_sources.append(str(evt.metadata.get("tool_name", "")))
            if evt.type == EventType.TOOL_CALL:
                stats.tool_calls += 1

        stats.duration_sec = time.time() - start
        stats.conclusion = conclusion
        stats.events = events
        stats.steps = len([e for e in events if e["type"] == "thought"])
        stats.hard_rules_passed = validated

        report = CaseReport(
            case=case,
            conclusion=conclusion,
            metrics=compute_metrics(stats),
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
                Path(__file__).resolve().parent.parent / "prompts"))
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
        report = CaseReport(case=case, conclusion=conclusion)
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
        return kw_ok and bh_ok and q_ok


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
    }
