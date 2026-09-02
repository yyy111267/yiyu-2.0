"""聊天任务阶段级 JSONL trace。

与 ``runtime.trace.Trace`` 的研究语义轨迹互补：这里记录 API 路由、preloop、
每轮 AgentEvent、异常原因和阶段耗时，即使任务在进入正式工具循环前失败也能归因。
不落用户原始问题、工具参数或完整回答正文。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from runtime.events import AgentEvent, EventType

logger = logging.getLogger(__name__)

_SAFE_META_KEYS = {
    "reason", "step", "tool_name", "success", "degraded", "validated",
    "duration_sec", "total_steps", "total_tokens", "research_complete",
    "budget_exhausted", "stop_reason", "soft_limit_reached",
    "soft_limit_reason", "synthesized_from_existing_evidence",
    "finalize_timeout", "hard_deadline_seconds",
    # step 级 token 埋点（input/output + 按字符分摊的结构占比）
    "llm_usage", "context_usage",
}


class RunTraceRecorder:
    """单任务 JSONL 记录器；写入失败只记日志，不影响研究主流程。"""

    def __init__(self, session_id: str, *, enabled: bool = True,
                 trace_dir: str = "./data/traces") -> None:
        self.enabled = enabled
        self.started_monotonic = time.monotonic()
        self.answer_chars = 0
        self._last_stream_mark = 0.0
        self._llm_calls: list[dict[str, Any]] = []
        safe_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", session_id or "session")[:96]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = Path(trace_dir) / f"{safe_id}-{stamp}.jsonl"
        if self.enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("[run-trace] 无法创建目录 %s: %s", self.path.parent, exc)
                self.enabled = False

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 3)

    def mark(self, name: str, **details: Any) -> None:
        if not self.enabled:
            return
        row = {
            "ts": time.time(),
            "elapsed_sec": self.elapsed,
            "name": name,
            "details": self._safe(details),
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.warning("[run-trace] 写入失败 %s: %s", self.path, exc)
            self.enabled = False

    def record_event(self, event: AgentEvent) -> None:
        if event.type == EventType.ANSWER_DELTA:
            self.answer_chars += len(event.content or "")
            now = time.monotonic()
            if now - self._last_stream_mark >= 5:
                self._last_stream_mark = now
                self.mark("llm_stream_progress", step=(event.metadata or {}).get("step"),
                          answer_chars=self.answer_chars)
            return
        meta = {
            key: value for key, value in (event.metadata or {}).items()
            if key in _SAFE_META_KEYS
        }
        details: dict[str, Any] = {"event_type": event.type.value, **meta}
        if event.type in {EventType.WARNING, EventType.ERROR}:
            details["message"] = (event.content or "")[:500]
            raw_error = (event.metadata or {}).get("error")
            if raw_error:
                details["error"] = str(raw_error)[:500]
        self.mark("agent_event", **details)

    def record_llm_usage(self, usage: dict[str, Any]) -> None:
        """LLM 能力层的统一账本入口；provider usage 是精确值。"""
        row = dict(usage)
        row["call"] = len(self._llm_calls) + 1
        self._llm_calls.append(row)
        self.mark("llm_call", **row)

    def token_ledger(self) -> dict[str, Any]:
        by_stage: dict[str, dict[str, int]] = {}
        for call in self._llm_calls:
            stage = str(call.get("stage") or "unknown")
            bucket = by_stage.setdefault(stage, {
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "cached_input_tokens": 0,
            })
            bucket["calls"] += 1
            for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"):
                bucket[key] += int(call.get(key, 0) or 0)
        return {
            "calls": len(self._llm_calls),
            "input_tokens": sum(int(c.get("input_tokens", 0) or 0) for c in self._llm_calls),
            "output_tokens": sum(int(c.get("output_tokens", 0) or 0) for c in self._llm_calls),
            "total_tokens": sum(int(c.get("total_tokens", 0) or 0) for c in self._llm_calls),
            "cached_input_tokens": sum(
                int(c.get("cached_input_tokens", 0) or 0) for c in self._llm_calls
            ),
            "by_stage": by_stage,
        }

    @staticmethod
    def _safe(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): RunTraceRecorder._safe(v) for k, v in value.items()
                    if str(k) not in {"arguments", "args", "content", "message_text"}}
        if isinstance(value, (list, tuple)):
            return [RunTraceRecorder._safe(v) for v in value[:20]]
        if isinstance(value, str):
            return value[:500]
        return value
