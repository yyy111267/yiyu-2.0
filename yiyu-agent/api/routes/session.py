"""
会话管理端点
"""

from fastapi import APIRouter

router = APIRouter()


@router.post("/sessions")
async def create_session():
    """创建新会话"""
    import uuid
    return {
        "session_id": str(uuid.uuid4()),
        "created_at": "2024-01-01T00:00:00Z",
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """获取会话信息"""
    return {
        "session_id": session_id,
        "status": "active",
        "messages_count": 0,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除会话"""
    return {"status": "deleted"}
