---
id: deep-research
name: 标的研究
description: 基于 preloop 生成的标的上下文和研究计划，调用当前阶段已开放的工具取证，并综合证据输出研究结论
max_steps: 15
tools: [entity.resolve, company.classify, market.get_bundle, calc.menu, calc.metric, calc.metrics, calc.run_code, cognition.recall, cognition.extract, web.search, web.fetch, delivery.finish]
capabilities: [value-investing-core, metric-calculation]
sub_agents: []
---

# 标的研究工作流

当用户要求"研究 / 分析 / 调研某标的"时启用本 Skill。

## 执行边界：代码管流程，模型做取证与综合

preloop 已负责轻量实体解析、基础事实、认知库检索和带 `data_requirement` 的研究计划。不要在正式 loop 里重新规划一遍完整工作流。系统在代码层注入当前阶段、可用工具和研究计划；你只执行当前已开放的工具，利用返回证据推进问题状态，最后综合结论。

**工具决策轮不得输出过程叙述**

- 不要在 `content` 中输出“我先锁定标的”“接下来并行取数”“我将……”等执行计划、自言自语或工具说明。
- 当本轮提供了 function tools 且尚需取证时，直接返回 tool calls；工具调用前的 `content` 必须为空。
- 研究进度由系统事件展示，不需要你用自然语言播报。
- 只有进入最终综合阶段后，才在 `content` 中输出面向用户的完整结论。

**按问题的取数需求推进（preloop 已完成轻量准备，不要重做）**

研究计划里每个问题都带 `data_requirement` 标记，按标记决定这一步该调什么：

- `light_evidence` → 用 `web.search` 或直接基于已有事实回答，不必动用重数据工具。
- `market_data_required` → 必须先调 `market.get_bundle(symbol)` 取行情/财报，再回答。
- `calc_required` → 单项标准指标调 `calc.metric(symbol, metric_id)`；多个标准指标优先调 `calc.metrics(symbol, metric_ids)`，复用统一数据包批量计算。非标情景/敏感性才用 `calc.run_code(symbol, code, fields)`。
- `memory_verify` → 来自认知库的历史判断，本轮要取证复核；对不上按反证处理。

preloop 只做轻量启动（实体解析 + 白名单搜索 + 认知库检索 + 计划），不会提前取财报/行情/指标。因此财务、估值、ROIC、毛利率这类问题一律要在这里按需取数，不要假设「前面已经取过了」。

以下是能力地图（只说明工具语义与质量要求，不是待重新规划的全流程；仅使用本轮实际提供的工具）：

**锁定标的**
- `entity.resolve(symbol)` → 标准化 symbol + 公司名。歧义时向用户确认，不得擅自替用户选择。

**商业模式识别**
- `company.classify(symbol)` → `group` / `stage` / `confidence` / `is_conglomerate`。
- `is_conglomerate=true` → 提示用户走多业务分析；`needs_review=true` 或 `confidence < 0.7` → 结论中提示"商业模式分类待复核"。
- 分类结果只决定加载哪个行业领域能力包；商业模式、发展阶段和事件作为可组合 Overlay。分类不足时只使用通用价值投资框架，不强行套行业包。

**地基指标计算**
- 上游会注入「本次研究执行配方」：其中的 `metric_ids`、必补资料与估值方法是本次研究的最小数据契约，不能因为回答方便而跳过。
- 标准指标使用 `calc.metric(symbol, metric_id)`，其口径、输入字段和四态由指标服务冻结；多个指标用 `calc.metrics(symbol, metric_ids)` 批量计算。
- 标 DEGRADED = 近似口径，可解读但结论须降权；标 NC = 字段缺失，按下方「质量铁律」中的补数优先级处理。

**认知双路检索**（研究开始时调一次）
- `cognition.recall(query="<研究主题>", user_id=<当前用户>)`：同时检索「默认投资框架」（四大师 + 常见偏误）与「用户个人认知库」，返回一致点、冲突点、潜在盲区 + `recall_block` 文本块。
- 收尾时按「默认框架怎么看 vs 你的个人框架怎么看」组织双路输出。

**行情快照 + 财报摘要**
- `market.get_bundle(symbol)` 取当前行情和财报原文。
- `market.get_bundle` 是研究主链路唯一的结构化行情取数入口，并生成可增量更新的统一数据包。`calc.metric`、`calc.metrics` 和 `calc.run_code` 只消费该数据包，不得自行联网或重新拉取行情。模型可以在同一轮声明行情与计算调用，编排层会先完成行情、再执行依赖它的计算，同时保留其他无依赖工具的并行。
- 计算返回字段缺失时，由研究编排继续调用行情、官方披露或公开搜索补数；补数写回新版数据包后只重算受影响指标。“统一取数”不等于禁止后续扩展数据。

**定性补充 + web 补数**（按需）
- `web.search`（可传 `sources="finance"` 限定财经白名单站点）补充管理层、行业、近期事件等定性信息。
- 需要补数字（如年报 EBIT/总资产）时：`web.search` + `web.fetch` 从可信财经站点取数，再调 `calc.run_code` 时通过参数 `web_data={"字段名": {"value": 数值, "url": 来源链接, "note": 检索说明}}` 传入沙箱，脚本内以 `DATA['_web']['字段名']['value']` 访问计算。

**组织分析（先中间推理，后成文）**
- 先产出「四视角 Evidence Pack」：按 evidence_pack.md 的强制 JSON 结构，从巴菲特（生意质量/护城河/资本配置）、芒格（多元思维/激励机制/反向思考）、段永平（对的生意/对的人/对的价格）、李录（长期文明趋势/能力圈）四个视角对同一标的各输出一条统一 JSON（evidence 可溯源 + judgment + confidence），不得用自由散文替代。
- 再产出「冲突矩阵」：对照四视角判断，列出实质性分歧与一致项（见 evidence_pack.md）。
- 最后综合：问题清单只用于后台防漏项，禁止逐题成文。最终按价值投资母框架组织成连续研报：投资结论与核心矛盾 → 生意本质 → 护城河 → 财务质量与资本配置 → 管理层 → 行业与长期趋势 → 估值与安全边际 → 逆向风险 → 证伪条件与跟踪指标。
- 认知陪练放在研报末尾：比较默认框架与用户个人框架的一处关键一致或冲突，指出一个潜在盲区，并只提出一个最值得用户回答的反思问题。用户认知库为空时，不输出机械的“个人框架为空”，直接给默认框架下的关键追问。

**收尾（唯一出口）**
- 数据齐备、结论写好后，直接调用 `delivery.finish`（携带 conclusion + self_check）结束研究。
- 硬规则校验（禁目标价、须区分 AI 置信度与投资确定性等）由系统在 finish 提交后自动执行；若被拦截，按返回原因修正结论文本后重新提交 `delivery.finish`，不要调用取数/分类工具，也不要从头重跑研究流程。
- 不要通过"不再调用工具"隐式结束，也不要无限追加计算或重复已完成的步骤。

**认知沉淀**（收尾后可选）
- `cognition.extract(research_summary=<结论文本>, user_id=<当前用户>, symbol=<代码>)`：仅生成候选确认卡，不写入用户认知库。用户确认或编辑后确认才会成为 active；拒绝项直接丢弃，不参与后续检索。

## 质量铁律（不可逾越）

- 分析中的数字必须来自工具返回值（calc.metric(s) / market / calc.run_code / web），禁止 LLM 心算或凭记忆报数。
- 缺字段补数优先级：① 若 `calc.metric(s)` 返回 `field_recovery_plan`，按其中指定的官方文件与校验规则补一手披露；市值缺失时先取得与价格同证券类别、同口径的总股本，再按 `price × total_shares` 派生。② 仅在已披露输入齐全时用 `calc.run_code` 计算非标情景或敏感性；相邻字段近似必须标 `DEGRADED`。**绝不心算补数**；仍无法补齐才标“数据不足”。
- web 来源数字算出的指标必须标 DEGRADED 并在结论中写明来源 URL，绝不与引擎结构化数据（东财/akshare/westock）同等对待。
- 估值章节必须引用 `calc.metric(s)` 指标服务的可溯源结果，禁止凭记忆报数。
- 四视角 Evidence Pack 不得跳过；最终输出必须包含双路结构（默认框架 + 个人框架 + 一致/冲突/盲区/反思），不得只输出单一视角。

## 分支

- **未盈利成长 Overlay**：禁用 PE / ROE / ROIC / FCF yield 等利润锚；改用收入质量、UE、现金可支撑月数、营收增速、PS / EV-Revenue 与隐含增长假设。没有成熟利润估值锚，不等于没有收入估值锚。

## 交付格式（所有 Adapter 共用）

最终结论除连续正文外，必须先给出以下结构；无可验证数据时在单元格写“数据不足”，不得编造：

1. **关键数据表**：指标、当前值、时点/口径、投资含义，至少覆盖本次 Recipe 的核心指标。
2. **估值与隐含预期表**：估值方法、当前倍数/结果、关键假设、含义。预测值必须明确写作预测，不能伪装为事实。
3. **同业比较表**：仅在已锁定可比同行且报告期、币种、发展阶段可比时输出；否则说明“暂不具备可比条件”。

## 收口（强制）

最终结论必须经 `delivery.finish` 硬规则校验通过后，才能返回给用户。未通过校验的结论不得输出。
