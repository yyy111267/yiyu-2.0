"""对话记录仓库 —— 历史列表与回放的数据源。

与 store/repos/session_repo.py 的 Agent 检查点彻底分离：
- session_checkpoints：运行时中断恢复（payload 是 AgentState，不是聊天内容）
- conversations + conversation_messages：用户说了什么、AI 回了什么

严格租户隔离：所有读写带 user_id，跨用户访问返回 None / False。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from store.models import Base, Conversation, ConversationMessage

logger = logging.getLogger(__name__)

# 标题清洗：去掉客套开头与句末语气词，只保留问题主体
_FILLER_PREFIX = (
    "帮我分析一下", "帮我看一下", "帮我看看", "帮我看下", "帮我分析", "帮我说说", "帮我",
    "请帮我分析一下", "请帮我", "请分析一下", "请分析", "请",
    "麻烦你", "麻烦",
    "我想问一下", "我想问", "我想知道", "我想了解", "我想看看", "我想",
    "能不能帮我", "能不能", "能否", "可以帮我", "可不可以",
)
_TITLE_MAX = 28


class ConversationRepo:
    """会话与消息的持久化。

    用法：
        repo = ConversationRepo("sqlite:///yiyu_agent.db")
        repo.ensure(sid, user_id, ConversationRepo.make_title(query))
        repo.add_message(sid, user_id, "user", query)
        repo.add_message(sid, user_id, "assistant", answer, {"citations": [...]})
    """

    def __init__(self, db_url: str = "sqlite:///yiyu_agent.db"):
        self.engine = create_engine(db_url, echo=False, future=True)
        Base.metadata.create_all(self.engine)
        self._Session = sessionmaker(self.engine, expire_on_commit=False)

    # ── 标题 ────────────────────────────────────────────────

    @staticmethod
    def make_title(text: str, limit: int = _TITLE_MAX) -> str:
        """首条 query → 侧边栏标题：清洗后截断，不额外调模型（不阻塞首次回答）。"""
        s = re.sub(r"\s+", " ", str(text or "")).strip()
        for prefix in sorted(_FILLER_PREFIX, key=len, reverse=True):
            if s.startswith(prefix) and len(s) > len(prefix):
                s = s[len(prefix):].strip()
                break
        s = s.strip("，,。.！!？?~～、 ")
        if len(s) > limit:
            s = s[:limit].rstrip("，,。.、 ") + "…"
        return s or "新对话"

    # ── 写 ──────────────────────────────────────────────────

    def ensure(self, conversation_id: str, user_id: str, title: str = "") -> None:
        """首次出现该会话时创建；已存在则不动标题（标题只来自首条 query）。"""
        with self._Session() as db:
            row = db.get(Conversation, conversation_id)
            if row is not None:
                return
            db.add(Conversation(
                id=conversation_id,
                user_id=user_id,
                title=title or "新对话",
            ))
            db.commit()

    def add_message(self, conversation_id: str, user_id: str, role: str,
                    content: str, metadata: Optional[dict] = None) -> Optional[str]:
        """追加一条消息；会话不存在时按给定标题自动创建。"""
        with self._Session() as db:
            row = db.get(Conversation, conversation_id)
            if row is None:
                row = Conversation(
                    id=conversation_id,
                    user_id=user_id,
                    title=self.make_title(content) if role == "user" else "新对话",
                )
                db.add(row)
            elif row.user_id != user_id:
                return None  # 跨用户写拒绝
            msg_id = f"m-{uuid.uuid4().hex[:16]}"
            db.add(ConversationMessage(
                id=msg_id,
                conversation_id=conversation_id,
                user_id=user_id,
                role=role,
                content=content or "",
                metadata_=metadata or {},
            ))
            row.updated_at = datetime.utcnow()
            db.commit()
            return msg_id

    # ── 读 ──────────────────────────────────────────────────

    def list_conversations(self, user_id: str, limit: int = 100) -> list[dict]:
        """历史列表：最近更新的在前，带消息条数。"""
        with self._Session() as db:
            counts = (
                select(
                    ConversationMessage.conversation_id,
                    func.count(ConversationMessage.id).label("n"),
                )
                .group_by(ConversationMessage.conversation_id)
                .subquery()
            )
            rows = db.execute(
                select(Conversation, func.coalesce(counts.c.n, 0))
                .outerjoin(counts, counts.c.conversation_id == Conversation.id)
                .where(Conversation.user_id == user_id)
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
            ).all()
            return [
                {
                    "session_id": c.id,
                    "title": c.title,
                    "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                    "message_count": int(n or 0),
                }
                for c, n in rows
            ]

    def get(self, conversation_id: str, *, user_id: str) -> Optional[dict]:
        """会话详情（含消息）。不存在/不属于该用户返回 None。"""
        with self._Session() as db:
            row = db.get(Conversation, conversation_id)
            if row is None or row.user_id != user_id:
                return None
            msgs = db.execute(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conversation_id)
                .order_by(ConversationMessage.created_at.asc(), ConversationMessage.id.asc())
            ).scalars().all()
            return {
                "session_id": row.id,
                "title": row.title,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "messages": [
                    {
                        "role": m.role,
                        "content": m.content,
                        "metadata": m.metadata_ or {},
                        "created_at": m.created_at.isoformat() if m.created_at else None,
                    }
                    for m in msgs
                ],
            }

    def recent_messages(self, conversation_id: str, *, user_id: str, limit: int = 6) -> list[dict]:
        """最近若干条消息，供路由/追问时带上对话上下文。"""
        conv = self.get(conversation_id, user_id=user_id)
        if conv is None:
            return []
        return conv["messages"][-limit:]

    # ── 删 ──────────────────────────────────────────────────

    def delete(self, conversation_id: str, *, user_id: str) -> bool:
        """删除会话及其消息。不属于该用户返回 False。"""
        with self._Session() as db:
            row = db.get(Conversation, conversation_id)
            if row is None or row.user_id != user_id:
                return False
            db.execute(
                ConversationMessage.__table__.delete()
                .where(ConversationMessage.conversation_id == conversation_id)
            )
            db.delete(row)
            db.commit()
            return True
