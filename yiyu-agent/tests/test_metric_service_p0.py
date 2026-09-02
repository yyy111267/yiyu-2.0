from __future__ import annotations

import asyncio

from toolkit.calc.metric import MetricTool, MetricsTool
from toolkit.calc.metric_service import (
    CALIBER_VERSION,
    compute_metric,
    compute_metrics,
    load_catalog,
)
from toolkit.market.market import DataStatus, Fundamentals, MarketBundle, Snapshot
from toolkit.market.research_data import clear_research_bundles, store_research_bundle


def _bundle(years: list[dict], market_cap: float | None = 1000.0) -> MarketBundle:
    return MarketBundle(
        symbol="TST",
        status=DataStatus.OK,
        snapshot=Snapshot(
            symbol="TST",
            source="westock",
            market_cap=market_cap,
            pe=19.5,
            pb=2.2,
            price=10.0,
            currency="CNY",
            asof="2026-08-27 10:00:00",
        ),
        fundamentals=Fundamentals(
            symbol="TST",
            source="westock",
            asof="2025-12-31",
            years=years,
        ),
    )


def test_p0_batch_computes_dictionary_contract() -> None:
    bundle = _bundle([
        {"year": "2024", "revenue": 180.0, "net_profit": 40.0, "equity": 450.0},
        {
            "year": "2025",
            "revenue": 200.0,
            "net_profit": 50.0,
            "equity": 550.0,
            "total_profit": 60.0,
            "income_tax": 12.0,
            "interest_expense": 5.0,
            "total_debt": 100.0,
            "cash": 80.0,
            "trading_financial_assets": 20.0,
            "operating_cash_flow": 70.0,
            "capital_expenditure": 30.0,
            "ebitda": 80.0,
        },
    ])

    result = compute_metrics(bundle)
    by_id = {m["metric_id"]: m for m in result["metrics"]}

    assert set(by_id) == {
        "pe_ttm", "pb", "ps_ttm", "ev_ebitda", "ev_revenue", "fcf_yield",
        "roic", "roe", "fcf", "fcf_margin", "capex_intensity", "runway",
    }
    assert by_id["pe_ttm"]["value"] == 20.0
    assert by_id["pb"]["value"] == 1.82
    assert by_id["ps_ttm"]["value"] == 5.0
    assert by_id["ev_ebitda"]["value"] == 12.5
    assert by_id["ev_revenue"]["value"] == 5.0
    assert by_id["fcf_yield"]["value"] == 4.0
    assert by_id["fcf"]["value"] == 40.0
    assert by_id["fcf_margin"]["value"] == 20.0
    assert by_id["capex_intensity"]["value"] == 15.0
    assert by_id["roe"]["value"] == 10.0
    assert by_id["roic"]["value"] == 8.67
    assert by_id["runway"]["status"] == "self_funded"
    assert all(m["caliber_version"] == CALIBER_VERSION for m in result["metrics"])
    assert by_id["roic"]["fields"]


def test_p0_catalog_is_loaded_from_yaml() -> None:
    catalog = load_catalog()
    by_id = {m["metric_id"]: m for m in catalog["metrics"]}

    assert catalog["version"] == CALIBER_VERSION
    assert by_id["pe_ttm"]["kernel"] == "pe_ttm"
    assert by_id["roic"]["required_fields"] == ["total_profit", "total_debt", "equity"]


def test_batch_tool_reuses_unified_bundle_for_multiple_standard_metrics() -> None:
    bundle = _bundle([
        {
            "year": "2025", "revenue": 200.0, "net_profit": -5.0,
            "cash": 80.0, "total_debt": 100.0,
            "operating_cash_flow": -30.0, "capital_expenditure": 10.0,
        },
    ])

    clear_research_bundles()
    pack_id = store_research_bundle(bundle)

    result = asyncio.run(MetricsTool().execute(
        "TST", ["ps_ttm", "ev_revenue", "runway", "ps_ttm"], group="G5",
    ))

    assert result["success"] is True
    assert result["data_pack_id"] == pack_id
    assert result["group"] == "G5"
    assert [metric["metric_id"] for metric in result["metrics"]] == [
        "ps_ttm", "ev_revenue", "runway",
    ]


def test_batch_tool_returns_verified_market_cap_recovery_plan() -> None:
    """市值缺失不能只报错：必须明确补总股本并说明派生口径。"""
    bundle = _bundle([{"year": "2025", "revenue": 200.0}], market_cap=None)

    clear_research_bundles()
    store_research_bundle(bundle)
    result = asyncio.run(MetricsTool().execute("TST", ["ps_ttm"]))

    recovery = result["field_recovery_plan"]
    assert recovery == [{
        "metric_id": "ps_ttm",
        "missing_field": "market_cap",
        "action": "derive_market_cap",
        "prerequisites": ["price", "total_shares"],
        "formula": "market_cap = price × total_shares",
        "documents": ["listing_announcement", "prospectus", "latest_annual_report"],
        "validation": "总股本须与价格同一证券类别，且披露时点不早于报告期。",
    }]


def test_p0_statuses_for_missing_and_not_meaningful() -> None:
    missing = compute_metric(_bundle([{"year": "2025", "revenue": 100.0}]), "ev_ebitda")
    assert missing["status"] == "not_disclosed"
    assert "ebitda" in missing["reason"]

    loss = compute_metric(
        _bundle([{"year": "2025", "revenue": 100.0, "net_profit": -5.0}]),
        "pe_ttm",
    )
    assert loss["status"] == "not_meaningful"

    runway = compute_metric(
        _bundle([{
            "year": "2025",
            "cash": 100.0,
            "operating_cash_flow": 10.0,
            "capital_expenditure": 30.0,
        }]),
        "runway",
    )
    assert runway["status"] == "ok"
    assert runway["value"] == 60.0


def test_metric_tool_routes_p0_ids_to_metric_service() -> None:
    clear_research_bundles()
    store_research_bundle(_bundle([{"year": "2025", "revenue": 200.0, "net_profit": 50.0}]))

    result = asyncio.run(MetricTool().execute(symbol="TST", metric_id="pe_ttm"))

    assert result["success"] is True
    assert result["metric_id"] == "pe_ttm"
    assert result["status"] == "ok"
    assert result["caliber_version"] == CALIBER_VERSION


def test_metric_tool_never_fetches_when_unified_bundle_is_missing() -> None:
    clear_research_bundles()

    result = asyncio.run(MetricTool().execute(symbol="TST", metric_id="pe_ttm"))

    assert result["success"] is False
    assert result["needs_market_data"] is True
    assert result["required_market_request"] == {
        "symbol": "TST",
        "metric_ids": ["pe_ttm"],
    }
