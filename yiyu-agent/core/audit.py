"""审计日志：关键操作落表，供事后追溯。

记录：登录/验码/认知写入/删除/会话恢复/限流触发。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, create_engine
from sqlalchemy.orm import Mapped, mapped_column

from core.config import settings
from store.models import Base

logger = logging.getLogger(__name__)


class AuditLog(Base):
    """审计日志表。"""
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True, default="")
    action: Mapped[str] = mapped_column(String, index=True)  # login/confirm_memory/delete_memory/...
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLogger:
    def __init__(self, db_url: str | None = None) -> None:
        url = db_url or settings.database_url.replace("+aiosqlite", "")
        self.engine = create_engine(url, future=True)
        Base.metadata.create_all(self.engine)
        from sqlalchemy.orm import sessionmaker
        self._Session = sessionmaker(self.engine, expire_on_commit=False)

    def log(self, action: str, *, user_id: str = "", detail: dict | None = None, ip: str = "") -> None:
        """记一条审计日志。失败不阻断主流程。"""
        try:
            with self._Session() as db:
                db.add(AuditLog(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    action=action,
                    detail=detail or {},
                    ip=ip,
                ))
                db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[audit] 记录失败 action=%s: %s", action, exc)


_logger: AuditLogger | None = None


def get_audit() -> AuditLogger:
    global _logger  # noqa: PLW0603
    if _logger is None:
        _logger = AuditLogger()
    return _logger
