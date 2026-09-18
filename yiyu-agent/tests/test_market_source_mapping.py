"""字段-来源索引表（source_mapping）与 field_registry 的对齐自检。

守三条线：
  1. registry 每个字段要么有取数路由，要么在 UNMAPPED_FIELDS 里写明原因；
  2. 索引表里不能出现 registry 不认识的字段（避免手写错名静默失效）；
  3. 同一字段的 priority 必须唯一升序（执行器依赖它决定主/副源顺序）。
"""

from __future__ import annotations

from toolkit.market.field_registry import FIELD_SPECS, FIELD_ALIASES
from toolkit.market.source_mapping import (
    SOURCE_MAPPING,
    UNMAPPED_FIELDS,
    VERIFY_STATUS,
    fallback_plan,
    mappings_for,
    provider_candidates,
    provider_fields,
    verify_status,
)


def test_every_registry_field_is_routed_or_declared_unmapped() -> None:
    missing = [f for f in FIELD_SPECS
               if f not in SOURCE_MAPPING and f not in UNMAPPED_FIELDS]
    assert missing == []


def test_index_tables_only_contain_registry_fields() -> None:
    assert [f for f in SOURCE_MAPPING if f not in FIELD_SPECS] == []
    assert [f for f in UNMAPPED_FIELDS if f not in FIELD_SPECS] == []


def test_priorities_are_unique_and_sorted() -> None:
    for field, specs in SOURCE_MAPPING.items():
        priorities = [s.priority for s in specs]
        assert priorities == sorted(set(priorities)), field


def test_unmapped_fields_have_no_provider() -> None:
    assert all(mappings_for(f) == () for f in UNMAPPED_FIELDS)
    assert all(UNMAPPED_FIELDS[f] for f in UNMAPPED_FIELDS)


def test_roic_required_fields_are_all_routed() -> None:
    """roic 依赖 total_profit / total_debt / equity：这三个曾因缺映射被静默丢弃，
    导致 roic 恒为 not_disclosed。回归防线。"""
    for field in ("total_profit", "total_debt", "equity"):
        assert provider_candidates(field), field


def test_verify_status_only_covers_real_routes() -> None:
    """实测表不能记录表里没有的路由（防漂移：改了映射忘了改实测结论，反之亦然）。

    live / unstable 必须在路由里（否则实测结论无处落地）；
    dead 允许不在路由里 —— 实测证明不可用后本就该从路由移除，保留结论是为了防止后人再捡回来。
    """
    for field, by_provider in VERIFY_STATUS.items():
        assert field in SOURCE_MAPPING, f"{field} 不在 SOURCE_MAPPING 里"
        for provider, status in by_provider.items():
            assert status in ("live", "unstable", "dead"), f"{field}/{provider}: {status}"
            if status != "dead":
                assert provider in provider_candidates(field), f"{field}/{provider} 无此路由"


def test_registered_aliases_resolve_to_known_fields() -> None:
    assert set(FIELD_ALIASES.values()) <= FIELD_SPECS.keys()


def test_local_master_backs_identity_fields_in_all_supported_markets() -> None:
    """代码、交易所和上市板块均走本地主数据，断网可用。"""
    for market in ("A", "HK", "US"):
        for field in ("symbol", "stock_code", "exchange", "market_code", "listing_place", "board"):
            assert provider_candidates(field, market=market) == ("preset",), (market, field)
        assert provider_candidates("name", market=market)[0] == "preset"


def test_total_debt_prefers_westock_because_akshare_understates() -> None:
    """不把只取到部分负债的近似和式列为总有息负债备源。"""
    plan = fallback_plan("total_debt")
    assert plan["providers"][0] == "westock"
    assert "akshare" not in provider_candidates("total_debt")
    assert verify_status("total_debt", "westock") == "unstable"


def test_fallback_plan_declares_action_when_sources_exhausted() -> None:
    for field in ("gross_margin", "eps", "market_cap"):
        plan = fallback_plan(field)
        assert plan["status"] == "supported"
        assert plan["on_exhausted"] == "request_failed"
        assert plan["on_failure"] == "return_to_agent"
        assert "derive_from" not in plan
    for field in UNMAPPED_FIELDS:
        plan = fallback_plan(field)
        assert plan["providers"] == [] and plan["unmapped_reason"], field
        assert plan["status"] == plan["on_exhausted"] == "not_supported", field


def test_unknown_field_returns_to_agent_without_structured_candidates() -> None:
    plan = fallback_plan("某公司未登记的经营指标")
    assert plan["status"] == plan["on_exhausted"] == "unregistered"
    assert plan["sources"] == []
    assert plan["unmapped_reason"]
    assert plan["on_failure"] == "return_to_agent"


def test_parent_profit_routes_in_order_with_market_and_period_constraints() -> None:
    plan = fallback_plan("归母净利润[20260630]", market="A")
    assert plan["field"] == "net_profit_parent[20260630]"
    assert plan["providers"] == ["eastmoney", "akshare", "westock"]
    assert plan["sources"][0]["source_field"] == "PARENTNETPROFIT"
    assert plan["status"] == "supported"
    assert provider_candidates("net_profit_parent", market="HK") == ()
    assert provider_candidates("net_profit_parent", market="US") == ()
    assert fallback_plan("market_cap", market="US")["status"] == "not_supported"
    assert fallback_plan("price[20260630]")["status"] == "not_supported"
    assert fallback_plan("归母净利润[20260101-20260630]")["status"] == "not_supported"


def test_revenue_growth_does_not_use_total_revenue_growth() -> None:
    sources = mappings_for("revenue_yoy")
    assert {s.source_field for s in sources} == {"OperatingRevenueGrowRate", "OPERATE_INCOME_YOY"}
    assert all(s.source_field != "TOTALOPERATEREVETZ" for s in sources)


def test_unknown_market_is_not_silently_treated_as_a_shares() -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown market"):
        fallback_plan("price", market="typo")


def test_provider_capability_sets_are_disjoint_enough() -> None:
    """快照类字段只有行情源能取，财报类字段只有财报源能取。"""
    quote_providers = {"eastmoney", "sina", "tencent"}
    for field in ("price", "change_pct", "market_cap", "pe_ttm"):
        assert set(provider_candidates(field)) & quote_providers, field
    assert "eastmoney" not in provider_candidates("revenue")
    assert "revenue" in provider_fields("akshare")
    assert "revenue" in provider_fields("westock")
    # akshare 不覆盖的字段只有 westock 能补（缺源字段不能被错记成 akshare 能力）
    assert provider_candidates("total_shares") == ("tencent", "eastmoney")
    assert provider_candidates("ebit") == ("westock",)


def test_every_route_has_sampled_field_values_and_an_exact_request() -> None:
    import json
    from pathlib import Path

    evidence = json.loads((Path(__file__).parent / "data/source_mapping_audit_20260905.json").read_text())
    newly_audited = {
        "security_master", "em_cashflow_da", "westock_hk_balance",
        "westock_us_income", "westock_us_cashflow",
    }
    for field, specs in SOURCE_MAPPING.items():
        for spec in specs:
            if spec.audit_key in newly_audited:
                assert spec.request and verify_status(field, spec.provider) in {"live", "unstable"}
                continue
            record = evidence[f"{field}|{spec.audit_key}|{spec.source_field}"]
            assert record["request"] == spec.request
            assert record["hits"] > 0
            assert record["attempts"] == (3 if spec.audit_key == "preset" else 9)
            assert spec.markets == ("A",)


def test_sampled_fallback_values_match_on_same_symbol_and_period() -> None:
    import itertools
    import json
    import math
    from pathlib import Path

    evidence = json.loads((Path(__file__).parent / "data/source_mapping_audit_20260905.json").read_text())
    for field, specs in SOURCE_MAPPING.items():
        for a, b in itertools.combinations(specs, 2):
            left_record = evidence.get(f"{field}|{a.audit_key}|{a.source_field}")
            right_record = evidence.get(f"{field}|{b.audit_key}|{b.source_field}")
            if left_record is None or right_record is None:
                continue
            left = left_record["samples"]
            right = right_record["samples"]
            for symbol in left.keys() & right.keys():
                for period in left[symbol].keys() & right[symbol].keys():
                    x, y = left[symbol][period], right[symbol][period]
                    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                        assert math.isclose(x, y, rel_tol=1e-4, abs_tol=1e-4), (field,symbol,period,x,y)


def test_similar_but_different_financial_concepts_are_not_fallbacks() -> None:
    assert {s.source_field for s in mappings_for("net_profit")} == {"净利润", "NETPROFIT"}
    assert {s.source_field for s in mappings_for("cash")} == {"期末现金及现金等价物余额", "END_CCE"}
    assert {s.source_field for s in mappings_for("accounts_receivable")} == {"应收账款", "ACCOUNTS_RECE"}
    assert {s.source_field for s in mappings_for("accounts_payable")} == {"应付账款", "ACCOUNTS_PAYABLE"}
    assert "ROEWeighted" in {s.source_field for s in mappings_for("roe")}
    assert "ROE" not in {s.source_field for s in mappings_for("roe")}


def test_delayed_and_daily_data_are_explicitly_distinguished() -> None:
    assert all(s.freshness == "realtime" for s in mappings_for("price"))
    assert mappings_for("market_cap")[1].freshness == "delayed"
    assert mappings_for("close_price")[0].freshness == "end_of_day"


def test_ebitda_routes_directly_in_us_and_uses_inputs_in_a_shares() -> None:
    assert provider_candidates("ebitda[20251231]", market="US") == ("westock",)
    assert provider_candidates("depreciation_amortization[20251231]", market="US") == ("westock",)
    assert provider_candidates("ebitda[20251231]", market="A") == ()
