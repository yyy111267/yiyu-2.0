# Fetch Executor：把取数计划变成可用数据

## 产品定位

Fetch Planner 回答“这一轮应该怎么取”，Fetch Executor 负责“现在按计划取，并把结果交付出来”。

它的价值不是再增加一层调用，而是把免费数据源的不稳定性管住：

1. 只执行 Planner 判定缺失的字段。
2. 主源成功后立即停止，不再打备用源。
3. 一个批次只成功了部分字段时，只降级查剩余字段。
4. 接口返回不等于成功；空值、错误类型、非有限数和错误报告期都会被拒绝。
5. 每一步都有硬超时，不让一个卡住的免费接口拖住整轮回答。
6. 所有尝试都留下成功、空结果或失败原因，方便解释和排查。

## 一轮执行的例子

Planner 要求获取 `price` 和 `net_profit_parent`：

```text
price: Sina → Tencent
net_profit_parent: Eastmoney → AKShare/Sina → WeStock
```

Executor 的行为是：

```text
Sina 返回 price
  → price 成功，不再调 Tencent

Eastmoney 返回 net_profit_parent
  → 校验数值和报告期
  → 成功，不再调 AKShare/Sina 和 WeStock
```

如果东财返回了表，但目标列为空，Executor 会把这次记为 `empty`，然后继续备用源。

## 结果状态

| 状态 | 产品含义 | Agent 后续动作 |
|---|---|---|
| `ok` | 本轮需求全部满足 | 直接使用 Data Pack |
| `partial` | 只取到部分字段 | 使用已有字段，对缺口决定是否搜索 |
| `unregistered` | 字段不在 Field Registry | 结构化底座不承诺，可转联网搜索 |
| `not_supported` | 字段已注册，但当前市场或时间没有可用路由 | 可转联网搜索 |
| `request_failed` | 有结构化路由，但本轮全部请求失败 | 可根据时效重试，或转联网搜索 |

## 实现分工

- `fetch_executor.py`：处理步骤顺序、字段级降级、校验、超时和状态归类。
- `route_provider.py`：执行 Source Mapping 中已验证的精确接口配方，将原始列转成标准字段。
- `market.py`：把执行结果写成 Snapshot、Fundamentals、Evidence 和完整的尝试轨迹。
- `research_data.py`：将本轮新结果与当前会话 Data Pack 合并，产生新版本。

Executor 不自己决定去哪里取，不自己改变字段口径，也不自己启动联网搜索。路由来自 Source Mapping，当前轮次的取舍来自 Planner，搜索决定留给 Agent。
