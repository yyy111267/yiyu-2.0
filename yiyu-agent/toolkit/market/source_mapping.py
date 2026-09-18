"""第二层：经公开接口实测的字段来源配方，不执行取数。

2026-09-05：600519.SH / 000001.SZ / 300750.SZ，每条接口三轮、绕过业务缓存。
完整原始响应、耗时、同报告期对照见 evaluation/reports/source_audit_20260905/。
live 只表示本轮样本通过，不代表长期 SLA，也不代表所有公司都有该科目。
2026-09-09 另行验证本地 A/HK/US 证券主数据、美股 EBIT/EBITDA/折旧摊销和
A 股折旧摊销分项；其他原 A 股配方仍不承诺给 HK / US。

provider 是执行渠道；upstream 是实际上游。AKShare 是库，不是独立数据库。
request 给出具体调用配方，source_field 必须精确匹配，禁止模糊匹配相近科目。
这些原始接口配方由 Fetch Planner 生成步骤，再由 Route Provider 按 request、报告期、
单位和限制执行；不能只读 provider 名称后调用一套宽泛接口。

规则：有序尝试，成功即停；无注册字段 unregistered，无适用配方 not_supported；
全部请求失败 request_failed。查不到才交回 Agent 选择白名单搜索。
仅允许 Registry 明确冻结的口径计算（当前为 EBITDA = EBIT + 折旧摊销）。
空值不是 0；未披露、不适用、请求失败须由实际证据区分。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from toolkit.market.field_registry import FIELD_SPECS, canonical_field, field_parts


@dataclass(frozen=True)
class SourceSpec:
    provider: str
    source_field: str
    method: str
    priority: int
    component: str
    request: str
    upstream: str
    audit_key: str
    markets: tuple[str, ...] = ("A",)
    time_scope: str = "report_period"  # latest / trading_day / report_period
    unit: str = "CNY"
    scale: float = 1.0
    freshness: str = "reported"  # realtime / delayed / end_of_day / reported / static
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# 调用配方中的占位符：symbol=600519.SH，code=600519，sina_code=sh600519，em_code=SH600519。
# 日期必须按原始报告期匹配，禁止仅按 year 拼表；WeStock 本轮财报最新仅到 2025-12-31。
def _em(field: str, priority: int = 1, *, unit: str = "%") -> SourceSpec:
    return SourceSpec("eastmoney", field, "fundamentals", priority, "fundamentals",
        "akshare.stock_financial_analysis_indicator_em(symbol='{symbol}', indicator='按报告期')",
        "eastmoney", "em_indicator", unit=unit,
        notes="完整带交易所后缀的 symbol；日期列 REPORT_DATE；按原始英文列精确取值")


def _sina(table: str, field: str, priority: int = 1, *, notes: str = "", unit: str = "CNY") -> SourceSpec:
    audit = {"利润表": "sina_income", "资产负债表": "sina_balance", "现金流量表": "sina_cashflow"}[table]
    return SourceSpec("akshare", field, "fundamentals", priority, "fundamentals",
        f"akshare.stock_financial_report_sina(stock='{{sina_code}}', symbol='{table}')",
        "sina", audit, unit=unit, notes="报告日=YYYYMMDD；" + notes)


def _em_report(table: str, field: str, priority: int = 2, *, unit: str = "CNY") -> SourceSpec:
    return SourceSpec("akshare", field, "fundamentals", priority, "fundamentals",
        f"akshare.stock_{table}_sheet_by_report_em(symbol='{{em_code}}')",
        "eastmoney", {"profit": "em_income", "balance": "em_balance", "cash_flow": "em_cashflow"}[table],
        unit=unit, notes="日期列 REPORT_DATE；全历史封装较慢（本轮约 3–9 秒），仅作后备")


def _abstract(field: str, priority: int = 2, *, unit: str = "%") -> SourceSpec:
    return SourceSpec("akshare", field, "fundamentals", priority, "fundamentals",
        "akshare.stock_financial_abstract(symbol='{code}')", "sina", "sina_abstract", unit=unit,
        notes="按指标行精确匹配、按 YYYYMMDD 日期列取值；重复指标行须值一致")


def _ws(table: str, field: str, priority: int = 3, *, unit: str = "CNY", notes: str = "") -> SourceSpec:
    return SourceSpec("westock", field, "fundamentals", priority, "fundamentals",
        f"westock-data-clawhub finance {{sina_code}} --type {table} --num 8",
        "tencent", "westock_raw", unit=unit,
        notes="日期列 _date；本轮最新 2025-12-31，缺请求期须跳过；禁止并发；" + notes)


def _tx(index: int, priority: int = 1, *, unit: str = "", scale: float = 1.0) -> SourceSpec:
    return SourceSpec("tencent", str(index), "snapshot", priority, "snapshot",
        "GET https://qt.gtimg.cn/q={sina_code}", "tencent", "tencent_quote",
        time_scope="latest", unit=unit, scale=scale, freshness="realtime",
        notes="GBK 解码、引号内按 ~ 分隔的零基下标；时间戳下标 30；须校验代码下标 2")


def _delay(field: str, priority: int = 2, *, unit: str = "") -> SourceSpec:
    return SourceSpec("eastmoney", field, "snapshot", priority, "snapshot",
        "GET https://push2delay.eastmoney.com/api/qt/stock/get?secid={secid}&fields={source_field}&fltt=2",
        "eastmoney", "em_quote_alt", time_scope="latest", unit=unit, freshness="delayed",
        notes="延迟行情：必须标注延迟；要求实时的请求不得使用。原 push2 域名本轮 0/9")


def _valuation(field: str, priority: int = 1, *, unit: str = "") -> SourceSpec:
    return SourceSpec("eastmoney", field, "snapshot", priority, "snapshot",
        "GET https://datacenter-web.eastmoney.com/api/data/v1/get reportName=RPT_VALUEANALYSIS_DET columns=ALL filter=(SECURITY_CODE=\"{code}\") sortColumns=TRADE_DATE sortTypes=-1 pageSize=2 pageNumber=1",
        "eastmoney", "em_valuation", time_scope="trading_day", unit=unit, freshness="end_of_day",
        notes="日终数据，日期列 TRADE_DATE；本轮仅验证最近两个交易日，历史日期须另验分页/日期参数；不得冒充实时")


def _sina_quote(field: str, priority: int = 1, *, unit: str = "") -> SourceSpec:
    return SourceSpec("sina", field, "snapshot", priority, "snapshot",
        "GET https://hq.sinajs.cn/list={sina_code} Referer=https://finance.sina.com.cn",
        "sina", "sina", time_scope="latest", unit=unit, freshness="realtime",
        notes="GBK 解码；引号内逗号分隔的零基下标；日期/时间下标 30/31")


def _preset(field: str, priority: int = 3) -> SourceSpec:
    return SourceSpec("preset", field, "lookup", priority, "snapshot",
        "toolkit/entity/data/known_a_share.json 按 code 精确匹配", "local", "preset",
        time_scope="latest", unit="", freshness="static", notes="静态清单；不保证简称更名实时更新")


def _master(
    field: str, priority: int = 1, *, markets: tuple[str, ...] = ("A", "HK", "US")
) -> SourceSpec:
    return SourceSpec(
        "preset", field, "lookup", priority, "snapshot",
        "security_master: known_a_share.json + known_hk_us.json 按标准 symbol 精确匹配",
        "local", "security_master", markets=markets, time_scope="latest", unit="",
        freshness="static", notes="进程内读取本地主数据；代码可确定的市场属性在读取时生成",
    )


# 每行就是一个字段的尝试顺序。不固定某家永远第一：先取语义正确、快速且本轮可用的源。
SOURCE_MAPPING: dict[str, tuple[SourceSpec, ...]] = {
    "symbol": (_master("symbol"),),
    "name": (_master("name"), _sina_quote("0", 2), _tx(1, 3)),
    "stock_name": (_master("name"), _sina_quote("0", 2), _tx(1, 3)),
    "stock_code": (_master("code"),),
    "code": (_master("code"),),
    "exchange": (_master("exchange"),),
    "market_code": (_master("market_code"),),
    "listing_place": (_master("listing_place"),),
    "board": (_master("board"),),
    "price": (_sina_quote("3", unit="CNY"), _tx(3, 2, unit="CNY")),
    "change_pct": (_tx(32, unit="%"), _delay("f170", unit="%")),
    "turnover_rate": (_tx(38, unit="%"), _delay("f168", unit="%")),
    "volume": (_tx(6, unit="手"), _delay("f47", unit="手")),
    "amount": (_tx(57, unit="CNY", scale=10000), _delay("f48", unit="CNY")),
    "market_cap": (_tx(45, unit="CNY", scale=1e8), _delay("f116", unit="CNY")),
    "float_market_cap": (_tx(44, unit="CNY", scale=1e8), _delay("f117", unit="CNY")),
    "pe_dynamic": (_tx(52, unit="x"), _delay("f162", unit="x")),
    "pe_static": (_tx(53, unit="x"), _delay("f163", unit="x")),
    "pe_ttm": (_tx(39, unit="x"), _delay("f164", unit="x")),
    "pb": (_tx(46, unit="x"), _delay("f167", unit="x")),
    "ps_ttm": (_valuation("PS_TTM", unit="x"),),
    "close_price": (_valuation("CLOSE_PRICE", unit="CNY"),),
    "industry": (_delay("f127", 1),),  # 分类体系未统一，不用其他供应商行业直接替换
    "listing_date": (_delay("f189", 1),),
    "total_shares": (_tx(73, unit="股"), _valuation("TOTAL_SHARES", 2, unit="股")),
    "net_profit_parent": (_em("PARENTNETPROFIT", unit="CNY"),
        _abstract("归母净利润", unit="CNY"), _ws("sum", "NPParentCompanyOwners")),
    "revenue": (_sina("利润表", "营业收入"), _ws("lrb", "OperatingRevenue", 2),
        _em_report("profit", "OPERATE_INCOME", 3)),
    "revenue_yoy": (_ws("sum", "OperatingRevenueGrowRate", 1, unit="%"),
        _em_report("profit", "OPERATE_INCOME_YOY", 2, unit="%")),
    "net_profit": (_sina("利润表", "净利润"), _em_report("profit", "NETPROFIT")),
    "net_profit_parent_yoy": (_em("PARENTNETPROFITTZ"), _ws("sum", "NPParentCompanyYOY", 2, unit="%")),
    "gross_margin": (_em("XSMLL"), _ws("sum", "GrossIncomeRatio", 2, unit="%")),
    "roe": (_em("ROEJQ"), _abstract("净资产收益率(ROE)"), _ws("sum", "ROEWeighted", unit="%", notes="加权 ROE，不能替换成摊薄 ROE")),
    "debt_ratio": (_em("ZCFZL"), _abstract("资产负债率"), _ws("sum", "DebtAssetsRatio", unit="%")),
    "operating_cash_flow": (_sina("现金流量表", "经营活动产生的现金流量净额"),
        _ws("xjll", "NetOperateCashFlow", 2), _em_report("cash_flow", "NETCASH_OPERATE", 3)),
    "capital_expenditure": (_sina("现金流量表", "购建固定资产、无形资产和其他长期资产支付的现金"),
        _em_report("cash_flow", "CONSTRUCT_LONG_ASSET")),
    "cash": (_sina("现金流量表", "期末现金及现金等价物余额"), _em_report("cash_flow", "END_CCE")),
    "equity": (_sina("资产负债表", "所有者权益(或股东权益)合计"), _em_report("balance", "TOTAL_EQUITY"), _ws("zcfz", "TotalShareholderEquity")),
    "total_assets": (_sina("资产负债表", "资产总计"), _ws("zcfz", "TotalAssets", 2), _em_report("balance", "TOTAL_ASSETS", 3)),
    "total_liabilities": (_sina("资产负债表", "负债合计"), _ws("zcfz", "TotalLiability", 2), _em_report("balance", "TOTAL_LIABILITIES", 3)),
    "total_debt": (_ws("zcfz", "InterestBearDebt", 1, notes="供应商有息负债口径，银行与非金融企业不可直接比较；禁止部分负债相加冒充总额"),),
    "trading_financial_assets": (_sina("资产负债表", "交易性金融资产"), _em_report("balance", "TRADE_FINASSET_NOTFVTPL")),
    "contract_liability": (_sina("资产负债表", "合同负债"), _ws("zcfz", "ContractLiability", 2), _em_report("balance", "CONTRACT_LIAB", 3)),
    "inventory": (_sina("资产负债表", "存货"), _ws("zcfz", "Inventories", 2), _em_report("balance", "INVENTORY", 3)),
    "accounts_receivable": (_sina("资产负债表", "应收账款"), _em_report("balance", "ACCOUNTS_RECE")),
    "accounts_payable": (_sina("资产负债表", "应付账款"), _em_report("balance", "ACCOUNTS_PAYABLE")),
    "fixed_assets": (_sina("资产负债表", "固定资产净额"), _ws("zcfz", "TotalFixedAsset", 2), _em_report("balance", "FIXED_ASSET", 3)),
    "current_liabilities": (_sina("资产负债表", "流动负债合计"), _ws("zcfz", "TotalCurrentLiability", 2), _em_report("balance", "TOTAL_CURRENT_LIAB", 3)),
    "minority_interest": (_sina("资产负债表", "少数股东权益"), _em_report("balance", "MINORITY_EQUITY")),
    "gross_profit": (_em("MLR", unit="CNY"),),  # 不用 WeStock 的 GrossProfitTTM 代替本期毛利
    "cogs": (_sina("利润表", "营业成本"), _ws("lrb", "OperatingCost", 2), _em_report("profit", "OPERATE_COST", 3)),
    "selling_expense": (_sina("利润表", "销售费用"), _ws("lrb", "OperatingExpense", 2), _em_report("profit", "SALE_EXPENSE", 3)),
    "admin_expense": (_sina("利润表", "管理费用"), _ws("lrb", "TotalAdminExpense", 2), _em_report("profit", "MANAGE_EXPENSE", 3)),
    "rd_expense": (_sina("利润表", "研发费用"), _ws("lrb", "RAndD", 2), _em_report("profit", "RESEARCH_EXPENSE", 3)),
    "interest_expense": (_sina("利润表", "财务费用", notes="遵循 Registry 当前标签财务费用，不是利息费用"),
        _ws("lrb", "FinancialExpense", 2), _em_report("profit", "FINANCE_EXPENSE", 3)),
    "operating_profit": (_sina("利润表", "营业利润"), _ws("lrb", "OperatingProfit", 2), _em_report("profit", "OPERATE_PROFIT", 3)),
    "total_profit": (_sina("利润表", "利润总额"), _ws("lrb", "TotalProfit", 2), _em_report("profit", "TOTAL_PROFIT", 3)),
    "income_tax": (_sina("利润表", "所得税费用"), _em_report("profit", "INCOME_TAX")),
    "eps": (_em("EPSJB", unit="CNY/share"), _sina("利润表", "基本每股收益", 2, unit="CNY/share"), _ws("sum", "BasicEPS", unit="CNY/share")),
    "ebit": (_ws("zcfz", "EBIT", 1, notes="A 股供应商直接值；不自行用营业利润加财务费用近似"),
        SourceSpec("westock", "EBIT", "fundamentals", 2, "fundamentals",
            "westock-data-clawhub finance {sina_code} --type zcfz --num 8",
            "tencent", "westock_hk_balance", markets=("HK",), unit="HKD",
            notes="港股资产负债数据中的供应商直接 EBIT"),
        SourceSpec("westock", "EBIT", "fundamentals", 3, "fundamentals",
            "westock-data-clawhub finance {sina_code} --type income --num 8",
            "tencent", "westock_us_income", markets=("US",), unit="USD", scale=1e6,
            notes="美股利润表单位为百万美元，换算为美元；非年末报告期取 EBIT_Q")),
    "ebitda": (SourceSpec("westock", "EBITDA", "fundamentals", 1, "fundamentals",
        "westock-data-clawhub finance {sina_code} --type income --num 8",
        "tencent", "westock_us_income", markets=("US",), unit="USD", scale=1e6,
        notes="美股利润表直接 EBITDA，非年末报告期取 EBITDA_Q；A/H 股使用固定公式"),),
    "depreciation_amortization": (
        SourceSpec("akshare", "cashflow_D&A_components", "fundamentals", 1, "fundamentals",
            "akshare.stock_cash_flow_sheet_by_report_em(symbol='{em_code}'); derive_depreciation_amortization",
            "eastmoney", "em_cashflow_da", markets=("A",), unit="CNY",
            notes="固定资产/投资性房地产折旧与油气生物资产折耗二者择一，另加无形资产、长期待摊和使用权资产摊销"),
        SourceSpec("westock", "DepCF", "fundamentals", 2, "fundamentals",
            "westock-data-clawhub finance {sina_code} --type cashflow --num 8",
            "tencent", "westock_us_cashflow", markets=("US",), unit="USD", scale=1e6,
            notes="美股现金流表直接折旧摊销，非年末报告期取 DepCF_Q；百万美元换算为美元"),
    ),
    "roic": (_ws("sum", "ROIC", 1, unit="%", notes="供应商 ROIC，与东财 ROIC 同期值不同，口径未对齐前不互为备源"),),
    "fcff": (_ws("xjll", "FCFF", 1, notes="供应商 FCFF，与东财 FCFF_FORWARD/BACK 不同，口径未对齐前不互为备源"),),
}

# 未找到已验证且同口径的直接来源。不根据字段名称猜接口，也不在本层补公式。
UNMAPPED_FIELDS: dict[str, str] = {
    "industry_ths": "本轮未验证同花顺分类；东财/腾讯行业不是同一分类，不能代填",
}

AUDIT_RESULTS = {'eastmoney': {'attempts': 9, 'responses': 0, 'median_seconds': 0.332},
 'sina': {'attempts': 9, 'responses': 9, 'median_seconds': 0.267},
 'akshare': {'attempts': 9, 'responses': 9, 'median_seconds': 1.945},
 'westock': {'attempts': 9, 'responses': 8, 'median_seconds': 2.885},
 'em_indicator': {'attempts': 9, 'responses': 9, 'median_seconds': 0.766},
 'sina_income': {'attempts': 9, 'responses': 9, 'median_seconds': 0.936},
 'sina_balance': {'attempts': 9, 'responses': 9, 'median_seconds': 0.938},
 'sina_cashflow': {'attempts': 9, 'responses': 9, 'median_seconds': 0.894},
 'sina_abstract': {'attempts': 9, 'responses': 9, 'median_seconds': 0.967},
 'em_valuation': {'attempts': 9, 'responses': 9, 'median_seconds': 0.389},
 'em_quote_alt': {'attempts': 9, 'responses': 9, 'median_seconds': 0.346},
 'tencent_quote': {'attempts': 9, 'responses': 9, 'median_seconds': 0.463},
 'westock_raw': {'attempts': 9, 'responses': 9, 'median_seconds': 2.323},
 'em_income': {'attempts': 9, 'responses': 9, 'median_seconds': 5.211},
 'em_balance': {'attempts': 9, 'responses': 9, 'median_seconds': 5.63},
 'em_cashflow': {'attempts': 9, 'responses': 9, 'median_seconds': 5.289},
 'em_info': {'attempts': 9, 'responses': 0, 'median_seconds': 0.775},
 'xq': {'attempts': 9, 'responses': 0, 'median_seconds': 0.911},
 'preset': {'attempts': 3, 'responses': 3, 'median_seconds': 0.0}}
AUDIT_RESULTS['security_master'] = {'attempts': 3, 'responses': 3, 'median_seconds': 0.0}
AUDIT_RESULTS['em_cashflow_da'] = {'attempts': 1, 'responses': 1, 'median_seconds': 9.844}
AUDIT_RESULTS['westock_hk_balance'] = {'attempts': 1, 'responses': 1, 'median_seconds': 1.0}
AUDIT_RESULTS['westock_us_income'] = {'attempts': 1, 'responses': 1, 'median_seconds': 1.22}
AUDIT_RESULTS['westock_us_cashflow'] = {'attempts': 1, 'responses': 1, 'median_seconds': 0.9}

# 这是接口层状态；字段缺失次数/报告期覆盖在逐字段报告中单独列出。
# WeStock CLI 退出成功仍可能返回空表，本轮 sum/lrb 8/9、zcfz/xjll 7/9，标 unstable。
VERIFY_STATUS: dict[str, dict[str, str]] = {
    field: {spec.provider: ("live" if spec.audit_key != "westock_raw" and AUDIT_RESULTS[spec.audit_key]["responses"] == AUDIT_RESULTS[spec.audit_key]["attempts"] else "unstable")
            for spec in specs}
    for field, specs in SOURCE_MAPPING.items()
}


def mappings_for(field: str, *, market: str | None = None) -> tuple[SourceSpec, ...]:
    """返回可尝试的有序候选；market 为 A / HK / US，省略时展示所有市场路由。

    带日期请求仍需执行器核对实际返回日期；不声明区间聚合能力。
    """
    if market is not None and market not in {"A", "HK", "US"}:
        raise ValueError(f"unknown market: {market}; expected A, HK or US")
    base, period = field_parts(field)
    if base not in FIELD_SPECS:
        return ()
    return tuple(sorted((
        item for item in SOURCE_MAPPING.get(base, ())
        if (market is None or market in item.markets)
        and (period is None or ("-" not in period and item.time_scope in {"report_period", "trading_day"}))
        and verify_status(base, item.provider) != "dead"
    ), key=lambda item: item.priority))


def provider_candidates(field: str, *, market: str | None = None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.provider for item in mappings_for(field, market=market)))


def provider_fields(provider: str) -> frozenset[str]:
    return frozenset(field for field in SOURCE_MAPPING if provider in provider_candidates(field))


def mapping_dict(fields: list[str], *, market: str | None = None) -> dict[str, tuple[dict, ...]]:
    return {field: tuple(item.to_dict() for item in mappings_for(field, market=market)) for field in fields}


def verify_status(field: str, provider: str) -> str:
    """本轮接口层记录；字段实际可得性须再核对报告期/空值。"""
    base, _ = field_parts(field)
    return VERIFY_STATUS.get(base, {}).get(provider, "unverified")


def fallback_plan(field: str, *, market: str | None = None) -> dict:
    """返回静态路由及失败契约，不调用 Provider，也不判断本次请求是否成功。

    status 是查表结果；on_exhausted 是未来执行器应返回的状态。
    有候选不保证有值；候选全部失败时应附各源失败原因交回 Agent。
    """
    canonical = canonical_field(field)
    base, _ = field_parts(canonical)
    specs = mappings_for(canonical, market=market)
    status = "supported" if specs else "not_supported" if base in FIELD_SPECS else "unregistered"
    reason = ""
    if status == "unregistered":
        reason = "字段未在 FIELD_SPECS 登记，结构化数据不具备标准能力"
    elif status == "not_supported":
        reason = UNMAPPED_FIELDS.get(base, "没有符合市场、时间要求且未标记 dead 的已接入来源")
    return {
        "field": canonical,
        "status": status,
        "providers": list(dict.fromkeys(s.provider for s in specs)),
        "sources": [
            {**s.to_dict(), "verify": verify_status(base, s.provider)} for s in specs
        ],
        "unmapped_reason": reason,
        "on_exhausted": "request_failed" if specs else status,
        "on_failure": "return_to_agent",
    }
