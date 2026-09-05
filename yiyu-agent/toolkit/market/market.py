"""行情数据基础设施层（Phase 0）：多 provider 路由的横切层。

深度研究 / 持仓追踪 / 筛股共用此层，不是任何单个切片的私有件。

数据源分工（A股 + 港美股）：
- A股实时行情(价格/PE/PB/市值) → 东财(主) → 新浪(补价) → WeStock(兜底补缺)
- A股三大报表                 → AKShare/新浪三表(主) → WeStock(字段级补缺)
- A股新闻/公告                → akshare(新闻) + 巨潮资讯网(公告，官方信披)
- 港股/美股                   → WeStock（主源）；不走 AKShare

财报不是「主源返回对象就完事」：完整性 = 本次 requested_fields 是否全部满足，
缺哪个字段才向兜底源要（AKShare 缺 total_shares/roic/fcff/ebit，靠 WeStock 补）。

并发约束（「支持并发」不等于「可以无限并发」）：
- WeStock 单次请求内部必须串行（后端并发会返回「数据为空」）；
- AKShare 底层仍是新浪/东财接口，并发过高会触发上游限流、断连或空结果；
- 故财报取数：同一标的串行（single-flight）+ 全局最多 market_fund_concurrency 个公司。

降级铁律：任一数据源超时/异常 → 对应字段置空并记 error，绝不中断流程；
限流/熔断时立即用缓存（含 stale）降级，不反复重试。
prompt 注入时按 data_status 强制标注，严禁用模型训练数据冒充实时行情。
双源比对：MVP 不实现；bundle(use_dual=True) 仅为预留位。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

import httpx

from toolkit.market.market_cache import MarketCache
from toolkit.market.market_router import (
    classify_symbol,
    normalize_hk_symbol,
    normalize_us_symbol,
    split_a_symbol,
    westock_code,
)
from toolkit.market.provider_guard import GuardRegistry, ProviderGuard
from toolkit.market.field_registry import (
    FIELD_ALIASES,
    FIELD_COMPONENTS,
    field_parts,
)
from toolkit.market.source_mapping import provider_fields

if TYPE_CHECKING:
    from core.config import Settings
    from toolkit.market.evidence_registry import EvidenceRegistry

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
    source: str                       # eastmoney / sina / westock
    name: str | None = None
    price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None   # 百分比数值，如 1.23 表示 +1.23%
    market_cap: float | None = None
    float_market_cap: float | None = None
    pe: float | None = None            # 兼容旧消费者；等同 pe_dynamic，不可作为 PE TTM 使用
    pe_dynamic: float | None = None
    pe_static: float | None = None
    pe_ttm: float | None = None
    ps_ttm: float | None = None
    pb: float | None = None
    industry: str | None = None
    listing_date: str | None = None
    exchange: str | None = None
    turnover_rate: float | None = None
    volume: float | None = None
    amount: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    currency: str | None = None
    asof: str | None = None           # 数据时间戳，'YYYY-MM-DD HH:MM:SS'


@dataclass
class Fundamentals:
    """财务数据。years 元素键可缺：year, revenue, net_profit, gross_margin, roe,
    ocf, capex, debt_ratio；金额为原始单位，比率为百分比数值。"""

    symbol: str
    source: str                       # akshare / westock
    years: list[dict] = field(default_factory=list)
    asof: str | None = None


@dataclass
class NewsItem:
    title: str
    source: str                       # akshare / cninfo / westock
    category: str = "news"            # news | announcement（巨潮公告）
    url: str | None = None
    published_at: str | None = None
    summary: str | None = None


@dataclass
class MarketBundle:
    """一个标的一次取数的完整结果，含降级状态与异常清单。

    P0 取数层改造新增字段：
      - field_evidence: 字段级来源证据（{field_name: FieldEvidence.to_dict()}）
      - missing_fields: 按需取数后仍未拿到的字段名清单
      - fetch_status: ok / partial / degraded（比 status 更细，服务 Metric Service）
      - fallback_results: 失败 3 次后白名单搜索的 evidence 列表
    """

    symbol: str
    status: DataStatus
    snapshot: Snapshot | None = None
    fundamentals: Fundamentals | None = None
    news: list[NewsItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # P0 新增
    field_evidence: dict[str, dict] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    fetch_status: str = "ok"           # ok / partial / degraded
    structured_status: str = "ok"      # ok / partial / unregistered / not_supported / request_failed
    fallback_results: list[dict] = field(default_factory=list)
    field_sources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    source_mapping: dict[str, tuple[dict, ...]] = field(default_factory=dict)
    derived_metrics: dict[str, dict] = field(default_factory=dict)
    fetch_attempts: list[dict] = field(default_factory=list)

    @property
    def fields(self) -> dict[str, dict]:
        """Project the existing components into one canonical, read-only view."""
        result: dict[str, dict] = {
            "symbol": {
                "field": "symbol", "value": self.symbol, "unit": "", "period": "current",
                "period_type": "current", "source": "entity", "source_field": "symbol",
                "fetched_at": "", "status": "ok",
            }
        }
        if self.snapshot is not None:
            for name in ("symbol", "name", "price", "market_cap", "float_market_cap", "pe",
                         "pe_dynamic", "pe_static", "pe_ttm", "ps_ttm", "pb", "industry",
                         "listing_date", "exchange", "turnover_rate", "volume", "amount"):
                value = self.symbol if name == "symbol" else getattr(self.snapshot, name, None)
                if value is not None:
                    canonical = FIELD_ALIASES.get(name, name)
                    result[canonical] = {
                        "field": canonical, "value": value, "unit": "", "period": "current",
                        "period_type": "current", "source": self.snapshot.source,
                        "source_field": name, "fetched_at": self.snapshot.asof or "", "status": "ok",
                    }
        if self.fundamentals is not None:
            for row in sorted(self.fundamentals.years, key=lambda item: str(item.get("year", ""))):
                period = str(row.get("year", ""))
                for raw_name, value in row.items():
                    if raw_name == "year" or value is None:
                        continue
                    name = FIELD_ALIASES.get(raw_name, raw_name)
                    result[name] = {
                        "field": name, "value": value, "unit": "", "period": period,
                        "period_type": "annual", "source": self.fundamentals.source,
                        "source_field": raw_name,
                        "fetched_at": self.fundamentals.asof or period, "status": "ok",
                    }
        return result

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
                f"总市值: {_fmt_money(s.market_cap)} | 流通市值: {_fmt_money(s.float_market_cap)} | "
                f"PE(TTM): {_fmt_num(s.pe_ttm)} | 动态PE: {_fmt_num(s.pe_dynamic)} | "
                f"PS(TTM): {_fmt_num(s.ps_ttm)} | "
                f"PB: {_fmt_num(s.pb)} | 52周区间: {w52}"
            )
            if s.industry or s.listing_date:
                lines.append(f"行业: {s.industry or '—'} | 上市日期: {s.listing_date or '—'}")

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
    fillable = (
        "name", "price", "prev_close", "change_pct", "market_cap", "float_market_cap",
        "pe", "pe_dynamic", "pe_static", "pe_ttm", "ps_ttm", "pb", "industry", "listing_date",
        "week52_high", "week52_low", "currency", "asof",
        "exchange", "turnover_rate", "volume", "amount",
    )
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
    收入/净利由 WeStock 补）。"""
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


# ── 财报字段路由（required_fields 驱动的完整性判定 + 兜底决策）────────────
# 兼容旧导入名，但实际定义统一来自 field_registry。
FUND_FIELD_ALIASES: dict[str, str] = FIELD_ALIASES
AK_FUND_FIELDS = provider_fields("akshare")
WESTOCK_FUND_FIELDS = provider_fields("westock")

# A 股财报无显式 required 时的基础完整判定字段（沿用原 total_shares 新鲜度要求）
A_FUND_BASE_FIELDS: tuple[str, ...] = ("revenue", "net_profit", "total_shares")


def covered_fund_fields(fund: Fundamentals | None) -> set[str]:
    """财报已覆盖的 canonical 字段集（任一年有值即算覆盖）。"""
    if fund is None or not fund.years:
        return set()
    out: set[str] = set()
    for y in fund.years:
        for k, v in y.items():
            if k == "year" or v is None:
                continue
            out.add(FUND_FIELD_ALIASES.get(k, k))
    return out


def missing_fund_fields(fund: Fundamentals | None, required: list[str] | None) -> list[str]:
    """完整性判定：本次 requested_fields 是否全部满足。

    取代旧的「只查 WeStock 有没有营收+净利润」——那样即使模型要合同负债、
    资本开支、应收账款，也会因为有营收净利而跳过兜底源。
    """
    if not required:
        return []
    covered = covered_fund_fields(fund)
    return [f for f in required if f not in covered]


def _news_to_dict(n: NewsItem) -> dict:
    return asdict(n)


def _news_from_dict(d: dict) -> NewsItem:
    return NewsItem(**d)


def _fund_lock_key(symbol: str) -> str:
    """财报 single-flight 锁键：同一证券的不同写法归一，避免重复打源。"""
    market = classify_symbol(symbol)
    if market == "A":
        code, ex = split_a_symbol(symbol)
        return f"A:{code}.{ex}"
    if market == "HK":
        return f"HK:{normalize_hk_symbol(symbol)}"
    return f"US:{normalize_us_symbol(symbol)}"


class MarketData:
    """行情数据门面：按市场路由到对应 provider 组合，统一降级 + 落盘缓存。

    providers 参数用于测试注入假实现；生产不传则按默认数据源装配。
    providers 结构::

        {
            "a_quote":  [东财, 新浪],   # A股快照 failover 链，按序尝试
            "a_fund":   akshare,        # A股财报
            "a_news":   [akshare, 巨潮], # A股新闻+公告，多源合并
            "overseas_quote": [WeStockProvider],  # 港美股：WeStock 主源
            "overseas_data":  WeStockProvider,  # 港美股财报/新闻
            "westock":   WeStockProvider,  # A 股备用；港美股主源
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
                SinaQuoteProvider,
                WeStockProvider,
            )

            self._client = httpx.AsyncClient(
                timeout=settings.market_timeout_seconds,
                headers={"User-Agent": _UA},
                http2=False,  # 东财 push2 对 HTTP/2 支持不稳，强制 HTTP/1.1
            )
            akshare = AKShareProvider()
            westock = (
                WeStockProvider(
                    bin_path=settings.westock_bin,
                    timeout=settings.westock_timeout_seconds,
                    years=settings.market_fundamental_years,
                )
                if settings.westock_enabled else None
            )
            providers = {
                "a_quote": [EMQuoteProvider(self._client), SinaQuoteProvider(self._client)],
                "a_fund": akshare,
                "a_news": [akshare, CNInfoProvider(self._client)],
                "overseas_quote": [westock] if westock is not None else [],
                "overseas_data": westock,
                "westock": westock,
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
        if self._westock is None and not user_provided and "westock" not in providers and settings.westock_enabled:
            from toolkit.market.sources.market_providers import WeStockProvider

            self._westock = WeStockProvider(
                bin_path=settings.westock_bin,
                timeout=settings.westock_timeout_seconds,
                years=settings.market_fundamental_years,
            )

        self._route_provider = providers.get("route") if user_provided else None
        if not user_provided and self._client is not None:
            from toolkit.market.route_provider import RouteProvider
            self._route_provider = RouteProvider(self._client, westock=self._westock)

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
                    "fundamentals_latest": settings.market_cache_ttl_fundamentals_latest_seconds,
                    "news": settings.market_cache_ttl_news_seconds,
                },
            )
        else:
            self._cache = None

        # 并发控制：财报取数全局最多 N 个公司并发，且同一标的串行。
        # 背景：WeStock 后端并发会返回「数据为空」；AKShare 底层仍是新浪/东财接口，
        # 无限并发会触发上游限流或空结果。「支持并发」不等于「可以无限并发」。
        self._fund_sem = asyncio.Semaphore(
            max(1, int(getattr(settings, "market_fund_concurrency", 2) or 2))
        )
        self._fund_inflight: dict[str, asyncio.Task] = {}

        # P2 provider guard：token bucket 限流 + circuit breaker 熔断。
        # 生产装配时自动建；测试注入 providers 时也建（可重置/绕过），保证 _safe 能走 guard。
        # guard 失败绝不阻断主流程——熔断中/无令牌 → 跳过该 provider。
        self._guards: GuardRegistry | None = None
        if not user_provided or cache is not None:
            self._guards = GuardRegistry()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ── 主入口 ────────────────────────────────────────────

    async def bundle(
        self,
        symbol: str,
        *,
        days: int | None = None,
        use_dual: bool = False,
        field_groups: list[str] | None = None,
        metric_ids: list[str] | None = None,
        requested_fields: list[str] | None = None,
        max_attempts: int = 3,
        fallback_web_search: bool = True,
    ) -> MarketBundle:
        """取一个标的的行情包。

        旧调用（不传 field_groups / metric_ids）：向后兼容，取全量。
        新调用（传 metric_ids 或 field_groups）：按需取数，只取覆盖字段所需组件，
        不取新闻/公告除非显式点名；单组件预算 + 失败 max_attempts 次跳白名单搜索；
        返回 MarketBundle 带 field_evidence / missing_fields / fetch_status / fallback_results。

        use_dual 为双源交叉验证预留位，MVP 不实现（传 True 仅记警告）。
        """
        if use_dual:
            logger.warning("双源比对尚未实现，按单源取数: %s", symbol)
        # 有需求驱动参数 → 走 v2
        if metric_ids is not None or requested_fields is not None or field_groups is not None:
            return await self.bundle_v2(
                symbol,
                days=days,
                field_groups=field_groups,
                metric_ids=metric_ids,
                requested_fields=requested_fields,
                max_attempts=max_attempts,
                fallback_web_search=fallback_web_search,
            )
        # 旧全量路径
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
        if status is DataStatus.OK and any("stale_cache" in e for e in errors):
            status = DataStatus.PARTIAL
        return MarketBundle(symbol=symbol, status=status, snapshot=snap,
                            fundamentals=fund, news=news, errors=errors,
                            fetch_status=status.value)

    async def bundle_v2(
        self,
        symbol: str,
        *,
        days: int | None = None,
        field_groups: list[str] | None = None,
        metric_ids: list[str] | None = None,
        requested_fields: list[str] | None = None,
        max_attempts: int = 3,
        fallback_web_search: bool = True,
    ) -> MarketBundle:
        """需求驱动 + 快失败 + evidence 注册 + 搜索兜底的新主入口。

        约定（P0）：
          1. 按 plan_fetch 反推要取的组件，未点名的 news 不取；
          2. 单组件用 asyncio.wait_for 套 COMPONENT_BUDGETS 预算；
          3. 同一组件本轮失败累计 >= max_attempts 后转 fallback_search（最多 1 次）；
          4. 结构化字段进 bundle 时登记 FieldEvidence；
          5. 缺字段进 missing_fields；Metric Service 见 missing 返回 not_disclosed。
        """
        from toolkit.market.evidence_registry import EvidenceRegistry
        from toolkit.market.fetch_planner import plan_fetch, should_fetch_news
        from toolkit.market.search_fallback import fallback_search

        plan = plan_fetch(
            symbol=symbol,
            metric_ids=metric_ids,
            requested_fields=requested_fields,
            field_groups=field_groups,
            days=days,
            provider_status=self.guard_stats(),
        )
        days = days or self.settings.market_news_days
        errors: list[str] = []
        evidence = EvidenceRegistry()
        failure_counts: dict[str, int] = {}
        fallback_results: list[dict] = []
        missing_fields: list[str] = list(plan.required_fields)

        snap: Snapshot | None = None
        fund: Fundamentals | None = None
        news: list[NewsItem] = []

        # 字段需求走 Planner steps；旧 field_groups 整包入口仍保留，避免破坏新闻等非字段组件。
        if self._route_provider is not None and (metric_ids is not None or requested_fields is not None):
            from toolkit.calc.metric_service import _source_level
            from toolkit.market.fetch_executor import execute_fetch_plan

            execution = await execute_fetch_plan(plan, self._execute_route_step)
            snapshot_values: dict[str, Any] = {}
            by_period: dict[str, dict] = {}
            sources: set[str] = set()
            fetched_times: list[str] = []
            fundamental_raw_names = {
                "operating_cash_flow": "ocf", "capital_expenditure": "capex",
                "total_assets": "assets",
            }
            for requested, item in execution.fields.items():
                base, _ = field_parts(requested)
                sources.add(item.provider)
                if item.fetched_at:
                    fetched_times.append(item.fetched_at)
                evidence.register(
                    requested, item.value, source=item.provider,
                    source_level=_source_level(item.provider), as_of=item.fetched_at,
                    period=item.period, caliber=base,
                )
                if FIELD_COMPONENTS.get(base) == "snapshot":
                    if base in Snapshot.__dataclass_fields__:
                        snapshot_values[base] = item.value
                else:
                    row = by_period.setdefault(item.period, {"year": item.period})
                    row[fundamental_raw_names.get(base, base)] = item.value
            source = next(iter(sources)) if len(sources) == 1 else "multi_source"
            asof = max(fetched_times, default="")
            if snapshot_values:
                snap = Snapshot(symbol=symbol, source=source, asof=asof, **snapshot_values)
            if by_period:
                fund = Fundamentals(
                    symbol=symbol, source=source,
                    years=[by_period[key] for key in sorted(by_period, reverse=True)], asof=asof,
                )
            if should_fetch_news(plan):
                news, e = await self._cached_news(symbol, days)
                errors.extend(e)

            missing_fields = list(execution.web_search_candidates)
            if not missing_fields:
                status, fetch_status = DataStatus.OK, "ok"
            elif execution.fields or plan.reusable_fields:
                status, fetch_status = DataStatus.PARTIAL, "partial"
            else:
                status, fetch_status = DataStatus.DEGRADED, "degraded"
            return MarketBundle(
                symbol=symbol, status=status, snapshot=snap, fundamentals=fund, news=news,
                errors=[attempt.reason for attempt in execution.attempts if attempt.reason],
                field_evidence=evidence.to_dict(), missing_fields=missing_fields,
                fetch_status=fetch_status, structured_status=execution.status,
                field_sources=plan.field_sources, source_mapping=plan.source_mapping,
                fetch_attempts=[attempt.to_dict() for attempt in execution.attempts],
            )

        # snapshot
        if "snapshot" in plan.components:
            snap, e = await self._cached_snapshot(symbol)
            errors.extend(e)
            if snap is None:
                failure_counts["snapshot"] = failure_counts.get("snapshot", 0) + 1
            else:
                self._register_snapshot_evidence(snap, evidence)
                missing_fields = self._drop_covered(missing_fields, snap, None)

        # fundamentals：把本次 required_fields 透传，用于字段级兜底与完整性判定
        if "fundamentals" in plan.components:
            fund, e = await self._cached_fundamentals(
                symbol, required_fields=plan.required_fields
            )
            errors.extend(e)
            if fund is None:
                failure_counts["fundamentals"] = failure_counts.get("fundamentals", 0) + 1
            else:
                self._register_fundamentals_evidence(fund, evidence)
                missing_fields = self._drop_covered(missing_fields, snap, fund)

        # news / announcements（metric_ids 驱动时不取，除非显式点名）
        if should_fetch_news(plan):
            news, e = await self._cached_news(symbol, days)
            errors.extend(e)
            if not news:
                failure_counts["news"] = failure_counts.get("news", 0) + 1

        # 搜索兜底：失败组件才触发，最多 1 次/组件
        if fallback_web_search:
            for comp, fails in failure_counts.items():
                if fails < max_attempts:
                    continue
                fb = await fallback_search(symbol, comp, missing_fields)
                if fb.results:
                    fallback_results.extend(fb.results)
                if fb.error:
                    errors.append(f"fallback_search[{comp}]: {fb.error}")

        # 状态判定
        if snap is None and fund is None and not news and not fallback_results:
            status = DataStatus.DEGRADED
            fetch_status = "degraded"
        elif snap is None or (fund is None and "fundamentals" in plan.components):
            status = DataStatus.PARTIAL
            fetch_status = "partial"
        else:
            status = DataStatus.OK
            fetch_status = "ok" if not failure_counts else "partial"
        if missing_fields and fetch_status == "ok":
            status = DataStatus.PARTIAL
            fetch_status = "partial"
        if fetch_status == "ok" and any("stale_cache" in e for e in errors):
            status = DataStatus.PARTIAL
            fetch_status = "partial"

        return MarketBundle(
            symbol=symbol,
            status=status,
            snapshot=snap,
            fundamentals=fund,
            news=news,
            errors=errors,
            field_evidence=evidence.to_dict(),
            missing_fields=missing_fields,
            fetch_status=fetch_status,
            fallback_results=fallback_results,
            field_sources=plan.field_sources,
            source_mapping=plan.source_mapping,
        )

    # ── 字段证据登记（bundle_v2 用）──────────────────────

    async def _execute_route_step(self, step, symbol: str):
        """通过现有 ProviderGuard 执行一条精确路由，保持限流和熔断语义。"""
        if self._guards is None:
            return await self._route_provider.fetch(step, symbol)
        guard = self._guards.get(step.provider)
        allowed, reason = await guard.allow()
        if not allowed:
            raise RuntimeError(reason)
        try:
            result = await self._route_provider.fetch(step, symbol)
        except asyncio.CancelledError:
            guard.record_failure("TimeoutError: planner step budget exceeded")
            raise
        except Exception as exc:
            guard.record_failure(f"{type(exc).__name__}: {exc}")
            raise
        guard.record_success()
        return result

    def _register_snapshot_evidence(self, snap: Snapshot, evidence: "EvidenceRegistry") -> None:
        """把快照字段登记成 FieldEvidence。"""
        from toolkit.calc.metric_service import _source_level
        src = str(getattr(snap, "source", "") or "market_snapshot")
        level = _source_level(src)
        as_of = str(getattr(snap, "asof", "") or "")
        for attr, caliber in (
            ("market_cap", "current_market_cap"),
            ("float_market_cap", "current_float_market_cap"),
            ("pe", "provider_dynamic_pe_legacy"),
            ("pe_dynamic", "provider_dynamic_pe"),
            ("pe_static", "provider_static_pe"),
            ("pe_ttm", "provider_pe_ttm"),
            ("ps_ttm", "provider_ps_ttm"),
            ("pb", "provider_pb"),
            ("price", "current_price"),
            ("industry", "provider_industry"),
            ("listing_date", "provider_listing_date"),
        ):
            val = getattr(snap, attr, None)
            if val is not None:
                evidence.register(
                    attr, val, source=src, source_level=level,
                    as_of=as_of, period="current", caliber=caliber,
                )

    def _register_fundamentals_evidence(self, fund: Fundamentals, evidence: "EvidenceRegistry") -> None:
        """把财报字段登记成 FieldEvidence（按 metric_service 别名归一）。"""
        from toolkit.calc.metric_service import _source_level
        src = str(getattr(fund, "source", "") or "fundamentals")
        level = _source_level(src)
        as_of = str(getattr(fund, "asof", "") or "")
        years = sorted(fund.years, key=lambda y: str(y.get("year", ""))) if fund.years else []
        for raw_key, canonical in FUND_FIELD_ALIASES.items():
            vals = [
                (str(y.get("year", "")), y.get(raw_key))
                for y in years
                if y.get(raw_key) is not None
            ]
            if not vals:
                continue
            period, value = vals[-1]
            evidence.register(
                canonical, value, source=src, source_level=level,
                as_of=as_of or period, period=period, caliber=canonical,
            )

    def _drop_covered(
        self,
        missing: list[str],
        snap: Snapshot | None,
        fund: Fundamentals | None,
    ) -> list[str]:
        """从 missing_fields 里去掉已被 snapshot/fundamentals 覆盖的字段。"""
        covered: set[str] = set()
        snap_aliases = {
            "market_cap": {"market_cap"},
            "float_market_cap": {"float_market_cap"},
            "pe": {"pe"},
            "pe_dynamic": {"pe_dynamic"},
            "pe_static": {"pe_static"},
            "pe_ttm": {"pe_ttm"},
            "ps_ttm": {"ps_ttm"},
            "pb": {"pb"},
            "price": {"price", "current_price"},
            "industry": {"industry"},
            "listing_date": {"listing_date"},
        }
        if snap is not None:
            for attr, names in snap_aliases.items():
                if getattr(snap, attr, None) is not None:
                    covered.update(names)
        fund_aliases = {
            "revenue": {"revenue"},
            "revenue_yoy": {"revenue_yoy"},
            "net_profit": {"net_profit"},
            "net_profit_parent": {"net_profit_parent"},
            "net_profit_parent_yoy": {"net_profit_parent_yoy"},
            "gross_margin": {"gross_margin"},
            "gross_profit_margin": {"gross_margin"},
            "ocf": {"operating_cash_flow", "ocf"},
            "capex": {"capital_expenditure", "capex"},
            "equity": {"equity", "total_equity", "shareholder_equity"},
            "total_debt": {"total_debt"},
            "cash": {"cash"},
            "trading_financial_assets": {"trading_financial_assets"},
            "ebit": {"ebit"},
            "ebitda": {"ebitda"},
            "depreciation_amortization": {"depreciation_amortization"},
            "interest_expense": {"interest_expense", "financial_expense"},
            "total_profit": {"total_profit"},
            "income_tax": {"income_tax"},
            "minority_interest": {"minority_interest"},
            "total_shares": {"total_shares"},
            "total_assets": {"total_assets", "assets"},
        }
        if fund is not None and fund.years:
            years = fund.years
            for raw_key, names in fund_aliases.items():
                if any(y.get(raw_key) is not None for y in years):
                    covered.update(names)
        return [f for f in missing if f not in covered]

    # ── 兼容便捷接口（持仓追踪等直接消费）──────────────────

    async def snapshot(self, symbol: str) -> Snapshot | None:
        return (await self.bundle(symbol)).snapshot

    async def verify_a_symbol(self, symbol: str) -> tuple[str | None, bool]:
        """A 股代码存在性验证：只走东财/新浪确定性行情源。

        返回 (公司名 | None, 是否网络异常)。

        与 bundle/snapshot 的区别：这里刻意**不走 westock 主源、不查缓存**，
        因为 westock 失败语义里含「无数据」（会对无效代码抛异常），会把
        「查无此股」误判成「网络异常」。东财/新浪对无效代码是**正常返回空**、
        对网络异常才抛异常，因此能干净区分两种情况：
        - 拿到 name → 代码有效；
        - 拿不到 name 且 errors 非空 → 网络异常；
        - 拿不到 name 且 errors 为空 → 查无此股（未收录/退市/输入有误）。
        """
        errors: list[str] = []
        for prov in self._a_quote:
            s, e = await self._safe(prov.snapshot(symbol), f"{prov.name} snapshot")
            errors.extend(e)
            if s is not None and s.name:
                return s.name, False
        return None, bool(errors)

    async def news(self, symbol: str, days: int = 7) -> list[NewsItem]:
        return (await self.bundle(symbol, days=days)).news

    async def fundamentals(self, symbol: str) -> Fundamentals | None:
        return (await self.bundle(symbol)).fundamentals

    # ── 内部：带缓存的各组件取数 ──────────────────────────

    async def _cached_snapshot(self, symbol: str) -> tuple[Snapshot | None, list[str]]:
        """快照：缓存 → 主源 → 兜底源字段级补齐。

        源顺序即字段优先级（_merge_snapshots 主源优先、缺字段后源补）：
          - A 股：东财（价格/PE/PB/市值齐全）→ 新浪补价 → WeStock 兜底补缺。
            实时行情不交给 AKShare（它是财报源，行情走东财/新浪更快更稳）。
          - 港美股：WeStock；不走 AKShare。
        """
        market = classify_symbol(symbol)
        if self._cache is not None:
            hit = await self._cache.get("snapshot", symbol)
            if hit is not None:
                return _snapshot_from_dict(hit), []
        snaps: list[Snapshot] = []
        errors: list[str] = []

        async def _westock() -> None:
            if self._westock is None:
                return
            s, e = await self._safe(
                self._westock.snapshot(westock_code(symbol)), "westock snapshot"
            )
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
            await _westock()
        else:
            await _westock()
            sym = normalize_hk_symbol(symbol) if market == "HK" else normalize_us_symbol(symbol)
            for prov in self._overseas_quote:
                s, e = await self._safe(prov.snapshot(sym), f"{prov.name} snapshot")
                errors.extend(e)
                if s is not None:
                    snaps.append(s)
        merged = _merge_snapshots(snaps)
        if self._cache is not None and merged is not None:
            await self._cache.set("snapshot", symbol, _snapshot_to_dict(merged))
        if self._cache is not None and merged is None and errors:
            stale = await self._cache.get_stale("snapshot", symbol)
            if stale is not None:
                value, stale_seconds = stale
                return _snapshot_from_dict(value), [
                    *errors,
                    f"snapshot stale_cache: expired {stale_seconds:.0f}s ago",
                ]
        return merged, errors

    def _suppliable_fields(
        self, required_fields: list[str] | None, symbol: str
    ) -> list[str]:
        """把需求字段收敛到「当前可用源真正给得出」的集合。

        两点必要性：
          - 无显式需求时，A 股沿用旧的新鲜度要求（revenue+net_profit+total_shares，
            total_shares 只有 WeStock 给得出）；
          - 拿不到的字段必须剔除。否则缓存永远「不新鲜」，每次调用都重打源，
            而重打也拿不到——白白放大上游限流暴露面。
        """
        market = classify_symbol(symbol)
        # 先归一到 canonical 名：调用方可能传 ocf/capex 这类财报原始字段名
        fields = [FUND_FIELD_ALIASES.get(f, f) for f in (required_fields or [])]
        if not fields and market == "A":
            fields = list(A_FUND_BASE_FIELDS)

        avail: set[str] = set()
        if market == "A" and self._a_fund is not None:
            avail |= AK_FUND_FIELDS          # 仅 A 股使用 AKShare
        if self._westock is not None:
            avail |= WESTOCK_FUND_FIELDS
        return [f for f in fields if f in avail]

    async def _cached_fundamentals(
        self, symbol: str, required_fields: list[str] | None = None
    ) -> tuple[Fundamentals | None, list[str]]:
        """财报：缓存 → 主源 → 按 required_fields 字段级补缺 → 字段级合并。

        主源分工：
          - A 股：AKShare 主取三表（新浪原始科目，capex 口径准），
            缺的字段再向 WeStock 补（WeStock 独有 total_shares/roic/fcff/ebit）。
          - 港美股：WeStock 主源；不走 AKShare。

        完整性 = 本次 requested_fields 是否全部满足。不是「主源返回对象就完事」，
        也不是旧逻辑的「只要有营收+净利润」——后者会让合同负债/资本开支/应收账款
        这类需求被误判为已满足而跳过兜底源。

        并发控制：同一标的串行（per-symbol Lock）+ 全局最多 N 个公司并发。
        取锁后会**双重检查缓存**：等待期间字段可能已被前一个任务填满。
        """
        required = self._suppliable_fields(required_fields, symbol)

        # ① 缓存（latest 短 TTL + 报告期永久）：字段满足才算命中
        cached, errors = await self._fund_from_cache(symbol, required)
        if cached is not None:
            return cached, errors

        # ② 未命中 → 同标的请求合并（single-flight）+ 全局受控并发
        # 注意：这里不能用 per-symbol Lock 等锁——后来者会一直等到 owner 跑完才进，
        # 那时 in-flight 任务已出队，合并就失效了。要让后来者**立即**看到并复用
        # 在飞行的任务，所以只靠 in-flight 表：同一 key 同时只存在一个刷新任务。
        key = _fund_lock_key(symbol)
        task = self._fund_inflight.get(key)
        if task is None:
            async def _refresh() -> tuple[Fundamentals | None, list[str]]:
                async with self._fund_sem:
                    return await self._refresh_fundamentals(symbol, required)

            task = asyncio.ensure_future(_refresh())
            self._fund_inflight[key] = task
            try:
                fund, errors = await task
            finally:
                self._fund_inflight.pop(key, None)
        else:
            fund, errors = await task

        # ③ 打源失败（含限流/熔断）→ 立即用 stale 缓存降级，不反复重试
        if fund is None and errors and self._cache is not None:
            stale = await self._cache.get_stale("fundamentals_latest", symbol)
            if stale is not None:
                value, stale_seconds = stale
                return _fundamentals_from_dict(value), [
                    *errors,
                    f"fundamentals stale_cache: expired {stale_seconds:.0f}s ago",
                ]
        return fund, errors

    async def _fund_from_cache(
        self, symbol: str, required: list[str]
    ) -> tuple[Fundamentals | None, list[str]]:
        """读缓存：命中且覆盖 required 才返回，否则视为未命中（交由打源补字段）。

        两级：
          1. `fundamentals_latest:{symbol}` —— 短 TTL（1 天），快速命中。
          2. `fundamentals_report:{symbol}:{period}:{version_hash}` —— 永久缓存，
             按报告期 + 内容 hash 固化，已发布年报不再重复拉取。
        """
        if self._cache is None:
            return None, []
        hit = await self._cache.get("fundamentals_latest", symbol)
        if hit is not None:
            cached = _fundamentals_from_dict(hit)
            if not missing_fund_fields(cached, required):
                return cached, []
        fund = await self._assemble_fundamentals_from_persistent(symbol)
        if fund is not None and not missing_fund_fields(fund, required):
            # 永久缓存命中后也回写 latest，加速下次命中
            await self._cache.set("fundamentals_latest", symbol, _fundamentals_to_dict(fund))
            return fund, []
        return None, []

    async def _refresh_fundamentals(
        self, symbol: str, required: list[str]
    ) -> tuple[Fundamentals | None, list[str]]:
        """打源取财报：主源 → 按缺口决定是否打兜底源 → 字段级合并 → 写两级缓存。"""
        market = classify_symbol(symbol)
        errors: list[str] = []
        primary: Fundamentals | None = None

        if market == "A":
            # A 股主源 = AKShare（新浪三表原始科目）
            f, e = await self._safe(
                self._a_fund.fundamentals(symbol), f"{self._a_fund.name} fundamentals"
            )
            errors.extend(e)
            primary = f
            fallback_covers = WESTOCK_FUND_FIELDS
        else:
            # 港美股主源 = WeStock（不走 AKShare）
            if self._westock is not None:
                f, e = await self._safe(
                    self._westock.fundamentals(westock_code(symbol)), "westock fundamentals"
                )
                errors.extend(e)
                if f is not None:
                    f.symbol = symbol
                primary = f
            fallback_covers = set()

        # 字段级补缺：只有兜底源真正能补上缺口时才打它，避免无谓的限流暴露
        missing = missing_fund_fields(primary, required)
        fund = primary
        if missing and set(missing) & fallback_covers:
            fb, e = await self._fallback_fundamentals(symbol, market)
            errors.extend(e)
            if fb is not None:
                before = missing_fund_fields(primary, required)
                merged = _merge_fundamentals(primary, fb)
                filled = set(before) - set(missing_fund_fields(merged, required))
                fund = merged
                if filled:
                    logger.info("[market] %s 财报缺口 %s 已由兜底源补齐：%s",
                                symbol, sorted(before), sorted(filled))

        if fund is not None:
            fund.symbol = symbol
        if self._cache is not None and fund is not None:
            await self._cache.set("fundamentals_latest", symbol, _fundamentals_to_dict(fund))
            await self._persist_fundamentals_by_period(symbol, fund)
        return fund, errors

    async def _fallback_fundamentals(
        self, symbol: str, market: str
    ) -> tuple[Fundamentals | None, list[str]]:
        """兜底财报源：A 股用 WeStock；港美股无第二结构化源。"""
        if market == "A":
            if self._westock is None:
                return None, []
            f, e = await self._safe(
                self._westock.fundamentals(westock_code(symbol)), "westock fundamentals"
            )
            if f is not None:
                f.symbol = symbol
            return f, e
        return None, []

    async def _assemble_fundamentals_from_persistent(self, symbol: str) -> Fundamentals | None:
        """从永久缓存按报告期拼装财报：扫描 fundamentals_report:{symbol}:* 的所有 key，
        合并成多年度 Fundamentals。

        P1 简化实现：用 LIKE 查同 symbol 的全部永久 key，按报告期合并。
        每条永久 key 存的是单个报告期的字段 dict（含 year），不是含 years 的结构。
        永久缓存命中即视为可信（内容 hash 已固化），不再校验。
        """
        from toolkit.market.market_cache import FUNDAMENTALS_REPORT_COMPONENT
        if self._cache is None:
            return None
        try:
            db = await self._cache._conn()
            pattern = f"{FUNDAMENTALS_REPORT_COMPONENT}:{symbol}:%"
            cur = await db.execute(
                "SELECT value FROM market_cache WHERE key LIKE ? ORDER BY key DESC", (pattern,)
            )
            rows = await cur.fetchall()
            if not rows:
                return None
            # 合并所有报告期的字段（去重 by year，保留最新——DESC 排序后先到的优先）
            all_years: dict[str, dict] = {}
            for (value_json,) in rows:
                try:
                    y = json.loads(value_json)
                    if not isinstance(y, dict):
                        continue
                    yr = str(y.get("year", ""))
                    if yr and yr not in all_years:
                        all_years[yr] = y
                except Exception:  # noqa: BLE001
                    continue
            if not all_years:
                return None
            years = [all_years[k] for k in sorted(all_years, reverse=True)]
            return Fundamentals(
                symbol=symbol,
                source="persistent_cache",
                asof="",
                years=years,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[market] 永久缓存拼装失败 [%s]: %s", symbol, e)
            return None

    async def _persist_fundamentals_by_period(self, symbol: str, fund: Fundamentals) -> None:
        """把财报按报告期拆分写入永久缓存。

        每个报告期一个永久 key：fundamentals_report:{symbol}:{period}:{version_hash}。
        version_hash = content_hash(该报告期的字段)。
        内容变（更正公告）→ hash 变 → 新 key（旧 key 保留供回溯，P1 不主动清理）。
        """
        from toolkit.market.market_cache import MarketCache
        for y in fund.years or []:
            period = str(y.get("year", "unknown"))
            version_hash = MarketCache.content_hash(y)
            key = MarketCache.report_key(symbol, period, version_hash)
            await self._cache.set_persistent(key, {"year": period, **{k: v for k, v in y.items() if k != "year"}})  # type: ignore[union-attr]



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
            if self._overseas_data is None:
                return [], ["westock 未启用，港美股新闻不可用"]
            got, e = await self._safe(
                self._overseas_data.news(sym, days=days), f"{self._overseas_data.name} news"
            )
            errors.extend(e)
            news = got or []
        if self._cache is not None and not news and errors:
            stale = await self._cache.get_stale("news", cache_key)
            if stale is not None:
                value, stale_seconds = stale
                return [_news_from_dict(d) for d in value], [
                    *errors,
                    f"news stale_cache: expired {stale_seconds:.0f}s ago",
                ]
        if self._cache is not None and news:
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
        """统一超时/异常降级 + P2 provider guard（限流 + 熔断）。

        label 格式约定："{provider_name} <component>"，从第一个空格前提取 provider 名。
        guard 行为：
          - 熔断中 / 无令牌 → 跳过该 provider，返回 (None, ["<label>: circuit_open/rate_limited"])
          - 允许 → 带 provider 级硬超时执行；成功记录 success，失败记录 failure。
        guard 失败绝不阻断主流程。
        """
        provider_name = label.split(" ", 1)[0] if " " in label else label
        # guard 未装配（极简测试场景）→ 退化到旧的纯 timeout 兜底
        if self._guards is None:
            try:
                return await asyncio.wait_for(awaitable, timeout=self.settings.market_timeout_seconds), []
            except Exception as exc:
                logger.warning("market 取数降级 [%s]: %s", label, exc)
                return None, [f"{label}: {type(exc).__name__}: {exc}"]

        guard = self._guards.get(provider_name)
        allowed, reason = await guard.allow()
        if not allowed:
            logger.info("[guard:%s] 跳过 [%s]: %s", provider_name, label, reason)
            if hasattr(awaitable, "close"):
                awaitable.close()
            return None, [f"{label}: {reason}"]
        try:
            result = await guard.run(awaitable, label)
            guard.record_success()
            return result, []
        except Exception as exc:
            guard.record_failure(f"{type(exc).__name__}: {exc}")
            logger.warning("market 取数降级 [%s]: %s（连续失败 %d）", label, exc,
                           guard.stats.consecutive_failures)
            return None, [f"{label}: {type(exc).__name__}: {exc}"]

    def guard_stats(self) -> dict[str, dict]:
        """所有 provider 的 guard 统计快照（P2 监控用）。guard 未装配返回空。"""
        if self._guards is None:
            return {}
        return self._guards.all_stats()

    def reset_guard(self, provider_name: str | None = None) -> None:
        """重置 guard 统计（测试用）。guard 未装配则空操作。"""
        if self._guards is not None:
            self._guards.reset(provider_name)
