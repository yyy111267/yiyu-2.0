"""
会话持久化仓库 - 支持中断恢复。

把 AgentState 序列化到 SQLite（复用 store/models.py 的 Base），
刷新/断网后可从检查点恢复，继续未完成的研究。

表结构：SessionCheckpoint（独立于业务表，只存运行时状态）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, String, Text, create_engine, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from store.models import Base
from runtime.state import AgentState, AgentPhase

logger = logging.getLogger(__name__)


class SessionCheckpoint(Base):
    """会话检查点表。"""
    __tablename__ = "session_checkpoints"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    phase: Mapped[str] = mapped_column(String, default="idle")
    payload: Mapped[str] = mapped_column(Text, default="{}")  # 序列化的 to_checkpoint()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SessionRepo:
    """
    会话仓库：保存 / 恢复 / 删除 AgentState 检查点。

    用法：
        repo = SessionRepo("sqlite:///yiyu.db")
        repo.save(state)           # 每轮循环后保存
        state = repo.load(sid)     # 恢复
    """

    def __init__(self, db_url: str = "sqlite:///yiyu_agent.db"):
        self.engine = create_engine(db_url, echo=False, future=True)
        Base.metadata.create_all(self.engine)
        self._Session = sessionmaker(self.engine, expire_on_commit=False)

    def save(self, state: AgentState) -> None:
        """保存（或更新）会话检查点。"""
        checkpoint = state.to_checkpoint()
        payload = json.dumps(checkpoint, ensure_ascii=False, default=str)
        with self._Session() as db:
            existing = db.get(SessionCheckpoint, state.session_id)
            if existing:
                existing.phase = state.phase.value
                existing.payload = payload
                existing.user_id = state.user_id
                existing.updated_at = datetime.utcnow()
            else:
                db.add(SessionCheckpoint(
                    session_id=state.session_id,
                    user_id=state.user_id,
                    phase=state.phase.value,
                    payload=payload,
                ))
            db.commit()
        logger.debug(f"检查点已保存: {state.session_id} @ phase={state.phase.value}")

    def load(self, session_id: str) -> Optional[AgentState]:
        """从检查点恢复 AgentState。不存在返回 None。"""
        with self._Session() as db:
            row = db.get(SessionCheckpoint, session_id)
            if row is None:
                return None
            try:
                data = json.loads(row.payload)
                return AgentState.from_checkpoint(data)
            except Exception as e:
                logger.error(f"恢复检查点失败 {session_id}: {e}")
                return None

    def delete(self, session_id: str) -> bool:
        """删除会话检查点。"""
        with self._Session() as db:
            row = db.get(SessionCheckpoint, session_id)
            if row is None:
                return False
            db.delete(row)
            db.commit()
            return True

    def list_sessions(self, user_id: Optional[str] = None) -> list[dict]:
        """列出会话（可按 user_id 过滤）。"""
        with self._Session() as db:
            stmt = select(SessionCheckpoint)
            if user_id:
                stmt = stmt.where(SessionCheckpoint.user_id == user_id)
            stmt = stmt.order_by(SessionCheckpoint.updated_at.desc())
            rows = db.execute(stmt).scalars().all()
            return [
                {
                    "session_id": r.session_id,
                    "user_id": r.user_id,
                    "phase": r.phase,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]
