"""
Badcase 仓库：评测失败用例自动入库，供定期复盘与迭代。

表结构：BadCase
    id            uuid
    eval_id       用例 id（eval01…）
    skill         用例归属 skill
    stage         live / offline
    error_type    失败类型（hard_fail / keyword / behavior / no_answer / error）
    conclusion    失败时的结论摘录（截断 2000 字）
    reason        失败原因
    metrics       指标 JSON
    created_at    入库时间
    fixed         是否已修复（复盘时标记）
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, String, Text, Boolean, create_engine, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from store.models import Base

logger = logging.getLogger(__name__)


class BadCase(Base):
    __tablename__ = "badcases"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    eval_id: Mapped[str] = mapped_column(String, index=True)
    skill: Mapped[str] = mapped_column(String, index=True, default="")
    stage: Mapped[str] = mapped_column(String, default="live")
    error_type: Mapped[str] = mapped_column(String, default="keyword")
    conclusion: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    fixed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BadcaseRepo:
    def __init__(self, db_url: str = "sqlite:///yiyu_agent.db"):
        self.engine = create_engine(db_url, echo=False, future=True)
        Base.metadata.create_all(self.engine)
        self._Session = sessionmaker(self.engine, expire_on_commit=False)

    def record(
        self,
        eval_id: str,
        error_type: str,
        reason: str,
        *,
        skill: str = "",
        stage: str = "live",
        conclusion: str = "",
        metrics: Optional[dict] = None,
    ) -> str:
        """记录一条失败用例，返回 badcase id。"""
        row = BadCase(
            eval_id=eval_id,
            skill=skill,
            stage=stage,
            error_type=error_type,
            conclusion=conclusion[:2000],
            reason=reason[:2000],
            metrics=metrics or {},
        )
        with self._Session() as db:
            db.add(row)
            db.commit()
            logger.info(f"[badcase] 入库 {eval_id} type={error_type} id={row.id}")
            return row.id

    def record_report(self, report: dict[str, Any]) -> Optional[str]:
        """从 CaseReport.to_dict() 自动判定失败类型并入库。"""
        if report.get("passed"):
            return None

        if report.get("error"):
            error_type, reason = "error", report["error"]
        elif report.get("keyword", {}).get("hard_fail"):
            error_type = "hard_fail"
            reason = "硬失败: " + "; ".join(report["keyword"]["hard_fail"])
        elif report.get("keyword") and report["keyword"].get("passed") is False:
            error_type = "keyword"
            reason = report["keyword"].get("detail", "关键词判定不通过")
        elif report.get("behavior", {}).get("passed") is False:
            error_type = "behavior"
            reason = "行为判定不通过"
        elif report.get("skipped"):
            error_type = "no_answer"
            reason = report.get("conclusion") and "未产出结论" or "被跳过"
        else:
            error_type, reason = "unknown", "未知失败原因"

        return self.record(
            eval_id=report.get("id", ""),
            skill=report.get("skill", ""),
            stage="live",
            error_type=error_type,
            reason=reason,
            conclusion=report.get("conclusion", ""),
            metrics=report.get("metrics"),
        )

    def list_unfixed(self, skill: Optional[str] = None, limit: int = 50) -> list[dict]:
        with self._Session() as db:
            stmt = select(BadCase).where(BadCase.fixed == False)  # noqa: E712
            if skill:
                stmt = stmt.where(BadCase.skill == skill)
            stmt = stmt.order_by(BadCase.created_at.desc()).limit(limit)
            return [
                {
                    "id": r.id,
                    "eval_id": r.eval_id,
                    "skill": r.skill,
                    "error_type": r.error_type,
                    "reason": r.reason,
                    "conclusion": r.conclusion[:200],
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in db.execute(stmt).scalars().all()
            ]

    def stats(self) -> dict[str, Any]:
        """按 error_type 统计失败分布。"""
        with self._Session() as db:
            rows = db.execute(
                select(BadCase.error_type, BadCase.fixed).order_by(BadCase.created_at)
            ).all()
            by_type: dict[str, int] = {}
            fixed = 0
            for et, f in rows:
                by_type[et] = by_type.get(et, 0) + 1
                if f:
                    fixed += 1
            return {"total": len(rows), "fixed": fixed, "by_type": by_type}

    def mark_fixed(self, badcase_id: str) -> bool:
        with self._Session() as db:
            row = db.get(BadCase, badcase_id)
            if row is None:
                return False
            row.fixed = True
            db.commit()
            return True
