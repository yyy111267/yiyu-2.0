"""Metric Service v1.0 P0 batch.

This module implements the product metric dictionary contract for the first
engineering batch. It owns the P0 metric kernels and field-level output;
shared plumbing (field mapping, band judging, formula registry) lives in
toolkit/calc/metric_base.py.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import yaml

CALIBER_VERSION = "v1.0"
CATALOG_PATH = Path(__file__).resolve().parent / "metrics_catalog.yaml"

STATUS_NOT_DISCLOSED = "not_disclosed"
STATUS_NOT_MEANINGFUL = "not_meaningful"
STATUS_DEGRADED = "degraded"
STATUS_OK = "ok"
STATUS_SELF_FUNDED = "self_funded"

_FALLBACK_CATALOG = {
    "version": CALIBER_VERSION,
    "metrics": [
        {"metric_id": "pe_ttm", "unit": "x", "kernel": "pe_ttm"},
        {"metric_id": "pb", "unit": "x", "kernel": "pb"},
        {"metric_id": "ps_ttm", "unit": "x", "kernel": "ps_ttm"},
        {"metric_id": "ev_ebitda", "unit": "x", "kernel": "ev_ebitda"},
        {"metric_id": "ev_revenue", "unit": "x", "kernel": "ev_revenue"},
        {"metric_id": "fcf_yield", "unit": "pct", "kernel": "fcf_yield"},
        {"metric_id": "roic", "unit": "pct", "kernel": "roic"},
        {"metric_id": "roe", "unit": "pct", "kernel": "roe"},
        {"metric_id": "fcf", "unit": "currency", "kernel": "fcf"},
        {"metric_id": "fcf_margin", "unit": "pct", "kernel": "fcf_margin"},
        {"metric_id": "capex_intensity", "unit": "pct", "kernel": "capex_intensity"},
        {"metric_id": "runway", "unit": "months", "kernel": "runway"},
    ],
}


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    """Load metric definitions from YAML, with a minimal in-code fallback."""
    try:
        data = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("metrics"), list):
            return data
    except Exception:
        pass
    return _FALLBACK_CATALOG


def _catalog_by_id() -> dict[str, dict]:
    return {
        str(m.get("metric_id")): dict(m)
        for m in load_catalog().get("metrics", [])
        if m.get("metric_id")
    }


P0_METRIC_IDS = set(_catalog_by_id())


@dataclass(frozen=True)
class FieldValue:
    name: str
    value: Any
    period: str
    source: str
    source_level: str
    as_of: str
    degraded: bool = False
    note: str = ""


def supported_metrics() -> list[str]:
    return sorted(_catalog_by_id())


def compute_metric(bundle: Any, metric_id: str) -> dict[str, Any]:
    """Compute one metric from a MarketBundle-like object."""
    metric_id = str(metric_id or "").strip()
    catalog = _catalog_by_id()
    mdef = catalog.get(metric_id)
    fields = _collect_fields(bundle)
    # P0 取数层改造：若有 field_evidence，用取数层的 source/source_level/as_of 覆盖
    # Metric Service 自己猜的来源（取数层更接近一手，优先级更高）
    field_evidence = getattr(bundle, "field_evidence", None)
    if isinstance(field_evidence, dict) and field_evidence:
        _merge_field_evidence(fields, field_evidence)
    fn = _KERNELS.get(str((mdef or {}).get("kernel") or metric_id))
    if fn is None:
        return {
            "success": False,
            "error": f"metric_id '{metric_id}' is not implemented by Metric Service P0",
            "supported_metrics": supported_metrics(),
        }
    result = fn(fields, mdef or {"metric_id": metric_id})
    if mdef:
        result["name"] = mdef.get("name", metric_id)
        result["period_policy"] = mdef.get("period_policy", "")
        result["required_fields"] = list(mdef.get("required_fields") or [])
    return result


def compute_metrics(bundle: Any, metric_ids: list[str] | None = None) -> dict[str, Any]:
    ids = metric_ids or supported_metrics()
    return {
        "success": True,
        "symbol": getattr(bundle, "symbol", ""),
        "caliber_version": CALIBER_VERSION,
        "metrics": [compute_metric(bundle, mid) for mid in ids],
    }


def _collect_fields(bundle: Any) -> dict[str, FieldValue]:
    fields: dict[str, FieldValue] = {}
    snapshot = getattr(bundle, "snapshot", None)
    fundamentals = getattr(bundle, "fundamentals", None)

    if snapshot is not None:
        source = str(getattr(snapshot, "source", "") or "market_snapshot")
        as_of = str(getattr(snapshot, "asof", "") or "")
        for attr in (
            "market_cap", "float_market_cap", "pe", "pe_dynamic", "pe_static", "pe_ttm", "ps_ttm",
            "pb", "price", "industry", "listing_date",
        ):
            val = getattr(snapshot, attr, None)
            if val is not None:
                fields[attr] = FieldValue(
                    name=attr,
                    value=val,
                    period="current",
                    source=source,
                    source_level=_source_level(source),
                    as_of=as_of,
                )

    if fundamentals is not None and getattr(fundamentals, "years", None):
        source = str(getattr(fundamentals, "source", "") or "fundamentals")
        as_of = str(getattr(fundamentals, "asof", "") or "")
        years = sorted(fundamentals.years, key=lambda y: str(y.get("year", "")))
        aliases = {
            "revenue": "revenue",
            "revenue_yoy": "revenue_yoy",
            "net_profit": "net_profit",
            "net_profit_parent": "net_profit_parent",
            "net_profit_parent_yoy": "net_profit_parent_yoy",
            "gross_margin": "gross_margin",
            "ocf": "operating_cash_flow",
            "operating_cash_flow": "operating_cash_flow",
            "capex": "capital_expenditure",
            "capital_expenditure": "capital_expenditure",
            "assets": "total_assets",
            "total_assets": "total_assets",
            "equity": "equity",
            "total_equity": "equity",
            "shareholder_equity": "equity",
            "minority_interest": "minority_interest",
            "total_shares": "total_shares",
            "total_debt": "total_debt",
            "cash": "cash",
            "trading_financial_assets": "trading_financial_assets",
            "ebit": "ebit",
            "ebitda": "ebitda",
            "depreciation_amortization": "depreciation_amortization",
            "interest_expense": "interest_expense",
            "financial_expense": "interest_expense",
            "total_profit": "total_profit",
            "income_tax": "income_tax",
            "roe": "roe",
        }
        for key, canonical in aliases.items():
            vals = [
                (str(y.get("year", "")), y.get(key))
                for y in years
                if y.get(key) is not None
            ]
            if not vals or canonical in fields:
                continue
            period, value = vals[-1]
            fields[canonical] = FieldValue(
                name=canonical,
                value=value,
                period=period,
                source=source,
                source_level=_source_level(source),
                as_of=as_of or period,
            )
            if len(vals) >= 2:
                prior_period, prior_value = vals[-2]
                fields[f"{canonical}__prior"] = FieldValue(
                    name=f"{canonical}__prior",
                    value=prior_value,
                    period=prior_period,
                    source=source,
                    source_level=_source_level(source),
                    as_of=as_of or prior_period,
                )

        if "market_cap" not in fields and "price" in fields and "total_shares" in fields:
            price = _num(fields["price"])
            shares = _num(fields["total_shares"])
            if price is not None and shares is not None and price > 0 and shares > 0:
                refs = [fields["price"], fields["total_shares"]]
                fields["market_cap"] = FieldValue(
                    name="market_cap",
                    value=price * shares,
                    period="current",
                    source="derived",
                    source_level=_min_source_level(refs),
                    as_of=_as_of(refs),
                    degraded=True,
                    note="price × latest disclosed total_shares",
                )

        if "gross_margin" not in fields:
            latest = years[-1]
            revenue = _num(latest.get("revenue"))
            gross_profit = _num(latest.get("gross_profit"))
            cogs = _num(latest.get("cogs"))
            if revenue and gross_profit is not None:
                fields["gross_margin"] = _derived(
                    "gross_margin", gross_profit / revenue, latest, source, as_of,
                    "gross_profit / revenue",
                )
            elif revenue and cogs is not None:
                fields["gross_margin"] = _derived(
                    "gross_margin", (revenue - cogs) / revenue, latest, source, as_of,
                    "(revenue - cogs) / revenue",
                )

        if "ebitda" not in fields:
            ebit = _num(_field(fields, "ebit"))
            da = _num(_field(fields, "depreciation_amortization"))
            if ebit is not None and da is not None:
                ref = _first(fields, "ebit", "depreciation_amortization")
                fields["ebitda"] = FieldValue(
                    "ebitda", ebit + da, ref.period, ref.source, ref.source_level, ref.as_of,
                    note="EBIT + depreciation_amortization",
                )

    return fields


def _source_level(source: str) -> str:
    if source in {"westock", "akshare", "eastmoney", "sina"}:
        return "A"
    return "B"


def _merge_field_evidence(
    fields: dict[str, FieldValue], evidence: dict[str, Any]
) -> None:
    """用取数层登记的 field_evidence 覆盖 FieldValue 的 source/source_level/as_of。

    只覆盖更优的来源（取数层 source_level A 优先）；evidence_id 注入到 note。
    value/period 不动（取数层与 metric_service 都是从同一 bundle 取，值一致）。
    """
    order = {"S": 0, "A": 1, "B": 2}
    for name, item in fields.items():
        ev = evidence.get(name)
        if not isinstance(ev, dict):
            continue
        source = item.source
        source_level = item.source_level
        as_of = item.as_of
        note = item.note
        ev_level = str(ev.get("source_level") or "B")
        if order.get(ev_level, 9) <= order.get(item.source_level, 9):
            source = str(ev.get("source") or item.source)
            source_level = ev_level
            as_of = str(ev.get("as_of") or item.as_of)
        eid = str(ev.get("evidence_id") or "")
        if eid and eid not in (note or ""):
            note = f"{note}; evidence_id={eid}".strip("; ") if note else f"evidence_id={eid}"
        fields[name] = replace(item, source=source, source_level=source_level, as_of=as_of, note=note)


def _derived(
    name: str,
    value: Any,
    row: dict,
    source: str,
    as_of: str,
    note: str,
) -> FieldValue:
    period = str(row.get("year", ""))
    return FieldValue(
        name, value, period, source, _source_level(source), as_of or period, note=note
    )


def _num(v: Any) -> float | None:
    if isinstance(v, FieldValue):
        v = v.value
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip().replace(",", "")
        if s in {"", "-", "--", "None", "nan", "N/A"}:
            return None
        return float(s)
    except ValueError:
        return None


def _field(fields: dict[str, FieldValue], name: str) -> Any:
    item = fields.get(name)
    return item.value if item else None


def _first(fields: dict[str, FieldValue], *names: str) -> FieldValue:
    for name in names:
        if name in fields:
            return fields[name]
    return FieldValue("", None, "", "", "B", "")


def _avg(fields: dict[str, FieldValue], name: str) -> tuple[float | None, list[FieldValue]]:
    cur = fields.get(name)
    prior = fields.get(f"{name}__prior")
    if cur is None:
        return None, []
    cur_v = _num(cur)
    if cur_v is None:
        return None, [cur]
    prior_v = _num(prior) if prior else None
    if prior_v is None:
        return cur_v, [cur]
    return (cur_v + prior_v) / 2, [prior, cur]


def _min_source_level(items: list[FieldValue]) -> str:
    order = {"S": 0, "A": 1, "B": 2}
    if not items:
        return "B"
    return max((i.source_level for i in items), key=lambda x: order.get(x, 9))


def _period(items: list[FieldValue]) -> str:
    periods = [i.period for i in items if i.period]
    return max(periods) if periods else "current"


def _as_of(items: list[FieldValue]) -> str:
    vals = [i.as_of for i in items if i.as_of]
    return max(vals) if vals else ""


def _missing(metric_id: str, required: list[str], fields: dict[str, FieldValue]) -> dict[str, Any]:
    missing = [r for r in required if r not in fields or _num(fields[r]) is None]
    return _result(
        metric_id,
        None,
        "",
        STATUS_NOT_DISCLOSED,
        [],
        reason="missing fields: " + ", ".join(missing),
    )


def _meaningless(
    metric_id: str, reason: str, items: list[FieldValue], unit: str = ""
) -> dict[str, Any]:
    return _result(metric_id, None, unit, STATUS_NOT_MEANINGFUL, items, reason=reason)


def _result(
    metric_id: str,
    value: Any,
    unit: str,
    status: str,
    inputs: list[FieldValue],
    *,
    reason: str = "",
    degraded_note: str = "",
) -> dict[str, Any]:
    if value is not None and isinstance(value, float):
        value = round(value, 2)
    notes = [i.note for i in inputs if i.note]
    if degraded_note:
        notes.append(degraded_note)
    final_status = status
    if final_status == STATUS_OK and any(i.degraded for i in inputs):
        final_status = STATUS_DEGRADED
    return {
        "success": True,
        "metric_id": metric_id,
        "value": value,
        "unit": unit,
        "caliber_version": CALIBER_VERSION,
        "period": _period(inputs),
        "source_level": _min_source_level(inputs),
        "as_of": _as_of(inputs),
        "status": final_status,
        "reason": reason,
        "fields": [
            {
                "field": i.name,
                "period": i.period,
                "source": i.source,
                "source_level": i.source_level,
                "as_of": i.as_of,
                "degraded": i.degraded,
                "note": i.note,
            }
            for i in inputs
        ],
        "degradation": "；".join(notes),
    }


def _ratio_metric(
    metric_id: str,
    numerator: str,
    denominator: str,
    fields: dict[str, FieldValue],
    unit: str = "x",
    *,
    denominator_positive: bool = False,
    numerator_positive: bool = False,
) -> dict[str, Any]:
    if numerator not in fields or denominator not in fields:
        return _missing(metric_id, [numerator, denominator], fields)
    n_item, d_item = fields[numerator], fields[denominator]
    n, d = _num(n_item), _num(d_item)
    if n is None or d is None:
        return _missing(metric_id, [numerator, denominator], fields)
    if denominator_positive and d <= 0:
        return _meaningless(metric_id, f"{denominator} <= 0", [n_item, d_item], unit)
    if numerator_positive and n <= 0:
        return _meaningless(metric_id, f"{numerator} <= 0", [n_item, d_item], unit)
    if d == 0:
        return _meaningless(metric_id, f"{denominator} is zero", [n_item, d_item], unit)
    return _result(metric_id, n / d, unit, STATUS_OK, [n_item, d_item])


def _market_cap_ratio(
    metric_id: str, denominator: str, fields: dict[str, FieldValue]
) -> dict[str, Any]:
    return _ratio_metric(
        metric_id, "market_cap", denominator, fields, "x", denominator_positive=True
    )


def _pe_ttm(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    if "pe_ttm" in fields:
        return _result("pe_ttm", _num(fields["pe_ttm"]), "x", STATUS_OK, [fields["pe_ttm"]])
    if "market_cap" in fields and "net_profit" in fields:
        return _market_cap_ratio("pe_ttm", "net_profit", fields)
    if "pe_dynamic" in fields:
        return _result(
            "pe_ttm", _num(fields["pe_dynamic"]), "x", STATUS_DEGRADED, [fields["pe_dynamic"]],
            reason="market_cap or net_profit missing; provider returned dynamic PE, not PE TTM",
        )
    if "pe" in fields:
        return _result(
            "pe_ttm", _num(fields["pe"]), "x", STATUS_DEGRADED, [fields["pe"]],
            reason="market_cap or net_profit missing; used provider PE directly",
        )
    return _missing("pe_ttm", ["market_cap", "net_profit"], fields)


def _pb(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    if "market_cap" in fields and "equity" in fields:
        return _market_cap_ratio("pb", "equity", fields)
    if "pb" in fields:
        return _result(
            "pb", _num(fields["pb"]), "x", STATUS_DEGRADED, [fields["pb"]],
            reason="market_cap or equity missing; used provider PB directly",
        )
    return _missing("pb", ["market_cap", "equity"], fields)


def _ps_ttm(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    if "ps_ttm" in fields:
        return _result("ps_ttm", _num(fields["ps_ttm"]), "x", STATUS_OK, [fields["ps_ttm"]])
    return _market_cap_ratio("ps_ttm", "revenue", fields)


def _ev(fields: dict[str, FieldValue]) -> tuple[float | None, list[FieldValue], str]:
    required = ["market_cap", "total_debt", "cash"]
    if any(r not in fields for r in required):
        return None, [fields[r] for r in required if r in fields], "missing fields: " + ", ".join(
            r for r in required if r not in fields
        )
    items = [fields["market_cap"], fields["total_debt"], fields["cash"]]
    market_cap, debt, cash = (_num(i) for i in items)
    if market_cap is None or debt is None or cash is None:
        return None, items, "missing fields: market_cap, total_debt or cash"
    tfa = _num(fields.get("trading_financial_assets")) or 0.0
    if "trading_financial_assets" in fields:
        items.append(fields["trading_financial_assets"])
    return market_cap + debt - cash - tfa, items, ""


def _ev_ebitda(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    ev, ev_items, reason = _ev(fields)
    if ev is None or "ebitda" not in fields:
        missing = reason or "missing fields: ebitda"
        if "ebitda" not in fields:
            missing = (missing + ", ebitda").strip(", ")
        return _result(
            "ev_ebitda", None, "x", STATUS_NOT_DISCLOSED, ev_items, reason=missing
        )
    ebitda = _num(fields["ebitda"])
    items = ev_items + [fields["ebitda"]]
    if ebitda is None:
        return _missing("ev_ebitda", ["ebitda"], fields)
    if ebitda <= 0:
        return _meaningless("ev_ebitda", "ebitda <= 0", items, "x")
    return _result("ev_ebitda", ev / ebitda, "x", STATUS_OK, items)


def _ev_revenue(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    ev, ev_items, reason = _ev(fields)
    if ev is None or "revenue" not in fields:
        missing = reason or "missing fields: revenue"
        if "revenue" not in fields:
            missing = (missing + ", revenue").strip(", ")
        return _result("ev_revenue", None, "x", STATUS_NOT_DISCLOSED, ev_items, reason=missing)
    revenue = _num(fields["revenue"])
    items = ev_items + [fields["revenue"]]
    if revenue is None:
        return _missing("ev_revenue", ["revenue"], fields)
    if revenue <= 0:
        return _meaningless("ev_revenue", "revenue <= 0", items, "x")
    return _result("ev_revenue", ev / revenue, "x", STATUS_OK, items)


def _fcf_value(fields: dict[str, FieldValue]) -> tuple[float | None, list[FieldValue], str]:
    if "operating_cash_flow" not in fields or "capital_expenditure" not in fields:
        items = [
            fields[k]
            for k in ("operating_cash_flow", "capital_expenditure")
            if k in fields
        ]
        return None, items, (
            "missing fields: "
            + ", ".join(
                k for k in ("operating_cash_flow", "capital_expenditure") if k not in fields
            )
        )
    ocf_item, capex_item = fields["operating_cash_flow"], fields["capital_expenditure"]
    ocf, capex = _num(ocf_item), _num(capex_item)
    if ocf is None or capex is None:
        return (
            None,
            [ocf_item, capex_item],
            "missing fields: operating_cash_flow or capital_expenditure",
        )
    return ocf - abs(capex), [ocf_item, capex_item], ""


def _fcf(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    value, items, reason = _fcf_value(fields)
    if value is None:
        return _result("fcf", None, "currency", STATUS_NOT_DISCLOSED, items, reason=reason)
    return _result("fcf", value, "currency", STATUS_OK, items)


def _fcf_margin(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    value, items, reason = _fcf_value(fields)
    if value is None or "revenue" not in fields:
        if "revenue" not in fields:
            reason = (reason + ", revenue").strip(", ")
        return _result("fcf_margin", None, "pct", STATUS_NOT_DISCLOSED, items, reason=reason)
    revenue = _num(fields["revenue"])
    items = items + [fields["revenue"]]
    if revenue is None:
        return _missing("fcf_margin", ["revenue"], fields)
    if revenue <= 0:
        return _meaningless("fcf_margin", "revenue <= 0", items, "pct")
    return _result("fcf_margin", value / revenue * 100, "pct", STATUS_OK, items)


def _fcf_yield(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    value, items, reason = _fcf_value(fields)
    if value is None or "market_cap" not in fields:
        if "market_cap" not in fields:
            reason = (reason + ", market_cap").strip(", ")
        return _result("fcf_yield", None, "pct", STATUS_NOT_DISCLOSED, items, reason=reason)
    market_cap = _num(fields["market_cap"])
    items = items + [fields["market_cap"]]
    if market_cap is None:
        return _missing("fcf_yield", ["market_cap"], fields)
    if market_cap <= 0:
        return _meaningless("fcf_yield", "market_cap <= 0", items, "pct")
    if value <= 0:
        return _meaningless("fcf_yield", "fcf <= 0", items, "pct")
    return _result("fcf_yield", value / market_cap * 100, "pct", STATUS_OK, items)


def _capex_intensity(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    if "capital_expenditure" not in fields or "revenue" not in fields:
        return _missing("capex_intensity", ["capital_expenditure", "revenue"], fields)
    capex = abs(_num(fields["capital_expenditure"]) or 0.0)
    revenue = _num(fields["revenue"])
    items = [fields["capital_expenditure"], fields["revenue"]]
    if revenue is None:
        return _missing("capex_intensity", ["revenue"], fields)
    if revenue <= 0:
        return _meaningless("capex_intensity", "revenue <= 0", items, "pct")
    return _result("capex_intensity", capex / revenue * 100, "pct", STATUS_OK, items)


def _roe(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    if "net_profit" not in fields:
        return _missing("roe", ["net_profit", "equity"], fields)
    avg_equity, eq_items = _avg(fields, "equity")
    if avg_equity is None:
        if "roe" in fields:
            return _result(
                "roe", _num(fields["roe"]), "pct", STATUS_DEGRADED, [fields["roe"]],
                reason="net_profit or average equity missing; used provider ROE directly",
            )
        return _missing("roe", ["equity"], fields)
    net_profit = _num(fields["net_profit"])
    items = [fields["net_profit"]] + eq_items
    if net_profit is None:
        return _missing("roe", ["net_profit"], fields)
    if avg_equity <= 0:
        return _meaningless("roe", "average equity <= 0", items, "pct")
    return _result("roe", net_profit / avg_equity * 100, "pct", STATUS_OK, items)


def _roic(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    required = ["total_profit", "total_debt", "equity"]
    if any(r not in fields for r in required):
        return _missing("roic", required, fields)
    total_profit = _num(fields["total_profit"])
    debt = _num(fields["total_debt"])
    if total_profit is None or debt is None:
        return _missing("roic", required, fields)
    avg_equity, eq_items = _avg(fields, "equity")
    if avg_equity is None:
        return _missing("roic", ["equity"], fields)
    interest = _num(fields.get("interest_expense"))
    degraded_note = ""
    inputs = [fields["total_profit"], fields["total_debt"]] + eq_items
    if interest is None:
        interest = 0.0
        degraded_note = "interest_expense missing; treated as 0 for MVP"
    else:
        inputs.append(fields["interest_expense"])
        if fields["interest_expense"].name == "interest_expense":
            degraded_note = "interest_expense may be financial expense proxy depending on provider"
    tax_rate = _tax_rate(fields)
    if tax_rate is None:
        return _missing("roic", ["income_tax"], fields)
    if "income_tax" in fields:
        inputs.append(fields["income_tax"])
    elif "net_profit" in fields:
        inputs.append(fields["net_profit"])
        degraded_note = (degraded_note + "；" if degraded_note else "") + (
            "tax_rate derived from (total_profit - net_profit) / total_profit"
        )
    minority = _num(fields.get("minority_interest")) or 0.0
    if "minority_interest" in fields:
        inputs.append(fields["minority_interest"])
    invested_capital = avg_equity + minority + debt
    if invested_capital <= 0:
        return _meaningless("roic", "invested capital <= 0", inputs, "pct")
    nopat = (total_profit + max(interest, 0.0)) * (1 - tax_rate)
    status = STATUS_DEGRADED if degraded_note else STATUS_OK
    return _result(
        "roic",
        nopat / invested_capital * 100,
        "pct",
        status,
        inputs,
        degraded_note=degraded_note,
    )


def _tax_rate(fields: dict[str, FieldValue]) -> float | None:
    total_profit = _num(fields.get("total_profit"))
    income_tax = _num(fields.get("income_tax"))
    if total_profit and income_tax is not None:
        return income_tax / total_profit
    net_profit = _num(fields.get("net_profit"))
    if total_profit and net_profit is not None:
        return (total_profit - net_profit) / total_profit
    return None


def _runway(fields: dict[str, FieldValue], mdef: dict) -> dict[str, Any]:
    cash = _num(fields.get("cash"))
    if cash is None:
        return _missing("runway", ["cash", "operating_cash_flow", "capital_expenditure"], fields)
    tfa = _num(fields.get("trading_financial_assets")) or 0.0
    fcf_value, fcf_items, reason = _fcf_value(fields)
    items = [fields["cash"]] + fcf_items
    if "trading_financial_assets" in fields:
        items.append(fields["trading_financial_assets"])
    if fcf_value is None:
        return _result("runway", None, "months", STATUS_NOT_DISCLOSED, items, reason=reason)
    if fcf_value >= 0:
        return _result(
            "runway",
            None,
            "months",
            STATUS_SELF_FUNDED,
            items,
            reason="FCF >= 0; self-funded",
        )
    monthly_burn = -fcf_value / 12
    if monthly_burn <= 0:
        return _meaningless("runway", "monthly cash burn <= 0", items, "months")
    return _result("runway", (cash + tfa) / monthly_burn, "months", STATUS_OK, items)


_KERNELS: dict[str, Callable[[dict[str, FieldValue], dict], dict[str, Any]]] = {
    "pe_ttm": _pe_ttm,
    "pb": _pb,
    "ps_ttm": _ps_ttm,
    "ev_ebitda": _ev_ebitda,
    "ev_revenue": _ev_revenue,
    "fcf_yield": _fcf_yield,
    "roic": _roic,
    "roe": _roe,
    "fcf": _fcf,
    "fcf_margin": _fcf_margin,
    "capex_intensity": _capex_intensity,
    "runway": _runway,
}
