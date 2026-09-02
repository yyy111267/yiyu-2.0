"""用户级研究任务限流（P0 上线必须）。

一次研究任务预算 50k token，恶意/滥用用户可刷爆 LLM 账单。
落库计数：每用户每日研究任务数上限 + 滚动窗口计数。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import Integer, String, DateTime, create_engine, select
from sqlalchemy.orm import Mapped, mapped_column

from core.config import settings
from store.models import Base

logger = logging.getLogger(__name__)


class UsageCounter(Base):
    """用户每日用量计数表。按 (user_id, date) 聚合。"""
    __tablename__ = "usage_counters"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    date: Mapped[str] = mapped_column(String, index=True)  # YYYY-MM-DD
    research_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RateLimiter:
    """研究任务日额度限流器。"""

    def __init__(self, db_url: str | None = None) -> None:
        url = db_url or settings.database_url.replace("+aiosqlite", "")
        self.engine = create_engine(url, future=True)
        Base.metadata.create_all(self.engine)
        from sqlalchemy.orm import sessionmaker
        self._Session = sessionmaker(self.engine, expire_on_commit=False)

    def check_and_incr(self, user_id: str, daily_limit: int = 20) -> tuple[bool, int]:
        """检查并递增今日研究任务计数。

        Returns: (allowed, today_count)。allowed=False 表示超限。
        daily_limit 默认 20 次/日（轻回答 + 研究任务合计）。
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")
        with self._Session() as db:
            row = db.scalars(
                select(UsageCounter)
                .where(UsageCounter.user_id == user_id, UsageCounter.date == today)
                .limit(1)
            ).first()
            if row is None:
                row = UsageCounter(
                    id=str(uuid.uuid4()), user_id=user_id, date=today,
                    research_count=0,
                )
                db.add(row)
            count = row.research_count or 0
            if count >= daily_limit:
                db.commit()
                return False, count
            row.research_count = count + 1
            row.updated_at = datetime.utcnow()
            db.commit()
            return True, row.research_count

    def get_today_count(self, user_id: str) -> int:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        with self._Session() as db:
            row = db.scalars(
                select(UsageCounter)
                .where(UsageCounter.user_id == user_id, UsageCounter.date == today)
                .limit(1)
            ).first()
            return row.research_count or 0 if row else 0


# 全局单例
_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter  # noqa: PLW0603
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
