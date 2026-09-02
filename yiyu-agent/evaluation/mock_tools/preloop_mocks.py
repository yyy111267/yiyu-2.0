"""循环前处理评测共享 mock 辅助（环节① facts_builder / ② granularity / ③ profiler）。

复用对话里已验证的确定性 mock 机制：
- _make_llm_client(responses): MagicMock 客户端，按顺序返回 chat_json 响应，超出重复末条。
- _make_web_search(results): 联网检索 mock。
- _make_market_data(years): MarketData.bundle 返回指定 fundamentals.years。
- make_entity(symbol, name, ...): 构造 CurrentEntitySchema 测试实体。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from runtime.schemas import CurrentEntitySchema


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
    """联网检索 mock。

    每条结果若未带 snippet（正文摘要），自动补全一段业务描述，
    避免 facts_builder 在"标题+空摘要"的贫瘠输入下反复重试降级。
    """
    enriched = []
    for i, r in enumerate(results):
        item = dict(r)
        if not item.get("snippet"):
            # 默认补全：让 LLM 有正文可抽取 one_line_business / segments
            name = item.get("title", item.get("url", f"公司{i}"))
            item["snippet"] = (
                f"{name}主要从事白酒生产及销售业务，旗下核心产品为高端酱香型白酒，"
                f"营收占比超 90%；采用「先款后货」的一次性销售模式，面向高端消费与商务场景，"
                f"客户以经销商与直营渠道为主，竞争优势来自品牌壁垒与产地稀缺性，"
                f"属轻资产、弱周期、现金流充沛的商业模式。"
            )
        enriched.append(item)

    async def web_search_fn(query, max_results=5, sources=None, **kwargs):
        return {
            "results": enriched[:max_results],
            "count": min(len(enriched), max_results),
            "sources_verified": bool(enriched),
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


def make_entity(symbol: str, name: str, exchange: str = "",
                alias_type: str = "fullname", entity_source: str = "explicit",
                primary_market: str = "A") -> CurrentEntitySchema:
    """构造测试用 current_entity。

    注意：CurrentEntitySchema 必填字段为 canonical_name / security_id / code /
    exchange / primary_market / entity_source / alias_type（alias_type 枚举：
    fullname/abbreviation/nickname/brand_or_subsidiary/fragment/description）。
    """
    return CurrentEntitySchema(
        security_id=symbol,
        canonical_name=name,
        code=symbol.split(".")[0],
        exchange=exchange or (".SZ" if symbol.endswith(".SZ") else (".SH" if symbol.endswith(".SH") else "")),
        primary_market=primary_market,
        alias_type=alias_type,
        entity_source=entity_source,
    )
