# 链路串联 · 遗留问题清单

> 本轮目标：把"商业模式分类 → 指标计算 → 定性研究"三段串起来，跑通主链路。
> 已完成的见下方"已完成"；未解决的断点见"遗留问题"，下一轮修复。
>
> 状态：**全行业 base_pack 配置已补齐 + 真实数据端到端链路已打通（8 组 66 OK / 19 NC）**。
> 下一轮重点：P1 编排强制、P3 四大师子Agent、P11 利息口径、P12 估值假设通道。

---

## 已完成（本轮）

1. ✅ `registry.py` deep-research 白名单加 `company.classify` + `calc.base_pack`
2. ✅ 新建 `toolkit/calc/base_pack.py`：base_pack 执行器（classify→取数→冻结函数→档位→prompt block）
3. ✅ `__init__.py` 注册 `calc.base_pack` 工具
4. ✅ `SKILL.md` 重写流程：三段串联 + 编排约束 + 工具列表同步
5. ✅ `orientation.md` 删掉自判 G1-G6，改为引用 classify 的 group
6. ✅ `checklist.md` 好生意关强制引用 base_pack 档位，禁止心算
7. ✅ `masters.md` 加 base_pack 输入，估值章节引用 ROIC/现金质量档位
8. ✅ `manifest.json` deep-research tools 同步（补 classify/base_pack/run_code）

---

## 遗留问题（下一轮修复）

### P0 · 字段缺口（base_pack 大量指标会 NC）

**问题**：`market.get_bundle` 的 `fundamentals.years` 只提供 7 个字段
（revenue/net_profit/gross_margin/roe/ocf/capex/debt_ratio），而 base_pack 的
`required_raw_fields` 要 24 个底层字段。以下字段 market 层完全没有：

```
contract_liability, sales_volume, ebit, total_debt, total_equity, cash,
tax_rate, cogs, inventory, accounts_receivable, accounts_payable,
selling_expense, total_assets, current_liabilities, ebitda,
depreciation_amortization, working_capital_change,
stock_based_compensation, weighted_average_shares_diluted
```

**影响**：ROIC 算不出（缺 ebit/tax_rate/total_debt/total_equity）、
合同负债同比算不出、量价拆分算不出、CCC 算不出——G1a 的核心指标全部 NC。

**现状**：base_pack 已做 best-effort 映射（ocf→operating_cash_flow 等），缺失字段
返回 NC 并在 prompt block 里诚实标注"数据缺口"。LLM 会看到 NC 标 grey。

**修复方向**（任选其一）：
- a) 扩展 `toolkit/market/sources/` 的财报 provider，补取资产负债表/现金流量表明细
  （akshare 的资产负债表接口能拿到 contract_liability/inventory/total_debt 等）
- b) 在 base_pack 里增加 `web.search` 兜底取数（财报原文抓取关键字段）
- c) 让 LLM 在 `calc.run_code` 沙箱里用 `web.search` 补取缺失字段后现算

### P1 · 编排顺序仅靠 SKILL.md 指令约束，无代码强制

**问题**：`loop.py` 是通用 ReAct 循环，LLM 可能跳过 classify 直接 get_bundle。
SKILL.md 写了"必须按序"，但 LLM 不保证遵守。

**现状**：靠 SKILL.md 的"编排约束"段 + 工具描述里的"在 classify 之后调用"引导。

**修复方向**：
- a) `loop.py` 增加 `skill_phase` 状态机：第 0/1/2 步未完成时，屏蔽后续工具
  （如未 classify 则不暴露 calc.base_pack）
- b) 或在 `calc.base_pack` 执行时检查上下文是否已有 classify 结果（需跨工具状态）

### P2 · `{{ base_pack }}` 等模板变量 assembler 未做替换

**问题**：`assembler.py` 把 SKILL.md 全文当 system prompt 注入，不处理子 md 的
`{{ base_pack }}` `{{ market_block }}` `{{ orientation }}` 等模板变量。
这些变量当前**不会被填充**——子 md 是设计文档，实际靠 LLM 在 ReAct 循环里
调工具拿结果进上下文。

**现状**：SKILL.md 的流程描述已明确"调 calc.base_pack 拿到 prompt block 作为输入"，
LLM 调工具后结果会进入 messages 上下文，六关/四大师分析时能引用。
模板变量 `{{ base_pack }}` 是给未来编排引擎预留的设计占位。

**修复方向**：
- a) 实现编排引擎：按 SKILL.md 的步骤顺序自动调工具，把结果填进子 md 的模板变量，
  再把填充后的子 md 作为 user message 喂给 LLM
- b) 或保持现状（ReAct 自驱），靠 SKILL.md 指令 + 工具描述引导 LLM 自觉调用

### P3 · 四大师子 Agent 未实现

**问题**：`agents/base.py` 的 `spawn_masters` 是占位符（返回空列表），
`loop.py` 从不派发 sub_agent。SKILL.md 声明的 4 个子 Agent 是纸面的。

**现状**：按用户要求"四大师先用 masters.md 让主 LLM 综合，子 Agent 并行派发作为 P3 后续"。
SKILL.md 第 5 步已改为"加载 masters.md 综合四大师框架"，主 LLM 一人分饰四角。

**修复方向**（P3）：
- a) 实现 `agents/masters/duan.py` 等 4 个子 Agent
- b) `loop.py` 增加 sub_agent 派发逻辑（并行调用 + 结果汇总）
- c) 补齐 `agents/masters/buffett.md` / `munger.md` / `li_lu.md` 方法论文档

### P4 · `calc.run_code` 依赖 `var/data_pack.json`，取数引擎不存在

**问题**：`run_code.py` 从 `var/data_pack.json` 读数据包，但没有任何代码写入这个文件。
LLM 调 `calc.run_code` 会拿到空 DATA。

**现状**：本轮未修。base_pack 是自己调 `market.bundle` 取数（不依赖 data_pack.json），
所以主链路不受影响。run_code 是 base_pack 之外的非标计算通道。

**修复方向**：
- a) 让 base_pack 把取到的字段写入 `var/data_pack.json`，供 run_code 复用
- b) 或改 run_code 直接调 `market.bundle` 取数（去掉 data_pack.json 依赖）

### P5 · 其他组（G1b/G2a/G2b/G3/G4/G5/G6）的 base_pack.yaml 未建

**问题**：只有 G1a 有完整的 `base_pack.yaml` + `G1a_metrics.yaml` + `formulas_G1a.py`。
其他组的这些文件可能还是占位。

**现状**：base_pack 执行器做了降级——组配置缺失时只算 core 通用指标。

**修复方向**：逐组补齐 base_pack.yaml + metrics.yaml + formulas_<group>.py
（按用户优先级，G2a 银行 / G3 周期 可能优先级更高）。

### P6 · `_compute_pack` 的 inputs 列表形参数对齐逻辑较脆弱

**问题**：当指标的 inputs 是列表形式（如 `[ebit, tax_rate, total_debt, total_equity, cash]`），
执行器用 `inspect.signature` 取函数参数名按位置对齐。如果 yaml inputs 顺序与函数签名
参数顺序不一致，会错位。

**现状**：core.yaml 的 inputs 顺序与 formulas_core 函数签名一致（已核对 roic/ccc 等）。
contract_liability_yoy 用的是 dict 形式 inputs（{current: ..@latest, prior: ..@yoy}），
按 kwargs 传，无对齐问题。

**修复方向**：统一 inputs 为 dict 形式（key=参数名），消除顺序依赖。

---

## 本轮冒烟验证修复记录（已修复）

冒烟脚本 `scripts/smoke_base_pack.py`（茅台真实数据，离线）暴露并修复了：

1. ✅ **字符串引号嵌套语法错误**：base_pack.py 中 `"六关"好生意"关"` 英文引号嵌套导致
   SyntaxError，改为中文引号「好生意」。
2. ✅ **`sig.parameters` 迭代产物**：`for p in sig.parameters` 产生的是参数**名字字符串**
   不是 Parameter 对象，`params[i].name` 抛 AttributeError 被 except 吞掉 → kwargs={} →
   所有列表形 inputs 指标报 missing 参数。改为直接用名字。
3. ✅ **core.yaml 无法用 yaml.safe_load 解析**：core.yaml 头部是给 LLM 看的裸文本
   （非合法 YAML，classify.py 也是 try/except 跳过）。base_pack 内置 `_CORE_METRIC_DEFS`
   作为 fallback，core 指标（roic/现金质量/应计项等 15 个）不再"定义缺失"。
4. ✅ **无后缀 input 引用传 list 给单值函数**：`ratio`/`ccc` 等冻结函数用 `_f(list)` 会转
   None → NC。约定改为：**无后缀=传单值（最新）；需要序列的指标 yaml 显式写 @series**。
   `G1a_metrics.yaml` 的 `price_volume_split` 已加 `@series`。
5. ✅ **red_flag_precheck 混入指标计算**：红旗是组合条件（condition 列表）非 formula，
   从 `ids_to_compute` 排除，避免输出无意义的"指标定义缺失"。

### 冒烟结果（茅台 2023/2024 真实数据，group=G1a）

16 项 OK / 6 项 NC（NC 均为合理诚实标注）。关键档位：
- ROIC=34.6%[优秀]、OCF/净利润=1.07x[健康]、应计项=-2.2%
- 净债/EBITDA=-0.80x[稳健]、合同负债同比=-56.0%[恶化⚠high]（2024 主动去库真实信号）
- 量价拆分：营收+15.7% = 量+9.5% + 价+6.1%
- 反推DCF：隐含永续增速 2.95%、正常化PE=21.8x

剩余 NC 原因（均为数据不足/需 LLM 假设，诚实标注）：
- `roic_vs_wacc` 缺 wacc、`incremental_roic` 缺 ebit_change（需两年 EBIT 差分）
- `interest_coverage` 缺 interest_expense（茅台无有息负债，数据源未提供）
- `roic_volatility_10y` 缺 10 年 ROIC 序列、`pe_historical_percentile` 历史点 <5
- `dividend_yield_percentile` 无冻结函数且无指标定义

### 新增待办

### P7 · LLM 假设字段（wacc/ebit_change 等）无输入通道
base_pack 是自动计算，但 wacc、normalized_earnings_ps、discount_rate、ebit_change 等
是 LLM 假设值。当前这些指标直接 NC（诚实但浪费）。
**修复方向**：base_pack 工具加 `assumptions` 参数（LLM 可传 {wacc: 0.08, discount_rate: 0.09}），
或拆成"指标依赖假设 → 提示 LLM 用 calc.run_code 传入后算"。

### P8 · G1a 的 CCC band 对重存货子行业（白酒）会误标"偏弱"
茅台 CCC=1120 天[偏弱]，但白酒存货是基酒（会升值），CCC 天然高是行业特性，不是劣势。
**修复方向**：G1a 的 cash_conversion_cycle 加 note 说明"白酒存货=基酒，CCC 高属正常；
真实信号看合同负债同比 + 应收账款"；或按子行业（白酒/食饮/家电）细分 band。

### P9 · market.bundle 缺字段导致 base_pack 多数指标 NC（真实运行场景）
冒烟用的是完整字段；真实 `market.get_bundle` 只给 7 个字段（revenue/net_profit/
gross_margin/roe/ocf/capex/debt_ratio），G1a 的合同负债/量价拆分/CCC 全部 NC。
见 P0（数据源扩展）。冒烟脚本的完整字段模拟"数据源扩展后"的场景。

---

## 本轮端到端验证修复记录（✅ 已修复）

用 `scripts/e2e_base_pack.py 600519.SH`（真实 westock 数据）跑通整条链路，
修复了以下 P0/P1 级问题：

1. ✅ **westock 字段扩展**：`WeStockProvider.fundamentals` 从只提取 7 个字段
   → 扩展为 28 个字段（新增 ebit/cash/contract_liability/inventory/total_debt/
   fixed_assets/current_liabilities/cogs/selling_expense/rd_expense/interest_expense/
   fcff/roic/operating_profit/total_profit/assets/equity 等）。
   方法：A股走 sum(摘要,含ROIC) + lrb(利润表) + zcfz(资产负债表) + xjll(现金流量表) 四表。
2. ✅ **年报优先修复**：westock 同一财年返回 Q1/Q2/Q3/Q4 多期，原代码 `_year_of` 只取
   前 4 位导致 2025-03-31 的 Q1 累计数覆盖 2025 年报 → OCF/净利严重失真（茅台 OCF 从
   615亿变131亿）。改为按报告期**优先取 12-31 年报**，同一年保留更晚报告期。
3. ✅ **assets/equity 透传修复**：`years_list` 的透传循环漏了 assets/equity 键，
   导致 ROIC 分母缺失。已补 `row["assets"]`/`row["equity"]`。
4. ✅ **`_FIELD_ALIASES` 源键修正**：westock years 键是 `assets`/`equity`（非
   `total_assets`/`total_equity`），映射源键已改。
5. ✅ **tax_rate 推导**：westock 无税率字段，用 `(利润总额−归母净利)/利润总额` 推导
   有效税率（茅台 27.7%，符合实际）。
6. ✅ **配置 bug 修复**：`core/config.py` 的 `cors_origins` 是 list[str] 但 `.env` 的
   `CORS_ORIGINS` 是 JSON 字符串，pydantic 解析失败阻塞整个服务启动。
   加 `alias="CORS_ORIGINS"` 修复。

### 端到端结果（真实茅台数据，group=G1a）

已算 12 项 / NC 10 项（NC 均为合理：估值类缺快照价格、wacc 等 LLM 假设）。
- ROIC=30.0%[优秀]、ROIC剔超额现金=56.3%[优秀]
- OCF/净利=0.75x[偏弱⚠]、FCF/净利=1.13x、应计项=6.8%
- 所有者收益=823亿、净债/EBITDA=-0.29x[稳健]
- 合同负债同比=-16.5%[恶化⚠]、现金转换周期=1407天

### 新增待办

### P10 · `_load_metric_defs` 的组 yaml 布局兼容
`_load_metric_defs` 现在支持 3 种布局（目录内 _metrics / 目录内同名 / 平铺同名），
但 G1b 用的是 `G1b_metrics.yaml`、G2a 用 `G2a.yaml`、G2b 用 `G2b.yaml`，已全部验证通过。

### P11 · 利息保障倍数对"无息负债+负财务费用"公司（茅台）误报危险
茅台无有息负债、财务费用为负（利息收入>支出），`interest_coverage=EBIT/利息费用=-139x`
被 bands 判为 [危险⚠]。应加 `na_when: [interest_expense_near_zero]` 或对负财务费用
（利息净收入）判 NA/稳健。G1a_metrics.yaml 的 interest_coverage 缺该豁免。
**修复方向**：在冻结函数或 yaml 层加"财务费用≤0 → 无利息负担，标 NA 或稳健"。

### P12 · 估值类指标依赖 LLM 假设（wacc/current_price/PE 序列）
`roic_vs_wacc`/`normalized_pe`/`reverse_dcf`/`pe_historical_percentile` 需要
wacc、normalized_earnings_ps、current_price、PE 历史序列——base_pack 自动计算
拿不到这些（current_price 需快照，PE 历史需序列）。
**修复方向**：base_pack 工具加 `assumptions` 参数（LLM 可传 {wacc, discount_rate,
normalized_earnings_ps}），或从 bundle.snapshot 注入 current_price/pe（已做部分），
PE 历史序列靠 LLM 在 calc.run_code 沙箱里补。→ 已有 P7 记录，合并。

### P13 · 全行业 base_pack 配置已补齐（✅）
G1b/G2a/G2b/G3/G4/G6 的 `base_pack.yaml` + metrics 定义已补（G3/G4/G6 从裸文本
占位转为合法 yaml）。`scripts/smoke_all_groups.py` 验证 8 组均能加载+计算。
- G1a 16 项 OK / G1b 9 OK / G2a 10 OK / G2b 5 OK / G3 5 OK / G4 9 OK / G6 9 OK
- 剩 NC 为真实数据缺口（LLM 假设/外部运营数据），诚实标注。
- G2a 拨备覆盖率口径 bug：`ratio` 返回裸比值(3.67=367%)，bands 按 pct 写判错
  （2200/600 应为"稳健/或有反哺"却判"偏薄"）。**修复方向**：银行指标用
  `ratio*100` 或 bands 按比值写。
