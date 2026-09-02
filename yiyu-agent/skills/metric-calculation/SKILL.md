---
name: metric-calculation
description: 在研究工作流需要计算标准财务指标、派生指标或非标情景时，提供可溯源的确定性计算。不直接承接完整公司研究。
---

# 指标计算能力包

## 定位：能力包，不是任务 Skill

**不直接对用户路由**，而是被 `deep-research` / `private-company` 等研究 Skill 在研究过程中调用。
用户不会说"用指标计算 Skill"，只会说"研究茅台"——但研究过程中你需要算 ROIC、FCF、估值，
这时进入本能力包。

三层职责分离：

| 层 | 回答什么 | 落在哪里 |
|---|---|---|
| **Skill**（本文件） | 什么时候算、怎么选计算方式、有什么纪律 | `SKILL.md` |
| **Metric Catalog** | 有哪些标准指标、需要什么字段、什么口径 | `toolkit/calc/metrics_catalog.yaml` |
| **Tool / Kernel** | 取数 → 校验 → 计算 → 返回结果 | `toolkit/calc/` + `bus_router/formulas_*.py` |

## 什么时候使用

研究过程中需要：

- 计算 ROIC / ROE / FCF / PE / PB / EV-EBITDA 等**标准指标**
- 从原始财务字段生成**派生指标**
- 计算指标**趋势**或跨期比较
- 计算**目录之外**的非标准指标（SOTP 分部加总、敏感性表、单位经济模型）

## 什么时候不要使用

- 指标在目录里，但你打算"估一个数" —— **禁止**，走 `calc.metric`
- 数据缺失，但想凑一个 —— **禁止**，返回缺失字段
- 数字已由工具返回、只是想换个单位 —— 不需要重算

## 两条路径

**① 标准指标 → `calc.metric`**（目录内 12 个 P0 指标）

```
calc.metric(symbol="600519.SH", metric_id="roic")
```

拿不准该看哪些指标时，先 `calc.menu(group=...)` 看该商业模式通常关注什么。
菜单是「推荐菜」，不是「强制套餐」——点不点、点几个，由你判断。
菜单里每道菜都带**解读陷阱**（如"白酒 CCC 高是基酒窖藏，不是缺陷"），务必读。

**② 非标指标 → `calc.run_code`**（目录之外）

仅限目录里没有的指标。**标准指标不得走沙箱。**

## 四态语义（必须严格区分）

P0 指标字典批次返回小写 `status`：

| status | 含义 | 正确处理 |
|---|---|---|
| `ok` | 口径完整，算出来了 | 正常解读 |
| `degraded` | 用了近似口径（如财务费用代理利息支出） | 可解读但**结论须降权**，写明降级原因 |
| `not_disclosed` | 缺字段，算不出来 | 按 `missing` 补数，**禁止心算** |
| `not_meaningful` | 该指标对这家公司不适用（如净现金公司算利息保障） | **不是缺陷**，别当负面信号 |
| `self_funded` | 现金跑道专用：FCF ≥ 0，不烧钱 | 正面信号，不是"算不出来" |

**最常见的误判**：把 `not_meaningful` 当成 `not_disclosed` 去补数，或把 `not_meaningful`
解读成公司有问题。前者是"这题对这家公司没意义"，后者是"有意义但没数据"。

## 缺字段时怎么办

`calc.metric` 返回 `missing: [{field, source_hint}]`，`source_hint` 告诉你该去哪张报表找
（如 `operating_cash_flow → 现金流量表·经营活动产生的现金流量净额`）。

补数优先级：

1. `web.search` + `web.fetch` 从可信财经站点取数
2. 通过 `calc.run_code` 的 `web_data` 参数传入沙箱：`{"字段名": {"value": 数值, "url": 来源, "note": 说明}}`，
   脚本内以 `DATA['_web']['字段名']['value']` 访问
3. **web 来源算出的指标必须标 DEGRADED 并写明 URL**，绝不与引擎结构化数据同等对待
4. 都补不到 → 标"数据不足"，**绝不心算**

## 口径纪律

- 结果自带 `caliber_version`（当前 `v1.0`），引用时必须保留
- 口径易错的指标（ROIC / CCC / TTM 差分 / 正常化盈利）由**冻结函数**计算，禁止自己手写公式
- 沙箱内也一样：有标准口径的指标必须 `from formulas_core import ...`，不许现写

## 沙箱纪律（calc.run_code）

- **物理断网**：socket 被替换，subprocess / ctypes / 文件写全部禁用（运行时强制，不是约定）
- **输入只能来自数据包 `DATA`**（按 `fields` 白名单选字段），禁止在代码里硬编码业务数字——那等于心算
- 用 web 数据或近似推导算出的指标必须返回 `DEGRADED(值, "原因")` 并注明 provenance，**禁止伪装成 OK**
- 每次执行自动存档（代码 + 数据包 hash + 输出），可复现

## 溯源纪律

每个输入字段自带 `source` / `source_level` / `as_of` / `period`，结果整体带 `fields[]` 数组：

- **信源等级取木桶短板**：指标整体 `source_level` = 所有输入字段里最差的那个（S > A > B）
- 任一输入字段 `degraded` → 整个指标降级为 `degraded`
- 取数层登记的 `field_evidence` 优先级高于计算层推断，会覆盖来源并注入 `evidence_id`

**结论里的数字若缺少上述溯源信息，不得进入最终报告。**
