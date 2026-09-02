"""新增行情/财务字段的离线映射回归测试。"""
from __future__ import annotations

import pandas as pd

from toolkit.calc.metric_service import compute_metric
from toolkit.market.market import DataStatus, Fundamentals, MarketBundle, Snapshot
from toolkit.market.sources.market_providers import EMQuoteProvider, _merge_ak_income, _parse_ak_indicator


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"data": {
            "f58": "贵州茅台", "f43": 1298.96, "f60": 1300.0, "f170": -0.08,
            "f116": 1_630_000_000_000, "f117": 1_620_000_000_000,
            "f162": 20.1, "f163": 21.5, "f164": 18.25, "f167": 8.2,
            "f127": "酿酒行业", "f189": "20010827",
        }}


class _Client:
    async def get(self, *args, **kwargs) -> _Response:
        if (kwargs.get("params") or {}).get("reportName") == "RPT_VALUEANALYSIS_DET":
            return _ValuationResponse()
        return _Response()


class _ValuationResponse(_Response):
    def json(self) -> dict:
        return {"result": {"data": [{"PS_TTM": 9.75}]}}


async def _snapshot() -> Snapshot | None:
    return await EMQuoteProvider(_Client()).snapshot("600519")


def test_eastmoney_snapshot_keeps_valuation_calibers_separate() -> None:
    import asyncio

    snap = asyncio.run(_snapshot())
    assert snap is not None
    assert snap.market_cap == 1_630_000_000_000
    assert snap.float_market_cap == 1_620_000_000_000
    assert snap.pe == snap.pe_dynamic == 20.1
    assert snap.pe_static == 21.5
    assert snap.pe_ttm == 18.25
    assert snap.ps_ttm == 9.75
    assert snap.industry == "酿酒行业"
    assert snap.listing_date == "2001-08-27"


def test_akshare_financial_indicator_maps_yoy_and_parent_profit() -> None:
    indicators = pd.DataFrame([{
        "REPORT_DATE": "2025-12-31",
        "TOTAL_OPERATE_INCOME_YOY": "12.5",
        "PARENT_NETPROFIT_YOY": "8.4",
    }])
    rows = _parse_ak_indicator(indicators, years=1)
    assert rows == [{"year": "2025", "revenue_yoy": 12.5, "net_profit_parent_yoy": 8.4}]

    income = pd.DataFrame([{
        "报告日": "2025-12-31",
        "营业总收入": "1000",
        "归属于母公司股东的净利润": "500",
        "净利润": "480",
    }])
    target = [{"year": "2025"}]
    _merge_ak_income(target, income, years=1)
    assert target[0]["net_profit_parent"] == 500.0
    assert target[0]["net_profit"] == 500.0


def test_metric_service_uses_provider_pe_ttm_not_legacy_dynamic_pe() -> None:
    bundle = MarketBundle(
        symbol="600519",
        status=DataStatus.OK,
        snapshot=Snapshot(
            symbol="600519", source="eastmoney", pe=20.1, pe_dynamic=20.1, pe_ttm=18.25,
        ),
        fundamentals=Fundamentals(symbol="600519", source="akshare", years=[]),
    )
    result = compute_metric(bundle, "pe_ttm")
    assert result["status"] == "ok"
    assert result["value"] == 18.25
