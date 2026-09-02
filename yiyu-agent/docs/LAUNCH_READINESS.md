# 上线就绪度（2026-08-27）

## 当前结论

项目已具备离线回归、API 启动和生产部署配置的基础，但在真实 LLM 端到端路测通过前，不应直接公网全量上线。

## 已验证

- Python 测试：79 passed。
- 环节评测：96 passed / 22 skipped / 0 failed。
- 硬规则与攻击回归：22 passed。
- 质量样本回归：6/6 passed。
- API：`/health`、`/ready`、skills 清单/详情可用；chat 未授权返回 401。
- 失败结论不再绕过安全门降级输出。
- 兼容将工具调用 JSON 放在 `content` 的 OpenAI 兼容模型，且只接受本轮已授权工具。
- Docker 持久化目录已收敛到 `/app/data`，避免数据卷覆盖应用代码。

## 必须在发布前完成

1. 在允许访问已配置 LLM 端点的环境运行：

   ```bash
   .venv/bin/python -u scripts/llm_probe.py
   E2E_MODE=loop USE_REAL_MARKET=0 .venv/bin/python evaluation/e2e/scripts/e2e_hy3.py
   .venv/bin/python -m evaluation.e2e.scripts.run_eval --no-store
   ```

   请直接运行或使用 `tee`，不要在运行中管道到 `tail -40`；普通
   `tail -40` 会等待上游结束，看不到实时心跳与错误。

   两条命令都必须以退出码 0 结束；降级结论、空 trace 或未通过硬规则不算通过。

2. 复制 `.env.prod.example` 为 `.env.prod`，替换 LLM、SMTP、CORS 和域名占位值。`IC_ENV=prod` 会在配置不完整时拒绝启动。
3. 在安装 Docker 的发布机验证：

   ```bash
   docker compose config
   docker compose build
   docker compose up -d
   curl -f https://<domain>/ready
   ```

4. 先做小流量灰度，观测 LLM 失败率、平均耗时、降级率、单次 token 和工具失败率。

## 已知非阻塞/未交付能力

- `03_classify` 的 5 条 live 用例全跳过，不能计为已验收。
- quick-screen、private-company、holdings-track、trade-review 在 `/skills` 中会显示 `available=false`，不应在前端开放。
- 完整的三级记忆抽象和通用冲突仲裁仍有占位实现；当前上线范围应限于已接线的 cognition store 与个人底线闸门。
- 本地环境没有 Docker，因此未在本机完成镜像构建验证。
