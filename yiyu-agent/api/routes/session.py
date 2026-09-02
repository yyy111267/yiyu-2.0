"""会话管理端点：列表 / 详情 / 删除（历史回放入口）。

历史列表与回放的唯一数据源是 conversations + conversation_messages
（见 store/repos/conversation_repo.py）。session_checkpoints 只负责运行中断恢复，
不再承担聊天落库；详情里的 checkpoint 仅在确实存在时返回。

严格租户隔离：所有操作按当前登录 user_id 过滤，跨用户访问返回 404。
"""

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_current_user

router = APIRouter()


def _session_repo():
    from core.config import get_config
    from store.repos.session_repo import SessionRepo
    return SessionRepo(get_config().database_url.replace("+aiosqlite", ""))


def _conversation_repo():
    from core.config import get_config
    from store.repos.conversation_repo import ConversationRepo
    return ConversationRepo(get_config().database_url.replace("+aiosqlite", ""))


@router.get("/sessions")
async def list_sessions(user: dict = Depends(get_current_user)):
    """列出当前用户的历史会话（带真实标题与消息条数）。"""
    return {"items": _conversation_repo().list_conversations(user["user_id"])}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, user: dict = Depends(get_current_user)):
    """拉取会话详情用于回放：标题 + 消息列表。无权访问返回 404。"""
    conv = _conversation_repo().get(session_id, user_id=user["user_id"])
    if conv is None:
        raise HTTPException(status_code=404, detail="not_found_or_forbidden")
    state = _session_repo().load(session_id, user_id=user["user_id"])
    return {
        "ok": True,
        "session_id": session_id,
        "title": conv["title"],
        "updated_at": conv["updated_at"],
        "messages": conv["messages"],
        "checkpoint": state.to_checkpoint() if state is not None else None,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    """删除会话（对话记录 + 检查点）。无权访问返回 False。"""
    ok_conv = _conversation_repo().delete(session_id, user_id=user["user_id"])
    ok_cp = _session_repo().delete(session_id, user_id=user["user_id"])
    return {"ok": bool(ok_conv or ok_cp)}
