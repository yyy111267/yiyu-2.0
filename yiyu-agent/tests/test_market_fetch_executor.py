from __future__ import annotations

import asyncio

from toolkit.market.fetch_executor import FetchedField, execute_fetch_plan
from toolkit.market.fetch_planner import FetchPlan, FetchStep
from toolkit.market.market import MarketData
from toolkit.market.market import DataStatus, MarketBundle, Snapshot
from toolkit.market.research_data import clear_research_bundles, store_research_bundle
from toolkit.market import tools as market_tools
from toolkit.market.route_provider import RouteProvider


def _step(order, attempt, provider, fields, source_fields):
    return FetchStep(
        order=order, attempt=attempt, provider=provider, upstream=provider,
        method="fundamentals", request=f"{provider}:request", fields=fields,
        source_fields=source_fields, component="fundamentals", budget_seconds=5,
        conditional=attempt > 1, health="available", freshness="reported", reason="test",
    )


def test_executor_only_retries_the_fields_that_are_still_missing() -> None:
    plan = FetchPlan(
        components=["fundamentals"], required_fields=["revenue", "net_profit_parent"],
        missing_field_groups=[], fields_to_fetch=["revenue", "net_profit_parent"],
        symbol="600519.SH",
        steps=[
            _step(1, 1, "primary", ("revenue", "net_profit_parent"), ("REV", "NP")),
            _step(2, 2, "backup", ("revenue", "net_profit_parent"), ("REV2", "NP2")),
        ],
    )
    calls = []

    async def runner(step, symbol):
        calls.append((step.provider, step.fields, step.source_fields, symbol))
        if step.provider == "primary":
            return {"revenue": {"value": 100, "period": "20251231"}}
        return {"net_profit_parent": {"value": 20, "period": "20251231"}}

    result = asyncio.run(execute_fetch_plan(plan, runner))

    assert result.status == "ok"
    assert set(result.fields) == {"revenue", "net_profit_parent"}
    assert calls[1] == ("backup", ("net_profit_parent",), ("NP2",), "600519.SH")
    assert [attempt.status for attempt in result.attempts] == ["partial", "success"]


def test_executor_rejects_wrong_period_and_reports_request_failed() -> None:
    plan = FetchPlan(
        components=["fundamentals"], required_fields=["revenue[20251231]"],
        missing_field_groups=[], fields_to_fetch=["revenue[20251231]"], symbol="600519.SH",
        steps=[_step(1, 1, "primary", ("revenue[20251231]",), ("REV",))],
    )

    result = asyncio.run(execute_fetch_plan(
        plan, lambda step, symbol: {"revenue[20251231]": {"value": 100, "period": "20250930"}}
    ))

    assert result.status == "request_failed"
    assert result.missing_fields == ["revenue[20251231]"]
    assert result.attempts[0].status == "empty"


def test_executor_keeps_unregistered_separate_from_request_failure() -> None:
    plan = FetchPlan(
        components=[], required_fields=["private_kpi"], missing_field_groups=[],
        unregistered_fields=["private_kpi"], symbol="600519.SH",
    )
    result = asyncio.run(execute_fetch_plan(plan, lambda step, symbol: {}))
    assert result.status == "unregistered"
    assert result.unregistered_fields == ["private_kpi"]
    assert result.attempts == []


def test_executor_enforces_each_step_budget() -> None:
    step = _step(1, 1, "slow", ("revenue",), ("REV",))
    step = type(step)(**{**step.__dict__, "budget_seconds": 0.01})
    plan = FetchPlan(
        components=["fundamentals"], required_fields=["revenue"], missing_field_groups=[],
        fields_to_fetch=["revenue"], symbol="600519.SH", steps=[step],
    )

    async def slow_runner(step, symbol):
        await asyncio.sleep(1)
        return {"revenue": 100}

    result = asyncio.run(execute_fetch_plan(plan, slow_runner))
    assert result.status == "request_failed"
    assert result.attempts[0].status == "failed"
    assert result.attempts[0].reason.startswith("TimeoutError")


def test_route_provider_binds_financial_value_to_report_date() -> None:
    step = _step(1, 1, "eastmoney", ("net_profit_parent",), ("PARENTNETPROFIT",))
    step = type(step)(**{
        **step.__dict__,
        "request": "akshare.stock_financial_analysis_indicator_em(symbol='{symbol}')",
    })
    rows = [{"REPORT_DATE": "2026-06-30 00:00:00", "PARENTNETPROFIT": 44.5}]
    provider = RouteProvider.__new__(RouteProvider)
    assert provider._values(step, rows, "PARENTNETPROFIT") == [("20260630", 44.5)]


class _Route:
    async def fetch(self, step, symbol):
        values = {
            "market_cap": FetchedField("market_cap", 1000, "current", "tencent", "tencent", "45"),
            "net_profit": FetchedField("net_profit", 50, "20251231", "akshare", "sina", "净利润"),
        }
        return {name: values[name] for name in step.fields if name in values}


class _Noop:
    name = "noop"

    async def snapshot(self, symbol):
        return None

    async def fundamentals(self, symbol):
        return None

    async def news(self, symbol, days=7):
        return []


def test_market_data_materializes_executor_output_and_attempt_trace() -> None:
    class Settings:
        market_timeout_seconds = 5
        market_news_days = 7
        market_fund_concurrency = 1
        westock_enabled = False
        market_cache_enabled = False

    noop = _Noop()
    market = MarketData(Settings(), providers={
        "a_quote": [noop], "a_fund": noop, "a_news": [noop],
        "overseas_quote": [], "overseas_data": noop, "route": _Route(),
    })
    bundle = asyncio.run(market.bundle_v2("600519.SH", metric_ids=["pe_ttm"]))

    assert bundle.structured_status == "ok"
    assert bundle.snapshot.market_cap == 1000
    assert bundle.fundamentals.years[0]["net_profit"] == 50
    assert {item["returned_fields"][0] for item in bundle.fetch_attempts} == {
        "market_cap", "net_profit",
    }


def test_bundle_tool_returns_the_merged_data_pack_when_planner_reuses_a_field(monkeypatch) -> None:
    clear_research_bundles()
    store_research_bundle(MarketBundle(
        symbol="600519.SH", status=DataStatus.PARTIAL,
        snapshot=Snapshot(symbol="600519.SH", source="sina", price=10, asof="2099-01-01"),
    ))

    class Market:
        async def bundle(self, symbol, **kwargs):
            return MarketBundle(symbol=symbol, status=DataStatus.OK)

    monkeypatch.setattr(market_tools, "_get_market_data", lambda: Market())
    result = asyncio.run(market_tools.MarketBundleTool().execute(
        "600519.SH", requested_fields=["price"]
    ))
    assert "现价: 10.00" in result["prompt_block"]
