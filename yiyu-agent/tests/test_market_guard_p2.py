"""取数层 P2 Provider Guard 冒烟测试。

验证：
  - token bucket 限流：高频请求拿不到令牌
  - circuit breaker 熔断：连续失败 3 次后熔断，熔断期间直接跳过
  - 熔断期满半开：熔断时间过后允许一次尝试
  - 成功清零连续失败
  - guard 统计快照
  - MarketData._safe 走 guard：熔断中跳过 provider
"""
from __future__ import annotations

import asyncio
import time

from toolkit.market.provider_guard import (
    DEFAULT_GUARD_CONFIG,
    GuardRegistry,
    ProviderGuard,
    TokenBucket,
)


# ── TokenBucket ───────────────────────────────────────────

def test_token_bucket_allows_within_rate() -> None:
    bucket = TokenBucket(qps=10.0, capacity=2)
    # 容量 2，前两次应该都能拿到
    assert asyncio.run(bucket.acquire()) is True
    assert asyncio.run(bucket.acquire()) is True


def test_token_bucket_blocks_when_empty() -> None:
    bucket = TokenBucket(qps=0.1, capacity=1)
    asyncio.run(bucket.acquire())  # 耗尽
    # qps=0.1 → 10s 才补一个，立即再取应失败
    assert asyncio.run(bucket.acquire()) is False


# ── ProviderGuard 熔断 ─────────────────────────────────────

def test_guard_allows_when_healthy() -> None:
    g = ProviderGuard("test", {"timeout_seconds": 5, "max_attempts": 3,
                                "qps": 10, "circuit_breaker_seconds": 60})
    ok, reason = asyncio.run(g.allow())
    assert ok
    assert reason == ""


def test_guard_circuit_opens_after_consecutive_failures() -> None:
    g = ProviderGuard("test", {"timeout_seconds": 5, "max_attempts": 3,
                                "qps": 10, "circuit_breaker_seconds": 60})
    for _ in range(3):
        g.record_failure("timeout")
    ok, reason = asyncio.run(g.allow())
    assert not ok
    assert "circuit_open" in reason
    assert g.stats.circuit_open is True
    assert g.stats.consecutive_failures == 3


def test_guard_circuit_half_open_after_expiry() -> None:
    g = ProviderGuard("test", {"timeout_seconds": 5, "max_attempts": 3,
                                "qps": 10, "circuit_breaker_seconds": 0.1})
    for _ in range(3):
        g.record_failure("err")
    # 熔断中
    ok, _ = asyncio.run(g.allow())
    assert not ok
    # 等 0.1s 熔断期满
    time.sleep(0.15)
    # 半开：允许一次尝试
    ok, reason = asyncio.run(g.allow())
    assert ok, f"应半开允许，但 {reason}"
    assert g.stats.circuit_open is False


def test_guard_success_resets_consecutive_failures() -> None:
    g = ProviderGuard("test", {"timeout_seconds": 5, "max_attempts": 3,
                                "qps": 10, "circuit_breaker_seconds": 60})
    g.record_failure("err")
    g.record_failure("err")
    assert g.stats.consecutive_failures == 2
    g.record_success()
    assert g.stats.consecutive_failures == 0
    # 未到阈值，不应熔断
    assert g.stats.circuit_open is False


def test_guard_run_times_out() -> None:
    g = ProviderGuard("test", {"timeout_seconds": 0.1, "max_attempts": 3,
                                "qps": 10, "circuit_breaker_seconds": 60})

    async def slow():
        await asyncio.sleep(1)
        return "done"

    try:
        asyncio.run(g.run(slow(), "test"))
        assert False, "应超时"
    except asyncio.TimeoutError:
        pass
    # 超时算失败，记录
    g.record_failure("timeout")
    assert g.stats.total_failures == 1


# ── GuardRegistry ──────────────────────────────────────────

def test_registry_get_creates_and_caches() -> None:
    reg = GuardRegistry()
    g1 = reg.get("akshare")
    g2 = reg.get("akshare")
    assert g1 is g2
    # 不同名创建新 guard
    g3 = reg.get("cninfo")
    assert g3 is not g1


def test_registry_all_stats() -> None:
    reg = GuardRegistry()
    reg.get("akshare").record_failure("err")
    stats = reg.all_stats()
    assert "akshare" in stats
    assert stats["akshare"]["total_failures"] == 1


def test_registry_reset() -> None:
    reg = GuardRegistry()
    reg.get("akshare").record_failure("err")
    reg.reset("akshare")
    # reset 后重新 get 是新实例
    assert reg.get("akshare").stats.total_failures == 0


# ── MarketData._safe 走 guard ───────────────────────────────

def test_safe_skips_provider_when_circuit_open() -> None:
    """熔断中的 provider 被 _safe 跳过，返回 (None, [circuit_open reason])。"""
    from toolkit.market.market import MarketData

    class _FakeProv:
        name = "fakeprov"
        call_count = 0

        async def snapshot(self, symbol):
            _FakeProv.call_count += 1
            return None

    fund = _FakeProv()
    quote = _FakeProv()
    news = _FakeProv()
    providers = {
        "a_quote": [quote], "a_fund": fund, "a_news": [news],
        "overseas_quote": [quote], "overseas_data": fund,
    }

    class _S:
        market_timeout_seconds = 5.0
        market_news_days = 7
        market_fundamental_years = 5
        westock_enabled = False
        market_cache_enabled = False
        market_cache_path = ""
        market_cache_ttl_quote_seconds = 300
        market_cache_ttl_fundamentals_seconds = 86400
        market_cache_ttl_fundamentals_latest_seconds = 86400
        market_cache_ttl_news_seconds = 3600

    md = MarketData(_S(), providers=providers, cache=None)  # type: ignore[arg-type]
    # guard 在 cache is not None 时才装配，这里 cache=None 但 user_provided=True
    # → guard 不装配。需要显式注入 guard 测试。手动建一个熔断的 guard。
    from toolkit.market.provider_guard import GuardRegistry
    md._guards = GuardRegistry()
    guard = md._guards.get("fakeprov")
    # 手动熔断
    for _ in range(3):
        guard.record_failure("err")
    assert guard.stats.circuit_open

    async def fake_awaitable():
        return "should_not_reach"

    result, errors = asyncio.run(md._safe(fake_awaitable(), "fakeprov snapshot"))
    assert result is None
    assert any("circuit_open" in e for e in errors)
    # provider 的 fetch 没被调用
    assert _FakeProv.call_count == 0


def test_safe_records_failure_on_timeout() -> None:
    """_safe 超时时记录 provider 失败，累计到熔断。"""
    from toolkit.market.market import MarketData

    class _FakeProv:
        name = "slowprov"

        async def snapshot(self, symbol):
            await asyncio.sleep(10)
            return None

    quote = _FakeProv()
    fund = _FakeProv()
    news = _FakeProv()
    providers = {
        "a_quote": [quote], "a_fund": fund, "a_news": [news],
        "overseas_quote": [quote], "overseas_data": fund,
    }

    class _S:
        market_timeout_seconds = 5.0
        market_news_days = 7
        market_fundamental_years = 5
        westock_enabled = False
        market_cache_enabled = False
        market_cache_path = ""
        market_cache_ttl_quote_seconds = 300
        market_cache_ttl_fundamentals_seconds = 86400
        market_cache_ttl_fundamentals_latest_seconds = 86400
        market_cache_ttl_news_seconds = 3600

    md = MarketData(_S(), providers=providers, cache=None)  # type: ignore[arg-type]
    from toolkit.market.provider_guard import GuardRegistry
    md._guards = GuardRegistry()
    # 覆盖 slowprov 的 guard 配置：超时 0.1s
    from toolkit.market.provider_guard import ProviderGuard
    md._guards._guards["slowprov"] = ProviderGuard("slowprov", {
        "timeout_seconds": 0.1, "max_attempts": 3,
        "qps": 10, "circuit_breaker_seconds": 60,
    })

    result, errors = asyncio.run(md._safe(quote.snapshot("600519"), "slowprov snapshot"))
    assert result is None
    assert any("TimeoutError" in e for e in errors)
    # 失败已记录
    guard = md._guards.get("slowprov")
    assert guard.stats.consecutive_failures == 1
    assert guard.stats.total_failures == 1


def test_safe_without_guards_falls_back_to_legacy_timeout() -> None:
    """guard 未装配时（极简测试），_safe 退化到旧的纯 timeout 兜底。"""
    from toolkit.market.market import MarketData

    class _FakeProv:
        name = "p"

        async def snapshot(self, symbol):
            return "ok"

    quote = _FakeProv()
    fund = _FakeProv()
    news = _FakeProv()
    providers = {
        "a_quote": [quote], "a_fund": fund, "a_news": [news],
        "overseas_quote": [quote], "overseas_data": fund,
    }

    class _S:
        market_timeout_seconds = 5.0
        market_news_days = 7
        market_fundamental_years = 5
        westock_enabled = False
        market_cache_enabled = False
        market_cache_path = ""
        market_cache_ttl_quote_seconds = 300
        market_cache_ttl_fundamentals_seconds = 86400
        market_cache_ttl_fundamentals_latest_seconds = 86400
        market_cache_ttl_news_seconds = 3600

    md = MarketData(_S(), providers=providers, cache=None)  # type: ignore[arg-type]
    # guard 未装配（cache=None 且 user_provided=True）
    assert md._guards is None
    result, errors = asyncio.run(md._safe(quote.snapshot("600519"), "p snapshot"))
    assert result == "ok"
    assert errors == []


def test_guard_stats_via_market_data() -> None:
    """guard_stats 暴露所有 provider 统计。"""
    from toolkit.market.market import MarketData
    from toolkit.market.provider_guard import GuardRegistry

    class _P:
        name = "p"
        async def snapshot(self, s): return None
    quote = _P(); fund = _P(); news = _P()
    providers = {"a_quote": [quote], "a_fund": fund, "a_news": [news],
                 "overseas_quote": [quote], "overseas_data": fund}

    class _S:
        market_timeout_seconds = 5.0; market_news_days = 7; market_fundamental_years = 5
        westock_enabled = False; market_cache_enabled = False; market_cache_path = ""
        market_cache_ttl_quote_seconds = 300; market_cache_ttl_fundamentals_seconds = 86400
        market_cache_ttl_fundamentals_latest_seconds = 86400; market_cache_ttl_news_seconds = 3600

    md = MarketData(_S(), providers=providers, cache=None)  # type: ignore[arg-type]
    md._guards = GuardRegistry()
    md._guards.get("p").record_failure("err")
    stats = md.guard_stats()
    assert "p" in stats
    assert stats["p"]["total_failures"] == 1
    # 无 guard 时返回空
    md._guards = None
    assert md.guard_stats() == {}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
