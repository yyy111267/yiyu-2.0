from __future__ import annotations

from toolkit.market.fetch_planner import plan_fetch
from toolkit.market.field_registry import canonical_fields, field_definition
from toolkit.market.market import DataStatus, Fundamentals, MarketBundle, Snapshot


def test_requested_fields_are_canonicalized_and_planned_by_provider() -> None:
    plan = plan_fetch(requested_fields=["capex", "ocf", "price"])

    assert plan.required_fields == ["capital_expenditure", "operating_cash_flow", "price"]
    assert plan.components == ["fundamentals", "snapshot"]
    # capex 两条原始配方都通过 AKShare，实际上游分别是新浪和东财。
    assert plan.field_sources["capital_expenditure"] == ("akshare",)
    assert plan.field_sources["price"] == ("sina", "tencent")


def test_market_bundle_exposes_one_canonical_field_view() -> None:
    bundle = MarketBundle(
        symbol="600519.SH",
        status=DataStatus.OK,
        snapshot=Snapshot(symbol="600519.SH", source="eastmoney", price=1298.96),
        fundamentals=Fundamentals(
            symbol="600519.SH",
            source="akshare",
            asof="2025-12-31",
            years=[{"year": "2025", "ocf": 10.0, "capex": 3.0}],
        ),
    )

    assert bundle.fields["price"]["value"] == 1298.96
    assert bundle.fields["operating_cash_flow"]["source_field"] == "ocf"
    assert bundle.fields["capital_expenditure"]["source_field"] == "capex"
    assert canonical_fields(["ocf", "operating_cash_flow"]) == ["operating_cash_flow"]


def test_legacy_pe_is_resolved_to_dynamic_pe() -> None:
    assert field_definition("pe")["name"] == "pe_dynamic"
    assert field_definition("pe_ttm") == {
        "name": "pe_ttm",
        "label": "PE(TTM)",
        "type": "float",
        "unit": "x",
        "category": "valuation",
        "definition": "当前总市值 / 最近十二个月归母净利润",
        "time_semantics": "point_in_time",
        "nullable": True,
        "component": "snapshot",
        "aliases": [],
    }


def test_exported_market_columns_have_definitions() -> None:
    columns = [
        "market_code", "code", "股票代码", "股票简称", "收盘价", "最新涨跌幅",
        "收盘价[20260804]", "成交额[20260831]", "换手率[20260831]", "涨跌幅[20260831]",
        "成交量[20260831]", "市销率(ps,ttm)[20260828]", "市盈率(pe,ttm)[20260828]",
        "营业收入同比增长率[20260630]", "销售毛利率[20260630]", "归母净利润[20260630]",
        "总市值[20260828]", "上市地点", "所属同花顺行业", "上市板块",
        "市净率[20260828]", "a股流通市值[20260828]", "动态市盈率[20260828]",
        "营业收入[20260630]", "归母净利润同比增长率[20260630]",
    ]
    definitions = [field_definition(column) for column in columns]
    assert all(item["definition"] and item["category"] for item in definitions)
    assert field_definition("市盈率(pe,ttm)[20260828]")["name"] == "pe_ttm[20260828]"
    assert field_definition("归母净利润[20260630]")["name"] == "net_profit_parent[20260630]"


def test_labels_and_dated_aliases_share_the_same_registry() -> None:
    from toolkit.market.field_registry import FIELD_SPECS, canonical_field

    for spec in FIELD_SPECS.values():
        assert canonical_field(spec.label) == spec.name
        assert canonical_field(f"{spec.label}[20260630]") == f"{spec.name}[20260630]"
    assert field_definition("归母净利润[20260630]")["time_semantics"] == "period_end"
    assert field_definition("最新价[20260630]")["time_semantics"] == "point_in_time"


def test_unknown_field_has_no_invented_definition() -> None:
    import pytest

    with pytest.raises(KeyError, match="unknown field"):
        field_definition("unregistered_metric")
