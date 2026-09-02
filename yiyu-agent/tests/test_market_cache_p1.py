"""取数层 P1 财报两级缓存冒烟测试。

验证：
  - fundamentals_latest:{symbol} 短 TTL 命中
  - fundamentals_report:{symbol}:{period}:{version} 永久缓存落盘
  - 内容变（更正公告）→ hash 变 → 新永久 key（旧 key 保留）
  - 永久缓存能拼装回多年度 Fundamentals
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from toolkit.market.market import DataStatus, Fundamentals, Snapshot
from toolkit.market.market_cache import (
    FUNDAMENTALS_REPORT_COMPONENT,
    MarketCache,
)


def _tmp_db() -> str:
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return p


# ── MarketCache 永久缓存单元 ───────────────────────────────

def test_content_hash_stable_and_changes_on_content() -> None:
    a = MarketCache.content_hash({"year": "2024", "revenue": 100})
    b = MarketCache.content_hash({"year": "2024", "revenue": 100})
    c = MarketCache.content_hash({"year": "2024", "revenue": 101})
    assert a == b            # 相同内容 hash 相同
    assert a != c            # 内容变 hash 变
    assert len(a) == 8


def test_report_key_format() -> None:
    k = MarketCache.report_key("600519", "2024", "abcd1234")
    assert k == f"{FUNDAMENTALS_REPORT_COMPONENT}:600519:2024:abcd1234"


def test_persistent_set_get_never_expires() -> None:
    db = _tmp_db()
    try:
        cache = MarketCache(db, {"fundamentals_latest": 1})  # TTL 1s 也不影响永久缓存
        asyncio.run(cache.set_persistent("fundamentals_report:600519:2024:hash1",
                                        {"year": "2024", "revenue": 100}))
        got = asyncio.run(cache.get_persistent("fundamentals_report:600519:2024:hash1"))
        assert got == {"year": "2024", "revenue": 100}

        # 等 TTL 过期后永久缓存仍命中
        import time
        time.sleep(1.2)
        got2 = asyncio.run(cache.get_persistent("fundamentals_report:600519:2024:hash1"))
        assert got2 == {"year": "2024", "revenue": 100}
        asyncio.run(cache.close())
    finally:
        Path(db).unlink(missing_ok=True)


def test_get_stale_returns_expired_value_without_deleting() -> None:
    db = _tmp_db()
    try:
        cache = MarketCache(db, {"snapshot": -1})
        asyncio.run(cache.set("snapshot", "600519", {"symbol": "600519", "price": 10}))

        assert asyncio.run(cache.get("snapshot", "600519")) is None
        stale = asyncio.run(cache.get_stale("snapshot", "600519"))
        assert stale is not None
        value, stale_seconds = stale
        assert value["price"] == 10
        assert stale_seconds >= 0
        asyncio.run(cache.close())
    finally:
        Path(db).unlink(missing_ok=True)


def test_persistent_key_isolation_by_version_hash() -> None:
    """内容变（更正公告）→ hash 变 → 新永久 key，旧 key 保留。"""
    db = _tmp_db()
    try:
        cache = MarketCache(db)
        v1 = MarketCache.content_hash({"year": "2024", "revenue": 100})
        v2 = MarketCache.content_hash({"year": "2024", "revenue": 105})  # 更正
        k1 = MarketCache.report_key("600519", "2024", v1)
        k2 = MarketCache.report_key("600519", "2024", v2)
        assert k1 != k2

        asyncio.run(cache.set_persistent(k1, {"year": "2024", "revenue": 100}))
        asyncio.run(cache.set_persistent(k2, {"year": "2024", "revenue": 105}))

        assert asyncio.run(cache.get_persistent(k1))["revenue"] == 100
        assert asyncio.run(cache.get_persistent(k2))["revenue"] == 105
        asyncio.run(cache.close())
    finally:
        Path(db).unlink(missing_ok=True)


# ── MarketData 两级缓存集成（假 provider + 真缓存）──────────

class _FakeFund:
    name = "fake_fund"
    call_count = 0

    async def fundamentals(self, symbol: str) -> Fundamentals:
        _FakeFund.call_count += 1
        return Fundamentals(
            symbol=symbol, source="fake_fund", asof="2024-12-31",
            years=[
                {"year": "2023", "revenue": 180.0, "net_profit": 40.0},
                {"year": "2024", "revenue": 200.0, "net_profit": 50.0},
            ],
        )


class _FakeQuote:
    name = "fake_quote"

    async def snapshot(self, symbol: str):
        return None


class _FailingQuote:
    name = "failing_quote"

    async def snapshot(self, symbol: str):
        raise RuntimeError("provider down")


class _FakeNews:
    name = "fake_news"

    async def news(self, symbol: str, days: int = 7):
        return []


def _make_market_with_cache(db_path: str):
    from toolkit.market.market import MarketData
    fund = _FakeFund()
    quote = _FakeQuote()
    news = _FakeNews()
    providers = {
        "a_quote": [quote], "a_fund": fund, "a_news": [news],
        "overseas_quote": [quote], "overseas_data": fund,
    }

    class _S:
        market_timeout_seconds = 5.0
        market_news_days = 7
        market_fundamental_years = 5
        westock_enabled = False
        market_cache_enabled = True
        market_cache_path = db_path
        market_cache_ttl_quote_seconds = 300
        market_cache_ttl_fundamentals_seconds = 86400
        market_cache_ttl_fundamentals_latest_seconds = 86400
        market_cache_ttl_news_seconds = 3600

    cache = MarketCache(db_path, {
        "snapshot": 300, "fundamentals": 86400,
        "fundamentals_latest": 86400, "news": 3600,
    })
    return MarketData(_S(), providers=providers, cache=cache)  # type: ignore[arg-type]


def _make_market_with_failing_quote_cache(db_path: str):
    from toolkit.market.market import MarketData
    fund = _FakeFund()
    quote = _FailingQuote()
    news = _FakeNews()
    providers = {
        "a_quote": [quote], "a_fund": fund, "a_news": [news],
        "overseas_quote": [quote], "overseas_data": fund,
    }

    class _S:
        market_timeout_seconds = 5.0
        market_news_days = 7
        market_fundamental_years = 5
        westock_enabled = False
        market_cache_enabled = True
        market_cache_path = db_path
        market_cache_ttl_quote_seconds = -1
        market_cache_ttl_fundamentals_seconds = 86400
        market_cache_ttl_fundamentals_latest_seconds = 86400
        market_cache_ttl_news_seconds = 3600

    cache = MarketCache(db_path, {
        "snapshot": -1, "fundamentals": 86400,
        "fundamentals_latest": 86400, "news": 3600,
    })
    return MarketData(_S(), providers=providers, cache=cache)  # type: ignore[arg-type]


def test_two_level_cache_avoids_repeated_fetch() -> None:
    """同一 symbol 连续两次 bundle：第二次应命中缓存，不打源。"""
    db = _tmp_db()
    _FakeFund.call_count = 0
    try:
        md = _make_market_with_cache(db)
        # 第一次：打源 + 写 latest + 写永久 key
        b1 = asyncio.run(md.bundle("600519", field_groups=["fundamentals"]))
        assert b1.fundamentals is not None
        assert _FakeFund.call_count == 1

        # 第二次：应命中 latest 缓存，不再打源
        b2 = asyncio.run(md.bundle("600519", field_groups=["fundamentals"]))
        assert b2.fundamentals is not None
        assert _FakeFund.call_count == 1  # 仍是 1，没再打源
        asyncio.run(md._cache.close())  # type: ignore[union-attr]
    finally:
        Path(db).unlink(missing_ok=True)


def test_snapshot_stale_cache_fallback_when_provider_fails() -> None:
    db = _tmp_db()
    try:
        md = _make_market_with_failing_quote_cache(db)
        stale_snapshot = Snapshot(
            symbol="600519", source="eastmoney", name="TST", price=10.0,
            market_cap=1000.0, asof="2026-08-26 15:00:00",
        )
        asyncio.run(md._cache.set("snapshot", "600519", stale_snapshot.__dict__))  # type: ignore[union-attr]

        b = asyncio.run(md.bundle("600519", field_groups=["snapshot"]))
        assert b.snapshot is not None
        assert b.snapshot.price == 10.0
        assert b.status is DataStatus.PARTIAL
        assert b.fetch_status == "partial"
        assert any("stale_cache" in e for e in b.errors)
        asyncio.run(md._cache.close())  # type: ignore[union-attr]
    finally:
        Path(db).unlink(missing_ok=True)


def test_persistent_cache_survives_latest_expiry() -> None:
    """latest 过期后，永久缓存仍能拼装回财报（只是要再查永久 key）。"""
    db = _tmp_db()
    _FakeFund.call_count = 0
    try:
        md = _make_market_with_cache(db)
        b1 = asyncio.run(md.bundle("600519", field_groups=["fundamentals"]))
        assert _FakeFund.call_count == 1

        # 模拟 latest 过期：直接删 latest key
        asyncio.run(md._cache._conn())  # type: ignore[union-attr]
        db_obj = md._cache._db  # type: ignore[union-attr]
        asyncio.run(db_obj.execute("DELETE FROM market_cache WHERE key LIKE 'fundamentals_latest:%'"))
        asyncio.run(db_obj.commit())

        # 再次取：latest 没了 → 查永久缓存拼装 → 仍不应打源
        b2 = asyncio.run(md.bundle("600519", field_groups=["fundamentals"]))
        assert b2.fundamentals is not None
        assert b2.fundamentals.years  # 拼装回多年度数据
        assert _FakeFund.call_count == 1  # 永久缓存命中，没打源
        asyncio.run(md._cache.close())  # type: ignore[union-attr]
    finally:
        Path(db).unlink(missing_ok=True)


def test_persistent_keys_written_per_period() -> None:
    """财报写入后，每个报告期应有对应永久 key。"""
    db = _tmp_db()
    _FakeFund.call_count = 0
    try:
        md = _make_market_with_cache(db)
        asyncio.run(md.bundle("600519", field_groups=["fundamentals"]))

        db_obj = md._cache._db  # type: ignore[union-attr]
        cur = asyncio.run(db_obj.execute(
            "SELECT key FROM market_cache WHERE key LIKE 'fundamentals_report:%'"
        ))
        rows = asyncio.run(cur.fetchall())
        keys = [r[0] for r in rows]
        # 两个报告期 → 两个永久 key
        assert len(keys) == 2
        assert any("600519:2023:" in k for k in keys)
        assert any("600519:2024:" in k for k in keys)
        asyncio.run(md._cache.close())  # type: ignore[union-attr]
    finally:
        Path(db).unlink(missing_ok=True)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
