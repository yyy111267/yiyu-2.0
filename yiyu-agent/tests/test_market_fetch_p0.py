"""取数层改造 P0 冒烟测试：验证按需取数 + evidence 登记 + 快失败契约。

不依赖联网/真实 provider，用假 provider 注入 MarketData 验证行为。
"""
from __future__ import annotations

import asyncio
from typing import Any

from toolkit.market.evidence_registry import EvidenceRegistry
from toolkit.market.fetch_planner import (
    COMPONENT_BUDGETS,
    MAX_FAILURES_PER_COMPONENT,
    expand_required_fields,
    plan_fetch,
    should_fetch_news,
)
from toolkit.market.market import DataStatus, Fundamentals, MarketBundle, MarketData, Snapshot
from toolkit.market.search_fallback import FallbackResult, fallback_search


# ── fetch_planner ──────────────────────────────────────────

def test_plan_fetch_metric_ids_excludes_news() -> None:
    plan = plan_fetch(metric_ids=["pe_ttm"])
    # pe_ttm 只需 market_cap + net_profit → snapshot + fundamentals，不取 news
    assert "news" not in plan.components
    assert "snapshot" in plan.components
    assert "fundamentals" in plan.components
    assert set(plan.required_fields) == {"market_cap", "net_profit"}


def test_plan_fetch_field_groups_includes_news_when_named() -> None:
    plan = plan_fetch(field_groups=["snapshot", "news"])
    assert "news" in plan.components
    assert "snapshot" in plan.components
    assert "fundamentals" not in plan.components


def test_plan_fetch_default_takes_all() -> None:
    plan = plan_fetch()
    # 向后兼容：不传需求 → 取全量
    assert set(plan.components) == {"snapshot", "fundamentals", "news"}


def test_plan_fetch_budgets_match_catalog() -> None:
    plan = plan_fetch(metric_ids=["pe_ttm"])
    assert plan.budget_for("snapshot") == COMPONENT_BUDGETS["snapshot"]
    assert plan.budget_for("fundamentals") == COMPONENT_BUDGETS["fundamentals"]
    assert plan.max_failures == MAX_FAILURES_PER_COMPONENT


def test_expand_required_fields_unknown_metric_id_treated_as_field() -> None:
    fields = expand_required_fields(["market_cap"])
    assert fields == ["market_cap"]


def test_should_fetch_news_false_for_metric_ids() -> None:
    plan = plan_fetch(metric_ids=["pe_ttm"])
    assert should_fetch_news(plan) is False


# ── evidence_registry ──────────────────────────────────────

def test_evidence_registry_dedupes_by_field() -> None:
    reg = EvidenceRegistry()
    e1 = reg.register("market_cap", 100.0, source="eastmoney", source_level="A",
                      as_of="2026-08-27", period="current")
    e2 = reg.register("market_cap", 200.0, source="sina", source_level="A",
                      as_of="2026-08-27", period="current")
    # 同名字段只保留第一条
    assert e1 is e2
    assert reg.get("market_cap").value == 100.0
    assert reg.all_fields()["market_cap"].evidence_id == "fe1"


def test_evidence_registry_to_dict() -> None:
    reg = EvidenceRegistry()
    reg.register("pe", 19.5, source="eastmoney", source_level="A",
                as_of="2026-08-27", period="current", caliber="provider_pe")
    d = reg.to_dict()
    assert "pe" in d
    assert d["pe"]["source"] == "eastmoney"
    assert d["pe"]["caliber"] == "provider_pe"
    assert d["pe"]["evidence_id"] == "fe1"


# ── MarketData.bundle_v2 行为（假 provider）─────────────────

class _FakeQuote:
    name = "fake_quote"

    async def snapshot(self, symbol: str) -> Snapshot:
        return Snapshot(
            symbol=symbol, source="fake_quote", name="TST",
            price=10.0, market_cap=1000.0, pe=19.5, pb=2.2,
            currency="CNY", asof="2026-08-27 10:00:00",
        )


class _FakeFund:
    name = "fake_fund"

    async def fundamentals(self, symbol: str) -> Fundamentals:
        return Fundamentals(
            symbol=symbol, source="fake_fund", asof="2025-12-31",
            years=[{"year": "2025", "revenue": 200.0, "net_profit": 50.0, "equity": 550.0}],
        )


class _FakeNews:
    name = "fake_news"

    async def news(self, symbol: str, days: int = 7):
        return []


def _make_market_with_fakes() -> MarketData:
    """用假 provider 装配 MarketData，避免联网与 westock。"""
    fund = _FakeFund()
    quote = _FakeQuote()
    news = _FakeNews()
    providers = {
        "a_quote": [quote],
        "a_fund": fund,
        "a_news": [news],
        "overseas_quote": [quote],
        "overseas_data": fund,
    }
    # 用一个轻量假 settings（避免读 .env）
    class _S:
        market_timeout_seconds = 5.0
        market_news_days = 7
        market_fundamental_years = 5
        westock_enabled = False
        market_cache_enabled = False
        market_cache_path = ""
        market_cache_ttl_quote_seconds = 300
        market_cache_ttl_fundamentals_seconds = 86400
        market_cache_ttl_news_seconds = 3600
    return MarketData(_S(), providers=providers, cache=None)  # type: ignore[arg-type]


def test_bundle_v2_metric_ids_no_news() -> None:
    md = _make_market_with_fakes()
    bundle = asyncio.run(md.bundle_v2("600519", metric_ids=["pe_ttm"]))
    # metric_ids 驱动 → 不取新闻
    assert bundle.news == []
    # 取到 snapshot + fundamentals
    assert bundle.snapshot is not None
    assert bundle.fundamentals is not None
    # field_evidence 登记了 market_cap / net_profit
    assert "market_cap" in bundle.field_evidence
    assert "net_profit" in bundle.field_evidence
    # fetch_status 应为 ok（假 provider 全成功）
    assert bundle.fetch_status == "ok"


def test_bundle_v2_field_groups_snapshot_only() -> None:
    md = _make_market_with_fakes()
    bundle = asyncio.run(md.bundle_v2("600519", field_groups=["snapshot"]))
    assert bundle.snapshot is not None
    assert bundle.fundamentals is None
    assert bundle.news == []


def test_bundle_v2_missing_fields_when_field_groups_snapshot_only() -> None:
    md = _make_market_with_fakes()
    bundle = asyncio.run(md.bundle_v2("600519", field_groups=["snapshot"]))
    # 没取 fundamentals → 缺字段不在这里报（required_fields 只在 metric_ids 驱动时填充）
    # 但 fetch_status 应 ok（snapshot 成功）
    assert bundle.fetch_status == "ok"


def test_bundle_v2_unknown_metric_reports_missing_field_partial() -> None:
    md = _make_market_with_fakes()
    bundle = asyncio.run(md.bundle_v2("600519", metric_ids=["nonexistent_field_xyz"]))
    # Registry 未登记的字段不能误打 fundamentals；后续由搜索分支处理。
    assert bundle.fetch_status == "degraded"
    assert bundle.status is DataStatus.DEGRADED
    assert "nonexistent_field_xyz" in bundle.missing_fields


def test_bundle_v2_evidence_has_source_and_level() -> None:
    md = _make_market_with_fakes()
    bundle = asyncio.run(md.bundle_v2("600519", metric_ids=["pe_ttm"]))
    ev = bundle.field_evidence["market_cap"]
    assert ev["source"] == "fake_quote"
    assert ev["source_level"] == "B"  # fake_quote 不在 A 级白名单
    assert ev["evidence_id"].startswith("fe")


# ── 搜索兜底 ───────────────────────────────────────────────

def test_fallback_search_uses_injected_fn() -> None:
    async def fake_search(*, query: str, max_results: int, sources: Any) -> dict:
        return {
            "results": [
                {"title": "年报", "url": "https://cninfo.com.cn/r",
                 "snippet": "营收 20 亿", "source_level": "A"},
            ],
        }
    res = asyncio.run(fallback_search("600519", "fundamentals", ["revenue"], search_fn=fake_search))
    assert res.status == "fallback_search"
    assert len(res.results) == 1
    assert res.results[0]["url"] == "https://cninfo.com.cn/r"
    assert "revenue" in res.missing_fields


def test_fallback_search_returns_failed_on_timeout() -> None:
    async def slow_search(**kwargs):
        await asyncio.sleep(10)
        return {}
    res = asyncio.run(fallback_search("600519", "fundamentals", ["revenue"],
                                       search_fn=slow_search, budget_seconds=0.1))
    assert res.status == "fallback_failed"
    assert "timed out" in res.error


def test_fallback_search_no_results_returns_failed() -> None:
    async def empty_search(**kwargs):
        return {"results": []}
    res = asyncio.run(fallback_search("600519", "fundamentals", ["revenue"], search_fn=empty_search))
    assert res.status == "fallback_failed"
    assert res.error == "no results"


# ── 旧 bundle() 向后兼容 ──────────────────────────────────

def test_bundle_legacy_no_metric_ids_takes_all() -> None:
    md = _make_market_with_fakes()
    bundle = asyncio.run(md.bundle("600519"))
    # 旧调用 → 取全量（含 news）
    assert bundle.snapshot is not None
    assert bundle.fundamentals is not None
    # 假 news 返回空列表，但 news 字段应该是 [] 而不是 None
    assert bundle.news == []
    # 旧路径不填 field_evidence（只有 bundle_v2 才登记）
    assert bundle.field_evidence == {}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
