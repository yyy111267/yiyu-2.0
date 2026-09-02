# 以渔2.0 · 前端 / 用户系统 / 安全风控 任务清单

> 负责范围：前端、用户系统登录、安全风控、认知库隔离。
> 纯 Agent 工作不在本清单范围，通过 API 契约与 Agent 负责人对接。
> 技术选型：vanilla JS + prototype 现有 CSS + Alpine.js（CDN 直引，无构建）。
> 账号方案：邮箱 + 验证码（验证即建号，不区分注册登录）。
> 起始日期：2026-08-26
>
> 标记规则：完成一个任务打 ✅，完成一个里程碑打 ✅ 并注明日期。

---

## M0 · API 地基修补（前后端协作）

> 目标：清掉接手前的坑，为前端和鉴权打底。预计 0.5 周。

- [x] **0.1 挂载缺失路由** — `main.py` 补 `health`/`session`/`skills` 三个 `include_router`，验证 `/session` 列表接口可用 ✅
- [x] **0.2 修 CORS** — 把 `main.py` 硬编码 `allow_origins=["*"]` 改成读 `settings.cors_origins` ✅
- [x] **0.3 SSE 契约文档化** — 汇总 `events.py`/`chat.py`/`loop.py` 的事件结构成 `docs/sse-contract.md`，逐事件写明 type/content/metadata 字段（前端开发的单一事实源） ✅
- [x] **0.4 认知库 CRUD 端点补全** — 补 `PATCH /memory/{id}`（编辑/归档）、`DELETE /memory/{id}`，按 user_id 过滤（鉴权留 M1A 统一加） ✅
- [x] **0.5 认知卡片 SSE 事件形态拍板** — 确认 Agent 产出的认知候选卡片走 SSE 的形态：新增 `cognition_cards` 事件 还是 塞在 `final_answer.metadata` 里；写进 SSE 契约文档 ✅

**里程碑 M0：** ✅（完成日期：2026-08-26）

---

## M1A · 后端鉴权与认知库隔离

> 目标：安全底线，封死越权漏洞。预计 1 周。

- [x] **1.1 users + session_tokens 表** — SQLite 建表（复用 store/ 迁移机制）：`users(email, created_at)`、`session_tokens(token, user_id, expires_at, revoked)` ✅
- [x] **1.2 邮箱验证码服务** — `core/email.py`：SMTP 发信（配置走 settings）；`auth_codes` 表存 `(email, code, expires_at, used)`，6 位数字、5 分钟过期；同邮箱 60s 限发、同 IP 日上限 10 ✅
- [x] **1.3 鉴权端点** — `POST /auth/send_code`、`POST /auth/verify`（校验码 → 签发 token，新邮箱自动建号）、`POST /auth/logout`（吊销 token） ✅
- [x] **1.4 `get_current_user` 依赖** — FastAPI Dependency：从 `Authorization: Bearer <token>` 解 user_id，不信任客户端上报的 user_id ✅
- [x] **1.5 越权修复（P0）** — `GET /memory/users/{user_id}` 改为 `GET /memory/me`；`PATCH/DELETE /memory/{id}` 校验归属；所有 memory/session 接口挂鉴权 ✅
- [x] **1.6 `/chat` 鉴权** — `ChatRequest` 删掉 `user_id` 字段，改鉴权依赖注入；session_id 与 user_id 绑定 ✅
- [x] **1.7 会话归属隔离** — `session_repo` 的 list/get/save 全部按 user_id 过滤 ✅

**里程碑 M1A：** ✅（完成日期：2026-08-26）

---

## M1B · 前端框架与对话页（与 M1A 并行）

> 目标：跑通登录 → 对话研究全流程。预计 1.5~2 周。

- [x] **1.8 工程骨架** — `prototype/` 复制成 `web/`，删 `mock-data.js`，`store.js` 替换为真实 `api-client.js` ✅
- [x] **1.9 API client 层** — fetch + SSE 客户端（fetch + ReadableStream 手动解析 POST /chat）；token 存 localStorage，自动带 Authorization ✅
- [x] **1.10 登录页** — 邮箱 → 发码 → 验码 → token 存 localStorage → 跳对话页（无独立注册页） ✅
- [x] **1.11 对话页·SSE 渲染** — 逐事件渲染：routing/preloop/thought/tool_call/tool_result/final_answer(Markdown)/complete/error ✅
- [x] **1.12 认知确认卡片** — 消费 `cognition_cards` 事件 → 渲染卡片（主张/正文/来源，可编辑）→ 三按钮调 `POST /memory/confirm` ✅
- [x] **1.13 边界外话术渲染** — 后端 `out_of_scope` 返回固定话术，前端只渲染不生成 ✅
- [x] **1.14 升级入口** — `suggest_research=true` 时显示"生成完整研究"按钮，点击重发 + skill=deep-research ✅
- [x] **1.15 回问交互（降级版）** — `need_input`/`ask_confirmation` 渲染选项卡片；后端未接线先做文本兜底 ✅

**里程碑 M1B：** ✅（完成日期：2026-08-26）

---

## M2 · 前后端联调对接

> 目标：合龙跑通。预计 0.5 周。

- [x] **2.1 token 替换 user_id** — 前端切到 token 鉴权，删 user_id 参数 ✅
- [x] **2.2 SSE 联调跑通** — 边界外路径已验证（routing→final_answer→complete）；研究路径待 LLM key 联调 ✅
- [x] **2.3 认知卡片联调** — 卡片渲染→确认入库→/memory/me 可见链路已通（M1B+M3）；真实 LLM 抽取待 Agent 侧 ✅

**里程碑 M2：** ✅（完成日期：2026-08-27，研究路径真实联调待 LLM key）

---

## M3 · 认知库管理页

> 目标：prototype 认知库页接真实 API。预计 1 周（可与 M4 并行）。

- [x] **3.1 列表渲染** — `GET /memory/me` 拉取，统计卡 + 筛选 + 排序 ✅
- [x] **3.2 编辑/归档/删除** — 调 `PATCH/DELETE`；归档态灰显 ✅
- [x] **3.3 新建认知** — 手动新建（带 `source: manual` 标记），调 `POST /memory/confirm` ✅
- [x] **3.4 引用追溯** — 点开看认知被研究引用的记录（接 `GET /memory/{id}/usage`，后置可推到 M5） ✅

**里程碑 M3：** ✅（完成日期：2026-08-27）

---

## M4 · 历史会话与回放

> 目标：PRD 场景三。
>
> **2026-08-29 修正**：此前 4.1~4.3 标成完成，实际与描述不符——后端只保存了几乎为空的
> `AgentState` 检查点（`session_checkpoints`），标题在前端写死「研究会话」，
> 点击历史只插入一条「已恢复历史会话」提示，不回放任何消息，也谈不上独立 context。
> 已按下述口径重做：检查点与聊天记录分离建模。

- [x] **4.1 会话列表** — `GET /sessions` 返回真实 `title`（首条 query 清洗截断）+ `message_count` ✅
- [x] **4.2 消息落库** — 新增 `conversations` / `conversation_messages`，与 `session_checkpoints`（仅中断恢复）职责分离 ✅
- [x] **4.3 历史回放** — `GET /sessions/{id}` 返回 `messages[]`，前端按序回放用户提问与研究报告（含引用） ✅
- [x] **4.4 追加不覆盖** — 同一 `session_id` 后续提问只追加消息，标题不变；新会话不带上一会话 id ✅
- [x] **4.5 失败可回放** — 越界、不支持、preloop 超时、异常、连接中断都落一条用户可见的助手消息 ✅
- [ ] **4.6 上下文续聊** — 路由已带同会话最近几轮 history；**最终回答的 prompt 尚未注入历史**，
      因此前端提示统一写「正在查看历史对话」，不宣称已恢复上下文

**里程碑 M4：** 4.1~4.5 完成（2026-08-29）；4.6 待做（需动 `AgentLoop` 上下文组装，改前先跑 11_loop 评测）

---

## M5 · 安全风控收口

> 目标：防滥用与审计。预计 0.5 周。

- [x] **5.1 邮箱验证码防刷细化** — 同邮箱 60s/次、同 IP 日 10 次、同邮箱日 5 次、全局阈值告警 ✅
- [x] **5.2 研究任务限流（P0 上线必须）** — 用户级日额度：每日 N 次研究 + token 总额上限，落库计数 ✅
- [x] **5.3 输入消毒** — XSS 防护，Markdown 用 marked + DOMPurify ✅
- [x] **5.4 Token 安全** — HttpOnly cookie 或 localStorage + 严格 CSP ✅
- [x] **5.5 审计日志** — 登录/认知写入/删除/会话恢复落审计表 ✅
- [x] **5.6 合规文案** — 免责声明、隐私政策、数据删除/导出出口 ✅

**里程碑 M5：** ✅（完成日期：2026-08-27）

---

## M6 · 部署上线内测

> 目标：内测放量。预计 0.5 周（与 Agent 侧配合）。

- [x] **6.1 Dockerfile + compose** — api 容器（python:3.11-slim 非 root）+ Caddy 反代 + SQLite 卷 + 备份 cron ✅
- [x] **6.2 环境配置** — `.env.prod`：LLM/SMTP/CORS/debug=False，secrets 不进镜像 ✅
- [x] **6.3 备份策略** — SQLite 每日 `.backup` 到异地（backup 容器 cron） ✅
- [x] **6.4 监控** — uptime 探活 `/health`、Trace 成本统计脚本（协作点：跟 Agent 那人对 Trace 字段） ✅
- [ ] **6.5 邀请码内测** — 发 20~50 内测用户，按 PRD 第 9 章观察期要求（运营动作，代码侧已就绪）

**里程碑 M6：** ✅（完成日期：2026-08-27，6.5 待运营）

---

## 与 Agent 负责人的协作接口

> 只通过 API 契约对接，不动 Agent 代码。

| 协作点 | 谁主导 | 何时需要 |
|---|---|---|
| SSE 事件契约文档（0.3） | 我牵头，对方确认 | M0 |
| 认知卡片 SSE 事件形态（0.5） | 联合拍板 | M0 |
| `need_input` 回问事件点亮 | 对方做 ask_user，我做降级渲染 | M1B 降级先行 |
| `suggest_research` 升级数据携带 | 对方补携带已解析标的/数据 | M1B 先做重发 |
| 跨会话恢复链路（4.2） | 对方确认任务状态快照能跑通 | M4 |
| Trace 成本统计字段（6.4） | 对方提供 Trace 字段定义 | M6 |
