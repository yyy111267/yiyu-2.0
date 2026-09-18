from __future__ import annotations

import asyncio

from toolkit.market.fetch_executor import FetchedField, execute_fetch_plan
from toolkit.market.fetch_planner import FetchPlan, FetchStep
from toolkit.market.market import MarketData
from toolkit.market.market import DataStatus, MarketBundle, Snapshot
from toolkit.market.research_data import clear_research_bundles, store_research_bundle
from toolkit.market import tools as market_tools
from toolkit.market.route_provider import RouteProvider, _security_master_record


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


def test_executor_runs_independent_first_choice_steps_in_parallel() -> None:
    plan = FetchPlan(
        components=["fundamentals"], required_fields=["revenue", "net_profit"],
        missing_field_groups=[], fields_to_fetch=["revenue", "net_profit"],
        budgets={"fundamentals": 1}, symbol="600519.SH",
        steps=[
            _step(1, 1, "source_a", ("revenue",), ("REV",)),
            _step(2, 1, "source_b", ("net_profit",), ("NP",)),
        ],
    )
    active = 0
    peak = 0

    async def runner(step, symbol):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {name: 1 for name in step.fields}

    result = asyncio.run(execute_fetch_plan(plan, runner))
    assert result.status == "ok"
    assert peak == 2


def test_executor_shares_one_component_budget_across_fallback_attempts() -> None:
    plan = FetchPlan(
        components=["fundamentals"], required_fields=["revenue"],
        missing_field_groups=[], fields_to_fetch=["revenue"],
        budgets={"fundamentals": 0.05}, symbol="600519.SH",
        steps=[
            _step(1, 1, "primary", ("revenue",), ("REV",)),
            _step(2, 2, "backup", ("revenue",), ("REV2",)),
        ],
    )

    async def runner(step, symbol):
        await asyncio.sleep(0.035)
        return {} if step.provider == "primary" else {"revenue": 1}

    result = asyncio.run(execute_fetch_plan(plan, runner))
    assert result.status == "request_failed"
    assert result.attempts[-1].reason.startswith("TimeoutError")


def test_route_provider_binds_financial_value_to_report_date() -> None:
    step = _step(1, 1, "eastmoney", ("net_profit_parent",), ("PARENTNETPROFIT",))
    step = type(step)(**{
        **step.__dict__,
        "request": "akshare.stock_financial_analysis_indicator_em(symbol='{symbol}')",
    })
    rows = [{"REPORT_DATE": "2026-06-30 00:00:00", "PARENTNETPROFIT": 44.5}]
    provider = RouteProvider.__new__(RouteProvider)
    assert provider._values(step, rows, "net_profit_parent", "PARENTNETPROFIT") == [
        ("20260630", 44.5)
    ]


def test_default_market_data_attaches_enabled_westock_to_route_provider() -> None:
    from core.config import Settings

    settings = Settings()
    settings.westock_enabled = True
    settings.market_cache_enabled = False
    market = MarketData(settings)
    try:
        assert market._westock is not None
        assert market._route_provider._westock is market._westock
    finally:
        asyncio.run(market.aclose())


def test_planner_keeps_alias_fields_aligned_with_the_same_source_field() -> None:
    from toolkit.market.fetch_planner import plan_fetch

    plan = plan_fetch(
        symbol="600519.SH", requested_fields=["name", "stock_name"],
        use_research_data=False,
    )
    first = plan.steps[0]
    assert first.fields == ("name", "stock_name")
    assert first.source_fields == ("name", "name")

    async def runner(step, symbol):
        return {name: "贵州茅台" for name in step.fields}

    result = asyncio.run(execute_fetch_plan(plan, runner))
    assert result.status == "ok"
    assert set(result.fields) == {"name", "stock_name"}


def test_route_provider_applies_scale_per_field_in_a_batched_request() -> None:
    from toolkit.market.fetch_planner import plan_fetch

    plan = plan_fetch(
        symbol="600519.SH", requested_fields=["market_cap", "pe_ttm"],
        use_research_data=False,
    )
    step = next(item for item in plan.steps if item.provider == "tencent")
    provider = RouteProvider.__new__(RouteProvider)
    records = [""] * 74
    records[45] = "16137.05"
    records[39] = "19.82"

    result = {
        field: provider._values(step, records, field, source)[0][1]
        for field, source in zip(step.fields, step.source_fields)
    }
    assert result == {"market_cap": 1_613_705_000_000.0, "pe_ttm": 19.82}


def test_local_security_master_returns_a_hk_and_us_identity() -> None:
    a_share = _security_master_record("600519.SH")
    hk_share = _security_master_record("00700.HK")
    us_share = _security_master_record("AAPL")

    assert (a_share["name"], a_share["exchange"], a_share["listing_place"], a_share["board"]) == (
        "贵州茅台", "SH", "上海证券交易所", "沪市主板",
    )
    assert (hk_share["exchange"], hk_share["listing_place"], hk_share["board"]) == (
        "HKEX", "香港交易所", "主板",
    )
    assert (us_share["exchange"], us_share["board"]) == (
        "NASDAQ", "NASDAQ Global Select Market",
    )


def test_market_data_executes_local_master_without_guard_or_duplicate_symbol() -> None:
    class Settings:
        market_timeout_seconds = 5
        market_news_days = 7
        market_fund_concurrency = 1
        westock_enabled = False
        market_cache_enabled = False

    noop = _Noop()
    market = MarketData(Settings(), providers={
        "a_quote": [noop], "a_fund": noop, "a_news": [noop],
        "overseas_quote": [], "overseas_data": noop,
        "route": RouteProvider(None),
    })
    bundle = asyncio.run(market.bundle_v2(
        "600519.SH",
        requested_fields=["symbol", "name", "exchange", "market_code", "listing_place", "board"],
    ))

    assert bundle.structured_status == "ok"
    assert bundle.snapshot is not None and bundle.snapshot.symbol == "600519.SH"
    assert bundle.fields["board"]["value"] == "沪市主板"
    assert bundle.web_search_candidates == []


def test_a_share_depreciation_amortization_uses_disclosed_components_without_double_counting() -> None:
    step = FetchStep(
        order=1, attempt=1, provider="akshare", upstream="eastmoney",
        method="fundamentals",
        request="akshare.stock_cash_flow_sheet_by_report_em(symbol='{em_code}'); derive_depreciation_amortization",
        fields=("depreciation_amortization[20251231]",),
        source_fields=("cashflow_D&A_components",), component="fundamentals",
        budget_seconds=18, conditional=False, health="available",
        freshness="reported", reason="test",
    )
    rows = [{
        "REPORT_DATE": "2025-12-31", "FA_IR_DEPR": 1_893_338_311.91,
        "OILGAS_BIOLOGY_DEPR": 1_893_338_311.91, "IA_AMORTIZE": 289_613_682.99,
        "LPE_AMORTIZE": 20_637_734.49, "USERIGHT_ASSET_AMORTIZE": 55_797_324.89,
    }]
    provider = RouteProvider.__new__(RouteProvider)
    assert provider._values(
        step, rows, "depreciation_amortization[20251231]", "cashflow_D&A_components"
    ) == [("20251231", 2_259_387_054.28)]


def test_us_quarterly_financial_route_selects_quarter_column() -> None:
    step = FetchStep(
        order=1, attempt=1, provider="westock", upstream="tencent",
        method="fundamentals",
        request="westock-data-clawhub finance {sina_code} --type income --num 8",
        fields=("ebitda[20250930]",), source_fields=("EBITDA",),
        component="fundamentals", budget_seconds=18, conditional=False,
        health="available", freshness="reported", reason="test",
    )
    provider = RouteProvider.__new__(RouteProvider)
    rows = [{"_date": "2025-09-30", "EBITDA": "-", "EBITDA_Q": "31032.00"}]
    assert provider._values(step, rows, "ebitda[20250930]", "EBITDA") == [
        ("20250930", 31_032_000_000.0)
    ]


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


def test_market_data_returns_an_unregistered_field_to_the_agent() -> None:
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
    bundle = asyncio.run(market.bundle_v2(
        "600519.SH", requested_fields=["important_unlisted_kpi"]
    ))

    assert bundle.structured_status == "unregistered"
    assert bundle.unregistered_fields == ["important_unlisted_kpi"]
    assert bundle.web_search_candidates == ["important_unlisted_kpi"]
    assert bundle.fetch_attempts == []


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
