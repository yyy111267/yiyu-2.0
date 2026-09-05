from __future__ import annotations

from datetime import datetime, timezone

from toolkit.market.fetch_planner import plan_fetch


NOW = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)


def _field(value, *, fetched_at="2026-09-05T05:59:00+00:00", period="current",
           fresh=None, location=None):
    result = {"value": value, "fetched_at": fetched_at, "period": period, "status": "ok"}
    if fresh is not None:
        result["fresh"] = fresh
    if location is not None:
        result["location"] = location
    return result


def test_reuses_fresh_fields_and_only_plans_the_missing_fields() -> None:
    plan = plan_fetch(
        symbol="600519.SH",
        requested_fields=["price", "pe_ttm", "revenue", "capex"],
        current_data={"fields": {"price": _field(1298.96), "pe_ttm": _field(18.25)}},
        now=NOW,
    )

    assert plan.reusable_fields == ["price", "pe_ttm"]
    assert plan.fields_to_fetch == ["revenue", "capital_expenditure"]
    assert plan.components == ["fundamentals"]
    assert {step.fields for step in plan.steps if step.attempt == 1} == {
        ("revenue",), ("capital_expenditure",),
    }
    assert all(step.provider == "akshare" for step in plan.steps if step.attempt == 1)


def test_same_real_request_is_merged_but_different_statements_are_not() -> None:
    plan = plan_fetch(
        symbol="600519.SH",
        requested_fields=["revenue", "net_profit", "cogs", "capex"],
        use_research_data=False,
    )
    first = [step for step in plan.steps if step.attempt == 1]

    profit = next(step for step in first if "利润表" in step.request)
    cashflow = next(step for step in first if "现金流量表" in step.request)
    assert profit.fields == ("revenue", "net_profit", "cogs")
    assert cashflow.fields == ("capital_expenditure",)


def test_expired_snapshot_is_refetched_but_explicit_fresh_cache_is_reused() -> None:
    plan = plan_fetch(
        symbol="600519.SH",
        requested_fields=["price", "revenue"],
        current_data={"fields": {"price": _field(100, fetched_at="2026-09-05T05:50:00+00:00")}},
        cache_fields={"revenue": _field(10, fresh=True, period="2025")},
        now=NOW,
    )

    assert plan.stale_fields == ["price"]
    assert plan.reusable_fields == ["revenue"]
    assert plan.fields_to_fetch == ["price"]


def test_fresh_cache_can_replace_a_stale_data_pack_field() -> None:
    plan = plan_fetch(
        symbol="600519.SH", requested_fields=["price"],
        current_data={"fields": {"price": _field(100, fetched_at="2026-09-05T05:00:00+00:00")}},
        cache_fields={"price": _field(101, fresh=True, location="cache")}, now=NOW,
    )
    assert plan.status == "satisfied"
    assert plan.reusable_fields == ["price"]
    assert plan.stale_fields == []


def test_exact_annual_period_can_reuse_year_record_but_quarter_cannot() -> None:
    annual = {"value": 10, "period": "2025", "fetched_at": "2026-04-01", "status": "ok"}
    year_plan = plan_fetch(symbol="600519.SH", requested_fields=["revenue[20251231]"],
                           current_data={"fields": {"revenue": annual}}, now=NOW)
    quarter_plan = plan_fetch(symbol="600519.SH", requested_fields=["revenue[20250930]"],
                              current_data={"fields": {"revenue": annual}}, now=NOW)

    assert year_plan.status == "satisfied"
    assert quarter_plan.fields_to_fetch == ["revenue[20250930]"]


def test_provider_health_promotes_backup_and_removes_open_circuit() -> None:
    plan = plan_fetch(
        symbol="600519.SH",
        requested_fields=["net_profit_parent"],
        provider_status={"em_indicator": "circuit_open", "sina_abstract": "available",
                         "westock_raw": "degraded"},
        use_research_data=False,
    )

    assert plan.steps[0].upstream == "sina"
    assert plan.steps[0].source_fields == ("归母净利润",)
    assert all(step.upstream != "eastmoney" for step in plan.steps)
    assert plan.steps[-1].health == "degraded"


def test_all_providers_blocked_does_not_emit_a_fake_structured_step() -> None:
    plan = plan_fetch(
        symbol="600519.SH", requested_fields=["price"],
        provider_status={"sina": "down", "tencent": "circuit_open"},
        use_research_data=False,
    )

    assert plan.status == "blocked"
    assert plan.provider_blocked_fields == ["price"]
    assert plan.steps == []
    assert plan.web_search_candidates == ["price"]


def test_accepts_provider_guard_stats_shape() -> None:
    plan = plan_fetch(
        symbol="600519.SH", requested_fields=["net_profit_parent"],
        provider_status={"eastmoney": {"circuit_open": True},
                         "akshare": {"circuit_open": False},
                         "westock": {"circuit_open": True}},
        use_research_data=False,
    )
    assert {step.provider for step in plan.steps} == {"akshare"}


def test_unregistered_and_market_unsupported_are_separated() -> None:
    plan = plan_fetch(
        symbol="AAPL", requested_fields=["price", "some_private_kpi"],
        use_research_data=False,
    )

    assert plan.market == "US"
    assert plan.unsupported_fields == ["price"]
    assert plan.unregistered_fields == ["some_private_kpi"]
    assert plan.steps == []


def test_raw_values_in_current_data_are_treated_as_current_turn_facts() -> None:
    plan = plan_fetch(
        symbol="600519.SH", requested_fields=["price", "pe_ttm"],
        current_data={"price": 1298.96, "pe_ttm": 18.25}, now=NOW,
    )
    assert plan.status == "satisfied"
    assert plan.reusable_fields == ["price", "pe_ttm"]
    assert plan.steps == []


def test_reuses_a_calculation_only_when_bound_to_current_data_pack_version() -> None:
    current = {
        "data_pack_id": "600519.SH:v3",
        "fields": {},
        "derived_metrics": {
            "roic": {"value": 18.2, "status": "ok", "data_pack_id": "600519.SH:v3"},
            "pe_ttm": {"value": 20.1, "status": "ok", "data_pack_id": "600519.SH:v2"},
        },
    }
    plan = plan_fetch(symbol="600519.SH", metric_ids=["roic", "pe_ttm"],
                      current_data=current, now=NOW)

    assert plan.reusable_metrics == ["roic"]
    assert plan.metrics_to_compute == ["pe_ttm"]
    assert set(plan.required_fields) == {"market_cap", "net_profit"}


def test_reads_the_current_conversation_data_pack_by_symbol() -> None:
    from toolkit.market.market import DataStatus, MarketBundle, Snapshot
    from toolkit.market.research_data import clear_research_bundles, store_research_bundle

    clear_research_bundles()
    pack_id = store_research_bundle(MarketBundle(
        symbol="600519.SH", status=DataStatus.OK,
        snapshot=Snapshot(symbol="600519.SH", source="sina", price=1298.96,
                          asof="2026-09-05T05:59:00+00:00"),
    ))
    plan = plan_fetch(symbol="600519.SH", requested_fields=["price"], now=NOW)

    assert plan.data_pack_id == pack_id
    assert plan.status == "satisfied"
    assert plan.reusable_fields == ["price"]
