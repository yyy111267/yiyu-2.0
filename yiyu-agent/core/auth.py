"""邮箱验证码登录：发码 / 验码 / token 签发 / token 校验。

单机 MVP 用服务端 session token（存 SQLite），不引入 JWT，可主动吊销。
严格租户隔离：token → user_id 一一对应，所有数据访问以 user_id 过滤。
"""

from __future__ import annotations

import logging
import secrets
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from core.config import settings
from store.models import AuthCode, Base, SessionToken, User

logger = logging.getLogger(__name__)


class AuthRepo:
    """鉴权数据访问层（SQLite，与 CognitionRepo 同构）。"""

    def __init__(self, db_url: str | None = None) -> None:
        from sqlalchemy import create_engine
        url = db_url or settings.database_url.replace("+aiosqlite", "")
        self.engine = create_engine(url, future=True)
        Base.metadata.create_all(self.engine)
        self._Session = sessionmaker(self.engine, expire_on_commit=False)
        self._migrate_user_email()

    def _migrate_user_email(self) -> None:
        """已有 users 表补 email 列（新库 create_all 已含）。"""
        from sqlalchemy import inspect, text
        insp = inspect(self.engine)
        if "users" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("users")}
        if "email" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR DEFAULT ''"))
            # SQLite 不支持 CREATE UNIQUE INDEX IF NOT EXISTS 加列后直接唯一，留作后续数据迁移
            logger.info("[auth] users.email 列已补，注意：历史数据 email 为空，需用户重新登录补全")

    # ── 发码 ─────────────────────────────────────────────────
    def create_code(self, email: str, ip: str = "") -> tuple[str, int] | None:
        """生成 6 位验证码；受同邮箱冷却 + 同 IP 日上限约束。

        返回 (code, ttl_seconds) 或 None（被限频拒绝）。
        """
        now = datetime.utcnow()
        cooldown = settings.auth_send_code_cooldown_seconds
        ip_limit = settings.auth_ip_daily_limit

        with self._Session() as db:
            # 同邮箱冷却：最近 cooldown 秒内已发且未用
            recent = db.scalars(
                select(AuthCode)
                .where(AuthCode.email == email, AuthCode.used.is_(False))
                .order_by(AuthCode.created_at.desc())
                .limit(1)
            ).first()
            if recent and (now - recent.created_at).total_seconds() < cooldown:
                return None

            # 同 IP 日上限
            today_start = now - timedelta(hours=24)
            ip_count = len(db.scalars(
                select(AuthCode).where(AuthCode.ip == ip, AuthCode.created_at >= today_start)
            ).all())
            if ip and ip_count >= ip_limit:
                logger.warning("[auth] IP %s 触发日发码上限", ip)
                return None

            # 同邮箱日上限（防枚举轰炸：同邮箱一天最多 5 次）
            email_count = len(db.scalars(
                select(AuthCode).where(AuthCode.email == email, AuthCode.created_at >= today_start)
            ).all())
            if email_count >= 5:
                logger.warning("[auth] 邮箱 %s 触发日发码上限", email)
                return None

            code = f"{secrets.randbelow(1000000):06d}"
            ttl_min = settings.auth_code_ttl_minutes
            ac = AuthCode(
                id=str(uuid.uuid4()), email=email, code=code, ip=ip,
                expires_at=now + timedelta(minutes=ttl_min),
            )
            db.add(ac)
            db.commit()
            return code, ttl_min * 60

    def get_cooldown_remaining(self, email: str) -> int:
        """查询该邮箱距可再次发码的剩余秒数（0 = 可发）。"""
        now = datetime.utcnow()
        with self._Session() as db:
            recent = db.scalars(
                select(AuthCode)
                .where(AuthCode.email == email, AuthCode.used.is_(False))
                .order_by(AuthCode.created_at.desc())
                .limit(1)
            ).first()
            if recent is None:
                return 0
            elapsed = (now - recent.created_at).total_seconds()
            return max(0, int(settings.auth_send_code_cooldown_seconds - elapsed))

    # ── 验码 → 建/取用户 → 签发 token ──────────────────────
    def verify_code(self, email: str, code: str) -> tuple[bool, str | None]:
        """校验验证码。成功则建/取用户、签发 token。

        Returns: (ok, token | None)。token 存在表示登录成功。
        """
        now = datetime.utcnow()
        with self._Session() as db:
            ac = db.scalars(
                select(AuthCode)
                .where(AuthCode.email == email, AuthCode.used.is_(False))
                .order_by(AuthCode.created_at.desc())
                .limit(1)
            ).first()
            if ac is None or ac.expires_at < now or ac.code != code:
                return False, None
            ac.used = True

            # 建用户：邮箱首次出现则建号（验证即建号，不区分注册登录）
            user = db.scalars(select(User).where(User.email == email)).first()
            if user is None:
                user = User(id=str(uuid.uuid4()), email=email)
                db.add(user)

            # 签发 token
            token = secrets.token_urlsafe(32)
            ttl_days = settings.session_token_ttl_days
            st = SessionToken(
                token=token, user_id=user.id,
                expires_at=now + timedelta(days=ttl_days),
            )
            db.add(st)
            db.commit()
            return True, token

    # ── token 校验（FastAPI 依赖用）──────────────────────────
    def get_user_by_token(self, token: str) -> Optional[dict]:
        """token → user 信息；过期/吊销/不存在返回 None。"""
        now = datetime.utcnow()
        with self._Session() as db:
            st = db.get(SessionToken, token)
            if st is None or st.revoked or st.expires_at < now:
                return None
            u = db.get(User, st.user_id)
            if u is None:
                return None
            return {"user_id": u.id, "email": u.email}

    def revoke_token(self, token: str) -> bool:
        with self._Session() as db:
            st = db.get(SessionToken, token)
            if st is None:
                return False
            st.revoked = True
            db.commit()
            return True


# ── 邮件发送 ────────────────────────────────────────────────
def send_code_email(to_email: str, code: str) -> bool:
    """通过 SMTP 发送验证码邮件。配置缺失时降级为日志（仅 dev）。"""
    host = settings.smtp_host
    if not host:
        logger.warning("[auth] SMTP 未配置，验证码 %s 仅打印日志（dev 模式）", code)
        return False
    msg = EmailMessage()
    msg["Subject"] = "以渔 · 登录验证码"
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to_email
    msg.set_content(
        f"你的登录验证码是：{code}\n\n"
        f"验证码 {settings.auth_code_ttl_minutes} 分钟内有效，请勿泄露给他人。\n"
        f"如非本人操作请忽略此邮件。\n\n"
        f"——以渔 投研陪练"
    )
    try:
        with smtplib.SMTP_SSL(host, settings.smtp_port, timeout=10) as s:
            s.login(settings.smtp_user, settings.smtp_pass)
            s.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("[auth] 邮件发送失败: %s", exc)
        return False


# ── 全局单例（与 cognition_store 一致）────────────────────────
_repo: AuthRepo | None = None


def get_auth_repo() -> AuthRepo:
    global _repo  # noqa: PLW0603
    if _repo is None:
        _repo = AuthRepo()
    return _repo
