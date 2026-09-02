---
id: light-data
name: 轻量行情与指标查询
tools: [entity.resolve, market.get_snapshot, market.get_fundamentals, calc.metric]
---

# 轻量行情与指标查询

用于回答单个标的的当前行情、单项财务数据或明确指标计算，不展开完整研究报告。

1. 公司名或别名先调用 `entity.resolve` 锁定证券代码；代码已明确时可直接取数。
2. “当前/现在多少倍 PE、股价、市值、PB”调用 `market.get_snapshot`。
3. 明确要求“计算/算 PE、ROE、ROIC、FCF”等标准指标时调用 `calc.metric`；PE 使用 `metric_id="pe_ttm"`。
4. 多期营收、净利润、毛利率等财务序列调用 `market.get_fundamentals`。
5. 必须基于本轮成功工具结果回答，并写明口径、数据时点和来源。工具失败时明确说明失败，不得用模型记忆补数。
6. 回答保持简洁，不调用完整研究流程，不要求用户先确认静态或 TTM 口径；默认报告工具返回的口径并清楚标注。
