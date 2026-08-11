# ============================================
# 以渔2.0 - 多智能体投资研究系统
# ============================================

## 项目简介

以渔2.0是一个**标准的多智能体（Multi-Agent）投资研究系统**，基于 ReAct 循环架构，
实现了自主推理、工具调用、子 Agent 协作等核心能力。

### 核心特性

- ✅ **Agent Loop**: 标准 ReAct 循环（感知→推理→行动→观察）
- ✅ **Tool Calling**: LLM 自主选择和调用工具
- ✅ **Multi-Agent**: 四大师并行分析（段永平、巴菲特、芒格、李录）
- ✅ **SSE 流式输出**: 实时推送思考过程给前端
- ✅ **硬规则安全门**: 三条不可绕过的投资纪律约束
- ✅ **三级记忆系统**: 工作记忆 + 情景记忆 + 用户画像
- ✅ **熔断器保护**: 防止服务雪崩和资源耗尽

---

## 技术架构

```
用户请求 → API (SSE) → Agent Loop → LLM + Tools + SubAgents → SSE 事件流
```

### 核心模块

| 模块 | 职责 |
|------|------|
| `runtime/loop.py` | Agent 主循环（ReAct）|
| `runtime/state.py` | 显式状态机管理 |
| `runtime/assembler.py` | Prompt 六步组装 |
| `toolkit/` | 工具注册、执行、权限控制 |
| `agents/` | 子 Agent 系统（四大师等）|
| `skills/` | 业务技能定义（SKILL.md）|
| `store/` | 持久化存储（会话、报告、方法库）|

---

## 快速开始

### 1. 安装依赖

```bash
cd yiyu-agent
pip install -e ".[dev]"
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入你的 API Key
```

### 3. 启动服务

```bash
# 开发模式
python -m api.main

# 或使用 uvicorn
uvicorn api.main:app --reload --port 8000
```

### 4. 测试接口

```bash
# 健康检查
curl http://localhost:8000/health

# SSE 对话测试
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好"}'
```

---

## 开发指南

### 项目结构说明

详见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)

### 迁移计划

本项目从 `back-invest-coach` 迁移而来，复用率约 87%。

迁移进度：
- [x] Phase 1: 基础设施搭建（骨架已完成）
- [ ] Phase 2: 接入工具层
- [ ] Phase 3: 接入业务 Skills
- [ ] Phase 4: 工程化完善

---

## 测试

```bash
# 运行所有测试
pytest tests/

# 运行硬规则回归测试
python evaluation/test_hard_rules.py

# 运行安全攻击测试
python evaluation/gate_bypass.py
```

---

## 安全机制

系统实现了三层安全防护：

1. **硬规则拦截**: 提交结论前强制校验三条投资纪律
2. **权限分离**: 读操作自动放行，写操作需确认
3. **熔断保护**: 防止恶意调用导致资源耗尽

详见 `evaluation/gate_bypass.py` 的安全测试用例。

---

## License

MIT License
