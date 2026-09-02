"""SSE 聊天端点：把用户请求路由到 Skill，交给 AgentLoop 流式产出结论。

协议：前端 POST /api/chat，body 为 {message, session_id?, skill?}；
服务以 text/event-stream 持续推送 data 帧，每帧为 AgentEvent.to_json() 的 JSON 字符串：
    {"type": "start"|"thought"|"tool_call"|"tool_result"|"master_evidence"|
            "master_conclusion"|"final_answer"|"error"|"complete",
     "content": "...", "metadata": {...}, "timestamp": "..."}
前端按 event.data 解析后读取 type/content 即可。
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from api.deps import get_current_user
from core.config import settings
from runtime.boundary import OUT_OF_SCOPE, boundary_message
from runtime.preloop import run_preloop
from runtime.router import route_async
from runtime.skill_availability import is_skill_available

logger = logging.getLogger(__name__)

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    skill: Optional[str] = None  # 可选：显式指定技能，缺省时自动路由
    # user_id 已移除：改由 token 鉴权推导，绝不信任客户端上报


class MemoryConfirmRequest(BaseModel):
    cards: list[dict]
    source_task_id: str = ""


def _cognition_store():
    from core.config import get_config
    from store.cognition_store import CognitionStore
    return CognitionStore(get_config().database_url.replace("+aiosqlite", ""))


def _conversation_repo():
    """对话记录仓库：历史列表与回放的数据源（与运行中断恢复的检查点分离）。"""
    from core.config import get_config
    from store.repos.conversation_repo import ConversationRepo
    return ConversationRepo(get_config().database_url.replace("+aiosqlite", ""))


@router.get("/memory/me")
async def list_memory(user: dict = Depends(get_current_user)):
    """供认知库管理页读取；只返回当前登录用户的条目。"""
    return {"items": _cognition_store().repo.list_atoms(user["user_id"], limit=500)}


@router.post("/memory/confirm")
async def confirm_memory_cards(req: MemoryConfirmRequest, user: dict = Depends(get_current_user)):
    """确认卡唯一写入口：确认/编辑后确认才会写 active。user_id 从 token 推导。"""
    results = _cognition_store().confirm_cards(
        req.cards, user_id=user["user_id"], source_task_id=req.source_task_id,
    )
    from core.audit import get_audit
    get_audit().log("confirm_memory", user_id=user["user_id"],
                    detail={"count": len(req.cards), "task_id": req.source_task_id})
    return {"results": results}


class MemoryUpdateRequest(BaseModel):
    """PATCH 语义：只传需更新的字段。user_id 从 token 推导。"""
    fields: dict


@router.patch("/memory/{atom_id}")
async def update_memory(atom_id: str, req: MemoryUpdateRequest, user: dict = Depends(get_current_user)):
    """编辑认知条目字段（statement/content/status 等）。严格租户隔离。"""
    updated = _cognition_store().repo.update_atom(
        atom_id, user_id=user["user_id"], fields=req.fields,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="not_found_or_forbidden")
    from core.audit import get_audit
    get_audit().log("update_memory", user_id=user["user_id"], detail={"atom_id": atom_id})
    return {"ok": True, "item": updated}


@router.delete("/memory/{atom_id}")
async def delete_memory(atom_id: str, user: dict = Depends(get_current_user)):
    """硬删除认知条目。严格租户隔离：非本人条目返回 False。"""
    ok = _cognition_store().repo.delete_atom(atom_id, user_id=user["user_id"])
    if ok:
        from core.audit import get_audit
        get_audit().log("delete_memory", user_id=user["user_id"], detail={"atom_id": atom_id})
    return {"ok": ok}


@router.get("/memory/{atom_id}/usage")
async def get_memory_usage(atom_id: str, user: dict = Depends(get_current_user)):
    """引用追溯：查认知条目的来源会话与注入状态。无权返回 404。"""
    usage = _cognition_store().repo.get_usage(atom_id, user_id=user["user_id"])
    if usage is None:
        raise HTTPException(status_code=404, detail="not_found_or_forbidden")
    return {"ok": True, **usage}


@router.get("/memory/export")
async def export_memory(user: dict = Depends(get_current_user)):
    """数据导出：用户可导出自己的全部认知库（GDPR 式最小要求）。"""
    items = _cognition_store().repo.list_atoms(user["user_id"], limit=10000)
    from core.audit import get_audit
    get_audit().log("export_memory", user_id=user["user_id"])
    return {"ok": True, "user_id": user["user_id"], "email": user.get("email", ""), "items": items}


@router.delete("/memory/all")
async def delete_all_memory(user: dict = Depends(get_current_user)):
    """数据删除出口：清空当前用户全部认知库。不可撤销。"""
    repo = _cognition_store().repo
    items = repo.list_atoms(user["user_id"], limit=10000)
    for item in items:
        repo.delete_atom(item["id"], user_id=user["user_id"])
    from core.audit import get_audit
    get_audit().log("delete_all_memory", user_id=user["user_id"], detail={"count": len(items)})
    return {"ok": True, "deleted": len(items)}


@router.post("/chat")
async def chat_endpoint(req: ChatRequest, request: Request, user: dict = Depends(get_current_user)):
    factory = getattr(request.app.state, "agent_loop_factory", None)
    agent_loop = factory() if factory else request.app.state.agent_loop

    # 会话与租户边界必须在任何记忆读取前确定。
    # user_id 从 token 推导，绝不信任客户端上报。
    session_id = req.session_id or f"session-{uuid.uuid4().hex[:12]}"
    user_id = user["user_id"]

    # 研究任务限流（P0）：每用户每日上限，超限拒绝。
    from core.rate_limit import get_rate_limiter
    allowed, count = get_rate_limiter().check_and_incr(user_id)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={
                "detail": "今日研究次数已达上限，请明日再来。"
                          "如有更多需求，联系管理员调整额度。",
                "today_count": count,
            },
        )

    from runtime.run_trace import RunTraceRecorder
    run_trace = RunTraceRecorder(
        session_id,
        enabled=settings.trace_enabled,
        trace_dir=settings.trace_dir,
    )
    run_trace.mark("request_accepted", explicit_skill=bool(req.skill))
    if run_trace.enabled:
        logger.info("[chat] run trace: %s", run_trace.path)

    # 会话记录（历史回放数据源）：SSE 开始前先落用户消息，
    # 标题取首条 query 的清洗截断结果，同一 session_id 的后续提问只追加、不覆盖。
    conv = _conversation_repo()
    history = conv.recent_messages(session_id, user_id=user_id, limit=6)
    try:
        from store.repos.conversation_repo import ConversationRepo
        conv.ensure(session_id, user_id, ConversationRepo.make_title(req.message))
        conv.add_message(session_id, user_id, "user", req.message)
    except Exception as exc:  # noqa: BLE001 - 落库失败不阻断本次研究
        logger.warning("[chat] 用户消息落库失败: %s", exc)

    assistant_saved = {"done": False}

    def save_assistant(content: str, metadata: Optional[dict] = None) -> None:
        """落一条助手消息：只记第一次最终回答；失败/降级同样入库，保证历史可回放。"""
        if assistant_saved["done"] or not (content or "").strip():
            return
        assistant_saved["done"] = True
        try:
            safe_meta = json.loads(json.dumps(metadata or {}, ensure_ascii=False, default=str))
            conv.add_message(session_id, user_id, "assistant", content, safe_meta)
        except Exception as exc:  # noqa: BLE001 - 落库失败不影响本次回答
            logger.warning("[chat] 助手消息落库失败: %s", exc)

    async def event_generator():
        from core.llm import capture_llm_usage, llm_usage_stage
        request_hard_deadline = (
            time.monotonic() + settings.research_hard_timeout_seconds
        )
        usage_capture = capture_llm_usage(run_trace.record_llm_usage)
        usage_capture.__enter__()
        try:
            # 必须先建立 SSE，再做任何可能调用 LLM 的路由/前处理。
            # 这样即使模型慢，前端也会在 1 秒内看到已受理状态。
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "accepted",
                    "content": "",
                    "metadata": {
                        "stage": "understand_question",
                        "status": "running",
                        "session_id": session_id,
                    },
                    "timestamp": time.time(),
                }, ensure_ascii=False),
            }

            # 1) 意图路由：显式指定 > 规则快速通道 > LLM 语义 > 默认轻回答
            run_trace.mark("routing_started")
            routing_started = time.monotonic()
            with llm_usage_stage("routing"):
                routed = await route_async(
                    llm_client=request.app.state.llm_client,
                    user_message=req.message,
                    explicit_skill=req.skill,
                    history=history,  # 追问时的实体/意图消歧依据（同会话最近几轮）
                )
            skill_name = routed.skill
            run_trace.mark(
                "routing_finished",
                duration_sec=round(time.monotonic() - routing_started, 3),
                skill=skill_name or "light_answer",
                route_result=routed.route_result,
                matched_by=getattr(routed, "matched_by", ""),
            )

            # 对外只透传阶段摘要：不暴露 skill / matched_by / confidence
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "routing",
                    "content": "",
                    "metadata": {
                        "stage": "understand_question",
                        "status": "running",
                        "route_result": routed.route_result,
                        "suggest_research": bool(getattr(routed, "suggest_research", False)),
                        "session_id": session_id,
                    },
                    "timestamp": time.time(),
                }, ensure_ascii=False),
            }

            if routed.route_result == OUT_OF_SCOPE:
                reason = (routed.route_reason.rsplit("：", 1)[-1] or "non_research")
                boundary_text = boundary_message(reason)
                save_assistant(boundary_text, {"route_result": OUT_OF_SCOPE, "boundary_reason": reason})
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "type": "final_answer",
                        "content": boundary_text,
                        "metadata": {
                            "validated": True,
                            "route_result": OUT_OF_SCOPE,
                            "boundary_reason": reason,
                            "session_id": session_id,
                        },
                        "timestamp": time.time(),
                    }, ensure_ascii=False),
                }
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "type": "complete",
                        "content": "",
                        "metadata": {},
                    }, ensure_ascii=False),
                }
                return

            if not is_skill_available(skill_name):
                save_assistant(boundary_message("unsupported_task"), {"unsupported_skill": skill_name})
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "type": "final_answer",
                        "content": boundary_message("unsupported_task"),
                        "metadata": {
                            "validated": True,
                            "unsupported_skill": skill_name,
                            "session_id": session_id,
                        },
                        "timestamp": time.time(),
                    }, ensure_ascii=False),
                }
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "type": "complete",
                        "content": "",
                        "metadata": {},
                    }, ensure_ascii=False),
                }
                return

            # 研究类任务：先跑 preloop 四件套（事实包/粒度/画像/计划），再进循环。
            # 失败降级为无计划执行（loop 内部回退轻量 Planner），不阻塞主流程。
            research_plan = None
            initial_context = None
            if skill_name == "deep-research":
                try:
                    candidate = routed.entity_candidates[0] if routed.entity_candidates else None
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "preloop_progress",
                            "content": "",
                            "metadata": {
                                "stage": "understand_question",
                                "status": "running",
                                "elapsed_sec": 0,
                            },
                            "timestamp": time.time(),
                        }, ensure_ascii=False),
                    }
                    preloop_started = time.monotonic()
                    run_trace.mark("preloop_started")
                    with llm_usage_stage("preloop"):
                        preloop_task = asyncio.create_task(run_preloop(
                                req.message,
                                llm_client=request.app.state.llm_client,
                                market_data=request.app.state.market_data,
                                web_search_fn=request.app.state.web_search_fn,
                                candidate=candidate,
                                user_id=user_id,
                                fast_mode=True,
                            )
                        )
                    while not preloop_task.done():
                        elapsed_raw = time.monotonic() - preloop_started
                        remaining = settings.preloop_timeout_seconds - elapsed_raw
                        if remaining <= 0:
                            preloop_task.cancel()
                            try:
                                await preloop_task
                            except asyncio.CancelledError:
                                pass
                            raise asyncio.TimeoutError
                        done, _ = await asyncio.wait(
                            {preloop_task}, timeout=min(8, remaining),
                        )
                        if done:
                            break
                        elapsed = round(time.monotonic() - preloop_started)
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "type": "preloop_progress",
                                "content": "",
                                "metadata": {
                                    "stage": "understand_question",
                                    "status": "running",
                                    "elapsed_sec": elapsed,
                                },
                                "timestamp": time.time(),
                            }, ensure_ascii=False),
                        }
                    preloop = await preloop_task
                    run_trace.mark(
                        "preloop_finished",
                        duration_sec=round(time.monotonic() - preloop_started, 3),
                        facts_version=getattr(preloop.facts, "facts_version", ""),
                        p0_count=len(preloop.plan.p0_questions),
                    )
                    research_plan = preloop.plan
                    initial_context = dict(preloop.initial_context)
                    initial_context["latency_mode"] = True
                    # 对外只发分析重点（问题文本 + 中文状态），不下发四件套内部结构
                    from runtime.visibility import _plan_public_payload
                    plan_meta = {"stage": "understand_question", "status": "done", "session_id": session_id}
                    plan_meta.update(_plan_public_payload(preloop.plan))
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "preloop",
                            "content": "",
                            "metadata": plan_meta,
                        }, ensure_ascii=False),
                    }
                except asyncio.TimeoutError:
                    run_trace.mark(
                        "preloop_timeout",
                        duration_sec=round(time.monotonic() - preloop_started, 3),
                        timeout_sec=settings.preloop_timeout_seconds,
                    )
                    logger.warning(
                        "[chat] preloop 超过 %.0fs，降级进入 Agent 主循环",
                        settings.preloop_timeout_seconds,
                    )
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "preloop_progress",
                            "content": "准备阶段未在时限内完成，已跳过并继续研究。",
                            "metadata": {
                                "stage": "understand_question",
                                "status": "degraded",
                                "reason": "preloop_timeout",
                            },
                            "timestamp": time.time(),
                        }, ensure_ascii=False),
                    }
                    initial_context = {
                        "preloop_degraded": True,
                        "preloop_degraded_reason": "timeout",
                    }
                except Exception as exc:  # noqa: BLE001 - preloop 失败降级，不阻断
                    run_trace.mark(
                        "preloop_failed",
                        duration_sec=round(time.monotonic() - preloop_started, 3),
                        error_type=type(exc).__name__, error=str(exc),
                    )
                    logger.warning("[chat] preloop 失败，降级为无计划执行: %s", exc)

            from runtime.events import EventType
            from runtime.visibility import to_public_event

            # 请求级 hard deadline 从 SSE 被受理时开始计时，preloop 已消耗的
            # 时间不会在 AgentLoop 中重新获得。
            initial_context = dict(initial_context or {})
            initial_context["_request_hard_deadline_monotonic"] = request_hard_deadline

            async for evt in agent_loop.run(
                user_message=req.message,
                session_id=session_id,
                skill_name=skill_name,
                research_plan=research_plan,
                initial_context=initial_context,
                user_id=user_id,
            ):
                run_trace.record_event(evt)
                # 对外进度摘要层：内部事件归并为 progress，白名单收口
                public = to_public_event(evt, plan=research_plan)
                if public is None:
                    continue
                if public.type == EventType.FINAL_ANSWER:
                    # 以服务端全文为准：硬规则可能已修正流式文本，这里落库的即回放内容
                    save_assistant(public.content, public.metadata)
                yield {
                    "event": "message",
                    "data": public.to_json(),
                }
        except Exception as exc:  # 兜底：避免 SSE 连接悬挂
            run_trace.mark(
                "request_failed", error_type=type(exc).__name__, error=str(exc),
            )
            logger.exception("[chat] 未捕获异常: %s", exc)
            err_text = "服务暂时不可用，请稍后重试。"
            save_assistant(err_text, {"degraded": True, "error": True})
            err = {
                "type": "error",
                "content": err_text,
                "metadata": {},
            }
            yield {"event": "message", "data": json.dumps(err, ensure_ascii=False)}
        finally:
            run_trace.mark(
                "request_closed", answer_chars=run_trace.answer_chars,
                token_ledger=run_trace.token_ledger(),
            )
            usage_capture.__exit__(None, None, None)
            # 兜底：连接中断/异常导致一个 final_answer 都没发出时，
            # 也留一条用户可见的记录，避免历史里只有提问没有回答。
            if not assistant_saved["done"]:
                save_assistant(
                    "本次研究未能在时间上限内给出完整结论，已停止，不输出买卖结论。\n\n"
                    "建议稍后重试，或把问题缩小为一个维度（例如只看估值或增长）。",
                    {"degraded": True, "incomplete": True},
                )

    return EventSourceResponse(event_generator())
