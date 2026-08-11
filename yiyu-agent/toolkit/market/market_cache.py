"""行情落盘缓存：SQLite 持久化，按组件（snapshot/fundamentals/news）分 TTL。

设计要点：
- 重启存活、可跨进程复用（同一 SQLite 文件）。
- 写/读失败一律容错：返回 None / 吞异常，绝不影响主流程（遵守降级铁律）。
- 组件级键：`{component}:{symbol}`，TTL 由 settings 注入。
"""
from __future__ import annotations

import asyncio
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


class MarketCache:
    """极简键值缓存，值以 JSON 存储，键带过期时间。"""

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
            if expires < time.time():
                await db.execute("DELETE FROM market_cache WHERE key=?", (key,))
                await db.commit()
                return None
            return json.loads(value)
        except Exception as e:  # noqa: BLE001 - 缓存失败绝不阻断主流程
            logger.warning("[market_cache] get 失败（已降级为实时取数）：%s", e)
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

    async def close(self) -> None:
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:  # noqa: BLE001
                pass
            self._db = None
