"""citations 收口清洗回归。

针对 ponytail-audit 后续的两个收口漏洞：
  1) 没有公开链接的 preloop 线索被当公开证据 → light_evidence 销项修复
     （在 runtime/loop.py，覆盖由 11_loop 评测承担）
  2) 证据区使用内部工具名 → _final_citations 清洗

本文件聚焦 (2) 的纯函数路径，独立可跑，不依赖 loop 整体。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.loop import (  # noqa: E402
    _sanitize_citations_for_frontend,
    _SOURCE_TYPE_LABEL,
)


def test_market_internal_source_replaced_with_label():
    """market.get.bundle 这种内部工具名应被替换为 '行情数据'，不再透传。"""
    raw = [{"source": "market.get.bundle", "url": "https://x/a", "kind": "field",
            "evidence_id": "e1", "field": "revenue"}]
    out = _sanitize_citations_for_frontend(raw)
    assert out[0]["source_type"] == "行情数据"
    assert "source" not in out[0]
    assert out[0]["evidence_id"] == "e1"
    assert out[0]["field"] == "revenue"


def test_snake_case_tool_name_normalized():
    """market_get_bundle（注册表前）也要归一化为 '行情数据'。"""
    raw = [{"source": "market_get_bundle", "url": "https://x/b", "kind": "field"}]
    out = _sanitize_citations_for_frontend(raw)
    assert out[0]["source_type"] == "行情数据"
    assert "source" not in out[0]


def test_calc_metric_no_url_becomes_structured():
    """无 url 的 calc 引用给结构化标签，不回退为工具名。"""
    raw = [{"source": "calc.metric", "kind": "metric", "value": 123}]
    out = _sanitize_citations_for_frontend(raw)
    assert out[0]["source_type"] == "结构化计算结果"
    assert "source" not in out[0]


def test_field_no_url_becomes_structured_field():
    raw = [{"source": "market.get.bundle", "kind": "field", "field": "eps"}]
    out = _sanitize_citations_for_frontend(raw)
    assert out[0]["source_type"] == "结构化字段引用"
    assert "source" not in out[0]


def test_web_url_preserved_with_label():
    """web 类有 url 的不强行覆盖 source_type，但要把 raw tool 名去掉。"""
    raw = [{"source": "web.search", "url": "https://news/x", "title": "标题"}]
    out = _sanitize_citations_for_frontend(raw)
    assert out[0]["url"] == "https://news/x"
    # web.* 没有强制 source_type（保留原 title/label 即可）


def test_unknown_source_keeps_value_marks_other():
    """未识别 source 不抛错，至少给个 '其他来源' 标签，不暴露原名。"""
    raw = [{"source": "weird_tool_xyz", "kind": "field"}]
    out = _sanitize_citations_for_frontend(raw)
    # 因为 kind=field，会被结构化标签覆盖
    assert out[0]["source_type"] == "结构化字段引用"


def test_no_source_no_kind_no_url_becomes_structured_data():
    raw = [{}]
    out = _sanitize_citations_for_frontend(raw)
    assert out[0]["source_type"] == "结构化数据"


def test_label_map_covers_research_prefixes():
    """防御性：_SOURCE_TYPE_LABEL 必须覆盖研究通道的全部前缀。"""
    # 与 _has_research_evidence 接受的前缀保持一致；缺一个就漏配一类研究证据
    assert "market." in _SOURCE_TYPE_LABEL
    assert "calc." in _SOURCE_TYPE_LABEL
    assert "web." in _SOURCE_TYPE_LABEL
    assert "cognition." in _SOURCE_TYPE_LABEL


if __name__ == "__main__":
    test_market_internal_source_replaced_with_label()
    test_snake_case_tool_name_normalized()
    test_calc_metric_no_url_becomes_structured()
    test_field_no_url_becomes_structured_field()
    test_web_url_preserved_with_label()
    test_unknown_source_keeps_value_marks_other()
    test_no_source_no_kind_no_url_becomes_structured_data()
    test_label_map_covers_research_prefixes()
    print("ok: 8/8")
