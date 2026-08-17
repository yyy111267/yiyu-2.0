# 以渔2.0 — 多智能体投资研究系统

> 📄 需求文档（飞书，含高亮块等富文本格式）：https://mxvwlx30e2n.feishu.cn/wiki/ETkkwLDY8igHStkwQUlcTx6Ynmx

> 一句话定位：给定一个投资标的，Agent 在明确的知识与规则边界内，自主完成一次**可解释、可追溯、会暴露不确定性**的投资研究，并在过程中轻量陪伴用户建立自己的投资认知（投研认知陪练）。

---

## 当前完成情况（2026-08-01）

### ✅ 已完成：Agent 核心引擎

| 能力 | 说明 |
|------|------|
| **ReAct Agent Loop** | `runtime/loop.py`：感知→推理→行动→观察的完整循环；多轮工具调用、观测回灌、最终回答前硬规则校验 |
| **Tool Calling** | `core/llm.py` `chat_with_tools`：OpenAI/DeepSeek 兼容 function calling，返回 content / tool_calls / tokens_used |
| **工具系统** | `toolkit/`：注册表 + 执行器 + 权限控制，已注册 **20+ 工具**（详见下表） |
| **子 Agent 抽象** | `agents/base.py`：AgentSpawner 骨架（四大师并行派发为 P3 待办，当前由主 LLM 分饰多角） |
| **SSE 流式输出** | `api/routes/chat.py`：思考过程实时推给前端，事件可见性按 Skill 控制 |
| **显式状态机** | `runtime/state.py`：会话状态显式管理 |
| **预算与熔断** | `runtime/budget.py` + `runtime/breaker.py`：token/工具调用预算控制，防资源耗尽 |

### ✅ 已完成：业务能力

| 模块 | 说明 |
|------|------|
| **商业模式路由 `bus_router/`** | 按行业分组（core 底座 + G1a/G1b/G2a-d/G3/G4/G5/G6），每组含指标解读协议、红旗、估值范式；`classifier_sotp_rules.yaml` 分类规则；**8 组 base_pack 真实数据端到端已打通（66 OK / 19 NC）** |
| **确定性计算 `calc/`** | `base_pack`（商业模式地基指标自动计算：classify→取数→冻结函数→档位）、`valuation`（DCF/CAGR/现金跑道）、`run_code`（断网计算沙箱） |
| **数据层 `market/`** | 东财 push2 原生接口 + WeStock CLI 主链路，akshare 兜底；行情快照、财务摘要、多源路由 |
| **Web 检索 `web/`** | DuckDuckGo 搜索 + 网页抓取，含 **SSRF 防护**（8 个内网用例全部拦截） |
| **认知陪练 RAG `cognition/`** | 双路检索（默认框架 + 用户个人认知库）+ 认知原子抽取，落库待用户确认 |
| **实体识别 `entity/`** | 实体消歧（代码/公司名/自然语言）→ 标准化 symbol + 商业模式分类 |
| **三级记忆 `store/`** | 工作记忆（TTL 24h）/ 情景记忆 / 用户画像 / 个人方法论库，`MemoryHub` 装配 |
| **会话持久化** | SQLite `SessionCheckpoint`：save/load/delete/list，支持中断恢复 |
| **上下文压缩** | `runtime/compactor.py`：阈值触发 + 保留近 5 条原文，防长研究溢出 |
| **矫正层注入** | `prompts/reminders/`：orientation_recall / anti_bias / discipline_recall 三份矫正提示 |
| **意图路由** | `runtime/router.py`：显式 > 关键词 > 兜底三级路由，5 个 skill 关键词规则 |

### ✅ 已完成：安全与质量防线

- **硬规则安全门**（`toolkit/delivery/submit_conclusion.py`）：5 条代码级硬规则 R1–R5，结论必须过校验才能输出；`delivery.finish` 显式收尾 + 信息充分性闸门
- **宪法 Prompt**（`prompts/constitution.md`）：六节硬约束（合规免责 / 数据可信禁心算 / 先取证后结论 / 引用可审计 / 镜子测试）
- **数据可得性清单**（`bus_router/data.md`）：direct / derive / proxy / unavailable / text 五级标记 + 降级阶梯，**禁止编数**
- **评测体系 `evaluation/`**：
  - 硬规则回归 `test_hard_rules.py`
  - 安全攻击回归 `gate_bypass.py`（19 个用例：prompt 注入 / 社会工程 / Unicode 谐音 / 格式伪装）
  - 回答质量评测集（18 个核心用例，live/offline 双模式）
  - 用户侧质量打分 `quality.py`（10 项指标 100 分制，纯规则零成本）
  - Badcase 回流入库，支持复盘迭代

### ✅ 已完成：产品与工程

- 产品设计文档：`单标的研究Agent产品设计.md`（PRD，九步主流程 + 验收标准）
- Skills 清单：`skills/manifest.json` 注册 6 个 skill（deep-research / knowledge_qa / quick-screen / private-company / holdings-track / trade-review）+ sotp-multi-business
- API 层：`/health`、`/api/v1/chat`（SSE）、`/session`、`/skills`
- `pyproject.toml`：Python 3.11+，FastAPI / pydantic v2 / aiosqlite / httpx / openai

---

## Agent 技术栈

```
用户请求 → API (SSE) → 意图路由 → Skill 自动路由 → Agent Loop (ReAct)
          → LLM + Tools（取数/计算/检索/落库）+ SubAgents → 硬规则校验 → SSE 事件流
```

**用到的 Agent 技术：**

1. **ReAct 循环**：`runtime/loop.py` — 推理（Reasoning）与行动（Acting）交替，工具观测回灌上下文
2. **Tool Calling / Function Calling**：LLM 自主选择工具并填充参数，多工具批处理
3. **多 Agent 编排**：子 Agent 抽象 + Spawner（并行派发 P3 落地）；当前四大师视角（巴菲特 / 芒格 / 格雷厄姆 / 费雪）由主 LLM 依据 Evidence Pack 统一综合
4. **Prompt 工程**：`runtime/assembler.py` 六步组装（宪法 + Skill 工作流 + 矫正提示 + 语气分层），模板变量注入
5. **RAG / 记忆检索**：认知库向量检索（默认框架 vs 个人框架双路）、三级记忆（working / episodic / profile / methodology）
6. **确定性增强（Tool-Augmented）**：关键指标走**冻结函数**（口径写死防漂移），LLM 只负责解读档位，禁止心算报数
7. **安全对齐**：宪法层 + 代码级硬规则 + 权限分离（读放行/写确认）+ 熔断器 + SSRF 防护
8. **上下文管理**：compactor 压缩 + 预算控制 + SSE 可见性过滤 + 敏感字段脱敏
9. **评测驱动**：四层评测（硬规则 / 安全攻击 / 回答质量 / 用户侧质量）+ Badcase 回流闭环

---

## 目录结构

```
yiyu-agent/
├── api/               FastAPI 层（main / routes: chat, health, session, skills）
├── runtime/           运行时（loop / state / router / assembler / budget / breaker / compactor / visibility / planner / events）
├── toolkit/           工具层（registry / executor / permission + delivery / market / calc / web / entity / cognition）
├── agents/            子 Agent 抽象（base.py + researchers 骨架）
├── skills/            业务 Skill 定义（deep-research / sotp-multi-business / manifest.json）
├── bus_router/        商业模式路由（core + G1~G6 分组 yaml + 分类规则 + 数据可得性清单）
├── store/             持久化（SQLite：会话 / 认知库 / 记忆 / badcase 回流）
├── prompts/           提示词资产（constitution / reminders / tone）
├── evaluation/        评测（硬规则 / 安全攻击 / 回答质量 / 用户侧质量）
├── core/              基础（config / llm / persona / arbitration）
├── scripts/           诊断与联调脚本（e2e_base_pack / smoke_all_groups 等）
├── data/              本地数据（entity_index.db / market_cache.db）
├── 单标的研究Agent产品设计.md   PRD
└── todo.md             开发任务清单
```

---

## 快速开始

```bash
cd yiyu-agent
pip install -e ".[dev]"

cp .env.example .env        # 填入 LLM API Key
python -m api.main          # 或 uvicorn api.main:app --reload --port 8000
```

验证：

```bash
curl http://localhost:8000/health

# SSE 对话
curl -N -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "研究一下贵州茅台"}'
```

---

## 测试与评测

```bash
# 一键全量评测（硬规则 + 安全攻击 + 评测集 live）
python -m evaluation.run_all

# 只跑硬规则回归
python evaluation/test_hard_rules.py

# 只跑安全攻击回归
python evaluation/gate_bypass.py

# 回答质量评测集（live / offline）
python -m evaluation.run_eval
python -m evaluation.run_eval --offline --conclusion "结论文本"

# 用户侧质量打分（100 分制）
python -m evaluation.run_quality --conclusion "结论文本……"

# 端到端 base_pack 冒烟
python scripts/e2e_base_pack.py
```

---

## 待办路线图

### P3 — 业务 Skill 与子 Agent 扩展（进行中）
- [ ] `quick-screen` / `private-company` / `holdings-track` / `trade-review` 的 SKILL.md 补齐
- [ ] **四大师子 Agent**（巴菲特 / 芒格 / 李录）+ `agents/masters/` 方法论文档，`loop.py` 并行派发逻辑
- [ ] 6 个研究员子 Agent（业务解码 / 财务侦探 等）
- [ ] `skills/adapters/berkshire.py` 外部 skill 能力映射

### P4 — 工程化收尾
- [ ] `report_repo` / `holding_repo` / `cognition_repo` 存档落库
- [ ] 路由准确率测试 `evaluation/routing.py`
- [ ] 矫正 prompt / 语气分层补全（novice 等）
- [ ] `docs/MIGRATION.md` 迁移报告落盘

### P5 — 取数纪律与数据源抗限流
- [ ] **取数纪律强化**：结论中每个数值必须可溯源到工具调用返回值（扩展 `validate_conclusion`）
- [ ] **akshare 限流对策**：财报永久缓存（已披露不可变）+ 一次取整表 + 限频令牌桶；生产主链路走东财 push2 / WeStock，akshare 降级兜底
- [ ] **商业模式路由去 L2 财务指纹判定**：改 L1 行业表 → L3 LLM 兜底漏斗，结果写分类缓存表（TTL 90 天）
- [ ] **bus_router 分组内容填充**：补齐各组 representative_industries / metrics / red_flags / valuation（先填 G1b / G5 作范例）

### 链路遗留问题（见 `bus_router/linkage_todo.md`）
- 字段缺口：`market.get_bundle` 仅 7 个字段，base_pack 需 24 个 → 部分指标 NC，需扩财报 provider
- 编排顺序仅靠 SKILL.md 指令约束，需 `loop.py` 增加 `skill_phase` 状态机强制
- `calc.run_code` 依赖 `var/data_pack.json`，暂无写入方（非主链路阻塞）

---

## License

MIT License
