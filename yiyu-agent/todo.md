# 以渔2.0 (yiyu-agent) 开发任务清单

> 当前状态：项目骨架已搭建并验证可运行（Phase 1 基础设施 ~95% 完成）
> 最后更新：2026-08-10

---

## 2026-08-26 项目记忆：后端 / Agent P0 交付清单

> 来源：按 PRD、现有代码和项目进度总结复核后的上线阻塞项。产品侧前端、登录和用户系统先不纳入本轮。

### 执行顺序

1. [x] **P0 护栏：out_of_scope 第三路由 + 固定边界话术**
   - 覆盖短期涨跌预测、目标价/买卖点位、替用户做交易决定、自动交易/回测/荐股、非投研请求。
   - 要求：命中后不进入 AgentLoop，不调用工具，不生成超范围承诺。
2. [x] **P0 护栏：未交付 skill 拦截**
   - `manifest` 中已注册但缺 `SKILL.md` 或工具不存在的能力，必须返回“后续版本”话术。
   - 当前重点：`quick-screen` / `holdings-track` / `trade-review` 等不能静默进循环。
3. [x] **P0 护栏：结论数字溯源校验**
   - 扩展 `validate_conclusion`：结论中的关键数字必须能对应工具观测 / 来源 / 时点 / 口径。
   - 先落地保守版本：研究结论含数字但无观测证据时拦截；后续再做字段级精确匹配。
4. [x] **P0 回归集：补齐 36 条核心端到端案例**
   - 六类：A 基础研究 6、B 动态规划 6、C 工具证据 6、D 安全防御 8、E 记忆跨轮 4、F 入口路由 6。
   - 要求：进入 `evaluation/e2e/datasets/benchmark/`，可被现有 `run_eval.py` 加载。
5. [x] **P0 环节遗留：实体解析 er_b06**
   - 片段 + 上下文 rerank 场景应收敛，不应回问。
6. [ ] **交付验证**
   - 当前机器只有 Python 3.9，项目要求 Python >=3.11；本轮先跑可执行的环节评测，最终交付前必须用 3.11 环境跑全量。

---

## 已完成的地基（✅ 可跳过）

- [x] 目录结构创建（runtime / toolkit / agents / api / skills / store / core / prompts）
- [x] 从旧项目迁移可复用模块（core、store、skills/deep-research、evaluation）
- [x] 虚拟环境 + 依赖安装（fastapi/uvicorn/sse-starlette/pydantic）
- [x] Agent Loop / 状态机 / SSE 事件 / 预算 / 熔断 骨架
- [x] Tool 基类 / 注册表 / 执行器 / 权限 骨架
- [x] 子 Agent 抽象 + Spawner
- [x] API 层（health / chat SSE / session / skills）已端到端验证通过
- [x] config.py 修复（settings 单例 + 大小写引用）

---

## P0 — 阻塞项（先做，否则后续模块无法联调）— ✅ 已完成

- [x] **`prompts/constitution.md`** — 编写宪法 prompt（~500 tokens）
  - ✅ 已创建：5 条硬规则红线 + 行为准则 + 优先级顺序
  - 验证：assembler 已能加载进 system prompt
- [x] **`skills/deep-research/SKILL.md`** — 深度研究主工作流
  - ✅ 已创建：6 步总流程 + C级/G5 分支 + 收口强制过硬规则
  - 验证：assembler 已能加载进 system prompt
- [x] **`toolkit/delivery/submit_conclusion.py`** — 硬规则安全门
  - ✅ 已创建：5 条代码级硬规则（R1–R5）+ SubmitConclusionTool + validate_conclusion()
  - 验证：4 个用例（G5买入/C级无验证/合规观望/含目标价）全部符合预期
  - 注：arbitration.py 的"个人方法论优先"逻辑在 R1–R5 之外，后续 P1 迁入 toolkit/delivery

---

## P1 — 让 Agent 真正"动起来" — ✅ 已完成

- [x] **`toolkit/market/tools.py`** — 行情数据封装为 Tool
  - ✅ 已创建：3 个只读 Tool（get_bundle / get_snapshot / get_fundamentals），惰性初始化 MarketData 单例
- [x] **`toolkit/calc/valuation.py`** — 确定性计算工具
  - ✅ 已创建：DCF 估值 / 增长率(CAGR) / 现金跑道，纯函数可单测，已验证数值正确
- [x] **`toolkit/delivery/submit_conclusion.py`** — 硬规则安全门（P0 已完成，此处复用）
- [x] **`toolkit/__init__.py`** — 工具注册入口
  - ✅ register_all() 幂等注册全部 7 个工具；修复 registry.get_all_tools 漏 return bug
- [x] **`core/llm.py` 加 `chat_with_tools`** — function calling 支持
  - ✅ OpenAI/DeepSeek 兼容协议，返回 content/tool_calls/tokens_used
- [x] **`runtime/loop.py` 升级多轮 ReAct** — 从 MVP 单轮到完整循环
  - ✅ 解析 tool_calls → execute_batch → 回灌 observations → 继续循环
  - ✅ 最终回答前过硬规则校验（validate_conclusion），不通过则拦截
- [x] **`agents/masters/duan.md`** — 第一个真实子 Agent（段永平）
  - ✅ 方法论武器 + 交叉质疑义务 + 输出契约 + G5/C 硬规则
- [ ] **`toolkit/web/`** — 搜索 / 抓取工具（search / fetch）— ⏸ 留 P2

---

## P2 — 完善（依赖 P0/P1 跑通）— ✅ 已完成

- [x] **`runtime/router.py`** — 意图识别 + Skill 自动路由
  - ✅ 显式 > 关键词 > 兜底三级路由；5 个 skill 关键词规则，已验证识别准确
- [x] **`store/repos/session_repo.py`** — 会话持久化（中断恢复核心）
  - ✅ SQLite + SessionCheckpoint 表；save/load/delete/list 全部验证通过
- [x] **`evaluation/gate_bypass.py`** — 硬规则攻击回归测试
  - ✅ 19 个攻击用例全部通过（prompt 注入/社会工程/Unicode 谐音/格式伪装）
- [x] **`runtime/reminder.py`** — 矫正层注入（改用 prompts 文件）
  - ✅ orientation_recall.md / anti_bias.md / discipline_recall.md 已创建，assembler 已能加载
- [x] **`runtime/visibility.py`** — SSE 字段过滤（按 Skill 控制可见事件）
  - ✅ 白名单摘要收口 to_public_event；旧 filter_event 白名单簇已删除（主链路未使用）
- [x] **`toolkit/web/tools.py`** — 搜索 / 抓取工具（含 SSRF 防护）
  - ✅ web.search(博查 API，原 DDG) + web.fetch；SSRF 防护 8 个内网用例全部拦截

---

## P3 — 业务 Skill 与子 Agent 扩展

- [ ] `skills/quick-screen/SKILL.md` — 快速筛选
- [ ] `skills/private-company/SKILL.md` — 未上市分析
- [ ] `skills/holdings-track/SKILL.md` — 持仓跟踪
- [ ] `skills/trade-review/SKILL.md` — 交易复盘
- [ ] `agents/masters/` — 巴菲特 / 芒格 / 李录 子 Agent
- [ ] `agents/researchers/` — 6 个研究员（业务解码 / 财务侦探 等）
- [ ] `skills/adapters/berkshire.py` — 外部 skill 能力映射

---

## P4 — 工程化收尾

- [ ] `store/repos/report_repo.py` — 报告存档
- [ ] `store/repos/holding_repo.py` — 持仓存档
- [ ] `store/repos/cognition_repo.py` — 认知库存档
- [ ] `evaluation/routing.py` — 路由准确率测试
- [ ] `prompts/reminders/*` — 全部矫正 prompt 文件
- [ ] `prompts/tone/novice.md` — 语气分层
- [ ] `docs/MIGRATION.md` — 新旧架构迁移报告（已生成草稿，待落盘）

---

## P5 — 取数纪律（硬约束）与数据源抗限流（新增）

- [ ] **硬约束：数值必须先取数后计算，禁止模型凭记忆报数**
  - 规则：遇到任何需要具体数值的指标（营收 / 净利 / ROE / PE / 毛利率 / 市值…），
    模型必须先触发取数动作（金融查询 / 财务工具 / 行情工具），拿到工具返回值后才能引用；
    **禁止直接从参数记忆或训练记忆里报数**。
  - 为什么：这是 Agent 必须触发「金融查询」动作的根本原因。不取数直接报数 = 编造数据，
    会污染下游所有分析（估值、红旗、结论），且不可审计。
  - 实现位置：
    - 提示词层：`prompts/constitution.md` §三「数据可信：禁用『心算』」已有关键条款，
      需强化为「结论中出现的每个数值必须可溯源到工具调用返回值」
    - 代码层：`runtime/loop.py` 最终回答前校验「结论中的数值字段」是否都来自工具观测
      （扩展现有 `validate_conclusion`）
    - 联动：取数层保证「取数动作」成本可控（缓存 + 一次取整表 + 限频），让模型愿意取而不是猜

- [ ] **akshare 限流对策（用户实测：请求两次即被禁）**
  - 事实：akshare 本质是爬东财/新浪网页接口的封装，无请求控制，高频即被封。
  - 对策：
    1. 财报永久缓存：已披露财报不可变 → 首次取数落本地，之后零请求（缓存键 `ticker+表+期间`）
    2. 一次取整表：三表接口一次拉全字段全期间，禁止逐字段请求
    3. 限频令牌桶：全局串行 + 2~3s 间隔
    4. 生产主链路绕开 akshare：快照走东财 push2 原生接口（`EMQuoteProvider` 已实现）、
       财报走 WeStock CLI（`WeStockProvider` 已实现），akshare 降级为兜底
- [ ] **商业模式路由层：去掉 L2 财务指纹判定（已决策）**
  - L2 阈值规则把「财务结构」当「商业模式」，误判率高于覆盖率（SaaS 轻资产高毛利 → 误判 G1a；
    猪周期底部连亏 → 误判 G5），直接砍掉。
  - 修正漏斗：L1 行业表（`bus_router/*.yaml` 的 representative_industries 汇聚 + 交易所行业字段）
    → 未命中 → L3 LLM 兜底（输入：公司名 + 主营构成 + 候选组判别标准 → 输出 group + confidence）
    → 结果写分类缓存表（TTL 90 天，低置信 needs_review）
- [ ] **bus_router 分组内容填充（阻塞项：现 12 个 yaml 除 G1a/core 外全是 G1a 占位副本）**
  - 每个组补：representative_industries（L1 行业表种子）+ metrics + red_flags + valuation
  - 先填 G1b / G5 做范例（估值模型差异最大的三个之一）

# 提示词：
- 1. 区分事实与估算。​ 思考里明确提到"回答里我要把'已披露事实'和'估算值'分开，避免把机构预测或市场测算写成确定事实"。这条规则直接体现在输出结构里——每个指标的表格都有"来源"列，标注"机构预测""金融数据工具""推算""估算值"等不同置信度。
- 2. 硬约束——遇到需要具体数值的指标，必须先取数再计算，禁止模型直接从参数记忆里报数。这也是为什么它会触发"金融查询"动作。
- 3. 要求模型在搜索后做一次"是否需要继续搜"的判断，而不是无脑搜完就答。
- 4. 每个指标都遵循 公式声明 → 参数取值（标注来源）→ 计算 → 通俗解释 的固定模板。





---










> 每完成一个 P0/P1 任务建议跑一次 `curl -N POST /api/v1/chat` 验证联调。
