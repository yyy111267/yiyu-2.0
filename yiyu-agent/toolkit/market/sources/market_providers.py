"""数据源 provider 实现（Phase 0）。

四个已拍板的数据源，全部免费免 token：
- EMQuoteProvider   东方财富 push2 实时行情（A股快照主源，含 PE/PB/市值）
- SinaQuoteProvider 新浪 hq.sinajs.cn 实时行情（A股快照备源）
- AKShareProvider   akshare（A股财报 stock_financial_* + 东财个股新闻）
- CNInfoProvider    巨潮资讯网（A股官方公告，证监会指定信披平台）
- YFinanceProvider  yfinance（港股/美股 快照+财报+新闻）

约定：
- akshare / yfinance 为同步库且 import 较重 → 函数内懒加载 + asyncio.to_thread。
- 所有 HTTP endpoint 硬编码为常量，仅 symbol 作为参数拼接（防 SSRF 面）。
- 解析一律防御性（缺字段得 None，不抛异常）；网络/格式异常向上抛，由
  MarketData._safe 统一降级。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from toolkit.market.market import Fundamentals, NewsItem, Snapshot
from toolkit.market.market_router import split_a_symbol

logger = logging.getLogger(__name__)

# ── 公开数据源 endpoint（硬编码常量，禁止外部传入 base url）────────────
_EM_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
# 东财网页端公开 token（仅标识客户端类型，非用户密钥）
_EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_SINA_QUOTE_URL = "https://hq.sinajs.cn/list={code}"
_SINA_REFERER = "https://finance.sina.com.cn"  # 不带 Referer 新浪返回 403
_CNINFO_SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
_CNINFO_ANN_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
_CNINFO_STATIC = "https://static.cninfo.com.cn"


def _f(v: Any) -> float | None:
    """宽松数值解析：'-'、''、None、nan/inf 一律视为缺失。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    s = str(v).strip().replace(",", "")
    if s in {"", "-", "--", "None", "nan", "inf", "-inf"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ────────────────────────────────────────────────────────────────────
# 东方财富实时行情（A股快照主源）
# ────────────────────────────────────────────────────────────────────

def _em_secid(symbol: str) -> str:
    """东财 secid：上交所前缀 1.，深/北交所前缀 0.。"""
    code, ex = split_a_symbol(symbol)
    return f"1.{code}" if ex == "SH" else f"0.{code}"


class EMQuoteProvider:
    """东方财富 push2 快照：现价/涨跌幅/总市值/PE/PB。52 周高低该接口不含，留 None。"""

    name = "eastmoney"
    _FIELDS = "f57,f58,f43,f44,f45,f46,f60,f169,f170,f116,f162,f167"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def snapshot(self, symbol: str) -> Snapshot | None:
        resp = await self._client.get(
            _EM_QUOTE_URL,
            params={
                "secid": _em_secid(symbol),
                "fields": self._FIELDS,
                "fltt": "2",   # 价格以浮点返回（默认放大 100 倍整数）
                "invt": "2",
                "ut": _EM_UT,
            },
        )
        resp.raise_for_status()
        data = (resp.json() or {}).get("data")
        if not data:
            return None
        return Snapshot(
            symbol=symbol,
            source=self.name,
            name=data.get("f58"),
            price=_f(data.get("f43")),
            prev_close=_f(data.get("f60")),
            change_pct=_f(data.get("f170")),
            market_cap=_f(data.get("f116")),
            pe=_f(data.get("f162")),
            pb=_f(data.get("f167")),
            currency="CNY",
            asof=_now_str(),
        )


# ────────────────────────────────────────────────────────────────────
# 东方财富实时行情（港股 / 美股 快照主源）
# ────────────────────────────────────────────────────────────────────

class EMOverseasQuoteProvider:
    """东方财富 push2 实时行情（港股 / 美股快照主源）。

    港股代码形如 0700.HK → secid 前缀 116. + 5 位代码；
    美股字母 ticker（如 AAPL）→ secid 前缀 105.(NASDAQ) / 106.(NYSE)。
    仅取价格类快照（现价/涨跌/昨收/名称/时间）；PE/PB/市值该海外接口
    字段不稳定，留 None，由 yfinance 兜底财报与新闻。
    取数失败或返回空 → 返回 None，交给 MarketData failover 落到 yfinance。
    """

    name = "eastmoney"
    _FIELDS = "f57,f58,f43,f44,f45,f46,f60,f169,f170,f116,f162,f167,f86"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    def _secids(self, symbol: str) -> list[str]:
        s = symbol.upper()
        if s.endswith(".HK"):
            code = re.sub(r"\D", "", s).zfill(5)   # 700 → 00700
            return [f"116.{code}"]
        # 美股：NASDAQ 优先，失败再试 NYSE
        return [f"105.{s}", f"106.{s}"]

    async def snapshot(self, symbol: str) -> Snapshot | None:
        for secid in self._secids(symbol):
            snap = await self._try_secid(secid, symbol)
            if snap is not None:
                return snap
        return None

    async def _try_secid(self, secid: str, symbol: str) -> Snapshot | None:
        is_hk = secid.startswith("116.")
        for _ in range(3):  # 东财偶发断连，重试提升稳定性
            try:
                resp = await self._client.get(
                    _EM_QUOTE_URL,
                    params={
                        "secid": secid,
                        "fields": self._FIELDS,
                        "fltt": "2",
                        "invt": "2",
                        "ut": _EM_UT,
                    },
                )
                resp.raise_for_status()
                data = (resp.json() or {}).get("data")
                if not data:
                    return None
                return self._parse(data, symbol, is_hk)
            except Exception:
                logger.warning("东财海外快照重试 secid=%s", secid, exc_info=True)
        return None

    @staticmethod
    def _parse(data: dict, symbol: str, is_hk: bool) -> Snapshot | None:
        price = _f(data.get("f43"))
        if price is None:
            return None
        prev = _f(data.get("f60"))
        change_pct = _f(data.get("f170")) or _f(data.get("f168"))
        if change_pct is None and prev:
            change_pct = round((price - prev) / prev * 100, 2)
        asof = _now_str()
        if data.get("f86"):
            try:
                asof = datetime.fromtimestamp(int(data["f86"])).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError, TypeError):
                pass
        return Snapshot(
            symbol=symbol,
            source=EMOverseasQuoteProvider.name,
            name=data.get("f58"),
            price=price,
            prev_close=prev,
            change_pct=change_pct,
            market_cap=_f(data.get("f116")),
            pe=_f(data.get("f162")),
            pb=_f(data.get("f167")),
            currency="HKD" if is_hk else "USD",
            asof=asof,
        )


# ────────────────────────────────────────────────────────────────────
# 新浪实时行情（A股快照备源）
# ────────────────────────────────────────────────────────────────────

def _sina_code(symbol: str) -> str:
    """600519.SH → sh600519；000001 → sz000001。"""
    code, ex = split_a_symbol(symbol)
    return f"{ex.lower()}{code}"


def parse_sina_quote(text: str, symbol: str) -> Snapshot | None:
    """解析新浪行情文本：var hq_str_sh600519="名称,今开,昨收,现价,最高,最低,..."。

    A股字段位置：0 名称, 1 今开, 2 昨收, 3 现价, 4 最高, 5 最低, 30 日期, 31 时间。
    新浪不提供 PE/PB/市值，相应字段为 None。
    """
    if '="' not in text:
        return None
    payload = text.split('="', 1)[1].rstrip('";\r\n ')
    parts = payload.split(",")
    if len(parts) < 32 or not parts[0]:
        return None
    price, prev = _f(parts[3]), _f(parts[2])
    change_pct = round((price - prev) / prev * 100, 2) if price and prev else None
    return Snapshot(
        symbol=symbol,
        source="sina",
        name=parts[0],
        price=price,
        prev_close=prev,
        change_pct=change_pct,
        currency="CNY",
        asof=f"{parts[30]} {parts[31]}",
    )


class SinaQuoteProvider:
    name = "sina"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def snapshot(self, symbol: str) -> Snapshot | None:
        resp = await self._client.get(
            _SINA_QUOTE_URL.format(code=_sina_code(symbol)),
            headers={"Referer": _SINA_REFERER},
        )
        resp.raise_for_status()
        # 新浪固定 GBK 编码，且响应头常缺失 charset，手动解码
        return parse_sina_quote(resp.content.decode("gbk", errors="ignore"), symbol)


# ────────────────────────────────────────────────────────────────────
# akshare（A股财报 + 东财个股新闻）
# ────────────────────────────────────────────────────────────────────

class AKShareProvider:
    name = "akshare"

    async def fundamentals(self, symbol: str, years: int = 5) -> Fundamentals | None:
        code, ex = split_a_symbol(symbol)
        return await asyncio.to_thread(self._fundamentals_sync, code, ex, years)

    def _fundamentals_sync(self, code: str, ex: str, years: int) -> Fundamentals | None:
        import akshare as ak

        # 说明：东财 em 系列接口（财务指标/三大报表 by_report_em）在当前 akshare
        # 下因东财页面改版集体失效（解析 None），故三表统一走新浪 stock_financial_report_sina。
        rows: list[dict] = []
        # 1) 财务指标（东财，可能失效）：ROE / 毛利率 / 资产负债率 / EPS
        try:
            df = ak.stock_financial_analysis_indicator_em(symbol=code)
            rows = _parse_ak_indicator(df, years)
        except Exception:
            logger.warning("akshare 财务指标(东财)取数失败，比率将由三表兜底推导: %s", code)
        # 2) 利润表（新浪）：年份锚 + 营收/净利 + EBIT 近似所需字段
        #    （东财财务指标接口失效时，用利润表的报告日生成年份锚，保证三表能 merge 上）
        pl = None
        try:
            pl = ak.stock_financial_report_sina(stock=f"{ex.lower()}{code}", symbol="利润表")
        except Exception:
            logger.exception("akshare 利润表取数失败: %s", code)
        if not rows and pl is not None and not pl.empty:
            rows = _ak_year_anchors(pl)[:years]
        _merge_ak_income(rows, pl, years)
        _merge_ak_profit(rows, pl)
        # 3) 资产负债表（新浪）：总资产/负债/净资产/货币资金/存货/合同负债/有息负债等
        #    ——补齐 base_pack 的 total_assets/total_equity/cash/total_debt/contract_liability
        try:
            bal = ak.stock_financial_report_sina(stock=f"{ex.lower()}{code}", symbol="资产负债表")
            _merge_ak_balance(rows, bal)
        except Exception:
            logger.warning("akshare 资产负债表(新浪)取数失败: %s", code)
        # 4) 现金流量表（新浪）：经营现金流 / 资本开支 —— 供 OCF/FCF 指标
        try:
            cf = ak.stock_financial_report_sina(stock=f"{ex.lower()}{code}", symbol="现金流量表")
            _merge_ak_cashflow(rows, cf)
        except Exception:
            logger.warning("akshare 现金流量表(新浪)取数失败: %s", code)
        # 5) 比率兜底推导：财务指标接口失效时，gross_margin/roe/debt_ratio 由三表字段推算
        _derive_ak_ratios(rows)
        if not rows:
            return None
        return Fundamentals(symbol=code, source=self.name, years=rows,
                            asof=date.today().isoformat())

    async def news(self, symbol: str, days: int = 7) -> list[NewsItem]:
        code, _ = split_a_symbol(symbol)
        return await asyncio.to_thread(self._news_sync, code, days)

    def _news_sync(self, code: str, days: int) -> list[NewsItem]:
        import akshare as ak

        df = ak.stock_news_em(symbol=code)
        cutoff = datetime.now() - timedelta(days=days)
        items: list[NewsItem] = []
        for _, row in df.iterrows():
            title = str(row.get("新闻标题", "")).strip()
            if not title:
                continue
            pub = str(row.get("发布时间", "")).strip()
            try:
                if datetime.strptime(pub, "%Y-%m-%d %H:%M:%S") < cutoff:
                    continue
            except ValueError:
                pass  # 时间格式异常不丢弃，保留该条
            summary = str(row.get("新闻内容", "")).strip()
            items.append(NewsItem(
                title=title,
                source=self.name,
                category="news",
                url=row.get("新闻链接") or None,
                published_at=pub or None,
                summary=summary[:200] if summary else None,
            ))
        return items


def _match_col(columns, *keywords: str) -> str | None:
    for col in columns:
        name = str(col)
        if all(k in name for k in keywords):
            return name
    return None


def _parse_ak_indicator(df, years: int) -> list[dict]:
    """东财财务指标 → years 列表。优先年报（12-31），不足则取最近若干期。"""
    if df is None or df.empty:
        return []
    col_date = _match_col(df.columns, "日期") or _match_col(df.columns, "REPORT")
    col_roe = _match_col(df.columns, "净资产收益率")
    col_gm = _match_col(df.columns, "毛利率")
    col_debt = _match_col(df.columns, "资产负债率")
    col_eps = _match_col(df.columns, "每股收益")
    if not col_date:
        return []

    records = df.to_dict("records")
    annual = [r for r in records if str(r.get(col_date, ""))[5:10] == "12-31"]
    picked = (annual or records)[:years]

    rows: list[dict] = []
    for r in picked:
        day = str(r.get(col_date, ""))[:10]
        row: dict[str, Any] = {"year": day[:4]}
        if col_roe:
            row["roe"] = _f(r.get(col_roe))
        if col_gm:
            row["gross_margin"] = _f(r.get(col_gm))
        if col_debt:
            row["debt_ratio"] = _f(r.get(col_debt))
        if col_eps:
            row["eps"] = _f(r.get(col_eps))
        rows.append({k: v for k, v in row.items() if v is not None or k == "year"})
    return rows


def _merge_ak_income(rows: list[dict], pl, years: int) -> None:
    """把新浪利润表的营收/净利按年份并入 rows（单位：元）。"""
    if pl is None or pl.empty or not rows:
        return
    col_date = _match_col(pl.columns, "报告日") or _match_col(pl.columns, "日期")
    col_rev = _match_col(pl.columns, "营业总收入") or _match_col(pl.columns, "营业收入")
    col_net = _match_col(pl.columns, "净利润")
    if not col_date:
        return
    by_year: dict[str, dict] = {}
    for r in pl.to_dict("records"):
        y = str(r.get(col_date, ""))[:4]
        if y and y not in by_year:
            by_year[y] = r
    for row in rows:
        r = by_year.get(str(row.get("year", "")))
        if not r:
            continue
        if col_rev:
            row.setdefault("revenue", _f(r.get(col_rev)))
        if col_net:
            row.setdefault("net_profit", _f(r.get(col_net)))


def _pick_col(columns, *cands: str) -> str | None:
    """列名匹配：先精确命中，再模糊子串匹配（支持中英文列名变体）。

    精确优先解决复合列歧义：'应收账款' 须命中独立列，而不是先碰到
    '应收票据及应收账款' 就返回（新浪三表列名里这类复合列常见）。
    """
    names = {str(c) for c in columns}
    for c in cands:
        if c in names:
            return c
    for c in cands:
        col = _match_col(columns, c)
        if col:
            return col
    return None


def _norm_report_day(raw) -> str | None:
    """报告期归一化为 'YYYY-MM-DD'。兼容 20260331 / 2024-12-31 / datetime。"""
    s = str(raw).strip()
    if not s:
        return None
    s = s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    return None


def _ak_rows_by_year(df, col_date: str) -> dict[str, dict]:
    """按报告期的报表 → {year: row}。优先 12-31 年报，同年取更晚报告期。"""
    out: dict[str, dict] = {}
    for r in df.to_dict("records"):
        day = _norm_report_day(r.get(col_date))
        if not day or not day[:4].isdigit():
            continue
        y = day[:4]
        cur = out.get(y)
        if cur is None or day.endswith("12-31") or day > str(cur.get("_day", "")):
            out[y] = {"_day": day, **r}
    return out


def _ak_year_anchors(df) -> list[dict]:
    """从报表（按报告日）生成年份锚序列：优先 12-31 年报，同年取更晚报告期。

    东财财务指标接口失效时，用利润表的报告日生成年份列表，保证三表能 merge 上。
    """
    col_date = _pick_col(df.columns, "报告日", "报告期", "REPORT_DATE", "REPORT")
    if not col_date:
        return []
    out: dict[str, dict] = {}
    for r in df.to_dict("records"):
        day = _norm_report_day(r.get(col_date))
        if not day or not day[:4].isdigit():
            continue
        y = day[:4]
        cur = out.get(y)
        if cur is None or day.endswith("12-31") or day > str(cur.get("_day", "")):
            out[y] = {"_day": day, "year": y}
    # 去掉内部 _day 键（仅排序/选期用），避免泄漏进 years 与 data_pack
    return [{"year": out[y]["year"]} for y in sorted(out, reverse=True)]


def _derive_ak_ratios(rows: list[dict]) -> None:
    """三表取数后的比率兜底推导（东财财务指标接口可能失效）。

    gross_margin = (营收−营业成本)/营收；roe = 净利/净资产；
    debt_ratio = 总负债/总资产。已有则不动（保留原接口口径）。
    """
    for row in rows:
        rev, cogs = row.get("revenue"), row.get("cogs")
        if "gross_margin" not in row and rev and cogs is not None and rev != 0:
            row["gross_margin"] = round((rev - cogs) / rev * 100, 2)
        np_, eq = row.get("net_profit"), row.get("equity")
        if "roe" not in row and np_ is not None and eq:
            row["roe"] = round(np_ / eq * 100, 2)
        liab, ast = row.get("total_liabilities"), row.get("assets")
        if "debt_ratio" not in row and liab is not None and ast:
            row["debt_ratio"] = round(liab / ast * 100, 2)


def _merge_ak_balance(rows: list[dict], df) -> None:
    """新浪资产负债表（单位：元）→ 按年并入 rows。

    补字段：assets / total_liabilities / equity / cash / inventory / fixed_assets /
    accounts_receivable / accounts_payable / contract_liability / total_debt(近似)。
    """
    if df is None or df.empty or not rows:
        return
    col_date = _pick_col(df.columns, "报告日", "报告期", "REPORT_DATE", "REPORT")
    if not col_date:
        return
    cols = {
        "assets": _pick_col(df.columns, "资产总计", "TOTAL_ASSETS", "ASSETS_TOTAL"),
        "total_liabilities": _pick_col(df.columns, "负债合计", "TOTAL_LIABILITIES"),
        "equity": _pick_col(df.columns, "所有者权益(或股东权益)合计", "所有者权益合计",
                            "股东权益合计", "归属于母公司股东的权益", "归属于母公司股东权益合计",
                            "所有者权益", "股东权益", "TOTAL_EQUITY", "OWNER_EQUITY"),
        "cash": _pick_col(df.columns, "货币资金", "MONETARYFUNDS", "CASH"),
        "inventory": _pick_col(df.columns, "存货", "INVENTORY", "INVENTORIES"),
        "fixed_assets": _pick_col(df.columns, "固定资产净额", "固定资产及清理合计",
                                  "固定资产", "FIXED_ASSET"),
        "accounts_receivable": _pick_col(df.columns, "应收账款", "ACCOUNTS_RECE"),
        "accounts_payable": _pick_col(df.columns, "应付账款", "ACCOUNTS_PAYA"),
        "contract_liability": _pick_col(df.columns, "合同负债", "CONTRACT_LIAB"),
        "short_loan": _pick_col(df.columns, "短期借款", "SHORT_LOAN"),
        "long_loan": _pick_col(df.columns, "长期借款", "LONG_LOAN"),
        "bond": _pick_col(df.columns, "应付债券", "BOND_PAYABLE"),
        "ncl_due": _pick_col(df.columns, "一年内到期的非流动负债", "NONCURRENT_LIAB_1Y"),
    }
    if not any(v is not None for v in cols.values()):
        return
    by_year = _ak_rows_by_year(df, col_date)
    for row in rows:
        rec = by_year.get(str(row.get("year", "")))
        if not rec:
            continue
        for field, col in cols.items():
            if col is None:
                continue
            v = _f(rec.get(col))
            if v is not None:
                row.setdefault(field, v)
        # 有息负债 ≈ 短借 + 长借 + 应付债券 + 一年内到期非流动负债（近似）
        if "total_debt" not in row:
            parts = [row.get(f) for f in ("short_loan", "long_loan", "bond", "ncl_due")]
            parts = [v for v in parts if v is not None]
            if parts:
                row["total_debt"] = sum(parts)


def _merge_ak_profit(rows: list[dict], df) -> None:
    """新浪利润表（单位：元）→ 按年并入 rows。

    补字段：cogs / selling_expense / admin_expense / rd_expense / interest_expense /
    operating_profit / total_profit / eps（供 EBIT 近似：营业利润+财务费用）。
    """
    if df is None or df.empty or not rows:
        return
    col_date = _pick_col(df.columns, "报告日", "报告期", "REPORT_DATE", "REPORT")
    if not col_date:
        return
    cols = {
        "cogs": _pick_col(df.columns, "营业成本", "OPERATING_COST"),
        "selling_expense": _pick_col(df.columns, "销售费用", "SALE_EXPENSE"),
        "admin_expense": _pick_col(df.columns, "管理费用", "ADMIN_EXPENSE"),
        "rd_expense": _pick_col(df.columns, "研发费用", "RD_EXPENSE"),
        "interest_expense": _pick_col(df.columns, "利息费用", "财务费用",
                                      "FINANCE_EXPENSE", "INTEREST_EXPENSE"),
        "operating_profit": _pick_col(df.columns, "营业利润", "OPERATE_PROFIT"),
        "total_profit": _pick_col(df.columns, "利润总额", "TOTAL_PROFIT"),
        "eps": _pick_col(df.columns, "基本每股收益", "EPS"),
    }
    if not any(v is not None for v in cols.values()):
        return
    by_year = _ak_rows_by_year(df, col_date)
    for row in rows:
        rec = by_year.get(str(row.get("year", "")))
        if not rec:
            continue
        for field, col in cols.items():
            if col is None:
                continue
            v = _f(rec.get(col))
            if v is not None:
                row.setdefault(field, v)


def _merge_ak_cashflow(rows: list[dict], df) -> None:
    """新浪现金流量表（单位：元）→ 按年并入 rows。

    补字段：ocf（经营现金流净额）/ capex（购建固定资产等支付的现金）。
    """
    if df is None or df.empty or not rows:
        return
    col_date = _pick_col(df.columns, "报告日", "报告期", "REPORT_DATE", "REPORT")
    if not col_date:
        return
    col_ocf = _pick_col(df.columns, "经营活动产生的现金流量净额", "NETCASH_OPERATE", "OCF")
    col_capex = _pick_col(df.columns, "购建固定资产", "CONSTRUCT_LONG_ASSET")
    if not col_ocf and not col_capex:
        return
    by_year = _ak_rows_by_year(df, col_date)
    for row in rows:
        rec = by_year.get(str(row.get("year", "")))
        if not rec:
            continue
        if col_ocf:
            v = _f(rec.get(col_ocf))
            if v is not None:
                row.setdefault("ocf", v)
        if col_capex:
            v = _f(rec.get(col_capex))
            if v is not None:
                row.setdefault("capex", v)


# ────────────────────────────────────────────────────────────────────
# 巨潮资讯网（A股官方公告）
# ────────────────────────────────────────────────────────────────────

class CNInfoProvider:
    """巨潮公告：先 topSearch 拿 orgId，再 hisAnnouncement 查公告列表。"""

    name = "cninfo"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def news(self, symbol: str, days: int = 7) -> list[NewsItem]:
        code, ex = split_a_symbol(symbol)
        org_id = await self._org_id(code)
        if not org_id:
            logger.warning("巨潮未找到 orgId: %s", code)
            return []
        column = {"SH": "sse", "SZ": "szse", "BJ": "bj"}[ex]
        end = date.today()
        start = end - timedelta(days=days)
        resp = await self._client.post(
            _CNINFO_ANN_URL,
            data={
                "pageNum": "1",
                "pageSize": "20",
                "column": column,
                "tabName": "fulltext",
                "plate": "",
                "stock": f"{code},{org_id}",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": f"{start.isoformat()}~{end.isoformat()}",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "false",
            },
        )
        resp.raise_for_status()
        announcements = (resp.json() or {}).get("announcements") or []
        items: list[NewsItem] = []
        for a in announcements:
            title = re.sub(r"<[^>]+>", "", str(a.get("announcementTitle") or "")).strip()
            if not title:
                continue
            ts = a.get("announcementTime")
            pub = (
                datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
                if ts else None
            )
            adjunct = a.get("adjunctUrl")
            items.append(NewsItem(
                title=title,
                source=self.name,
                category="announcement",
                url=f"{_CNINFO_STATIC}/{adjunct}" if adjunct else None,
                published_at=pub,
            ))
        return items

    async def _org_id(self, code: str) -> str | None:
        resp = await self._client.post(
            _CNINFO_SEARCH_URL, data={"keyWord": code, "maxNum": "10"}
        )
        resp.raise_for_status()
        for item in resp.json() or []:
            if str(item.get("code")) == code:
                return item.get("orgId")
        return None


# ────────────────────────────────────────────────────────────────────
# yfinance（港股 / 美股 全包）
# ────────────────────────────────────────────────────────────────────

class YFinanceProvider:
    name = "yfinance"

    async def snapshot(self, symbol: str) -> Snapshot | None:
        return await asyncio.to_thread(self._snapshot_sync, symbol)

    def _snapshot_sync(self, symbol: str) -> Snapshot | None:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        fast: dict = {}
        try:
            fast = dict(ticker.fast_info)
        except Exception:
            logger.exception("yfinance fast_info 失败: %s", symbol)
        info: dict = {}
        try:
            info = ticker.info or {}
        except Exception:
            # .info 接口不稳定，缺失时仅用 fast_info 字段
            logger.info("yfinance .info 不可用: %s", symbol)

        price = _f(fast.get("last_price")) or _f(info.get("currentPrice")) \
            or _f(info.get("regularMarketPrice"))
        prev = _f(fast.get("previous_close")) or _f(info.get("previousClose"))
        change_pct = round((price - prev) / prev * 100, 2) if price and prev else None
        if price is None and not info:
            return None
        return Snapshot(
            symbol=symbol,
            source=self.name,
            name=info.get("longName") or info.get("shortName"),
            price=price,
            prev_close=prev,
            change_pct=change_pct,
            market_cap=_f(fast.get("market_cap")) or _f(info.get("marketCap")),
            pe=_f(info.get("trailingPE")),
            pb=_f(info.get("priceToBook")),
            week52_high=_f(fast.get("year_high")) or _f(info.get("fiftyTwoWeekHigh")),
            week52_low=_f(fast.get("year_low")) or _f(info.get("fiftyTwoWeekLow")),
            currency=fast.get("currency") or info.get("currency"),
            asof=_now_str(),
        )

    async def fundamentals(self, symbol: str, years: int = 5) -> Fundamentals | None:
        return await asyncio.to_thread(self._fundamentals_sync, symbol, years)

    def _fundamentals_sync(self, symbol: str, years: int) -> Fundamentals | None:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        fin = _safe_df(ticker, "financials")
        bs = _safe_df(ticker, "balance_sheet")
        cf = _safe_df(ticker, "cashflow")
        if fin is None or fin.empty:
            return None

        rows: list[dict] = []
        for col in list(fin.columns)[:years]:
            year = str(getattr(col, "year", str(col)[:4]))
            revenue = _df_at(fin, col, "Total Revenue", "TotalRevenue")
            net = _df_at(fin, col, "Net Income", "NetIncome",
                         "Net Income Common Stockholders")
            gross = _df_at(fin, col, "Gross Profit", "GrossProfit")
            equity = _df_at(bs, col, "Stockholders Equity", "Total Stockholder Equity") if bs is not None else None
            assets = _df_at(bs, col, "Total Assets", "TotalAssets") if bs is not None else None
            debt = _df_at(bs, col, "Total Debt", "TotalDebt") if bs is not None else None
            ocf = _df_at(cf, col, "Operating Cash Flow",
                         "Total Cash From Operating Activities") if cf is not None else None
            capex = _df_at(cf, col, "Capital Expenditure", "CapitalExpenditure") if cf is not None else None

            row: dict[str, Any] = {"year": year}
            row["revenue"] = revenue
            row["net_profit"] = net
            if gross is not None and revenue:
                row["gross_margin"] = round(gross / revenue * 100, 1)
            if net is not None and equity:
                row["roe"] = round(net / equity * 100, 1)
            if debt is not None and assets:
                row["debt_ratio"] = round(debt / assets * 100, 1)
            row["ocf"] = ocf
            row["capex"] = capex
            rows.append({k: v for k, v in row.items() if v is not None or k == "year"})
        return Fundamentals(symbol=symbol, source=self.name, years=rows,
                            asof=date.today().isoformat())

    async def news(self, symbol: str, days: int = 7) -> list[NewsItem]:
        return await asyncio.to_thread(self._news_sync, symbol, days)

    def _news_sync(self, symbol: str, days: int) -> list[NewsItem]:
        import yfinance as yf

        raw = yf.Ticker(symbol).news or []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        items: list[NewsItem] = []
        for n in raw:
            title, url, pub_dt = parse_yf_news_item(n)
            if not title:
                continue
            if pub_dt is not None and pub_dt < cutoff:
                continue
            items.append(NewsItem(
                title=title,
                source=self.name,
                category="news",
                url=url,
                published_at=pub_dt.strftime("%Y-%m-%d %H:%M:%S") if pub_dt else None,
            ))
        return items


def _safe_df(ticker, attr: str):
    try:
        df = getattr(ticker, attr)
        return df if df is not None and not df.empty else None
    except Exception:
        logger.info("yfinance %s 不可用: %s", attr, ticker.ticker)
        return None


def _df_at(df, col, *keys: str) -> float | None:
    for key in keys:
        if key in df.index:
            return _f(df.loc[key, col])
    return None


def parse_yf_news_item(n: dict) -> tuple[str | None, str | None, datetime | None]:
    """兼容 yfinance 新旧两版 news 结构。

    新版: {"content": {"title", "canonicalUrl": {"url"}, "pubDate": ISO}}
    旧版: {"title", "link", "providerPublishTime": epoch}
    """
    content = n.get("content") if isinstance(n.get("content"), dict) else n
    title = content.get("title")
    url = (content.get("canonicalUrl") or {}).get("url") or content.get("link")
    pub_dt: datetime | None = None
    if content.get("pubDate"):
        try:
            pub_dt = datetime.fromisoformat(str(content["pubDate"]).replace("Z", "+00:00"))
        except ValueError:
            pub_dt = None
    elif content.get("providerPublishTime"):
        pub_dt = datetime.fromtimestamp(int(content["providerPublishTime"]), tz=timezone.utc)
    return title, url, pub_dt


# ──────────────────────────────────────────────────────────────────────────
# WeStockProvider —— 腾讯自选股公开接口 CLI（westock-data-clawhub），A股/港股/美股主源
#
# 解决"数据源不好抓"：港股/美股不再强依赖被限流的 yfinance；A股多一个更稳的来源。
# 覆盖：行情(kline/profile) + 财报(finance)；新闻/公告继续走现有 akshare/巨潮/yfinance 兜底。
# 失败时抛异常，由 MarketData 的 failover 落到现有数据源。
#
# 调用形态：westock-data-clawhub <cmd> <code> [--period ...] [--type ...] [--num N]
#   code 前缀：A股 sh/sz/bj + 6位；港股 hk + 5位(如 hk00700)；美股 us +  ticker(如 usAAPL)
# ──────────────────────────────────────────────────────────────────────────

class WeStockProvider:
    name = "westock"

    def __init__(self, bin_path: str = "westock-data-clawhub",
                 timeout: float = 15.0, years: int = 5) -> None:
        self._bin = bin_path
        self._timeout = timeout
        self._years = years

    # ── 子进程调用（失败即抛，触发 failover）──
    async def _run(self, *args: str) -> str:
        import shutil

        exe = shutil.which(self._bin) or self._bin
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"未找到 westock 可执行文件：{self._bin}（请先 `npm i -g westock-data-clawhub`）"
            ) from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.CancelledError:
            # 外层 _safe 超时取消：务必杀掉残留 node 进程，避免僵尸
            proc.kill()
            raise
        except asyncio.TimeoutError:
            if proc.returncode is None:
                proc.kill()
            raise TimeoutError(f"westock 调用超时（{self._timeout}s）：{' '.join(args)}")
        if proc.returncode != 0:
            msg = (err or b"").decode("utf-8", "ignore")[:300]
            raise RuntimeError(f"westock 返回非零({proc.returncode})：{msg}")
        return out.decode("utf-8", "ignore")

    # ── Markdown 表格解析 ──
    @staticmethod
    def _parse_md(text: str):
        """返回 (headers, rows)。空表/无数据消息返回 ([], [])。"""
        if not text or "无财务数据" in text or "数据为空" in text or "未找到" in text:
            return [], []
        lines = [l for l in text.splitlines() if l.strip().startswith("|")]
        if len(lines) < 3:
            return [], []
        headers = [h.strip() for h in lines[0].strip().strip("|").split("|")]
        rows = []
        for l in lines[2:]:  # 跳过表头与分隔行
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            if len(cells) != len(headers):
                continue
            rows.append(dict(zip(headers, cells)))
        return headers, rows

    @staticmethod
    def _norm(h: str) -> str:
        return re.sub(r"\s+", "", h).lower()

    @classmethod
    def _col_index(cls, headers: list[str]) -> dict[str, str]:
        return {cls._norm(h): h for h in headers}

    @staticmethod
    def _num(s):
        """解析金额，兼容逗号、中文 亿/万 单位。非数字返回 None。"""
        if s is None:
            return None
        s = s.strip().replace(",", "")
        if s in ("", "-", "--", "None", "nan"):
            return None
        unit = 1.0
        if "亿" in s:
            unit = 1e8
        elif "万" in s:
            unit = 1e4
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if not m:
            return None
        return float(m.group()) * unit

    @staticmethod
    def _year_of(row: dict):
        for key in ("_date", "date", "FinancialYear", "EndDate", "报告期"):
            v = row.get(key)
            if v:
                digits = re.search(r"\d{4}", str(v))
                if digits:
                    return int(digits.group())
        return None

    @classmethod
    def _pick(cls, idx: dict[str, str], row: dict, cands: list[str]):
        for c in cands:
            if c in idx:
                return cls._num(row.get(idx[c]))
        return None

    @staticmethod
    async def _try(coro):
        """吞掉单次 CLI 异常，返回文本或 None（best-effort 字段用）。"""
        try:
            return await coro
        except Exception:  # noqa: BLE001
            return None

    # ── 快照 ──
    # 注意：westock 后端对并发请求限流（并发会返回"数据为空"），故顺序调用。
    async def snapshot(self, code: str) -> Snapshot:
        day_text = await self._try(self._run("kline", code, "--period", "day", "--limit", "2"))
        month_text = await self._try(self._run("kline", code, "--period", "month", "--limit", "13"))  # 52周近似
        prof_text = await self._try(self._run("profile", code))

        price = prev_close = change_pct = asof = None
        if day_text:
            _, rows = self._parse_md(day_text)
            if rows:
                rows.sort(key=lambda r: str(r.get("_date") or r.get("date") or ""), reverse=True)
                latest = rows[0]
                price = self._num(latest.get("last"))
                asof = latest.get("_date") or latest.get("date")
                if len(rows) >= 2 and latest.get("last") is not None:
                    prev_close = self._num(rows[1].get("last"))
                    if prev_close:
                        change_pct = round((price - prev_close) / prev_close * 100, 2)

        week52_high = week52_low = None
        if month_text:
            _, rows = self._parse_md(month_text)
            if rows:
                highs = [self._num(r.get("high")) for r in rows]
                lows = [self._num(r.get("low")) for r in rows]
                highs = [x for x in highs if x is not None]
                lows = [x for x in lows if x is not None]
                if highs:
                    week52_high = max(highs)
                if lows:
                    week52_low = min(lows)

        name = None
        if prof_text:
            h, rows = self._parse_md(prof_text)
            idx = self._col_index(h)
            name_col = idx.get("name") or idx.get("股票简称") or idx.get("证券名称")
            if rows and name_col:
                name = rows[0].get(name_col) or None

        if price is None:
            raise RuntimeError("westock 未取到价格，降级到兜底源")

        currency = "CNY" if code[:1] in ("s", "b") else ("HKD" if code.startswith("hk") else "USD")
        return Snapshot(
            symbol=code, name=name, price=price, prev_close=prev_close, change_pct=change_pct,
            market_cap=None, pe=None, pb=None,
            week52_high=week52_high, week52_low=week52_low,
            currency=currency, source=self.name, asof=asof,
        )

    # ── 财报 ──
    # A股：sum(财务摘要，含 ROIC/主营构成) + lrb(利润表) + zcfz(资产负债表) + xjll(现金流量表)
    # 港/美股：income/balance/cashflow
    # 目的：一次拉齐 base_pack 需要的字段（EBIT/合同负债/存货/货币资金/有息负债/研发/财务费用等），
    #       避免"数据源只有 7 个字段、base_pack 全 NC"的断口。
    async def fundamentals(self, code: str) -> Fundamentals:
        if code.startswith("hk"):
            types = [("zhsy", "income"), ("zcfz", "balance"), ("xjll", "cashflow")]
            scale = 1.0  # 港股金额为原始港币（无 亿/万 后缀）
        elif code.startswith("us"):
            types = [("income", "income"), ("balance", "balance"), ("cashflow", "cashflow")]
            scale = 1e6  # 美股财报单位为百万（millions）
        else:
            types = [("sum", "summary"), ("lrb", "income"), ("zcfz", "balance"), ("xjll", "cashflow")]
            scale = 1.0  # A股金额带 亿/万 后缀，_num 已处理

        def _pick_scaled(idx, r, cands):
            v = self._pick(idx, r, cands)
            return v * scale if v is not None else None

        # 顺序调用：westock 后端对并发请求限流（并发会返回"数据为空"）
        texts = []
        for t, _ in types:
            texts.append(await self._try(
                self._run("finance", code, "--type", t, "--num", str(self._years))
            ))

        merged: dict[int, dict] = {}
        for (_, kind), text in zip(types, texts):
            if not text:
                continue
            h, rows = self._parse_md(text)
            if not rows:
                continue
            idx = self._col_index(h)
            # 报告期列：同一财年可能有多期（Q1/Q2/Q3/Q4），必须优先取 12-31 年报，
            # 否则 2025-03-31 的 Q1 累计数会把 2025 年报覆盖掉 → OCF/净利严重失真。
            date_col = None
            for cand in ("_date", "date", "EndDate", "报告期"):
                if cand in idx:
                    date_col = idx[cand]
                    break
            by_year_best: dict[int, dict] = {}
            for r in rows:
                raw_date = str(r.get(date_col) or "") if date_col else ""
                m = re.search(r"\d{4}-\d{2}-\d{2}", raw_date)
                if not m:
                    continue
                ymd = m.group()
                y = int(ymd[:4])
                # 12-31（年报）优先；同一年内保留更晚的报告期（Q4 > Q3 > ...）
                cur = by_year_best.get(y)
                if cur is None or ymd.endswith("12-31") or ymd > cur.get("_ymd", ""):
                    by_year_best[y] = {"_ymd": ymd, "_row": r}
            for y, rec in by_year_best.items():
                d = merged.setdefault(y, {})
                r = rec["_row"]
                if kind == "summary":  # 财务摘要：直接披露的比率 + 主数据
                    d.setdefault("roe", _pick_scaled(idx, r, ["roe", "净资产收益率"]))
                    d.setdefault("gross_margin", _pick_scaled(idx, r, ["grossincomeratio", "毛利率", "grossmargin"]))
                    d.setdefault("roic", _pick_scaled(idx, r, ["roic"]))
                    d.setdefault("revenue", _pick_scaled(idx, r, ["operatingrevenue", "totalrevenue", "营业收入", "营业总收入", "revenue"]))
                    d.setdefault("net_profit", _pick_scaled(idx, r, ["npparentcompanyowners", "netprofit", "netincome", "净利润", "归母"]))
                    d.setdefault("assets", _pick_scaled(idx, r, ["totalassets", "资产总计", "assets"]))
                    d.setdefault("total_liabilities", _pick_scaled(idx, r, ["totalliabilities", "总负债", "liabilities"]))
                    d.setdefault("equity", _pick_scaled(idx, r, ["totalshareholderequity", "commonstockequity", "shareholderequity", "股东权益", "净资产", "equity"]))
                    d.setdefault("ocf", _pick_scaled(idx, r, ["netoperatecashflow", "netoperatingcashflow", "operatingcashflow", "经营活动现金", "ocf"]))
                    d.setdefault("fcff", _pick_scaled(idx, r, ["fcff"]))
                    d.setdefault("f_eps", _pick_scaled(idx, r, ["basiceps", "eps", "每股收益"]))
                elif kind == "income":  # 利润表
                    d.setdefault("revenue", _pick_scaled(idx, r, ["operatingrevenue", "totalrevenue", "营业收入", "营业总收入", "revenue"]))
                    d.setdefault("net_profit", _pick_scaled(idx, r, ["npparentcompanyowners", "netprofit", "netincome", "净利润", "归母"]))
                    d.setdefault("gross_profit", _pick_scaled(idx, r, ["grossprofit", "毛利", "grossprofitttm"]))
                    d.setdefault("cogs", _pick_scaled(idx, r, ["operatingcost", "营业成本", "cogs"]))
                    d.setdefault("selling_expense", _pick_scaled(idx, r, ["operatingexpense", "销售费用", "sellingexpense"]))
                    d.setdefault("rd_expense", _pick_scaled(idx, r, ["randd", "研发费用", "rd"]))
                    d.setdefault("interest_expense", _pick_scaled(idx, r, ["financialexpense", "财务费用", "interestexpense"]))
                    d.setdefault("admin_expense", _pick_scaled(idx, r, ["totaladminexpense", "管理费用", "adminexpense"]))
                    d.setdefault("operating_profit", _pick_scaled(idx, r, ["operatingprofit", "营业利润", "operatingprofit"]))
                    d.setdefault("total_profit", _pick_scaled(idx, r, ["totalprofit", "利润总额", "totalprofit"]))
                elif kind == "balance":  # 资产负债表
                    d.setdefault("equity", _pick_scaled(idx, r, ["totalshareholderequity", "commonstockequity", "shareholderequity", "股东权益", "净资产", "equity"]))
                    d.setdefault("assets", _pick_scaled(idx, r, ["totalassets", "资产总计", "assets"]))
                    d.setdefault("total_liabilities", _pick_scaled(idx, r, ["totalliabilities", "总负债", "liabilities"]))
                    d.setdefault("cash", _pick_scaled(idx, r, ["cashequivalents", "货币资金", "cash"]))
                    d.setdefault("contract_liability", _pick_scaled(idx, r, ["contractliability", "合同负债", "contractliability"]))
                    d.setdefault("inventory", _pick_scaled(idx, r, ["inventories", "存货", "inventory"]))
                    d.setdefault("total_debt", _pick_scaled(idx, r, ["interestbeardebt", "有息负债", "interestbeardebt"]))
                    d.setdefault("fixed_assets", _pick_scaled(idx, r, ["totalfixedasset", "固定资产", "fixedassets"]))
                    d.setdefault("current_liabilities", _pick_scaled(idx, r, ["totalcurrentliability", "流动负债", "currentliabilities"]))
                    d.setdefault("accounts_receivable", _pick_scaled(idx, r, ["billaccreceivable", "应收账款", "accountsreceivable"]))
                    d.setdefault("accounts_payable", _pick_scaled(idx, r, ["notaccountspayable", "应付账款", "accountspayable"]))
                    d.setdefault("ebit", _pick_scaled(idx, r, ["ebit"]))
                elif kind == "cashflow":  # 现金流量表
                    d.setdefault("ocf", _pick_scaled(idx, r, ["netoperatecashflow", "netoperatingcashflow", "operatingcashflow", "经营活动现金", "ocf"]))
                    d.setdefault("fcff", _pick_scaled(idx, r, ["fcff"]))
                    d.setdefault("capex", _pick_scaled(idx, r, ["netinvestcashflow", "投资活动现金", "capex"]))

        years_list = []
        for y in sorted(merged.keys(), reverse=True)[: self._years]:
            d = merged[y]
            rev = d.get("revenue")
            gp = d.get("gross_profit")
            np_ = d.get("net_profit")
            eq = d.get("equity")
            ast = d.get("assets")
            liab = d.get("total_liabilities")
            ocf = d.get("ocf")
            gross_margin = d.get("gross_margin")
            if gross_margin is None and gp is not None and rev:
                gross_margin = round(gp / rev * 100, 2)
            roe = d.get("roe")
            if roe is None and np_ is not None and eq:
                roe = round(np_ / eq * 100, 2)
            debt_ratio = round(liab / ast * 100, 2) if liab is not None and ast else None
            row: dict[str, Any] = {
                "year": str(y),
                "revenue": rev, "net_profit": np_, "gross_profit": gp,
                "gross_margin": gross_margin, "roe": roe,
                "debt_ratio": debt_ratio, "ocf": ocf,
                "assets": ast, "equity": eq,
            }
            # 透传 base_pack 需要的明细字段（缺失自然不在 dict 里，上游按缺失处理）
            for f in ("ebit", "cash", "contract_liability", "inventory", "total_debt",
                      "fixed_assets", "current_liabilities", "accounts_receivable",
                      "accounts_payable", "cogs", "selling_expense", "rd_expense",
                      "interest_expense", "capex", "fcff", "roic", "operating_profit",
                      "total_profit"):
                if d.get(f) is not None:
                    row[f] = d[f]
            years_list.append(row)

        if not years_list:
            return None  # 触发兜底

        return Fundamentals(
            symbol=code,
            source=self.name,
            asof=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            years=years_list,
        )
