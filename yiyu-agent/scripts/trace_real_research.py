"""真实单请求端到端跟踪：路由 → preloop → AgentLoop → 最终结论。

不注入 mock，不固定实体，不替换行情/搜索/认知库工具。
只输出阶段、工具事件和最终答案，不输出模型隐藏思维链。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid

sys.path.insert(0, ".")

from agents.base import AgentSpawner
from core.config import settings
from core.llm import LLMClient, capture_llm_usage, llm_usage_stage
from runtime.assembler import PromptAssembler
from runtime.events import EventType
from runtime.loop import AgentLoop, LoopConfig
from runtime.preloop import run_preloop
from runtime.router import route_async
from runtime.run_trace import RunTraceRecorder
from toolkit import register_all
from toolkit.entity.resolver import close_ashare_cache
from toolkit.executor import ToolExecutor
from toolkit.market.market import MarketData
from toolkit.web.tools import WebSearchTool


def emit(label: str, value="") -> None:
    stamp = time.strftime("%H:%M:%S")
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    print(f"[{stamp}] {label} {value}", flush=True)


def compact(value, limit: int = 500) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) \
        if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= limit else text[:limit] + f"…(共{len(text)}字符)"


async def main(query: str) -> int:
    session_id = f"real-e2e-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    llm = LLMClient(settings)
    market_data = MarketData(settings)
    web_search = WebSearchTool()
    trace = RunTraceRecorder(
        session_id, enabled=True, trace_dir=settings.trace_dir,
    )

    register_all()
    usage_capture = capture_llm_usage(trace.record_llm_usage)
    usage_capture.__enter__()
    emit("TRACE", trace.path)
    emit("QUERY", query)
    emit("CONFIG", {
        "provider": llm.provider,
        "model": llm.model,
        "preloop_timeout": settings.preloop_timeout_seconds,
        "llm_timeout": settings.llm_request_timeout_seconds,
        "research_timeout": settings.research_timeout_seconds,
        "finalize_timeout": settings.finalize_timeout_seconds,
    })

    try:
        route_started = time.monotonic()
        trace.mark("routing_started")
        with llm_usage_stage("routing"):
            routed = await route_async(llm_client=llm, user_message=query)
        route_sec = time.monotonic() - route_started
        trace.mark(
            "routing_finished", duration_sec=round(route_sec, 3),
            skill=routed.skill, route_result=routed.route_result,
            matched_by=routed.matched_by,
        )
        emit("ROUTE", {
            "duration_sec": round(route_sec, 3),
            "route_result": routed.route_result,
            "skill": routed.skill,
            "matched_by": routed.matched_by,
            "entity_candidates": routed.entity_candidates,
        })

        candidate = routed.entity_candidates[0] if routed.entity_candidates else None
        preloop_started = time.monotonic()
        trace.mark("preloop_started")
        with llm_usage_stage("preloop"):
            preloop = await asyncio.wait_for(
                run_preloop(
                    query,
                    llm_client=llm,
                    market_data=market_data,
                    web_search_fn=web_search.execute,
                    candidate=candidate,
                    user_id="real-e2e-user",
                    fast_mode=True,
                ),
                timeout=settings.preloop_timeout_seconds,
            )
        preloop_sec = time.monotonic() - preloop_started
        trace.mark(
            "preloop_finished", duration_sec=round(preloop_sec, 3),
            facts_version=preloop.facts.facts_version,
            p0_count=len(preloop.plan.p0_questions),
        )
        emit("PRELOOP", {
            "duration_sec": round(preloop_sec, 3),
            "entity": preloop.entity.security_id,
            "company": preloop.entity.canonical_name,
            "info_richness": preloop.facts.info_richness.value,
            "adapter": [p.selected_adapter for p in preloop.profiles],
            "questions": [
                {
                    "id": q.id,
                    "priority": q.priority,
                    "data_requirement": q.data_requirement,
                    "question": q.question,
                }
                for q in preloop.plan.questions
            ],
        })

        context = dict(preloop.initial_context)
        context["latency_mode"] = True
        loop = AgentLoop(
            llm_client=llm,
            assembler=PromptAssembler(),
            tool_executor=ToolExecutor(),
            agent_spawner=AgentSpawner(),
            config=LoopConfig(
                max_steps=6,
                max_tool_calls=36,
                timeout_seconds=max(30, int(settings.research_timeout_seconds)),
                llm_timeout_seconds=settings.llm_request_timeout_seconds,
                finalize_timeout_seconds=settings.finalize_timeout_seconds,
                max_consecutive_llm_failures=2,
            ),
        )

        loop_started = time.monotonic()
        final_answer = ""
        async for event in loop.run(
            user_message=query,
            session_id=session_id,
            skill_name=routed.skill,
            research_plan=preloop.plan,
            initial_context=context,
            user_id="real-e2e-user",
        ):
            trace.record_event(event)
            meta = event.metadata or {}
            if event.type == EventType.TOOL_CALL:
                emit("TOOL_CALL", {
                    "tool": meta.get("tool_name"),
                    "arguments": meta.get("arguments", {}),
                })
            elif event.type == EventType.TOOL_RESULT:
                emit("TOOL_RESULT", {
                    "tool": meta.get("tool_name"),
                    "success": meta.get("success", True),
                    "preview": compact(event.content, 700),
                })
            elif event.type == EventType.WARNING:
                emit("WARNING", {
                    "reason": meta.get("reason"),
                    "message": event.content,
                })
            elif event.type == EventType.ERROR:
                emit("ERROR", {
                    "reason": meta.get("reason"),
                    "message": event.content,
                    "error": meta.get("error"),
                })
            elif event.type == EventType.FINAL_ANSWER:
                final_answer = event.content
                emit("FINAL_META", meta)
            elif event.type == EventType.COMPLETE:
                emit("COMPLETE", meta)

        loop_sec = time.monotonic() - loop_started
        total_sec = time.monotonic() - started
        trace.mark(
            "request_closed", duration_sec=round(total_sec, 3),
            answer_chars=len(final_answer), token_ledger=trace.token_ledger(),
        )
        emit("TIMING", {
            "route_sec": round(route_sec, 3),
            "preloop_sec": round(preloop_sec, 3),
            "loop_sec": round(loop_sec, 3),
            "total_sec": round(total_sec, 3),
        })
        emit("FINAL_ANSWER_START")
        print(final_answer, flush=True)
        emit("FINAL_ANSWER_END")
        return 0 if final_answer else 2
    except Exception as exc:
        trace.mark(
            "request_failed", error_type=type(exc).__name__, error=str(exc),
        )
        emit("FATAL", {
            "type": type(exc).__name__,
            "error": str(exc),
            "elapsed_sec": round(time.monotonic() - started, 3),
        })
        logging.exception("真实端到端跟踪失败")
        return 1
    finally:
        usage_capture.__exit__(None, None, None)
        for closer in (market_data.aclose, close_ashare_cache):
            try:
                await asyncio.wait_for(closer(), timeout=5)
            except Exception as exc:
                emit("CLOSE_WARNING", {
                    "type": type(exc).__name__,
                    "error": str(exc),
                })


if __name__ == "__main__":
    user_query = " ".join(sys.argv[1:]).strip() or "分析茅台"
    exit_code = asyncio.run(main(user_query))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
