"""取数层 P3 字段级 Evidence 全链路冒烟测试。

验证：
  - evidence_metadata 对 market.get_bundle：field_evidence → 字段级 citations（带 evidence_id）
  - evidence_metadata 对 market.get_bundle：fallback_results → web citations
  - evidence_metadata 对 calc.metric：fields 注入 evidence_id
  - Trace.add_evidence 灌入 fetch_status / missing_fields / field_evidence
  - EvidenceEntry.to_dict() 输出全链路字段
"""
from __future__ import annotations

from runtime.trace import Trace, evidence_metadata


# ── evidence_metadata: market.get_bundle ──────────────────

def test_metadata_market_bundle_expands_field_evidence() -> None:
    content = {
        "symbol": "600519",
        "status": "ok",
        "fetch_status": "ok",
        "missing_fields": [],
        "field_evidence": {
            "market_cap": {
                "value": 1000, "source": "eastmoney", "source_level": "A",
                "as_of": "2026-08-27", "period": "current",
                "caliber": "current_market_cap", "evidence_id": "fe1",
            },
            "net_profit": {
                "value": 50, "source": "akshare", "source_level": "A",
                "as_of": "2025-12-31", "period": "2025",
                "caliber": "net_profit", "evidence_id": "fe2",
            },
        },
        "fallback_results": [],
    }
    meta = evidence_metadata("market.get_bundle", content)
    assert meta["fetch_status"] == "ok"
    assert meta["field_evidence"] == content["field_evidence"]
    # 两个字段 → 两条 field citation
    assert len(meta["citations"]) == 2
    c0 = meta["citations"][0]
    assert c0["kind"] == "field"
    assert c0["field"] == "market_cap"
    assert c0["evidence_id"] == "fe1"
    assert c0["caliber"] == "current_market_cap"
    assert c0["source"] == "eastmoney"


def test_metadata_market_bundle_expands_fallback_results() -> None:
    content = {
        "fetch_status": "partial",
        "missing_fields": ["revenue"],
        "field_evidence": {},
        "fallback_results": [
            {"title": "年报", "url": "https://cninfo.com.cn/r",
             "source_level": "A", "snippet": "营收 20 亿"},
        ],
    }
    meta = evidence_metadata("market.get_bundle", content)
    assert meta["fetch_status"] == "partial"
    assert meta["missing_fields"] == ["revenue"]
    # fallback_results → 1 条 web citation
    assert len(meta["citations"]) == 1
    c = meta["citations"][0]
    assert c["kind"] == "fallback_search"
    assert c["url"] == "https://cninfo.com.cn/r"
    assert c["source_level"] == "A"


def test_metadata_market_bundle_empty_field_evidence() -> None:
    """market.get_bundle 但没取到字段（全缺失）—— citations 为空但不报错。"""
    content = {
        "fetch_status": "degraded",
        "missing_fields": ["market_cap", "net_profit"],
        "field_evidence": {},
        "fallback_results": [],
    }
    meta = evidence_metadata("market.get_bundle", content)
    assert meta["fetch_status"] == "degraded"
    assert set(meta["missing_fields"]) == {"market_cap", "net_profit"}
    assert meta["citations"] == []


# ── evidence_metadata: calc.metric（字段注入 evidence_id）─────

def test_metadata_calc_metric_injects_evidence_id_to_fields() -> None:
    content = {
        "metric_id": "pe_ttm",
        "value": 20.0,
        "unit": "x",
        "status": "ok",
        "fetch_status": "ok",
        "missing_fields": [],
        "fields": [
            {"field": "market_cap", "value": 1000, "source": "eastmoney"},
            {"field": "net_profit", "value": 50, "source": "akshare"},
        ],
        "field_evidence": {
            "market_cap": {"evidence_id": "fe1", "source": "eastmoney",
                           "source_level": "A", "value": 1000},
            "net_profit": {"evidence_id": "fe2", "source": "akshare",
                           "source_level": "A", "value": 50},
        },
    }
    meta = evidence_metadata("calc.metric", content)
    assert meta["fetch_status"] == "ok"
    assert meta["field_evidence"] == content["field_evidence"]
    # fields 注入了 evidence_id
    f0 = meta["fields"][0]
    assert f0["evidence_id"] == "fe1"
    assert f0["source_level"] == "A"
    f1 = meta["fields"][1]
    assert f1["evidence_id"] == "fe2"
    # citation 带 evidence_ids 聚合
    assert "fe1" in meta["citations"][0]["evidence_ids"]
    assert "fe2" in meta["citations"][0]["evidence_ids"]


def test_metadata_calc_metric_without_field_evidence() -> None:
    """calc.metric 没透传 field_evidence（旧路径）—— fields 不注入 evidence_id，不报错。"""
    content = {
        "metric_id": "pe_ttm",
        "value": 20.0,
        "fields": [{"field": "market_cap", "value": 1000}],
    }
    meta = evidence_metadata("calc.metric", content)
    assert meta["fields"][0].get("evidence_id", "") == ""
    assert meta["citations"][0]["evidence_ids"] == []


# ── Trace.add_evidence 灌入 P3 字段 ────────────────────────

def test_add_evidence_market_bundle_injects_p3_fields() -> None:
    tr = Trace()
    content = {
        "symbol": "600519",
        "status": "ok",
        "fetch_status": "partial",
        "missing_fields": ["revenue"],
        "field_evidence": {
            "market_cap": {"value": 1000, "source": "eastmoney",
                           "source_level": "A", "evidence_id": "fe1"},
        },
        "fallback_results": [
            {"title": "年报", "url": "https://cninfo.com.cn/r", "source_level": "A"},
        ],
        "prompt_block": "市值 1000 亿",
    }
    entry = tr.add_evidence("market.get_bundle", content, level="A")
    assert entry.fetch_status == "partial"
    assert entry.missing_fields == ["revenue"]
    assert "market_cap" in entry.field_evidence
    # citations 含 1 条 field + 1 条 fallback_search
    kinds = [c["kind"] for c in entry.citations]
    assert "field" in kinds
    assert "fallback_search" in kinds


def test_add_evidence_calc_metric_injects_p3_fields() -> None:
    tr = Trace()
    content = {
        "metric_id": "pe_ttm",
        "value": 20.0,
        "unit": "x",
        "status": "ok",
        "fetch_status": "ok",
        "missing_fields": [],
        "fields": [{"field": "market_cap", "value": 1000}],
        "field_evidence": {
            "market_cap": {"evidence_id": "fe1", "source": "eastmoney",
                           "source_level": "A", "value": 1000},
        },
    }
    entry = tr.add_evidence("calc.metric", content, level="A")
    assert entry.fetch_status == "ok"
    assert entry.field_evidence == content["field_evidence"]
    # fields 注入了 evidence_id
    assert entry.fields[0]["evidence_id"] == "fe1"


def test_evidence_entry_to_dict_outputs_p3_fields() -> None:
    tr = Trace()
    content = {
        "symbol": "600519",
        "fetch_status": "degraded",
        "missing_fields": ["revenue", "net_profit"],
        "field_evidence": {
            "market_cap": {"value": 1000, "source": "eastmoney",
                           "source_level": "A", "evidence_id": "fe1"},
        },
        "fallback_results": [],
        "prompt_block": "市值 1000 亿",
    }
    entry = tr.add_evidence("market.get_bundle", content, level="A")
    d = entry.to_dict()
    assert d["fetch_status"] == "degraded"
    assert set(d["missing_fields"]) == {"revenue", "net_profit"}
    assert d["field_evidence"]["market_cap"]["evidence_id"] == "fe1"
    # citations 含字段级 evidence_id
    field_cits = [c for c in d["citations"] if c["kind"] == "field"]
    assert len(field_cits) == 1
    assert field_cits[0]["evidence_id"] == "fe1"


def test_trace_to_dict_includes_p3_fields_in_evidence_pack() -> None:
    """Trace.to_dict 的 evidence_pack 含 P3 字段。"""
    tr = Trace()
    content = {
        "symbol": "600519",
        "fetch_status": "partial",
        "missing_fields": ["revenue"],
        "field_evidence": {
            "market_cap": {"value": 1000, "source": "eastmoney",
                           "source_level": "A", "evidence_id": "fe1"},
        },
        "fallback_results": [],
        "prompt_block": "市值 1000 亿",
    }
    tr.add_evidence("market.get_bundle", content, level="A")
    d = tr.to_dict()
    assert d["evidence_pack"]
    ep = d["evidence_pack"][0]
    assert ep["fetch_status"] == "partial"
    assert "market_cap" in ep["field_evidence"]
    assert ep["missing_fields"] == ["revenue"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
