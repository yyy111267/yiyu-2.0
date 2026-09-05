"""第一层：字段语义与别名，不声明 Provider 能力。

FIELD_SPECS 是当前已登记的标准字段集合，并非问财全量指标目录。
字段已登记不等于当前有可用来源；来源能力由 source_mapping 单独声明。
未知名称保持原样，查询定义时抛 KeyError，不能据此自动扩展结构化能力。
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class FieldSpec:
    name: str
    component: str
    label: str
    aliases: tuple[str, ...] = ()
    type: str = "float"
    unit: str = ""
    category: str = ""
    definition: str = ""
    time_semantics: str = "period_end"
    nullable: bool = True


FIELD_SPECS: dict[str, FieldSpec] = {
    "symbol": FieldSpec("symbol", "snapshot", "证券代码", type="string"),
    "name": FieldSpec("name", "snapshot", "证券名称", type="string"),
    "market_code": FieldSpec("market_code", "snapshot", "市场代码", type="string", category="identity", definition="数据源使用的市场标识"),
    "code": FieldSpec("code", "snapshot", "数据源代码", type="string", category="identity", definition="数据源返回的原始证券代码"),
    "stock_code": FieldSpec("stock_code", "snapshot", "股票代码", type="string", category="identity", definition="面向展示的证券代码"),
    "stock_name": FieldSpec("stock_name", "snapshot", "股票简称", type="string", category="identity", definition="证券简称"),
    "exchange": FieldSpec("exchange", "snapshot", "交易所", type="string"),
    "price": FieldSpec("price", "snapshot", "最新价", ("last_price", "current_price"), definition="最近交易时点的证券成交价", time_semantics="point_in_time"),
    "close_price": FieldSpec("close_price", "snapshot", "收盘价", type="float", category="market", definition="指定交易日的正式收盘价", time_semantics="point_in_time"),
    "change_pct": FieldSpec("change_pct", "snapshot", "涨跌幅"),
    "turnover_rate": FieldSpec("turnover_rate", "snapshot", "换手率"),
    "volume": FieldSpec("volume", "snapshot", "成交量"),
    "amount": FieldSpec("amount", "snapshot", "成交额"),
    "market_cap": FieldSpec("market_cap", "snapshot", "总市值", type="float", unit="CNY", category="valuation", definition="当前股价 × 当前总股本", time_semantics="point_in_time"),
    "float_market_cap": FieldSpec("float_market_cap", "snapshot", "流通市值"),
    "pe_dynamic": FieldSpec("pe_dynamic", "snapshot", "动态市盈率"),
    "pe_static": FieldSpec("pe_static", "snapshot", "静态市盈率"),
    "pe_ttm": FieldSpec("pe_ttm", "snapshot", "PE(TTM)", type="float", unit="x", category="valuation", definition="当前总市值 / 最近十二个月归母净利润", time_semantics="point_in_time"),
    "ps_ttm": FieldSpec("ps_ttm", "snapshot", "PS(TTM)"),
    "pb": FieldSpec("pb", "snapshot", "PB"),
    "industry": FieldSpec("industry", "snapshot", "所属行业"),
    "listing_date": FieldSpec("listing_date", "snapshot", "上市日期"),
    "listing_place": FieldSpec("listing_place", "snapshot", "上市地点", type="string", category="identity", definition="证券上市交易所或上市市场"),
    "industry_ths": FieldSpec("industry_ths", "snapshot", "所属同花顺行业", type="string", category="classification", definition="同花顺行业分类"),
    "board": FieldSpec("board", "snapshot", "上市板块", type="string", category="classification", definition="证券所属上市板块"),
    "revenue": FieldSpec("revenue", "fundamentals", "营业收入"),
    "revenue_yoy": FieldSpec("revenue_yoy", "fundamentals", "营业收入同比", type="float", unit="%", category="growth", definition="本期营业收入相对上年同期的增长率"),
    "net_profit": FieldSpec("net_profit", "fundamentals", "净利润"),
    "net_profit_parent": FieldSpec("net_profit_parent", "fundamentals", "归母净利润", type="float", unit="CNY", category="profitability", definition="归属于母公司股东的净利润"),
    "net_profit_parent_yoy": FieldSpec("net_profit_parent_yoy", "fundamentals", "归母净利润同比"),
    "gross_margin": FieldSpec("gross_margin", "fundamentals", "毛利率", ("gross_profit_margin",)),
    "operating_cash_flow": FieldSpec("operating_cash_flow", "fundamentals", "经营活动现金流", ("ocf",)),
    "capital_expenditure": FieldSpec("capital_expenditure", "fundamentals", "资本开支", ("capex",)),
    "equity": FieldSpec("equity", "fundamentals", "所有者权益", ("total_equity", "shareholder_equity")),
    "total_assets": FieldSpec("total_assets", "fundamentals", "资产总计", ("assets",)),
    "total_liabilities": FieldSpec("total_liabilities", "fundamentals", "负债总计"),
    "total_debt": FieldSpec("total_debt", "fundamentals", "有息负债"),
    "cash": FieldSpec("cash", "fundamentals", "现金及等价物"),
    "trading_financial_assets": FieldSpec("trading_financial_assets", "fundamentals", "交易性金融资产"),
    "total_shares": FieldSpec("total_shares", "fundamentals", "总股本"),
    "contract_liability": FieldSpec("contract_liability", "fundamentals", "合同负债"),
    "inventory": FieldSpec("inventory", "fundamentals", "存货"),
    "accounts_receivable": FieldSpec("accounts_receivable", "fundamentals", "应收账款"),
    "accounts_payable": FieldSpec("accounts_payable", "fundamentals", "应付账款"),
    "fixed_assets": FieldSpec("fixed_assets", "fundamentals", "固定资产"),
    "current_liabilities": FieldSpec("current_liabilities", "fundamentals", "流动负债"),
    "gross_profit": FieldSpec("gross_profit", "fundamentals", "毛利润"),
    "cogs": FieldSpec("cogs", "fundamentals", "营业成本"),
    "selling_expense": FieldSpec("selling_expense", "fundamentals", "销售费用"),
    "admin_expense": FieldSpec("admin_expense", "fundamentals", "管理费用"),
    "rd_expense": FieldSpec("rd_expense", "fundamentals", "研发费用"),
    "interest_expense": FieldSpec("interest_expense", "fundamentals", "财务费用", ("financial_expense",)),
    "operating_profit": FieldSpec("operating_profit", "fundamentals", "营业利润"),
    "total_profit": FieldSpec("total_profit", "fundamentals", "利润总额"),
    "ebit": FieldSpec("ebit", "fundamentals", "EBIT"),
    "ebitda": FieldSpec("ebitda", "fundamentals", "EBITDA"),
    "depreciation_amortization": FieldSpec("depreciation_amortization", "fundamentals", "折旧摊销"),
    "minority_interest": FieldSpec("minority_interest", "fundamentals", "少数股东权益"),
    "income_tax": FieldSpec("income_tax", "fundamentals", "所得税"),
    "eps": FieldSpec("eps", "fundamentals", "每股收益"),
    "fcff": FieldSpec("fcff", "fundamentals", "FCFF"),
    "roic": FieldSpec("roic", "fundamentals", "ROIC"),
    "roe": FieldSpec("roe", "fundamentals", "ROE"),
    "debt_ratio": FieldSpec("debt_ratio", "fundamentals", "资产负债率"),
}

FIELD_ALIASES = {
    alias: spec.name
    for spec in FIELD_SPECS.values()
    for alias in (spec.name, spec.label, *spec.aliases)
}
FIELD_COMPONENTS = {name: spec.component for name, spec in FIELD_SPECS.items()}
FIELD_ALIASES["pe"] = "pe_dynamic"  # Legacy input; canonical consumers must use pe_dynamic.
FIELD_ALIASES.update({
    "market_code": "market_code",
    "code": "code",
    "股票代码": "stock_code",
    "股票简称": "stock_name",
    "最新涨跌幅": "change_pct",
    "收盘价": "close_price",
    "成交额": "amount",
    "换手率": "turnover_rate",
    "涨跌幅": "change_pct",
    "成交量": "volume",
    "市销率(ps,ttm)": "ps_ttm",
    "市盈率(pe,ttm)": "pe_ttm",
    "营业收入同比增长率": "revenue_yoy",
    "销售毛利率": "gross_margin",
    "归母净利润": "net_profit_parent",
    "归母净利润同比增长率": "net_profit_parent_yoy",
    "上市地点": "listing_place",
    "所属同花顺行业": "industry_ths",
    "上市板块": "board",
    "a股流通市值": "float_market_cap",
    "流通市值": "float_market_cap",
    "总市值": "market_cap",
    "动态市盈率": "pe_dynamic",
    "市净率": "pb",
})


def canonical_field(name: str) -> str:
    value = str(name or "").strip()
    direct = FIELD_ALIASES.get(value)
    if direct is not None:
        return direct
    match = re.fullmatch(r"(.+?)\[(\d{8}(?:-\d{8})?)\]", value)
    if match:
        label, period = match.groups()
        base = FIELD_ALIASES.get(label)
        if base is not None:
            return f"{base}[{period}]"
    return value


def canonical_fields(names: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name in names or []:
        value = canonical_field(name)
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def field_parts(name: str) -> tuple[str, str | None]:
    """统一拆分标准字段和时间锚点；时间锚点不意味着来源支持历史取数。"""
    canonical = canonical_field(name)
    match = re.fullmatch(r"(.+?)\[(\d{8}(?:-\d{8})?)\]", canonical)
    return (match.group(1), match.group(2)) if match else (canonical, None)


def field_definition(name: str) -> dict:
    """Return the public definition for a canonical field or legacy alias."""
    canonical = canonical_field(name)
    base, period = field_parts(canonical)
    spec = FIELD_SPECS.get(base)
    if spec is None:
        raise KeyError(f"unknown field: {name}")
    definition = spec.definition or spec.label
    if period:
        definition = f"{definition}；时间锚点为 {period}"
    return {
        "name": canonical,
        "label": spec.label,
        "type": spec.type,
        "unit": spec.unit,
        "category": spec.category or spec.component,
        "definition": definition,
        "time_semantics": spec.time_semantics,
        "nullable": spec.nullable,
        "component": spec.component,
        "aliases": list(spec.aliases),
    }
