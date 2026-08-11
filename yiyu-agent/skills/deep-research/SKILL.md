---
id: deep-research
name: 标的研究
description: 对一只标的进行深度研究：锁定实体 → 识别商业模式 → 机器算指标 → 取行情 → 按行业框架写研报
max_steps: 15
tools: [entity.resolve, company.classify, market.get_bundle, calc.base_pack, calc.run_code, cognition.recall, cognition.extract, web.search, web.fetch, delivery.finish]
sub_agents: []
---

# 标的研究工作流

当用户要求"研究 / 分析 / 调研某标的"时启用本 Skill。

## 执行顺序（强制，前置未完成不得跳到后续）

1. **锁定标的**：调用 `entity.resolve(symbol)` 确定标准化 symbol + 公司名。歧义时必须向用户确认，不得擅自替用户选择。
2. **商业模式识别**：调用 `company.classify(symbol)` → 拿到 `group` / `stage` / `confidence` / `is_conglomerate`。
   - `is_conglomerate=true` → 提示用户走多业务分析；`needs_review=true` 或 `confidence < 0.7` → 结论中提示"商业模式分类待复核"。
   - **此步决定第 6 步加载哪套行业分析框架（bus_router/{group}/skill.md）**，禁止跳过直接取数。
3. **地基指标计算**：调用 `calc.base_pack(symbol)`（内部自动取数 + 按 group 调冻结函数算指标）。
   - 返回"档位 + 数值 + 三态"文本块，这是后续分析的数据基础。
   - 标 DEGRADED 的指标 = 用了近似口径（如 EBIT 用营业利润+财务费用近似），可解读但结论须降权；
   - 标 NC 的指标 = 字段缺失。补数优先级：
     ① 先调 `calc.run_code` 读数据包（base_pack 已把全部原始字段写入 `var/data_pack.json`），
        用相邻字段近似补算（优先 import 可信库 `ebit_approx`/`ebitda_approx`/`ocf_approx`，结果标 DEGRADED）；
     ② 数据包也补不了、但数字对估值结论关键的，走第 5 步 web 补数。
     **绝不心算补数**；两项都补不了才标"数据不足"。
4. **认知双路检索**（必做，研究开始时调一次）：调用 `cognition.recall(query="<研究主题>", user_id=<当前用户>)`。
   - 同时检索「默认投资框架」（四大师 + 常见偏误）和「用户个人认知库」；
   - 返回一致点、冲突点、潜在盲区 + `recall_block` 文本块；
   - 收尾时按此组织「默认框架怎么看 vs 你的个人框架怎么看」的双路输出结构。
5. **行情快照 + 财报摘要**：调用 `market.get_bundle(symbol)` 取当前行情和财报原文。
6. **补充定性信息 + web 补数**（可选）：调用 `web.search`（可传 `sources="finance"` 限定财经白名单站点）
   补充管理层、行业、近期事件等定性信息。
   - 需要补数字（如年报 EBIT/总资产）时：用 `web.search` + `web.fetch` 从可信财经站点取数，
     再调 `calc.run_code` 时通过参数 `web_data={"字段名": {"value": 数值, "url": 来源链接, "note": 检索说明}}`
     传入沙箱，脚本内以 `DATA['_web']['字段名']['value']` 访问计算。
   - **铁律**：web 来源数字算出的指标必须标 DEGRADED 并在结论中写明来源 URL，
     绝不与引擎结构化数据（东财/akshare/westock）同等对待。
6. **组织分析（先中间推理，后成文）**：
   - **6a 先产出「四视角 Evidence Pack」**：按 evidence_pack.md 的强制 JSON 结构，
     从巴菲特（生意质量/护城河/资本配置）、芒格（多元思维/激励机制/反向思考）、
     格雷厄姆（资产/估值/安全边际）、费雪（成长质量/研发/管理层/长期空间）
     四个视角对同一标的各输出一条**统一 JSON**（evidence 可溯源 + judgment + confidence），
     不得跳过、不得用自由散文替代。
   - **6b 再产出「冲突矩阵」**：对照四视角判断，列出实质性分歧与一致项（见 evidence_pack.md）。
   - **6c 最后综合**：基于 Evidence Pack + 冲突矩阵写综合判断 + 证伪条件，再按
     `bus_router/{group}/skill.md` 的框架组织成研报
     （生意本质 → 护城河 → 风险与红旗 → 管理层 → 估值 → 证伪条件与跟踪指标）。
   - **6d 双路输出结构**：最终研报须按「默认框架怎么看 / 按你的个人框架怎么看 /
     两者一致点 / 两者冲突点 / 潜在认知盲区 / 需要你回答的反思问题」组织
     （基于第 4 步 cognition.recall 的返回）。即使用户认知库为空，仍须给出默认框架视角 + 反思问题。
   估值章节必须引用 base_pack 算好的 ROIC / 现金质量档位，禁止凭记忆报数。
7. **收尾（唯一出口）**：直接调用 `delivery.finish`（携带 conclusion + self_check）结束研究。
   - **只有 `delivery.finish` 会结束研究**，不存在其他收尾工具。
   - 硬规则校验（禁目标价、须区分 AI 置信度与投资确定性等）由系统在 finish 提交后自动执行；
     若被拦截，按返回原因**修正结论文本后重新提交 `delivery.finish`**，不要调用取数/分类工具。
   - 数据已齐备并撰写好结论后，直接收尾，**不要再追加新的取数/估值计算，也不要重复已完成的步骤**。
8. **认知沉淀**（收尾后可选）：调用 `cognition.extract(research_summary=<结论文本>, user_id=<当前用户>, symbol=<代码>)`。
   - 从结论中抽取候选认知原子（candidate 状态），存入用户认知库供下次双路检索。
   - 抽取的原子需用户确认后才升级为 confirmed，不是自动写入"已确认方法论"。

## 编排约束（强制，违反即结论无效）
- 第 1 → 2 → 3 步必须按序：未 classify 不得 base_pack；未 base_pack 不得写分析。
- 第 4 步认知检索应在研究开始（分类后、取数前或并行）调一次，收尾输出须引用其结果。
- 分析中的数字必须来自工具返回值（base_pack / market / calc.run_code / web），禁止 LLM 心算或凭记忆报数。
- 补算数字三选一，优先级：base_pack 已算 → calc.run_code 用引擎数据包近似（DEGRADED）→ web 补数（DEGRADED + 注明来源 URL）。
- 估值章节必须引用 base_pack 的档位。
- 最终输出必须包含双路结构（默认框架 + 个人框架 + 一致/冲突/盲区/反思），不得只输出单一视角。
- **收尾是显式动作**：结论写好后调用 `delivery.finish` 结束，不要通过"不再调用工具"隐式结束，也不要无限追加计算。

## 分支
- **G5（早期硬科技·未盈利）**：禁用 ROE / FCF / PE，改用 UE / 现金可支撑月数 / 营收增速；安全边际降级为"暂无估值锚"。
- **C 级（信息稀缺）**：数据不足项一律标"数据不足"，不得硬判；结论只能 grey / warn。

## 收口（强制）
最终结论必须经 `delivery.submit_conclusion` 硬规则校验通过后，才能返回给用户。未通过校验的结论不得输出。
