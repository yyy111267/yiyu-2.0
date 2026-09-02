"""A 股财报主源路由冒烟测试：AKShare 主取三表 + WeStock 字段级补缺。

覆盖（不联网，全部用假 provider）：
  - A 股财报主源是 AKShare，不是 WeStock
  - 完整性 = 本次 requested_fields 是否全部满足（不是「有营收+净利润」就算完）
  - 只有兜底源能补上缺口时才打兜底源
  - 港美股不走 AKShare
  - 同标的请求合并 + 全局并发上限（受控并发，不裸并发）
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from toolkit.market.market import Fundamentals, MarketData
from toolkit.market.market_cache import MarketCache


class _AkFund:
    """假 AKShare：三表原始科目，有营收/净利/毛利率/资本开支，但**没有总股本**。"""

    name = "akshare"
    calls: list[str] = []

    async def fundamentals(self, symbol: str, years: int = 5) -> Fundamentals:
        _AkFund.calls.append(symbol)
        return Fundamentals(
            symbol=symbol, source="akshare", asof="2025-12-31",
            years=[{"year": "2025", "revenue": 100.0, "net_profit": 20.0,
                    "gross_margin": 40.0, "capex": 8.0}],
        )


class _WestockFund:
    """假 WeStock：财务摘要+三表，独有总股本/EBIT/合同负债。"""

    name = "westock"
    calls: list[str] = []

    async def fundamentals(self, code: str) -> Fundamentals:
        _WestockFund.calls.append(code)
        return Fundamentals(
            symbol=code, source="westock", asof="2025-12-31",
            years=[{"year": "2025", "total_shares": 12.56, "ebit": 25.0,
                    "contract_liability": 3.2}],
        )


class _YfFund:
    name = "yfinance"
    calls: list[str] = []

    async def fundamentals(self, symbol: str, years: int = 5) -> Fundamentals:
        _YfFund.calls.append(symbol)
        return Fundamentals(
            symbol=symbol, source="yfinance", asof="2025-12-31",
            years=[{"year": "2025", "revenue": 50.0, "net_profit": 9.0}],
        )


class _NoQuote:
    name = "no_quote"

    async def snapshot(self, symbol: str) -> None:
        return None

    async def news(self, symbol: str, days: int = 7) -> list:
        return []


class _S:
    market_timeout_seconds = 5.0
    market_news_days = 7
    market_fundamental_years = 5
    market_fund_concurrency = 2
    westock_enabled = False
    market_cache_enabled = False
    market_cache_path = ""
    market_cache_ttl_quote_seconds = 300
    market_cache_ttl_fundamentals_seconds = 86400
    market_cache_ttl_fundamentals_latest_seconds = 86400
    market_cache_ttl_news_seconds = 3600


def _tmp_db() -> str:
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return p


def _make(with_westock: bool = True) -> MarketData:
    for c in (_AkFund, _WestockFund, _YfFund):
        c.calls.clear()
    quote = _NoQuote()
    providers: dict[str, Any] = {
        "a_quote": [quote],
        "a_fund": _AkFund(),
        "a_news": [quote],
        "overseas_quote": [quote],
        "overseas_data": _YfFund(),
    }
    if with_westock:
        providers["westock"] = _WestockFund()
    return MarketData(_S(), providers=providers, cache=None)  # type: ignore[arg-type]


# ── 主源路由 ────────────────────────────────────────────────

def test_a_share_prefers_akshare_and_skips_westock_when_covered() -> None:
    md = _make()
    fund, errors = asyncio.run(md._cached_fundamentals("600519", ["revenue", "net_profit"]))
    assert fund is not None
    assert fund.years[0]["revenue"] == 100.0
    assert _AkFund.calls == ["600519"]   # 主源是 AKShare
    assert _WestockFund.calls == []      # 需求已满足 → 不打 WeStock
    assert errors == []


def test_a_share_falls_back_to_westock_for_total_shares() -> None:
    md = _make()
    fund, _ = asyncio.run(md._cached_fundamentals("600519", ["revenue", "total_shares"]))
    assert _AkFund.calls and _WestockFund.calls
    assert fund is not None
    assert fund.years[0]["revenue"] == 100.0        # AKShare 给的
    assert fund.years[0]["total_shares"] == 12.56   # WeStock 补的（按年字段级合并）


def test_completeness_depends_on_requested_fields_not_revenue_profit() -> None:
    """回归用例：旧逻辑只查「营收+净利润」，会让合同负债这类需求被跳过。"""
    md = _make()
    fund, _ = asyncio.run(md._cached_fundamentals(
        "600519", ["revenue", "net_profit", "contract_liability"]))
    assert _WestockFund.calls, "已有营收+净利，但缺合同负债，仍须打 WeStock"
    assert fund is not None
    assert fund.years[0]["contract_liability"] == 3.2


def test_default_a_share_requirement_includes_total_shares() -> None:
    md = _make()
    asyncio.run(md._cached_fundamentals("600519"))
    assert _WestockFund.calls, "A 股默认要求含 total_shares（只有 WeStock 给得出）"


def test_hk_us_never_calls_akshare() -> None:
    md = _make()
    fund, _ = asyncio.run(md._cached_fundamentals("00700.HK", ["revenue"]))
    assert _AkFund.calls == []      # 港美股不走 AKShare
    assert _WestockFund.calls       # 港美股主源 WeStock
    assert fund is not None


def test_without_westock_unsuppliable_field_is_dropped() -> None:
    """无 WeStock 时 total_shares 拿不到 → 不该让缓存永久失效、反复打源。"""
    md = _make(with_westock=False)
    fund, _ = asyncio.run(md._cached_fundamentals("600519", ["revenue", "total_shares"]))
    assert fund is not None
    assert _WestockFund.calls == []
    assert md._suppliable_fields(["revenue", "total_shares"], "600519") == ["revenue"]


# ── 缓存：字段不满足要重取 ──────────────────────────────────

def test_cache_hit_when_fields_covered_and_refetch_when_not() -> None:
    db = _tmp_db()
    try:
        cache = MarketCache(db, {"fundamentals_latest": 86400})
        # 预置一份只有 revenue 的缓存
        asyncio.run(cache.set("fundamentals_latest", "600519", {
            "symbol": "600519", "source": "akshare", "asof": "2025-12-31",
            "years": [{"year": "2025", "revenue": 100.0}],
        }))
        md = _make()
        md._cache = cache

        asyncio.run(md._cached_fundamentals("600519", ["revenue"]))
        assert _AkFund.calls == [], "字段已满足 → 命中缓存，不打源"

        fund, _ = asyncio.run(md._cached_fundamentals("600519", ["revenue", "capex"]))
        assert _AkFund.calls, "缓存缺 capex → 必须重打源"
        assert fund is not None
        assert fund.years[0]["capex"] == 8.0
        asyncio.run(cache.close())
    finally:
        Path(db).unlink(missing_ok=True)


# ── 并发：请求合并 + 受控并发 ───────────────────────────────

def test_same_symbol_concurrent_requests_are_merged() -> None:
    md = _make()

    async def run():
        return await asyncio.gather(
            md._cached_fundamentals("600519", ["revenue", "total_shares"]),
            md._cached_fundamentals("600519", ["revenue", "total_shares"]),
            md._cached_fundamentals("600519.SH", ["revenue", "total_shares"]),
        )

    results = asyncio.run(run())
    assert all(r[0] is not None for r in results)
    assert len(_AkFund.calls) == 1, "同标的（含 600519.SH 写法）应合并为一次取数"
    assert len(_WestockFund.calls) == 1


def test_fundamental_concurrency_is_capped() -> None:
    """4 个不同标的并发 → 同时打源的最多 2 个（受控并发，不裸并发）。"""

    class _SlowAk:
        name = "akshare"
        inflight = 0
        peak = 0

        async def fundamentals(self, symbol: str, years: int = 5) -> Fundamentals:
            _SlowAk.inflight += 1
            _SlowAk.peak = max(_SlowAk.peak, _SlowAk.inflight)
            await asyncio.sleep(0.05)
            _SlowAk.inflight -= 1
            return Fundamentals(symbol=symbol, source="akshare", asof="2025-12-31",
                                years=[{"year": "2025", "revenue": 100.0, "net_profit": 20.0}])

    quote = _NoQuote()
    md = MarketData(_S(), providers={  # type: ignore[arg-type]
        "a_quote": [quote], "a_fund": _SlowAk(), "a_news": [quote],
        "overseas_quote": [quote], "overseas_data": _YfFund(),
    }, cache=None)

    async def run():
        return await asyncio.gather(*[
            md._cached_fundamentals(sym, ["revenue"])
            for sym in ("600519", "000001", "300750", "601318")
        ])

    results = asyncio.run(run())
    assert all(r[0] is not None for r in results)
    assert _SlowAk.peak <= 2, f"并发上限应为 2，实测峰值 {_SlowAk.peak}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
