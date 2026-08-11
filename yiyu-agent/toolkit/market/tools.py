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

logger = logging.getLogger(__name__)

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
    """获取标的的完整行情包（快照 + 财报 + 新闻）。"""

    schema = ToolSchema(
        name="market.get_bundle",
        description=(
            "获取一个标的的完整行情数据包：实时价格快照、财务报表、近期新闻/公告。"
            "返回包含 data_status 的结构化数据，数据缺失时会降级标注，不会报错中断。"
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
            },
            "required": ["symbol"],
        },
        read_only=True,
        max_chars=8000,
        timeout_seconds=20,
    )

    async def execute(self, symbol: str, days: int = 7, **kwargs: Any) -> dict:
        md = _get_market_data()
        bundle = await md.bundle(symbol, days=days)
        # 直接返回 prompt block 文本 + 结构化 status，供 loop 注入 observations
        return {
            "symbol": bundle.symbol,
            "status": bundle.status.value,
            "prompt_block": bundle.to_prompt_block(),
        }


class MarketSnapshotTool(ReadOnlyTool):
    """仅取价格+估值快照（轻量，适合快筛/持仓追踪）。"""

    schema = ToolSchema(
        name="market.get_snapshot",
        description="获取标的的实时价格快照：现价、涨跌幅、市值、PE/PB、52 周区间。比 get_bundle 轻量。",
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
            "pe": snap.pe,
            "pb": snap.pb,
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
