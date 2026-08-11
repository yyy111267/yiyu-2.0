"""行情数据基础设施层（Phase 0）：多 provider 路由的横切层。

深度研究 / 持仓追踪 / 筛股共用此层，不是任何单个切片的私有件。
数据源分工（已拍板：A股+港美股全要，MVP 单源、预留双源位）：
- A股      → 东财实时行情（主）→ 新浪（备）；akshare 财报+新闻；巨潮资讯网公告
- 港股/美股 → 东财实时行情（主，snapshot）→ yfinance（兜底 snapshot + 财报/新闻）

降级铁律：任一数据源超时/异常 → 对应字段置空并记 error，绝不中断流程；
prompt 注入时按 data_status 强制标注，严禁用模型训练数据冒充实时行情。
双源比对：MVP 不实现；bundle(use_dual=True) 仅为预留位。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

import httpx

from toolkit.market.market_cache import MarketCache
from toolkit.market.market_router import classify_symbol, normalize_hk_symbol, normalize_us_symbol, westock_code

if TYPE_CHECKING:
    from core.config import Settings

logger = logging.getLogger(__name__)

# 数据源 UA（新浪/巨潮无 UA 会拒绝服务）
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


class DataStatus(str, Enum):
    OK = "ok"                # 快照 + 财报均取到
    PARTIAL = "partial"      # 快照或财报其一缺失
    DEGRADED = "degraded"    # 核心数据全缺，仅可降级回答


@dataclass
class Snapshot:
    """价格 + 估值快照。金额为原始单位（元/港元/美元），展示层负责格式化。"""

    symbol: str
    source: str                       # eastmoney / sina / yfinance
    name: str | None = None
    price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None   # 百分比数值，如 1.23 表示 +1.23%
    market_cap: float | None = None
    pe: float | None = None
    pb: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    currency: str | None = None
    asof: str | None = None           # 数据时间戳，'YYYY-MM-DD HH:MM:SS'


@dataclass
class Fundamentals:
    """财务数据。years 元素键可缺：year, revenue, net_profit, gross_margin, roe,
    ocf, capex, debt_ratio；金额为原始单位，比率为百分比数值。"""

    symbol: str
    source: str                       # akshare / yfinance
    years: list[dict] = field(default_factory=list)
    asof: str | None = None


@dataclass
class NewsItem:
    title: str
    source: str                       # akshare / cninfo / yfinance
    category: str = "news"            # news | announcement（巨潮公告）
    url: str | None = None
    published_at: str | None = None
    summary: str | None = None


@dataclass
class MarketBundle:
    """一个标的一次取数的完整结果，含降级状态与异常清单。"""

    symbol: str
    status: DataStatus
    snapshot: Snapshot | None = None
    fundamentals: Fundamentals | None = None
    news: list[NewsItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        """注入 LLM prompt 的市场数据文本块；按降级状态加强制声明。"""
        lines: list[str] = []
        if self.status is DataStatus.DEGRADED:
            lines.append(
                "【未能获得实时市场数据】以下回答若涉及行情与财务，必须标注"
                "「基于模型训练知识（截止 2024 前后），置信度已降级」，"
                "严禁将训练数据当作实时行情。"
            )
        elif self.status is DataStatus.PARTIAL:
            lines.append(
                "【市场数据不完整】缺失字段以「—」标注，不得编造；"
                "涉及缺失部分的判断须提示用户做一手验证。"
            )
        lines.append(f"【市场数据 · {self.symbol}】数据状态: {self.status.value}")

        if self.snapshot:
            s = self.snapshot
            lines.append(f"— 快照（来源: {s.source}，截至 {s.asof or '未知'}）—")
            pct = f"{s.change_pct:+.2f}%" if s.change_pct is not None else "—"
            cur = f" {s.currency}" if s.currency else ""
            lines.append(
                f"名称: {s.name or '—'} | 现价: {_fmt_num(s.price)}{cur} | "
                f"涨跌幅: {pct} | 昨收: {_fmt_num(s.prev_close)}"
            )
            w52 = (
                f"{_fmt_num(s.week52_low)} ~ {_fmt_num(s.week52_high)}"
                if s.week52_low is not None or s.week52_high is not None
                else "—"
            )
            lines.append(
                f"总市值: {_fmt_money(s.market_cap)} | PE: {_fmt_num(s.pe)} | "
                f"PB: {_fmt_num(s.pb)} | 52周区间: {w52}"
            )

        if self.fundamentals and self.fundamentals.years:
            f = self.fundamentals
            lines.append(f"— 财务（来源: {f.source}）—")
            for y in f.years:
                parts = [f"{y.get('year', '?')} 年"]
                if y.get("revenue") is not None:
                    parts.append(f"营收 {_fmt_money(y['revenue'])}")
                if y.get("net_profit") is not None:
                    parts.append(f"净利 {_fmt_money(y['net_profit'])}")
                if y.get("gross_margin") is not None:
                    parts.append(f"毛利率 {y['gross_margin']:.1f}%")
                if y.get("roe") is not None:
                    parts.append(f"ROE {y['roe']:.1f}%")
                if y.get("ocf") is not None:
                    parts.append(f"经营现金流 {_fmt_money(y['ocf'])}")
                if y.get("debt_ratio") is not None:
                    parts.append(f"资产负债率 {y['debt_ratio']:.1f}%")
                lines.append(" | ".join(parts))

        if self.news:
            lines.append("— 近期新闻/公告 —")
            for n in self.news[:15]:
                tag = "公告" if n.category == "announcement" else "新闻"
                when = (n.published_at or "")[:10]
                lines.append(f"[{tag} {when}] {n.title}（{n.source}）")

        if self.errors:
            lines.append("— 取数异常（已降级处理，不影响其余字段可信度）—")
            lines.extend(f"- {e}" for e in self.errors)
        return "\n".join(lines)


def _fmt_num(v: float | None) -> str:
    return "—" if v is None else f"{v:,.2f}"


def _fmt_money(v: float | None) -> str:
    """大额金额人性化显示（原始单位假定为元级货币）。"""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:.2f}万亿"
    if a >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if a >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:.2f}"


# ── 缓存序列化 / 快照字段合并（模块级，便于单测）────────────

def _merge_snapshots(snaps: list[Snapshot]) -> Snapshot | None:
    """字段级合并：主源优先，缺失字段由后续兜底源补齐（如 westock 给价格、东财补 PE/PB）。"""
    snaps = [s for s in snaps if s is not None]
    if not snaps:
        return None
    base = snaps[0]
    merged = Snapshot(**asdict(base))
    fillable = ("name", "price", "prev_close", "change_pct", "market_cap", "pe", "pb",
                "week52_high", "week52_low", "currency", "asof")
    for s in snaps[1:]:
        for f_name in fillable:
            if getattr(merged, f_name) is None and getattr(s, f_name) is not None:
                setattr(merged, f_name, getattr(s, f_name))
    merged.source = base.source
    merged.symbol = base.symbol
    return merged


def _snapshot_to_dict(s: Snapshot) -> dict:
    return asdict(s)


def _snapshot_from_dict(d: dict) -> Snapshot:
    return Snapshot(**d)


def _fundamentals_to_dict(f: Fundamentals) -> dict:
    return asdict(f)


def _fundamentals_from_dict(d: dict) -> Fundamentals:
    return Fundamentals(**d)


def _merge_fundamentals(a: Fundamentals | None, b: Fundamentals | None) -> Fundamentals | None:
    """财报按年字段合并：主源 a 优先，缺失字段由兜底 b 补齐（如 westock 港股只有资产负债表，
    收入/净利由 yfinance 补）。"""
    if a is None:
        return b
    if b is None:
        return a
    by_year: dict[str, dict] = {}
    for src in (a, b):
        for yd in src.years:
            y = str(yd.get("year"))
            slot = by_year.setdefault(y, {})
            for k, v in yd.items():
                if k == "year":
                    continue
                if slot.get(k) is None and v is not None:
                    slot[k] = v
    max_years = max(len(a.years), len(b.years))
    years = [{"year": y, **by_year[y]} for y in sorted(by_year, reverse=True)[:max_years]]
    return Fundamentals(symbol=a.symbol, source=a.source, asof=a.asof, years=years)


def _news_to_dict(n: NewsItem) -> dict:
    return asdict(n)


def _news_from_dict(d: dict) -> NewsItem:
    return NewsItem(**d)


class MarketData:
    """行情数据门面：按市场路由到对应 provider 组合，统一降级 + 落盘缓存。

    providers 参数用于测试注入假实现；生产不传则按默认数据源装配。
    providers 结构::

        {
            "a_quote":  [东财, 新浪],   # A股快照 failover 链，按序尝试
            "a_fund":   akshare,        # A股财报
            "a_news":   [akshare, 巨潮], # A股新闻+公告，多源合并
            "overseas_quote": [东财海外, yfinance],  # 港美股快照：东财优先
            "overseas_data":  yfinance,  # 港美股财报/新闻
            "westock":   WeStockProvider,  # 可选；不传则由 settings.westock_enabled 决定
        }

    缓存：snapshot/fundamentals/news 各按 TTL 落盘 SQLite（market_cache_path），
    命中即返回，未命中/过期才打源并写回；缓存失败不影响主流程。
    """

    def __init__(self, settings: "Settings", providers: dict[str, Any] | None = None,
                 cache: "MarketCache | None" = None) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        user_provided = providers is not None
        if providers is None:
            from toolkit.market.sources.market_providers import (
                AKShareProvider,
                CNInfoProvider,
                EMQuoteProvider,
                EMOverseasQuoteProvider,
                SinaQuoteProvider,
                WeStockProvider,
                YFinanceProvider,
            )

            self._client = httpx.AsyncClient(
                timeout=settings.market_timeout_seconds,
                headers={"User-Agent": _UA},
                http2=False,  # 东财 push2 对 HTTP/2 支持不稳，强制 HTTP/1.1
            )
            akshare = AKShareProvider()
            yfinance = YFinanceProvider()
            providers = {
                "a_quote": [EMQuoteProvider(self._client), SinaQuoteProvider(self._client)],
                "a_fund": akshare,
                "a_news": [akshare, CNInfoProvider(self._client)],
                # 港美股快照：东财实时优先，yfinance 兜底；财报/新闻仍走 yfinance
                "overseas_quote": [EMOverseasQuoteProvider(self._client), yfinance],
                "overseas_data": yfinance,
            }
        self._a_quote: list = providers["a_quote"]
        self._a_fund = providers["a_fund"]
        self._a_news: list = providers["a_news"]
        # 兼容测试：旧调用可能只传 "overseas"（单 provider 同时管快照/财报/新闻）
        # 注意：不要用 dict.get(k, providers["overseas"]) —— 默认参数会先被求值而炸 KeyError
        if "overseas_quote" in providers:
            self._overseas_quote: list = providers["overseas_quote"]
        else:
            self._overseas_quote = [providers["overseas"]]
        if "overseas_data" in providers:
            self._overseas_data = providers["overseas_data"]
        else:
            self._overseas_data = providers["overseas"]

        # westock 主源：测试可注入 providers["westock"]；生产按 settings.westock_enabled 装配
        self._westock = None
        if user_provided and "westock" in providers:
            self._westock = providers["westock"]
        if self._westock is None and not user_provided and settings.westock_enabled:
            from toolkit.market.sources.market_providers import WeStockProvider

            self._westock = WeStockProvider(
                bin_path=settings.westock_bin,
                timeout=settings.westock_timeout_seconds,
                years=settings.market_fundamental_years,
            )

        # 落盘缓存：命中即返回，未命中/过期才打源并写回；缓存失败不影响主流程。
        # 仅生产装配（未注入 providers）时自动建缓存，测试注入 providers 时保持无缓存，避免互相干扰。
        if cache is not None:
            self._cache = cache
        elif settings.market_cache_enabled and not user_provided:
            self._cache = MarketCache(
                settings.market_cache_path,
                {
                    "snapshot": settings.market_cache_ttl_quote_seconds,
                    "fundamentals": settings.market_cache_ttl_fundamentals_seconds,
                    "news": settings.market_cache_ttl_news_seconds,
                },
            )
        else:
            self._cache = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ── 主入口 ────────────────────────────────────────────

    async def bundle(self, symbol: str, *, days: int | None = None, use_dual: bool = False) -> MarketBundle:
        """取一个标的的快照+财报+新闻，组装成 MarketBundle（带落盘缓存 + westock 主源）。

        use_dual 为双源交叉验证预留位，MVP 不实现（传 True 仅记警告）。
        """
        if use_dual:
            logger.warning("双源比对尚未实现，按单源取数: %s", symbol)
        days = days or self.settings.market_news_days
        errors: list[str] = []

        snap, e = await self._cached_snapshot(symbol)
        errors.extend(e)
        fund, e = await self._cached_fundamentals(symbol)
        errors.extend(e)
        news, e = await self._cached_news(symbol, days)
        errors.extend(e)

        if snap is None and fund is None and not news:
            status = DataStatus.DEGRADED
        elif snap is None or fund is None:
            status = DataStatus.PARTIAL
        else:
            status = DataStatus.OK
        return MarketBundle(symbol=symbol, status=status, snapshot=snap,
                            fundamentals=fund, news=news, errors=errors)

    # ── 兼容便捷接口（持仓追踪等直接消费）──────────────────

    async def snapshot(self, symbol: str) -> Snapshot | None:
        return (await self.bundle(symbol)).snapshot

    async def news(self, symbol: str, days: int = 7) -> list[NewsItem]:
        return (await self.bundle(symbol, days=days)).news

    async def fundamentals(self, symbol: str) -> Fundamentals | None:
        return (await self.bundle(symbol)).fundamentals

    # ── 内部：带缓存的各组件取数 ──────────────────────────

    async def _cached_snapshot(self, symbol: str) -> tuple[Snapshot | None, list[str]]:
        """快照：先查缓存，未命中则 westock(主) + 现有源(兜底) 字段级合并，写回缓存。"""
        market = classify_symbol(symbol)
        if self._cache is not None:
            hit = await self._cache.get("snapshot", symbol)
            if hit is not None:
                return _snapshot_from_dict(hit), []
        snaps: list[Snapshot] = []
        errors: list[str] = []
        if self._westock is not None:
            code = westock_code(symbol)
            s, e = await self._safe(self._westock.snapshot(code), "westock snapshot")
            errors.extend(e)
            if s is not None:
                s.symbol = symbol
                snaps.append(s)
        if market == "A":
            for prov in self._a_quote:
                s, e = await self._safe(prov.snapshot(symbol), f"{prov.name} snapshot")
                errors.extend(e)
                if s is not None:
                    snaps.append(s)
        else:
            sym = normalize_hk_symbol(symbol) if market == "HK" else normalize_us_symbol(symbol)
            for prov in self._overseas_quote:
                s, e = await self._safe(prov.snapshot(sym), f"{prov.name} snapshot")
                errors.extend(e)
                if s is not None:
                    snaps.append(s)
        merged = _merge_snapshots(snaps)
        if self._cache is not None and merged is not None:
            await self._cache.set("snapshot", symbol, _snapshot_to_dict(merged))
        return merged, errors

    async def _cached_fundamentals(self, symbol: str) -> tuple[Fundamentals | None, list[str]]:
        """财报：先查缓存；未命中则 westock(主) 优先，若缺营收+净利关键字段再取兜底按年合并补全。"""
        market = classify_symbol(symbol)
        if self._cache is not None:
            hit = await self._cache.get("fundamentals", symbol)
            if hit is not None:
                return _fundamentals_from_dict(hit), []
        errors: list[str] = []
        westock_fund: Fundamentals | None = None
        if self._westock is not None:
            code = westock_code(symbol)
            f, e = await self._safe(self._westock.fundamentals(code), "westock fundamentals")
            errors.extend(e)
            if f is not None:
                f.symbol = symbol
                westock_fund = f

        # 主源含营收+净利即视为完整，直接用（避免无谓打兜底源、减少限流暴露）
        complete = westock_fund is not None and any(
            y.get("revenue") is not None and y.get("net_profit") is not None
            for y in westock_fund.years
        )
        if complete:
            fund = westock_fund
        else:
            fallback: Fundamentals | None = None
            if market == "A":
                f, e = await self._safe(
                    self._a_fund.fundamentals(symbol), f"{self._a_fund.name} fundamentals"
                )
                errors.extend(e)
                fallback = f
            else:
                sym = normalize_hk_symbol(symbol) if market == "HK" else normalize_us_symbol(symbol)
                f, e = await self._safe(
                    self._overseas_data.fundamentals(sym),
                    f"{self._overseas_data.name} fundamentals",
                )
                errors.extend(e)
                fallback = f
            fund = _merge_fundamentals(westock_fund, fallback)

        if fund is not None:
            fund.symbol = symbol
        if self._cache is not None and fund is not None:
            await self._cache.set("fundamentals", symbol, _fundamentals_to_dict(fund))
        return fund, errors

    async def _cached_news(self, symbol: str, days: int) -> tuple[list[NewsItem], list[str]]:
        """新闻：先查缓存（按 symbol+days），未命中则取源并写回。"""
        market = classify_symbol(symbol)
        cache_key = f"{symbol}:{days}"
        if self._cache is not None:
            hit = await self._cache.get("news", cache_key)
            if hit is not None:
                return [_news_from_dict(d) for d in hit], []
        errors: list[str] = []
        if market == "A":
            items, e = await self._collect_a_news(symbol, days)
            errors.extend(e)
            news = items
        else:
            sym = normalize_hk_symbol(symbol) if market == "HK" else normalize_us_symbol(symbol)
            got, e = await self._safe(
                self._overseas_data.news(sym, days=days), f"{self._overseas_data.name} news"
            )
            errors.extend(e)
            news = got or []
        if self._cache is not None:
            await self._cache.set("news", cache_key, [_news_to_dict(n) for n in news])
        return news, errors

    async def _collect_a_news(self, symbol: str, days: int) -> tuple[list[NewsItem], list[str]]:
        """A股新闻+公告多源合并：单源失败不影响另一源。"""
        items: list[NewsItem] = []
        errors: list[str] = []
        for prov in self._a_news:
            got, err = await self._safe(prov.news(symbol, days=days), f"{prov.name} news")
            errors.extend(err)
            if got:
                items.extend(got)
        items.sort(key=lambda n: n.published_at or "", reverse=True)
        return items, errors

    async def _safe(self, awaitable, label: str) -> tuple[Any, list[str]]:
        """统一超时/异常降级：返回 (结果|None, 错误清单)。"""
        try:
            return await asyncio.wait_for(awaitable, timeout=self.settings.market_timeout_seconds), []
        except Exception as exc:
            logger.warning("market 取数降级 [%s]: %s", label, exc)
            return None, [f"{label}: {type(exc).__name__}: {exc}"]
