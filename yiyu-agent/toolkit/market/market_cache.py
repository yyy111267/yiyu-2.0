"""行情落盘缓存：SQLite 持久化，按组件（snapshot/fundamentals/news）分 TTL。

设计要点：
- 重启存活、可跨进程复用（同一 SQLite 文件）。
- 写/读失败一律容错：返回 None / 吞异常，绝不影响主流程（遵守降级铁律）。
- 组件级键：`{component}:{symbol}`，TTL 由 settings 注入。
- P1 财报两级缓存：
  - `fundamentals_latest:{symbol}` —— 短 TTL（1 天），快速命中。
  - `fundamentals_report:{symbol}:{report_period}:{version_hash}` —— 永久缓存，
    按报告期 + 版本 hash 固化，避免 AKShare 每次重复整表拉取。
    更正公告检测后才刷新对应报告期（P1 先不做更正公告解析，永久 key 落下来即可）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_TTLS: dict[str, int] = {
    "snapshot": 300,
    "fundamentals": 86400,
    "news": 3600,
}

# P1 财报两级缓存的永久组件名（不参与 TTL 过期）
FUNDAMENTALS_REPORT_COMPONENT = "fundamentals_report"
# 短 TTL 的 latest 组件名（独立于旧 fundamentals，避免互相覆盖语义混乱）
FUNDAMENTALS_LATEST_COMPONENT = "fundamentals_latest"

# 永久缓存标记：expires_at 设为该值表示永不过期
_NEVER_EXPIRES: float = -1.0


class MarketCache:
    """极简键值缓存，值以 JSON 存储，键带过期时间。

    P1 扩展：
      - `get_persistent` / `set_persistent`：永久缓存（按报告期固化），
        不参与 TTL 过期，用于财报整表结果落盘。
      - `get` / `set`：原有 TTL 缓存（snapshot/fundamentals_latest/news）。
    """

    def __init__(self, path: str, ttls: dict[str, int] | None = None) -> None:
        self._path = path
        self._ttls = {**DEFAULT_TTLS, **(ttls or {})}
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            p = Path(self._path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(str(p))
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS market_cache ("
                "  key TEXT PRIMARY KEY,"
                "  value TEXT NOT NULL,"
                "  expires_at REAL NOT NULL)"
            )
            await self._db.commit()
        return self._db

    @staticmethod
    def _key(component: str, symbol: str) -> str:
        return f"{component}:{symbol}"

    async def get(self, component: str, symbol: str) -> dict | list | None:
        """命中且未过期返回反序列化对象；未命中/过期/异常返回 None。"""
        try:
            db = await self._conn()
            key = self._key(component, symbol)
            cur = await db.execute(
                "SELECT value, expires_at FROM market_cache WHERE key=?", (key,)
            )
            row = await cur.fetchone()
            if row is None:
                return None
            value, expires = row
            # 永久缓存（expires_at == _NEVER_EXPIRES）不过期
            if expires != _NEVER_EXPIRES and expires < time.time():
                return None
            return json.loads(value)
        except Exception as e:  # noqa: BLE001 - 缓存失败绝不阻断主流程
            logger.warning("[market_cache] get 失败（已降级为实时取数）：%s", e)
            return None

    async def get_stale(self, component: str, symbol: str) -> tuple[dict | list, float] | None:
        """读取过期缓存，用于实时源失败后的 stale fallback。

        返回 (value, stale_seconds)。未命中、未过期、永久缓存或异常均返回 None。
        """
        try:
            db = await self._conn()
            key = self._key(component, symbol)
            cur = await db.execute(
                "SELECT value, expires_at FROM market_cache WHERE key=?", (key,)
            )
            row = await cur.fetchone()
            if row is None:
                return None
            value, expires = row
            now = time.time()
            if expires == _NEVER_EXPIRES or expires >= now:
                return None
            return json.loads(value), now - expires
        except Exception as e:  # noqa: BLE001
            logger.warning("[market_cache] get_stale 失败（已跳过 stale fallback）：%s", e)
            return None

    async def set(self, component: str, symbol: str, value: dict | list) -> None:
        """写入缓存；失败仅告警。"""
        try:
            db = await self._conn()
            key = self._key(component, symbol)
            expires = time.time() + self._ttls.get(component, 300)
            await db.execute(
                "INSERT INTO market_cache(key, value, expires_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at",
                (key, json.dumps(value, ensure_ascii=False), expires),
            )
            await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("[market_cache] set 失败（已跳过缓存）：%s", e)

    # ── P1：永久缓存（按报告期 + 版本 hash 固化）──────────────

    @staticmethod
    def report_key(symbol: str, report_period: str, version_hash: str) -> str:
        """财报报告期永久 key：fundamentals_report:{symbol}:{period}:{version}。

        version_hash 由调用方对财报内容（或 AKShare 整表结果）算 hash 得到，
        内容变（更正公告）则 hash 变 → 新 key，旧 key 保留供回溯。
        """
        return f"{FUNDAMENTALS_REPORT_COMPONENT}:{symbol}:{report_period}:{version_hash}"

    async def get_persistent(self, key: str) -> dict | list | None:
        """读取永久缓存。命中返回对象；未命中/异常返回 None。

        永久缓存用 expires_at=_NEVER_EXPIRES 标记，不走 TTL 过期。
        """
        try:
            db = await self._conn()
            cur = await db.execute(
                "SELECT value, expires_at FROM market_cache WHERE key=?", (key,)
            )
            row = await cur.fetchone()
            if row is None:
                return None
            value, expires = row
            # 永久缓存不应过期；若被误写成 TTL（expires > 0 且已过期）也容错返回 None
            if expires != _NEVER_EXPIRES and 0 < expires < time.time():
                return None
            return json.loads(value)
        except Exception as e:  # noqa: BLE001
            logger.warning("[market_cache] get_persistent 失败：%s", e)
            return None

    async def set_persistent(self, key: str, value: dict | list) -> None:
        """写入永久缓存（expires_at = _NEVER_EXPIRES）。失败仅告警。"""
        try:
            db = await self._conn()
            await db.execute(
                "INSERT INTO market_cache(key, value, expires_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at",
                (key, json.dumps(value, ensure_ascii=False), _NEVER_EXPIRES),
            )
            await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("[market_cache] set_persistent 失败：%s", e)

    @staticmethod
    def content_hash(value: dict | list) -> str:
        """对财报内容算 8 位 hash，用作 version_hash。

        用于更正公告检测：内容变则 hash 变 → 新永久 key。
        """
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
            return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
        except Exception:  # noqa: BLE001
            return "unknown"

    async def close(self) -> None:
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:  # noqa: BLE001
                pass
            self._db = None
