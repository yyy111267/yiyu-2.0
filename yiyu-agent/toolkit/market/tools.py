"""
行情数据工具集 —— 把 toolkit/market/market.py 的 MarketData 门面封装为 Tool 契约。

封装为三个只读 Tool：
  - market.get_bundle     : 快照+财报+新闻 完整包（深度研究主入口）
  - market.get_snapshot  : 仅价格+估值快照（快筛/持仓追踪）
  - market.get_fundamentals : 仅财报（研究员复用）

延迟初始化：MarketData 依赖 settings 与外部 provider（含网络），故按需创建并缓存。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from toolkit.base import Tool, ToolResult, ToolSchema, ReadOnlyTool
from toolkit.market.research_data import get_research_bundle, store_research_bundle

logger = logging.getLogger(__name__)

# 深度研究不传指标时的最小完整性契约。这些字段也会进入
# field_evidence / missing_fields，使循环能区分“工具没报错”和“财务数据已齐”。
_DEFAULT_RESEARCH_FIELDS = [
    "price", "market_cap", "revenue", "net_profit",
    "gross_margin", "operating_cash_flow",
]

# 单例缓存：避免每次 tool call 都重建 httpx client
_market_data_instance: Optional["MarketData"] = None  # type: ignore[name-defined]


def _get_market_data():
    """惰性创建 MarketData 单例（首次调用时读 settings + 装配 providers）。"""
    global _market_data_instance
    if _market_data_instance is None:
        from core.config import settings
        from toolkit.market.market import MarketData
        _market_data_instance = MarketData(settings)
    return _market_data_instance


class MarketBundleTool(ReadOnlyTool):
    """获取标的的行情数据包（按需取数 + 快失败 + evidence 登记）。"""

    schema = ToolSchema(
        name="market.get_bundle",
        description=(
            "获取一个标的的行情数据包：实时价格快照、财务报表、近期新闻/公告。"
            "返回包含 data_status 的结构化数据，数据缺失时会降级标注，不会报错中断。\n"
            "P0 取数层改造：支持按需取数。\n"
            "  - metric_ids：从 metrics_catalog.yaml 展开 required_fields，只取覆盖这些字段的组件，"
            "不取新闻/公告除非显式点名。例 metric_ids=['pe_ttm'] 只取 snapshot+fundamentals，不取新闻。\n"
            "  - field_groups：显式指定组件（snapshot/fundamentals/news/announcements）。\n"
            "  - 按 Source Mapping 逐字段执行主备路由，一旦成功就停止该字段的降级。\n"
            "  - 结构化源仍有缺口时返回原因和 missing_fields，由 Agent 决定是否联网搜索。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码，支持 A 股(600519/000001)、港股(00700.HK)、美股(AAPL)"
                },
                "days": {
                    "type": "integer",
                    "description": "新闻回看天数，默认 7",
                    "default": 7,
                },
                "metric_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "按指标反推要取的字段。例 ['pe_ttm'] 只取 market_cap+net_profit，"
                        "不取新闻/公告。与 field_groups 二选一，metric_ids 优先。"
                    ),
                },
                "requested_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "按 canonical 字段名请求数据；只取缺少或指定的字段。"
                        "例如 ['cash_dividend_ttm', 'market_cap']。"
                    ),
                },
                "field_groups": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "显式指定要取的组件：snapshot/fundamentals/news/announcements。"
                        "不传则取全量（向后兼容）。"
                    ),
                },
                "max_attempts": {
                    "type": "integer",
                    "description": "同一组件连续失败多少次后转搜索兜底，默认 3。",
                    "default": 3,
                },
                "fallback_web_search": {
                    "type": "boolean",
                    "description": "旧整包取数路径的搜索开关；字段级路径只返回缺口给 Agent。",
                    "default": True,
                },
            },
            "required": ["symbol"],
        },
        read_only=True,
        max_chars=8000,
        timeout_seconds=20,
    )

    async def execute(
        self,
        symbol: str,
        days: int = 7,
        metric_ids: list[str] | None = None,
        requested_fields: list[str] | None = None,
        field_groups: list[str] | None = None,
        max_attempts: int = 3,
        fallback_web_search: bool = True,
        **kwargs: Any,
    ) -> dict:
        md = _get_market_data()
        bundle = await md.bundle(
            symbol,
            days=days,
            metric_ids=metric_ids,
            requested_fields=requested_fields,
            field_groups=field_groups,
            max_attempts=max_attempts,
            fallback_web_search=fallback_web_search,
        )
        data_pack_id = store_research_bundle(bundle)
        stored = get_research_bundle(symbol)
        if stored is not None:
            data_pack_id, bundle = stored
        # 返回 prompt block 文本 + 结构化 status + P0 新字段
        return {
            "symbol": bundle.symbol,
            "status": bundle.status.value,
            "fetch_status": bundle.fetch_status,
            "structured_status": bundle.structured_status,
            "missing_fields": list(bundle.missing_fields),
            "field_evidence": bundle.field_evidence,
            "field_sources": bundle.field_sources,
            "source_mapping": bundle.source_mapping,
            "fallback_results": list(bundle.fallback_results),
            "fetch_attempts": list(bundle.fetch_attempts),
            "data_pack_id": data_pack_id,
            "prompt_block": bundle.to_prompt_block(),
        }


class MarketSnapshotTool(ReadOnlyTool):
    """仅取价格+估值快照（轻量，适合快筛/持仓追踪）。"""

    schema = ToolSchema(
        name="market.get_snapshot",
        description="获取标的的实时价格快照：现价、涨跌幅、总/流通市值、PE(TTM)/动态PE/PS(TTM)/PB、行业、上市日期、52 周区间。比 get_bundle 轻量。",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码"},
            },
            "required": ["symbol"],
        },
        read_only=True,
        max_chars=2000,
        timeout_seconds=15,
    )

    async def execute(self, symbol: str, **kwargs: Any) -> dict:
        md = _get_market_data()
        snap = await md.snapshot(symbol)
        if snap is None:
            return {"symbol": symbol, "status": "degraded", "error": "未能获取快照"}
        return {
            "symbol": snap.symbol,
            "name": snap.name,
            "price": snap.price,
            "change_pct": snap.change_pct,
            "market_cap": snap.market_cap,
            "float_market_cap": snap.float_market_cap,
            "pe": snap.pe,
            "pe_dynamic": snap.pe_dynamic,
            "pe_static": snap.pe_static,
            "pe_ttm": snap.pe_ttm,
            "ps_ttm": snap.ps_ttm,
            "pb": snap.pb,
            "industry": snap.industry,
            "listing_date": snap.listing_date,
            "currency": snap.currency,
            "asof": snap.asof,
        }


class MarketFundamentalsTool(ReadOnlyTool):
    """仅取财报数据（研究员子 Agent 复用）。"""

    schema = ToolSchema(
        name="market.get_fundamentals",
        description="获取标的近年财务报表：营收、净利、毛利率、ROE、现金流、负债率等。",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码"},
            },
            "required": ["symbol"],
        },
        read_only=True,
        max_chars=4000,
        timeout_seconds=20,
    )

    async def execute(self, symbol: str, **kwargs: Any) -> dict:
        md = _get_market_data()
        fund = await md.fundamentals(symbol)
        if fund is None:
            return {"symbol": symbol, "status": "degraded", "error": "未能获取财报"}
        return {
            "symbol": fund.symbol,
            "source": fund.source,
            "years": fund.years,
            "asof": fund.asof,
        }


# 便于 registry 批量注册
MARKET_TOOLS: list[Tool] = [
    MarketBundleTool(),
    MarketSnapshotTool(),
    MarketFundamentalsTool(),
]
