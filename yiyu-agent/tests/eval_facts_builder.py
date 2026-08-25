"""
facts_builder 评测骨架

使用方式：
  PYTHONPATH=. python3 tests/eval_facts_builder.py

每条评测用例结构：
  {
    "id":          str,          # 用例编号
    "desc":        str,          # 场景描述
    "entity":      dict,         # CurrentEntitySchema 构造参数
    "search_results": list[dict],# mock 联网搜索返回（[{title,snippet,url,source_level}]）
    "llm_responses": list[dict], # mock LLM 依次返回的 JSON（支持多轮重试）
    "bundle_years": list[dict],  # mock market.bundle fundamentals.years
    "bundle_currency": str,      # mock snapshot.currency，默认 "CNY"
    "expect": {
        "one_line_not_empty":   bool,   # one_line_business 非空
        "min_segments":         int,    # segments 至少有几个
        "source_trace_fields":  list,   # source_trace 必须包含的 field 名
        "has_revenue_ttm":      bool,   # financial_snapshot.revenue_ttm 非 None
        "has_gross_margin":     bool,
        "open_qs_contains":     list,   # open_questions 必须包含的关键词（子串匹配）
        "open_qs_not_contains": list,   # open_questions 不应包含的关键词
        "facts_version_not_empty": bool,
        "cache_hit_on_second_call": bool, # 第二次调用应命中缓存（不触发 LLM）
    }
  }

────────────────────────────────────────────────
【在下方 CASES 列表里填入你的评测用例】
────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")

from runtime.preloop.facts_builder import build_company_facts, clear_cache
from runtime.schemas import AliasType, CurrentEntitySchema, EntitySource

# ════════════════════════════════════════════════════════════════
# 填入评测用例
# ════════════════════════════════════════════════════════════════

CASES: list[dict] = [
    # ── 示例占位（运行时会跳过 id 以 "placeholder" 开头的） ──
    {
        "id": "placeholder_01",
        "desc": "示例占位，请替换为真实用例",
        "entity": {
            "canonical_name": "示例公司",
            "security_id": "000001.SZ",
            "code": "000001",
            "exchange": "SZ",
            "primary_market": "A",
            "entity_source": "explicit",
            "alias_type": "abbreviation",
        },
        "search_results": [],
        "llm_responses": [{}],
        "bundle_years": [],
        "bundle_currency": "CNY",
        "expect": {
            "one_line_not_empty": False,
            "min_segments": 0,
            "source_trace_fields": [],
            "has_revenue_ttm": False,
            "has_gross_margin": False,
            "open_qs_contains": [],
            "open_qs_not_contains": [],
            "facts_version_not_empty": True,
            "cache_hit_on_second_call": True,
        },
    },
]

# ════════════════════════════════════════════════════════════════
# 评测执行框架（无需修改）
# ════════════════════════════════════════════════════════════════


def _make_entity(d: dict) -> CurrentEntitySchema:
    return CurrentEntitySchema(
        canonical_name=d["canonical_name"],
        security_id=d["security_id"],
        code=d["code"],
        exchange=d["exchange"],
        primary_market=d["primary_market"],
        entity_source=EntitySource(d.get("entity_source", "explicit")),
        alias_type=AliasType(d.get("alias_type", "fullname")),
        scope_note=d.get("scope_note"),
    )


def _make_llm_client(responses: list[dict]):
    """按顺序依次返回 responses 里的 dict；超出后重复最后一个。"""
    call_count = [0]

    async def chat_json(system, user, temperature=0.1, **kwargs):
        idx = min(call_count[0], len(responses) - 1)
        call_count[0] += 1
        return responses[idx]

    client = MagicMock()
    client.chat_json = chat_json
    client._call_count = call_count
    return client


def _make_web_search(results: list[dict]):
    async def web_search_fn(query, max_results=5, sources=None, **kwargs):
        return {
            "results": results[:max_results],
            "count": min(len(results), max_results),
            "sources_verified": bool(results),
        }
    return web_search_fn


def _make_market_data(years: list[dict], currency: str = "CNY"):
    """构造 mock MarketData，返回指定 fundamentals.years。"""
    fundamentals = MagicMock()
    fundamentals.years = years
    fundamentals.source = "mock"
    fundamentals.asof = "2025-12-31"

    snapshot = MagicMock()
    snapshot.currency = currency
    snapshot.name = "Mock Company"

    bundle = MagicMock()
    bundle.fundamentals = fundamentals
    bundle.snapshot = snapshot
    bundle.errors = []

    market_data = MagicMock()
    market_data.bundle = AsyncMock(return_value=bundle)
    return market_data


@dataclass
class CaseResult:
    case_id: str
    desc: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    facts_summary: dict = field(default_factory=dict)


def _check(facts, expect: dict, llm_client) -> list[str]:
    failures: list[str] = []

    if expect.get("one_line_not_empty", True):
        if not facts.one_line_business or "待补充" in facts.one_line_business:
            failures.append(f"one_line_business 为空或降级: {facts.one_line_business!r}")

    min_segs = expect.get("min_segments", 0)
    if len(facts.segments) < min_segs:
        failures.append(f"segments 数量 {len(facts.segments)} < 期望 {min_segs}")

    for f_field in expect.get("source_trace_fields", []):
        traced = {t.field for t in facts.source_trace}
        if f_field not in traced:
            failures.append(f"source_trace 缺少 field={f_field!r}，实际: {sorted(traced)}")

    if expect.get("has_revenue_ttm") and facts.financial_snapshot.revenue_ttm is None:
        failures.append("financial_snapshot.revenue_ttm 应非空")

    if expect.get("has_gross_margin") and facts.financial_snapshot.gross_margin is None:
        failures.append("financial_snapshot.gross_margin 应非空")

    for kw in expect.get("open_qs_contains", []):
        if not any(kw in q for q in facts.open_questions):
            failures.append(f"open_questions 应含关键词 {kw!r}，实际: {facts.open_questions}")

    for kw in expect.get("open_qs_not_contains", []):
        if any(kw in q for q in facts.open_questions):
            failures.append(f"open_questions 不应含关键词 {kw!r}")

    if expect.get("facts_version_not_empty", True):
        if not facts.facts_version:
            failures.append("facts_version 为空")

    return failures


async def run_case(case: dict) -> CaseResult:
    cid = case["id"]
    desc = case["desc"]

    if cid.startswith("placeholder"):
        return CaseResult(cid, desc, passed=True, failures=["[跳过占位用例]"])

    clear_cache()  # 每个用例前清缓存，保证独立

    entity = _make_entity(case["entity"])
    llm_client = _make_llm_client(case["llm_responses"])
    web_search_fn = _make_web_search(case["search_results"])
    market_data = _make_market_data(
        case.get("bundle_years", []),
        case.get("bundle_currency", "CNY"),
    )

    try:
        facts = await build_company_facts(
            entity, llm_client, market_data, web_search_fn
        )
    except Exception as e:
        return CaseResult(cid, desc, passed=False,
                          failures=[f"build_company_facts 抛出异常: {e}"])

    failures = _check(facts, case["expect"], llm_client)

    # 缓存命中测试：第二次调用不应触发 LLM
    if case["expect"].get("cache_hit_on_second_call"):
        call_count_before = llm_client._call_count[0]
        facts2 = await build_company_facts(
            entity, llm_client, market_data, web_search_fn
        )
        call_count_after = llm_client._call_count[0]
        if call_count_after != call_count_before:
            failures.append(
                f"第二次调用触发了 LLM（call_count {call_count_before}→{call_count_after}），"
                "缓存未命中"
            )
        if facts2.facts_version != facts.facts_version:
            failures.append(
                f"缓存命中后 facts_version 不一致: "
                f"{facts.facts_version!r} vs {facts2.facts_version!r}"
            )

    summary = {
        "one_line": facts.one_line_business[:40] if facts.one_line_business else "",
        "segments": [s.name for s in facts.segments],
        "revenue_ttm": facts.financial_snapshot.revenue_ttm,
        "gross_margin": facts.financial_snapshot.gross_margin,
        "open_qs": facts.open_questions,
        "source_trace_count": len(facts.source_trace),
        "facts_version": facts.facts_version,
    }
    return CaseResult(cid, desc, passed=len(failures) == 0,
                      failures=failures, facts_summary=summary)


async def main():
    real_cases = [c for c in CASES if not c["id"].startswith("placeholder")]
    if not real_cases:
        print("⚠  CASES 列表为空（只有占位用例），请填入评测用例后重新运行。")
        return

    print(f"\n运行 {len(real_cases)} 条评测用例...\n")
    results = []
    for case in real_cases:
        r = await run_case(case)
        results.append(r)
        icon = "✓" if r.passed else "✗"
        print(f"  {icon} [{r.case_id}] {r.desc}")
        if not r.passed:
            for f in r.failures:
                print(f"      ↳ {f}")
        else:
            print(f"      ↳ summary: {r.facts_summary}")

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\n{'='*50}")
    print(f"通过 {passed} / {total}  {'✓ 全部通过' if passed == total else f'✗ {total - passed} 个失败'}")


if __name__ == "__main__":
    asyncio.run(main())
