"""Execute the exact request recipes emitted by ``fetch_planner``."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

import httpx

from toolkit.market.fetch_executor import FetchedField
from toolkit.market.fetch_planner import FetchStep
from toolkit.market.field_registry import field_parts
from toolkit.market.market_router import split_a_symbol, westock_code
from toolkit.market.process_runner import run_sync_in_process
from toolkit.market.sources.market_providers import WeStockProvider


_EM_DELAY_URL = "https://push2delay.eastmoney.com/api/qt/stock/get"
_EM_VALUATION_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_SINA_URL = "https://hq.sinajs.cn/list={code}"
_TENCENT_URL = "https://qt.gtimg.cn/q={code}"
_KNOWN_A = Path(__file__).resolve().parents[1] / "entity/data/known_a_share.json"
_KNOWN_HK_US = Path(__file__).resolve().parents[1] / "entity/data/known_hk_us.json"
_KNOWN_HK_US_EXTRA = Path(__file__).resolve().parents[1] / "entity/data/known_hk_us_extra.json"

_DA_COMPONENTS = (
    "FA_IR_DEPR", "OILGAS_BIOLOGY_DEPR", "IA_AMORTIZE", "LPE_AMORTIZE",
    "USERIGHT_ASSET_AMORTIZE",
)


def _akshare_records(audit_key: str, symbol: str, source_fields: tuple[str, ...]) -> list[dict]:
    import akshare as ak

    code, exchange = split_a_symbol(symbol)
    calls = {
        "em_indicator": (ak.stock_financial_analysis_indicator_em, {"symbol": symbol}),
        "sina_income": (ak.stock_financial_report_sina,
                        {"stock": exchange.lower() + code, "symbol": "利润表"}),
        "sina_balance": (ak.stock_financial_report_sina,
                         {"stock": exchange.lower() + code, "symbol": "资产负债表"}),
        "sina_cashflow": (ak.stock_financial_report_sina,
                          {"stock": exchange.lower() + code, "symbol": "现金流量表"}),
        "sina_abstract": (ak.stock_financial_abstract, {"symbol": code}),
        "em_income": (ak.stock_profit_sheet_by_report_em, {"symbol": exchange + code}),
        "em_balance": (ak.stock_balance_sheet_by_report_em, {"symbol": exchange + code}),
        "em_cashflow": (ak.stock_cash_flow_sheet_by_report_em, {"symbol": exchange + code}),
        "em_cashflow_da": (ak.stock_cash_flow_sheet_by_report_em, {"symbol": exchange + code}),
    }
    function, kwargs = calls[audit_key]
    frame = function(**kwargs)
    if frame is None or frame.empty:
        return []
    if audit_key == "sina_abstract":
        keep = [name for name in frame.columns if name == "指标" or re.fullmatch(r"\d{8}", str(name))]
    else:
        dates = {"REPORT_DATE", "报告日", "报告期", "_date", "date"}
        wanted = _DA_COMPONENTS if audit_key == "em_cashflow_da" else source_fields
        keep = [name for name in frame.columns if name in dates or name in wanted]
    return frame[keep].to_dict("records")


def _a_board(code: str, exchange: str) -> str:
    if exchange == "BJ":
        return "北交所"
    if exchange == "SH":
        return "科创板" if code.startswith(("688", "689")) else "沪市主板"
    return "创业板" if code.startswith(("300", "301")) else "深市主板"


def _security_master_record(symbol: str) -> dict[str, Any]:
    from toolkit.market.market_router import classify_symbol, normalize_hk_symbol, normalize_us_symbol

    market = classify_symbol(symbol)
    if market == "A":
        code, exchange = split_a_symbol(symbol)
        row = next(
            (item for item in json.loads(_KNOWN_A.read_text(encoding="utf-8"))
             if str(item.get("code")) == code),
            {},
        )
        return {
            **row, "symbol": f"{code}.{exchange}", "code": code,
            "exchange": exchange, "market_code": "A",
            "listing_place": {"SH": "上海证券交易所", "SZ": "深圳证券交易所", "BJ": "北京证券交易所"}[exchange],
            "board": _a_board(code, exchange),
        }
    canonical = normalize_hk_symbol(symbol) if market == "HK" else normalize_us_symbol(symbol)
    rows = json.loads(_KNOWN_HK_US.read_text(encoding="utf-8"))
    if _KNOWN_HK_US_EXTRA.exists():
        rows += json.loads(_KNOWN_HK_US_EXTRA.read_text(encoding="utf-8"))
    row = next((item for item in rows if str(item.get("symbol")).upper() == canonical.upper()), {})
    code = canonical.removesuffix(".HK")
    defaults = {
        "symbol": canonical, "code": code, "market_code": market,
        "exchange": "HKEX" if market == "HK" else row.get("exchange"),
        "listing_place": "香港交易所" if market == "HK" else "美国证券市场",
        "board": ("GEM" if code.startswith("08") else "主板") if market == "HK" else row.get("board"),
    }
    return {**defaults, **row}


def _period(value: Any) -> str:
    text = str(value or "")[:10]
    digits = re.sub(r"\D", "", text)
    return digits[:8] if len(digits) >= 8 else ""


def _number(value: Any, scale: float) -> Any:
    if value is None or str(value).strip() in {"", "-", "--", "None", "nan"}:
        return None
    try:
        return float(str(value).replace(",", "")) * scale
    except (TypeError, ValueError):
        return str(value).strip()


class RouteProvider:
    """把 Source Mapping 的一条配方执行成统一字段结果。"""

    def __init__(self, client: httpx.AsyncClient, *, westock: WeStockProvider | None = None) -> None:
        self._client = client
        self._westock = westock

    async def fetch(self, step: FetchStep, symbol: str) -> dict[str, FetchedField]:
        records = await self._records(step, symbol)
        fetched_at = datetime.now(timezone.utc).isoformat()
        result: dict[str, FetchedField] = {}
        for field_name, source_field in zip(step.fields, step.source_fields):
            candidates = self._values(step, records, field_name, source_field)
            _, requested_period = field_parts(field_name)
            if requested_period:
                candidates = [item for item in candidates if item[0] == requested_period]
            candidates = [item for item in candidates if item[1] is not None]
            if not candidates:
                continue
            period, value = max(candidates, key=lambda item: item[0])
            result[field_name] = FetchedField(
                field=field_name, value=value, period=period or "current",
                provider=step.provider, upstream=step.upstream,
                source_field=source_field, fetched_at=fetched_at,
                unit=self._route_unit(step, field_name),
            )
        return result

    async def _records(self, step: FetchStep, symbol: str) -> Any:
        audit_key = self._audit_key(step)
        if audit_key == "security_master":
            return _security_master_record(symbol)
        if audit_key == "westock_raw":
            if self._westock is None:
                raise RuntimeError("westock provider is disabled")
            table = re.search(r"--type\s+(\w+)", step.request)
            if table is None:
                raise ValueError("westock request has no table type")
            text = await self._westock._run(
                "finance", westock_code(symbol), "--type", table.group(1), "--num", "8"
            )
            return self._westock._parse_md(text)[1]
        code, exchange = split_a_symbol(symbol)
        provider_code = exchange.lower() + code
        if audit_key == "sina":
            response = await self._client.get(
                _SINA_URL.format(code=provider_code),
                headers={"Referer": "https://finance.sina.com.cn"},
            )
            response.raise_for_status()
            payload = response.content.decode("gbk", errors="ignore")
            return payload.split('="', 1)[1].rstrip('";\r\n ').split(",") if '="' in payload else []
        if audit_key == "tencent_quote":
            response = await self._client.get(_TENCENT_URL.format(code=provider_code))
            response.raise_for_status()
            payload = response.content.decode("gbk", errors="ignore")
            return payload.split('"', 2)[1].split("~") if '"' in payload else []
        if audit_key == "em_quote_alt":
            response = await self._client.get(_EM_DELAY_URL, params={
                "secid": ("1." if exchange == "SH" else "0.") + code,
                "fields": ",".join(step.source_fields), "fltt": "2",
            })
            response.raise_for_status()
            return (response.json() or {}).get("data") or {}
        if audit_key == "em_valuation":
            response = await self._client.get(_EM_VALUATION_URL, params={
                "reportName": "RPT_VALUEANALYSIS_DET", "columns": "ALL",
                "filter": f'(SECURITY_CODE="{code}")', "sortColumns": "TRADE_DATE",
                "sortTypes": "-1", "pageSize": "20", "pageNumber": "1",
            })
            response.raise_for_status()
            return ((response.json() or {}).get("result") or {}).get("data") or []
        if audit_key == "preset":
            rows = json.loads(_KNOWN_A.read_text(encoding="utf-8"))
            return [item for item in rows if str(item.get("code")) == code]
        return await run_sync_in_process(_akshare_records, audit_key, symbol, step.source_fields)

    def _values(
        self, step: FetchStep, records: Any, field_name: str, source_field: str
    ) -> list[tuple[str, Any]]:
        audit_key = self._audit_key(step)
        scale = self._route_scale(step, field_name)
        _, requested_period = field_parts(field_name)
        # WeStock 美股表同时提供年度列和单季列：年末取 EBIT/EBITDA/DepCF，
        # 其他报告期取对应 *_Q，避免把年度空值误判成上游失败。
        if (
            audit_key == "westock_raw" and requested_period
            and not requested_period.endswith("1231")
            and ("--type income" in step.request or "--type cashflow" in step.request)
        ):
            source_field = f"{source_field}_Q"
        if audit_key in {"sina", "tencent_quote"}:
            try:
                raw = records[int(source_field)]
            except (IndexError, TypeError, ValueError):
                return []
            return [("current", _number(raw, scale))]
        if audit_key in {"em_quote_alt", "preset", "security_master"}:
            row = records if isinstance(records, dict) else (records[0] if records else {})
            return [("current", _number(row.get(source_field), scale))]
        if audit_key == "sina_abstract":
            return [
                (_period(key), _number(value, scale))
                for row in records if row.get("指标") == source_field
                for key, value in row.items() if re.fullmatch(r"\d{8}", str(key))
            ]
        date_keys = ("REPORT_DATE", "TRADE_DATE", "报告日", "报告期", "_date", "date")
        values: list[tuple[str, Any]] = []
        for row in records or []:
            period = next((_period(row.get(key)) for key in date_keys if row.get(key)), "")
            if period:
                if audit_key == "em_cashflow_da":
                    depreciation = next(
                        (_number(row.get(key), scale) for key in _DA_COMPONENTS[:2]
                         if _number(row.get(key), scale) is not None),
                        None,
                    )
                    additions = [
                        _number(row.get(key), scale) for key in _DA_COMPONENTS[2:]
                        if _number(row.get(key), scale) is not None
                    ]
                    value = sum([depreciation, *additions]) if depreciation is not None else None
                else:
                    value = _number(row.get(source_field), scale)
                values.append((period, value))
        return values

    @staticmethod
    def _audit_key(step: FetchStep) -> str:
        # Planner 已经把精确请求放进 step；此处只做确定性分发。
        request = step.request
        if request.startswith("security_master:"):
            return "security_master"
        if "derive_depreciation_amortization" in request:
            return "em_cashflow_da"
        if "hq.sinajs.cn" in request:
            return "sina"
        if "qt.gtimg.cn" in request:
            return "tencent_quote"
        if "push2delay" in request:
            return "em_quote_alt"
        if "RPT_VALUEANALYSIS_DET" in request:
            return "em_valuation"
        if "known_a_share.json" in request:
            return "preset"
        if "westock-data-clawhub" in request:
            return "westock_raw"
        if "analysis_indicator_em" in request:
            return "em_indicator"
        if "financial_abstract" in request:
            return "sina_abstract"
        if "financial_report_sina" in request:
            return {"利润表": "sina_income", "资产负债表": "sina_balance",
                    "现金流量表": "sina_cashflow"}.get(
                        next((name for name in ("利润表", "资产负债表", "现金流量表") if name in request), ""), ""
                    )
        for name, key in (("profit_sheet", "em_income"), ("balance_sheet", "em_balance"),
                          ("cash_flow_sheet", "em_cashflow")):
            if name in request:
                return key
        raise ValueError(f"unsupported request recipe: {request}")

    @staticmethod
    def _route_scale(step: FetchStep, field_name: str) -> float:
        from toolkit.market.source_mapping import mappings_for
        return next(
            (spec.scale for spec in mappings_for(field_name) if spec.request == step.request),
            1.0,
        )

    @staticmethod
    def _route_unit(step: FetchStep, field_name: str) -> str:
        from toolkit.market.source_mapping import mappings_for
        return next((spec.unit for spec in mappings_for(field_name) if spec.request == step.request), "")
