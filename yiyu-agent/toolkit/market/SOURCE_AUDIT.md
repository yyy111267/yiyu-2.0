# 字段主备源实测与修订（2026-09-05）

结论：原来的主备源表不能照单使用。既有实际连接失败，也有参数、列名和财务口径错误。本轮已按原始响应修订来源配方，未改 Fetch Planner / Provider 的执行代码，未恢复计算兜底。

## 先看归母净利润这个例子

东财财务指标（`PARENTNETPROFIT`） → AKShare 转接新浪财务摘要（指标行“归母净利润”） → WeStock（`NPParentCompanyOwners`）。

前两条各 9/9 次返回字段值，并在三只样本的 2025 年报、2026 中报相互一致。WeStock 摘要 8/9 次有数据，只到 2025-12-31；不能拿来补 2026 中报。AKShare 是访问库，新浪/东财才是实际上游，不能把两次调用同一东财接口当作两个独立来源。

## 实测范围与计数

样本：贵州茅台 600519.SH、平安银行 000001.SZ、宁德时代 300750.SZ；18 类调用各三轮，共 162 次调用试跑，另核对 3 条本地证券记录。每次调用可能包含多个 HTTP 请求；不经过业务缓存。本轮仅检验 A 股，不能推及港美股或长期可用率。

“接口返回成功”只表示得到响应；下方逐字段表另列非空值次数。空值不补零，银行没有某个工业企业科目也不能判定网络失败。

| 调用路径 | 成功响应 / 试跑 | 中位耗时（秒，含进程启动） | 结论 |
|---|---:|---:|---|
| 原东财 push2 行情封装 | 0/9 | 0.332 | 连接被断开，移出主源；不代表东财其他接口失效 |
| 新浪行情 | 9/9 | 0.267 | 本轮可重复返回，逐字段仍需检查空值/日期 |
| 原 AKShare 财报封装 | 9/9 | 1.945 | 有返回但解析错口径，不作为正确性依据 |
| 原 WeStock 行情+财报封装 | 8/9 | 2.885 | 一次失败；非空包不等于字段齐全 |
| 东财财务指标 | 9/9 | 0.766 | 本轮可重复返回，逐字段仍需检查空值/日期 |
| 新浪利润表（AKShare） | 9/9 | 0.936 | 本轮可重复返回，逐字段仍需检查空值/日期 |
| 新浪资产负债表（AKShare） | 9/9 | 0.938 | 本轮可重复返回，逐字段仍需检查空值/日期 |
| 新浪现金流表（AKShare） | 9/9 | 0.894 | 本轮可重复返回，逐字段仍需检查空值/日期 |
| 新浪财务摘要（AKShare） | 9/9 | 0.967 | 本轮可重复返回，逐字段仍需检查空值/日期 |
| 东财日终估值 | 9/9 | 0.389 | 日终数据，本轮验证最近两天 |
| 东财延迟行情 | 9/9 | 0.346 | 延迟行情，不能冒充实时 |
| 腾讯行情 | 9/9 | 0.463 | 本轮可重复返回，逐字段仍需检查空值/日期 |
| WeStock（腾讯） | 9/9 | 2.323 | CLI 9次退出成功，但摘要/利润表只有8次非空，资产负债/现金流表只有7次非空 |
| 东财利润表（AKShare） | 9/9 | 5.211 | 本轮可重复返回，逐字段仍需检查空值/日期 |
| 东财资产负债表（AKShare） | 9/9 | 5.630 | 本轮可重复返回，逐字段仍需检查空值/日期 |
| 东财现金流表（AKShare） | 9/9 | 5.289 | 本轮可重复返回，逐字段仍需检查空值/日期 |
| AKShare 东财个股资料 | 0/9 | 0.775 | 原行情地址连接失败；本轮不采用 |
| AKShare 雪球行情 | 0/9 | 0.911 | 需要有效登录态；未引入账号或 token |

## 修正了哪些会取错数的地方

- 东财财务指标要传 `600519.SH`，原封装传 `600519`；原封装还用中文去匹配英文指标列。
- 营业收入不能取营业总收入。茅台 2025 年对应约 1688.38 亿元和 1720.54 亿元，原封装混用了两者。同比也必须与选用的收入口径一致。
- 净利润不能用归母净利润代替。茅台 2025 年对应约 853.10 亿元和 823.20 亿元。
- 现金及等价物改取现金流表期末余额；不能用资产负债表的货币资金替代。
- WeStock 的 `TotalLiability`、`TotalAdminExpense` 有值，旧表声称无来源不成立。
- WeStock 的 `BillAccReceivable` / `NotAccountsPayable` 含票据口径，移出应收账款/应付账款的备源。宁德时代样本显著不同。
- 加权 ROE 对应 `ROEWeighted`，不是 `ROE`。宁德时代 2025 年分别为 24.91% 与 21.4179%。
- 总股数使用腾讯下标 73，并与东财日终总股数核对；下标 72 在宁德时代样本对应不同股份范围。
- ROIC、FCFF 的供应商口径未对齐，保留单源及限制，不把不同算法的值强行互备。折旧摊销合计、EBITDA 不在本轮自动计算。

这些对照证明的是本轮接口之间的一致性及已发现的错配，不等于逐份审计公司原始公告。

## 每个字段现在先取谁、失败再取谁

每项括号内为本轮“至少一个已返回报告期有非空字段值”的次数。6/9 可能是两只非金融样本有值、银行为空；不能把它当接口成功率。不同来源报告期不匹配时必须跳过，不可按年份拼接。

| 字段 | 主源 → 备源（原始列名；字段非空次数） |
|---|---|
| 证券名称 `name` | 新浪行情 `0`（9/9） → 腾讯行情 `1`（9/9） → 本地清单 `name`（3/3） |
| 股票简称 `stock_name` | 新浪行情 `0`（9/9） → 腾讯行情 `1`（9/9） → 本地清单 `name`（3/3） |
| 股票代码 `stock_code` | 本地清单 `code`（3/3） |
| 数据源代码 `code` | 本地清单 `code`（3/3） |
| 最新价 `price` | 新浪行情 `3`（9/9） → 腾讯行情 `3`（9/9） |
| 涨跌幅 `change_pct` | 腾讯行情 `32`（9/9） → 东财延迟行情 `f170`（9/9） |
| 换手率 `turnover_rate` | 腾讯行情 `38`（9/9） → 东财延迟行情 `f168`（9/9） |
| 成交量 `volume` | 腾讯行情 `6`（9/9） → 东财延迟行情 `f47`（9/9） |
| 成交额 `amount` | 腾讯行情 `57`（9/9） → 东财延迟行情 `f48`（9/9） |
| 总市值 `market_cap` | 腾讯行情 `45`（9/9） → 东财延迟行情 `f116`（9/9） |
| 流通市值 `float_market_cap` | 腾讯行情 `44`（9/9） → 东财延迟行情 `f117`（9/9） |
| 动态市盈率 `pe_dynamic` | 腾讯行情 `52`（9/9） → 东财延迟行情 `f162`（9/9） |
| 静态市盈率 `pe_static` | 腾讯行情 `53`（9/9） → 东财延迟行情 `f163`（9/9） |
| PE(TTM) `pe_ttm` | 腾讯行情 `39`（9/9） → 东财延迟行情 `f164`（9/9） |
| PB `pb` | 腾讯行情 `46`（9/9） → 东财延迟行情 `f167`（9/9） |
| PS(TTM) `ps_ttm` | 东财日终估值 `PS_TTM`（9/9） |
| 收盘价 `close_price` | 东财日终估值 `CLOSE_PRICE`（9/9） |
| 所属行业 `industry` | 东财延迟行情 `f127`（9/9） |
| 上市日期 `listing_date` | 东财延迟行情 `f189`（9/9） |
| 总股本 `total_shares` | 腾讯行情 `73`（9/9） → 东财日终估值 `TOTAL_SHARES`（9/9） |
| 归母净利润 `net_profit_parent` | 东财财务指标 `PARENTNETPROFIT`（9/9） → 新浪财务摘要（AKShare） `归母净利润`（9/9） → WeStock（腾讯） `NPParentCompanyOwners`（8/9） |
| 营业收入 `revenue` | 新浪利润表（AKShare） `营业收入`（9/9） → WeStock（腾讯） `OperatingRevenue`（8/9） → 东财利润表（AKShare） `OPERATE_INCOME`（9/9） |
| 营业收入同比 `revenue_yoy` | WeStock（腾讯） `OperatingRevenueGrowRate`（8/9） → 东财利润表（AKShare） `OPERATE_INCOME_YOY`（9/9） |
| 净利润 `net_profit` | 新浪利润表（AKShare） `净利润`（9/9） → 东财利润表（AKShare） `NETPROFIT`（9/9） |
| 归母净利润同比 `net_profit_parent_yoy` | 东财财务指标 `PARENTNETPROFITTZ`（9/9） → WeStock（腾讯） `NPParentCompanyYOY`（8/9） |
| 毛利率 `gross_margin` | 东财财务指标 `XSMLL`（6/9） → WeStock（腾讯） `GrossIncomeRatio`（5/9） |
| ROE `roe` | 东财财务指标 `ROEJQ`（9/9） → 新浪财务摘要（AKShare） `净资产收益率(ROE)`（9/9） → WeStock（腾讯） `ROEWeighted`（8/9） |
| 资产负债率 `debt_ratio` | 东财财务指标 `ZCFZL`（9/9） → 新浪财务摘要（AKShare） `资产负债率`（9/9） → WeStock（腾讯） `DebtAssetsRatio`（8/9） |
| 经营活动现金流 `operating_cash_flow` | 新浪现金流表（AKShare） `经营活动产生的现金流量净额`（9/9） → WeStock（腾讯） `NetOperateCashFlow`（7/9） → 东财现金流表（AKShare） `NETCASH_OPERATE`（9/9） |
| 资本开支 `capital_expenditure` | 新浪现金流表（AKShare） `购建固定资产、无形资产和其他长期资产支付的现金`（3/9） → 东财现金流表（AKShare） `CONSTRUCT_LONG_ASSET`（9/9） |
| 现金及等价物 `cash` | 新浪现金流表（AKShare） `期末现金及现金等价物余额`（9/9） → 东财现金流表（AKShare） `END_CCE`（9/9） |
| 所有者权益 `equity` | 新浪资产负债表（AKShare） `所有者权益(或股东权益)合计`（6/9） → 东财资产负债表（AKShare） `TOTAL_EQUITY`（9/9） → WeStock（腾讯） `TotalShareholderEquity`（7/9） |
| 资产总计 `total_assets` | 新浪资产负债表（AKShare） `资产总计`（9/9） → WeStock（腾讯） `TotalAssets`（7/9） → 东财资产负债表（AKShare） `TOTAL_ASSETS`（9/9） |
| 负债总计 `total_liabilities` | 新浪资产负债表（AKShare） `负债合计`（9/9） → WeStock（腾讯） `TotalLiability`（7/9） → 东财资产负债表（AKShare） `TOTAL_LIABILITIES`（9/9） |
| 有息负债 `total_debt` | WeStock（腾讯） `InterestBearDebt`（7/9） |
| 交易性金融资产 `trading_financial_assets` | 新浪资产负债表（AKShare） `交易性金融资产`（9/9） → 东财资产负债表（AKShare） `TRADE_FINASSET_NOTFVTPL`（9/9） |
| 合同负债 `contract_liability` | 新浪资产负债表（AKShare） `合同负债`（6/9） → WeStock（腾讯） `ContractLiability`（5/9） → 东财资产负债表（AKShare） `CONTRACT_LIAB`（6/9） |
| 存货 `inventory` | 新浪资产负债表（AKShare） `存货`（6/9） → WeStock（腾讯） `Inventories`（5/9） → 东财资产负债表（AKShare） `INVENTORY`（6/9） |
| 应收账款 `accounts_receivable` | 新浪资产负债表（AKShare） `应收账款`（6/9） → 东财资产负债表（AKShare） `ACCOUNTS_RECE`（6/9） |
| 应付账款 `accounts_payable` | 新浪资产负债表（AKShare） `应付账款`（6/9） → 东财资产负债表（AKShare） `ACCOUNTS_PAYABLE`（6/9） |
| 固定资产 `fixed_assets` | 新浪资产负债表（AKShare） `固定资产净额`（9/9） → WeStock（腾讯） `TotalFixedAsset`（7/9） → 东财资产负债表（AKShare） `FIXED_ASSET`（9/9） |
| 流动负债 `current_liabilities` | 新浪资产负债表（AKShare） `流动负债合计`（6/9） → WeStock（腾讯） `TotalCurrentLiability`（5/9） → 东财资产负债表（AKShare） `TOTAL_CURRENT_LIAB`（6/9） |
| 少数股东权益 `minority_interest` | 新浪资产负债表（AKShare） `少数股东权益`（6/9） → 东财资产负债表（AKShare） `MINORITY_EQUITY`（6/9） |
| 毛利润 `gross_profit` | 东财财务指标 `MLR`（6/9） |
| 营业成本 `cogs` | 新浪利润表（AKShare） `营业成本`（6/9） → WeStock（腾讯） `OperatingCost`（5/9） → 东财利润表（AKShare） `OPERATE_COST`（6/9） |
| 销售费用 `selling_expense` | 新浪利润表（AKShare） `销售费用`（6/9） → WeStock（腾讯） `OperatingExpense`（5/9） → 东财利润表（AKShare） `SALE_EXPENSE`（6/9） |
| 管理费用 `admin_expense` | 新浪利润表（AKShare） `管理费用`（6/9） → WeStock（腾讯） `TotalAdminExpense`（8/9） → 东财利润表（AKShare） `MANAGE_EXPENSE`（6/9） |
| 研发费用 `rd_expense` | 新浪利润表（AKShare） `研发费用`（6/9） → WeStock（腾讯） `RAndD`（5/9） → 东财利润表（AKShare） `RESEARCH_EXPENSE`（6/9） |
| 财务费用 `interest_expense` | 新浪利润表（AKShare） `财务费用`（6/9） → WeStock（腾讯） `FinancialExpense`（5/9） → 东财利润表（AKShare） `FINANCE_EXPENSE`（6/9） |
| 营业利润 `operating_profit` | 新浪利润表（AKShare） `营业利润`（9/9） → WeStock（腾讯） `OperatingProfit`（8/9） → 东财利润表（AKShare） `OPERATE_PROFIT`（9/9） |
| 利润总额 `total_profit` | 新浪利润表（AKShare） `利润总额`（9/9） → WeStock（腾讯） `TotalProfit`（8/9） → 东财利润表（AKShare） `TOTAL_PROFIT`（9/9） |
| 所得税 `income_tax` | 新浪利润表（AKShare） `所得税费用`（6/9） → 东财利润表（AKShare） `INCOME_TAX`（9/9） |
| 每股收益 `eps` | 东财财务指标 `EPSJB`（9/9） → 新浪利润表（AKShare） `基本每股收益`（9/9） → WeStock（腾讯） `BasicEPS`（8/9） |
| EBIT `ebit` | WeStock（腾讯） `EBIT`（5/9） |
| ROIC `roic` | WeStock（腾讯） `ROIC`（5/9） |
| FCFF `fcff` | WeStock（腾讯） `FCFF`（7/9） |

## 暂无已验证直接配方的字段

- `symbol`：标准证券标识应由实体层输入，不是外部财务指标。
- `exchange`：交易所应由实体层标准证券标识提供，本轮未验证独立来源。
- `market_code`：供应商专用市场代码是请求参数，不是通用返回字段。
- `listing_place`：本轮没有验证同口径来源，不能以公司注册地址代替上市地点。
- `industry_ths`：本轮未验证同花顺分类；东财/腾讯行业不是同一分类，不能代填。
- `board`：本轮未验证上市板块映射；估值接口 BOARD_NAME 是行业，不是上市板块。
- `depreciation_amortization`：原始现金流有分项，无已验证的同口径合计字段；本轮不做计算兜底。
- `ebitda`：本轮未找到已验证的直接 EBITDA 字段；本轮不做计算兜底。

## 落地范围与复跑

这轮产出是来源静态配方及验源证据，尚未把新请求、精确列名、市场/报告期限制接进旧 Provider。当前应用的实际取数行为不能因此宣称已修复。按你的分层顺序，下一层应读取完整配方，而不是只按 provider 名字调用旧封装。

复跑入口：`tests/audit_market_sources.py`，不带参数测原四类封装；传来源名称测具体原始接口，例如：

```sh
.venv/bin/python tests/audit_market_sources.py em_indicator sina_abstract westock_raw
.venv/bin/python tests/audit_market_sources.py --summarize
.venv/bin/python -m pytest tests/test_market_source_mapping.py tests/test_market_field_registry.py -q
.venv/bin/python evaluation/stage/06_metrics/run.py
.venv/bin/python evaluation/stage/12_metric_skill/run.py
```

原始响应与时间戳：`evaluation/reports/source_audit_20260905/`。逐路由完整结果：该目录 `field_results.json`。离线回归用的精简样本：`tests/data/source_mapping_audit_20260905.json`。仅复跑一部分时其余证据仍保留原测量日期，不能视为全表已重测；静态 AUDIT_RESULTS 也须依据新结果更新。

测试结果：25 个相关测试通过；06_metrics 8/8、12_metric_skill 12/12。

接口定义参考：[AKShare 官方股票数据文档](https://akshare.akfamily.xyz/data/stock/stock.html)。具体参数另核对本机 AKShare 1.18.83 实现，并以本轮真实响应为准。
