# TODO：Metric Service 落地（下一版）

> 创建时间：2026-08-24
> 背景：MVP 阶段选择选项 A——新 Adapter 只靠 skill.md 给模型提示，不依赖 base_pack/指标字典的工程实现。
> 当前状态：模型对 C/D/E/F 类指标（ROIC/净现比/CCC 等）边看原始数据边推导，口径不保证与指标字典对齐，三态输出缺失。
> 触发条件：skill.md 四个稳定、真模型评测通过后，开始本版迭代。

---

## 一、要做的事

### 1. 指标字典工程化（33 条）

- 把 `指标字典.md` 的每条指标实现为 Metric Service 的确定性函数
- 每条指标输出格式：`{value, metric_id, 口径版本, 报告期, 来源等级, 时点}`
- 三态输出必须实现：`not_disclosed` / `not_meaningful` / `degraded`（degraded 必须标注退化方式）
- 每条指标至少 3 个单元测试（正常值 / 缺失 / 边界值）
- 口径不得自行修改，字段不可用须反向反馈产品更新指标字典

优先级排序（按 Adapter 使用频率）：

| 优先级 | 指标类 | 具体 metric_id | 说明 |
|--------|--------|----------------|------|
| P0 | 估值类 H | pe_ttm / pb / ps_ttm / ev_ebitda / ev_revenue / fcf_yield / hist_pct | 4 个 Adapter 都用 |
| P0 | 回报类 C | roic / roe | consumer_brand / semiconductor / robot_manufacturing 核心 |
| P0 | 现金流类 D | fcf / fcf_margin / capex_intensity / runway | ai_software 的 runway 是★最优先 |
| P1 | 成长类 A | rev_yoy / np_yoy / rev_cagr_3y | 全行业需要 |
| P1 | 盈利类 B | gross_margin / op_margin / net_margin / exp_ratio | 全行业需要 |
| P1 | 营运类 E | ar_rev_gap / ar_days / inv_days / asset_turnover | consumer_brand 重点 |
| P2 | 偿债类 F | debt_ratio / net_debt / interest_cover | 全行业风险评估 |
| P2 | 股东回报类 G | share_change / payout / buyback | consumer_brand 重点 |

### 2. base_pack.yaml 迁移对齐

- 旧路径（toolkit/calc/base_pack.py + G1a_metrics.yaml 等）的指标计算逻辑迁移到 Metric Service
- base_pack.yaml 改为只做**调度配置**：声明"预热时触发哪些 metric_id"，计算和口径全走 Metric Service
- 迁移后旧路径可以继续兼容读取（不破坏现有调用方）

### 3. Adapter 的赛道指标补充声明

赛道指标（指标字典第 6 节说明：不在标准库，由 Adapter 自己声明）：

| Adapter | 赛道指标 | 数据依赖说明 |
|---------|----------|-------------|
| ai_software | ARR / NDR / 付费客户数 / revenue_quality / gross_margin_slope | 年报附注 + 搜索 |
| semiconductor | 产能利用率 / book-to-bill / 折旧年限 vs 同业 / 研发资本化比例 | 公告 + 搜索 |
| robot_manufacturing | book-to-bill / 国产替代份额 / 单价年降幅 / 客户集中度 | 公告 + 搜索 |
| consumer_brand | 量价拆分 / 合同负债同比 / CCC / 渠道库存天数 | 年报 + 渠道调研 |

需要为赛道指标建立标准声明格式（metric_id / 公式或数据依赖 / 来源等级 / 可得性）并在 adapters_catalog.yaml 里落地。

### 4. Harness 校验补充

- 校验 Adapter 配置里的 `recommended_metrics` 引用合法性：引用不在指标字典里的 metric_id 一律拦截（赛道指标例外，需在 Adapter 里显式声明）
- 校验 `invalid_metrics` 与指标字典硬规则（适用/不适用列）一致性

---

## 二、当前已知的质量损失（MVP 阶段接受）

| 场景 | 损失 | 影响程度 |
|------|------|----------|
| 模型推导 ROIC | 口径可能不与指标字典对齐（IC 简化口径 vs 完整口径）| 低——方向判断不受影响 |
| 三态输出缺失 | 亏损公司 PE 可能被算成负数而不是 not_meaningful | 中——需要 skill.md 里提醒模型 |
| 折旧年限 vs 同业 | 赛道指标，模型只能靠搜索近似，无确定性计算 | 中——semiconductor/robot 的关键红旗 |
| 量价拆分 | consumer_brand 核心指标，需联网搜索或年报文本抽取 | 中——如果信源不足退化为 open_question |

---

## 三、依赖项

- skill.md 四个写完并真模型验证通过（先决条件）
- 指标字典 v1.0 冻结（已完成，文件：`指标字典.md`）
- market.bundle 字段清单与指标字典的字段映射表（需补充）
