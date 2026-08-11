"""SSE 聊天端点：把用户请求路由到 Skill，交给 AgentLoop 流式产出结论。

协议：前端 POST /api/chat，body 为 {message, session_id?, skill?}；
服务以 text/event-stream 持续推送 data 帧，每帧为 AgentEvent.to_json() 的 JSON 字符串：
    {"type": "start"|"thought"|"tool_call"|"tool_result"|"master_evidence"|
            "master_conclusion"|"final_answer"|"error"|"complete",
     "content": "...", "metadata": {...}, "timestamp": "..."}
前端按 event.data 解析后读取 type/content 即可。
"""
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from runtime.router import route_async

logger = logging.getLogger(__name__)

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    skill: Optional[str] = None  # 可选：显式指定技能，缺省时自动路由


@router.post("/chat")
async def chat_endpoint(req: ChatRequest, request: Request):
    agent_loop = request.app.state.agent_loop

    # 1) 意图路由：显式指定 > LLM 语义 > 关键词 > 闲聊
    routed = await route_async(
        llm_client=request.app.state.llm_client,
        user_message=req.message,
        explicit_skill=req.skill,
    )
    skill_name = routed.skill

    # 2) 会话 id（缺省自动生成）
    session_id = req.session_id or f"session-{uuid.uuid4().hex[:12]}"

    async def event_generator():
        try:
            # 先透传路由决策（前端可展示"正在执行 xx 研究"）
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "routing",
                    "content": f"路由到 {skill_name or '通用对话'}（{routed.matched_by}）",
                    "metadata": {
                        "skill": skill_name,
                        "matched_by": routed.matched_by,
                        "confidence": routed.confidence,
                        "session_id": session_id,
                    },
                    "timestamp": time.time(),
                }, ensure_ascii=False),
            }

            async for evt in agent_loop.run(
                user_message=req.message,
                session_id=session_id,
                skill_name=skill_name,
            ):
                yield {
                    "event": "message",
                    "data": evt.to_json(),
                }
        except Exception as exc:  # 兜底：避免 SSE 连接悬挂
            logger.exception("[chat] 未捕获异常: %s", exc)
            err = {
                "type": "error",
                "content": f"服务异常：{exc}",
                "metadata": {},
            }
            yield {"event": "message", "data": json.dumps(err, ensure_ascii=False)}

    return EventSourceResponse(event_generator())
