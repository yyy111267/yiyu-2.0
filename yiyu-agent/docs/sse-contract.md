# SSE 事件契约

> 以渔2.0 前后端对接的单一事实源。
> 端点：`POST /api/v1/chat`（SSE 流式响应，`text/event-stream`）
> 每条 SSE 消息：`event: message\ndata: <json>\n\n`
> data 是 JSON 字符串，结构统一为 `{type, content, metadata}`。
> 最后更新：2026-08-28

---

## 对外可见性红线（本次收口）

用户端 **只收到用户可理解的信息**，以下内容一律不得出现在 SSE 下发中：

- ❌ 原始 `thought`（模型思考文本）
- ❌ Skill ID、路由规则（`matched_by`）、置信度（`confidence`）
- ❌ 工具名、调用参数、原始结果摘要
- ❌ 内部错误码、异常堆栈、模块名（错误只下发固定安全文案）
- ❌ 内部术语（P0 问题、事实包、粒度、画像、adapter 等）

完整路由决策、工具调用、异常细节只进**服务端日志**（受控调试模式扩展点：`runtime/visibility.py::filter_event`）。
下发前统一经过 `runtime/visibility.py::to_public_event` 白名单收口。

## 对外进度模型（三阶段）

前端按固定三阶段渲染「分析进度」，事件只在原位置更新状态、绝不追加技术步骤：

| stage 枚举值 | 用户可见文案 | 说明 |
|---|---|---|
| `understand_question` | 理解研究问题 | 受理 / 路由 / 前处理期间 |
| `evidence_check` | 收集并核验信息 | 循环取数、工具调用期间 |
| `synthesizing` | 整理分析结论 | 答案组织期间 |

前端映射：`understand_question → 0`，`evidence_check → 1`，`synthesizing → 2`；`status ∈ running / done / failed`。

---

## 事件总览（对外白名单）

| type | 阶段 | content | metadata（白名单） | 前端动作 |
|---|---|---|---|---|
| `accepted` | 受理 | "" | `{stage: understand_question, status: running, session_id}` | 阶段1 running |
| `routing` | 路由 | "" | `{stage, status, route_result, suggest_research, session_id}` | 阶段1 running「正在明确分析范围」 |
| `preloop_progress` | 前处理心跳 | "" | `{stage, status: running, elapsed_sec}` | 阶段1 同一行更新「已进行 N 秒」 |
| `preloop` | 前处理完成 | "" | `{stage: understand_question, status: done, p0_questions: [{question, status(中文)}], session_id}` | 阶段1 done + 渲染「本次分析重点」面板 |
| `plan` | 计划装载 | "" | `{stage, status: done, p0_questions}` | 更新「本次分析重点」面板（原地重绘） |
| `progress` | **进度摘要（新）** | "" | `{stage, status, sources?}` | 按阶段推进进度；累积信息来源 |
| `answer_delta` | 流式正文 | 答案片段 | `{}` | 阶段3 running + 流式渲染 |
| `final_answer` | 最终交付 | 研究报告（纯文本） | `{steps_taken, tokens_used, duration_sec, validated, degraded}` | 全阶段 done，进度收起为「✓ 分析完成 · 查看过程」 |
| `ask_confirmation` | 认知确认 | 引导语 | `{cards, source_task_id}` **或** 回问选项 | 认知卡片 |
| `warning` | 降级告警 | 告警文本 | `{kind}`（内部 error/reason 键被剔除） | 进度区单行安全提示 |
| `error` | 异常 | **固定安全文案** | `{stage: done, status: failed}` | 红条兜底 |
| `complete` | 收尾 | — | `{session_id, duration_sec, steps_taken, tokens_used}` | 关闭流、解锁输入 |

**不下发的内部事件**（被 `to_public_event` 归并为 `progress`）：`start` / `thought` / `debug` / `tool_call` / `tool_result` / `spawn_agent` / `agent_done`。

---

## 关键事件详解

### routing

路由判定后立刻发出。对外只携带阶段与路由结果，**不含 skill / matched_by / confidence**。

```json
{
  "type": "routing",
  "content": "",
  "metadata": {
    "stage": "understand_question",
    "status": "running",
    "route_result": "research_task",
    "suggest_research": false,
    "session_id": "sess_xxx"
  }
}
```

**route_result 枚举**：`light_answer` / `research_task` / `out_of_scope`

**out_of_scope 分支**：路由判定为边界外时，**不进入循环也不发 preloop**，直接发一条 `final_answer`（content 为固定话术模板）+ `complete`，前端只渲染不生成。四类话术见 `runtime/boundary.py`：
- `price_prediction` — 不预测涨跌/目标价
- `trade_decision` — 不代决策
- `unsupported_task` — 未交付能力
- `non_research` — 超范围

前端拿到 `route_result == "out_of_scope"` 时直接渲染 final_answer 的话术，**不展开过程流面板**。

### suggest_research 升级

`routing.metadata.suggest_research == true` 表示当前是轻回答，但检测到用户可能想要完整研究。前端在轻回答尾部显示"生成完整研究"按钮，点击后用同一条 message + `skill=deep-research` 重发 `/chat`。

> 注意：当前后端尚未实现"升级时携带已解析标的与已取数据"，前端先做"重发消息"语义，后端补完后无缝升级。这是与 Agent 负责人的协作点。

### preloop / plan（本次分析重点）

前处理完成后发出（仅 `deep-research`）。对外只发**分析重点清单**：问题文本 + 中文状态（待分析 / 分析中 / 已完成 / 已回答 / 无法回答）。内部四件套结构（entity / facts_version / granularity / adapters / skipped）不下发。

```json
{
  "type": "preloop",
  "content": "",
  "metadata": {
    "stage": "understand_question",
    "status": "done",
    "p0_questions": [
      {"question": "当前估值处于历史什么位置？", "status": "待分析"}
    ],
    "session_id": "sess_xxx"
  }
}
```

`plan` 事件同构（计划装载/更新时原地重绘面板）。

### progress（进度摘要）

内部过程事件的统一对外形态。心跳式下发，前端同一行更新：

```json
{
  "type": "progress",
  "content": "",
  "metadata": {"stage": "evidence_check", "status": "running"}
}
```

`sources`（可选）：工具调用产出的可引用来源，格式为字符串或 `{title, url}` 数组，仅用于「信息来源」区，帮助用户验证结论。

### final_answer

最终交付。content 是纯文本（前端按 pre-wrap 渲染）。metadata：

```json
{
  "steps_taken": 12,
  "tokens_used": 8500,
  "duration_sec": 45.2,
  "validated": true,
  "degraded": false
}
```

- `validated=true` 表示通过硬规则准出（R1–R5）
- `degraded=true` 表示预算耗尽强制降级输出（配合 `warning` 事件）

### error（安全文案）

**content 恒为固定安全文案**，不含任何内部错误码、异常类名或堆栈。原始异常仅记录在服务端日志。

```json
{
  "type": "error",
  "content": "研究过程出现异常，已停止。可稍后重试，或把问题缩小为一个维度。",
  "metadata": {"stage": "done", "status": "failed"}
}
```

### ask_confirmation（认知卡片）

**仅 `deep-research` skill 且抽取到候选认知时发出**，紧跟 `final_answer` 之后。这是 PRD 核心交互：研究交付后只提出候选卡，**绝不自动写入认知库**，须用户确认。

```json
{
  "type": "ask_confirmation",
  "content": "研究已完成。以下是候选认知，请确认、编辑后确认或拒绝；未确认不会入库。",
  "metadata": {
    "cards": [
      {
        "card_id": "sess_xxx:memory:1",
        "candidate": {
          "type": "preference | general | company",
          "statement": "核心主张（4~40字，不含公司名）",
          "content": "正文/理由",
          "symbol": "600519.SH",
          "scope_note": "适用条件（可选）",
          "verification_status": "needs_recheck",
          "source_text": "用户原话或研究结论片段",
          "company_name": "贵州茅台"
        }
      }
    ],
    "source_task_id": "sess_xxx"
  }
}
```

前端动作：
1. 渲染卡片列表（每张：statement 可编辑、content 可编辑、symbol/scope_note 只读）
2. 每张三按钮：**确认入库** / **编辑后入库** / **拒绝**
3. 调 `POST /api/v1/memory/confirm`，body：`{card_id, action: "confirm"|"edit", candidate: {...}}`
4. 入库成功后该卡片淡出，全部处理完后可关闭面板

> `ask_confirmation` 事件的**第二种用途**是回问（`need_input`），当前后端回问机制未完全接线（intervention_log 未做），前端先做文本兜底渲染，后端补完后点亮选项卡片。两种用途靠 metadata 是否含 `cards` 字段区分。

### complete

收尾事件，标志 SSE 流结束。前端关闭流、解锁输入框、保存 session_id。

```json
{
  "type": "complete",
  "content": "",
  "metadata": {
    "session_id": "sess_xxx",
    "duration_sec": 45.2,
    "steps_taken": 12,
    "tokens_used": 8500
  }
}
```

---

## 事件顺序示例（完整研究流程）

```
accepted          (阶段1 running)
routing           (阶段1 running，route_result=research_task)
preloop_progress  (阶段1 心跳 × N)
preloop           (阶段1 done + 分析重点清单)
progress          (阶段2 running，循环取数期间多次)
answer_delta      (阶段3 running + 流式正文 × N)
final_answer      (全部完成，进度收起)
ask_confirmation  (认知候选卡片)
complete          (收尾)
```

**轻回答流程**：`routing` → `progress`（预算 ≤5 的内部事件归并）→ `final_answer` → `complete`。前端在 `final_answer` 尾部按 `suggest_research` 决定是否显示升级按钮。

**边界外流程**：`routing`（route_result=out_of_scope）→ `final_answer`（固定话术）→ `complete`。

---

## 数据接口（非 SSE）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/memory/me` | 拉取当前用户认知库列表（M1A 改造后，替换 `/memory/users/{user_id}`） |
| POST | `/api/v1/memory/confirm` | 确认/编辑/拒绝认知卡片 |
| PATCH | `/api/v1/memory/{id}` | 编辑/归档认知条目（M0.4 补） |
| DELETE | `/api/v1/memory/{id}` | 删除认知条目（M0.4 补） |
| GET | `/api/v1/sessions` | 历史会话列表（按 user_id 过滤） |
| GET | `/api/v1/sessions/{session_id}` | 拉取会话状态快照（跨会话恢复） |
| GET | `/api/v1/skills` | 可用 skill 列表 |
| GET | `/health` / `/ready` | 健康检查 |

---

## 鉴权（M1A 落地后）

所有受保护接口需 `Authorization: Bearer <token>` 头。token 由 `POST /auth/verify` 签发。`/chat` 的 `user_id` 不再由客户端传，改从 token 推导。

---

## 验收清单（每次改 SSE 相关代码自查）

- [ ] 页面中不出现 `deep-research`、`rule`、`matched_by`、工具名或参数
- [ ] 不展示模型原始思考文本
- [ ] 等待再久也最多维持三个进度阶段
- [ ] 同一阶段更新而不是重复追加
- [ ] 异常和超时不泄露堆栈、模块名或内部策略
- [ ] 用户仍能知道系统在理解问题、核验资料还是整理结论
