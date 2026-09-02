"""取数层搜索兜底 —— 同一 symbol+component 连续失败 >= 3 次后转白名单搜索。

不要让兜底变成新的慢路径：
  - 最多 1 次搜索
  - 最多取前 3 条白名单结果
  - 总预算 5s
  - 白名单优先：巨潮/交易所/公司公告/SEC/HKEX（A 级），
    东方财富/新浪/Yahoo Finance 可作 B 级
  - 搜不到就返回缺失，不继续 retry

输出不是「模型理解后的数字」，而是 evidence（标题+URL+摘要+source_level）。
Metric Service 若没有结构化字段，仍返回 not_disclosed，但最终报告可展示
「已尝试搜索，未获得可复算结构化字段」。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 总预算 5s
FALLBACK_BUDGET_SECONDS: float = 5.0
# 最多取前 3 条结果
FALLBACK_MAX_RESULTS: int = 3

# 搜索查询模板（按 component 拼词）
_QUERY_TEMPLATES: dict[str, str] = {
    "snapshot": "{symbol} 股价 市值 PE PB",
    "fundamentals": "{symbol} 年报 营收 净利润 现金流",
    "news": "{symbol} 新闻 公告",
    "announcements": "{symbol} 公告 巨潮",
}


@dataclass
class FallbackResult:
    """一次搜索兜底的输出。"""

    status: str = "fallback_search"           # fallback_search / fallback_failed / fallback_skipped
    results: list[dict] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    query: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


async def fallback_search(
    symbol: str,
    component: str,
    missing_fields: list[str] | None = None,
    *,
    search_fn: Any = None,
    budget_seconds: float = FALLBACK_BUDGET_SECONDS,
) -> FallbackResult:
    """对失败组件做一次白名单搜索兜底。

    search_fn 为注入的搜索协程：async (query, max_results, sources) -> dict。
    生产留空则惰性创建 WebSearchTool；测试可注入假实现。
    """
    missing = list(missing_fields or [])
    if search_fn is None:
        search_fn = _default_search_fn

    query = _QUERY_TEMPLATES.get(component, "{symbol} 财报").format(symbol=symbol)
    try:
        raw = await asyncio.wait_for(
            search_fn(query=query, max_results=FALLBACK_MAX_RESULTS, sources="finance"),
            timeout=budget_seconds,
        )
    except asyncio.TimeoutError:
        return FallbackResult(
            status="fallback_failed",
            missing_fields=missing,
            query=query,
            error=f"search timed out after {budget_seconds}s",
        )
    except Exception as e:  # noqa: BLE001 - 兜底搜索失败不能阻塞 Agent
        logger.warning("fallback_search [%s/%s] 失败: %s", symbol, component, e)
        return FallbackResult(
            status="fallback_failed",
            missing_fields=missing,
            query=query,
            error=f"{type(e).__name__}: {e}",
        )

    results = _extract_results(raw)
    if not results:
        return FallbackResult(
            status="fallback_failed",
            missing_fields=missing,
            query=query,
            error="no results",
        )
    return FallbackResult(
        status="fallback_search",
        results=results[:FALLBACK_MAX_RESULTS],
        missing_fields=missing,
        query=query,
    )


def _extract_results(raw: Any) -> list[dict]:
    """从 web.search 返回结构里抽出带 url 的结果，统一成 evidence 形态。"""
    if not isinstance(raw, dict):
        return []
    out: list[dict] = []
    for item in raw.get("results") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        out.append({
            "title": str(item.get("title") or item.get("url") or ""),
            "url": str(item.get("url") or ""),
            "source_level": str(item.get("source_level") or "B"),
            "snippet": str(item.get("snippet") or "")[:500],
            "as_of": str(item.get("published_at") or item.get("as_of") or ""),
        })
    return out


async def _default_search_fn(*, query: str, max_results: int, sources: Any) -> dict:
    """惰性创建 WebSearchTool 执行一次搜索。"""
    from toolkit.web.tools import WebSearchTool
    tool = WebSearchTool()
    return await tool.execute(query=query, max_results=max_results, sources=sources)
