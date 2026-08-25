# 环节级评测规范（评测驱动开发）

> 宗旨：**评测先行**。改造任何环节之前，先让该环节的评测集就位。
> 失败清单 = 改造 backlog；全绿 = 环节验收通过 + 回归保障。

## 三条铁律

1. **一个环节一个目录**：`cases.yaml`（用例）+ `run.py`（入口），别无他物
2. **一条命令**：`python evaluation/stage/<环节>/run.py`，stdout 即报告，退出码即结果（`0` 全过 / `1` 有失败 / `2` 全跳过）
3. **不做平台**：不入库、不出 JSON 报告、不搞统一调度——就是「输入 → 期望输出 → 对比脚本」，自己跑、自己看、自己修

## 目录结构

```
evaluation/stage/
├── README.md                 本规范
├── common.py                 公共层：用例加载 + 断言引擎 + 运行汇总（~200行，别再长）
├── 01_intent/                5.1  意图识别与路由        → runtime/router.py
├── 02_entity/                5.2  实体解析              → toolkit/entity/resolver.py
├── 03_classify/              5.3  商业模式初判与分类    → toolkit/entity/classify.py
├── 04_granularity/           5.3  环节② 研究粒度决策   → runtime/granularity.py（待建）
├── 05_plan/                  5.3  环节④ 研究计划生成   → runtime/plan.py（待建）
├── 06_metrics/               第6章 指标字典            → bus_router/formulas_core.py
├── 07_cognition/             记忆管理·认知抽取         → store/cognition_store.py
└── 08_sufficiency/           5.5  硬规则/引用核对      → toolkit/delivery/submit_conclusion.py
```

端到端评测不在此重复建设——环节全绿后跑现有的 `evaluation/run_eval.py` 整链路回归。

## 用例格式（cases.yaml）

顶层：

| 字段 | 必填 | 说明 |
|---|---|---|
| `stage` | ✓ | 环节标识，与目录名一致 |
| `name` | ✓ | 环节中文名 |
| `target` | ✓ | 被测对象（文档性质，写清 PRD 章节 → 代码位置） |
| `status` | ✓ | `ready`（实现已存在）/ `pending`（目标实现待建，用例先就位） |
| `cases` | ✓ | 用例列表 |

每条用例：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✓ | 全局唯一，建议「环节前缀_序号」（如 `ir_001`） |
| `name` | ✓ | 一句话说明这条用例验收什么 |
| `live` | | `true` = 依赖联网/LLM；默认 `false`（离线可跑） |
| `input` | ✓ | 环节输入。**字段由各环节自定义**，规范只管顶层结构 |
| `expect.assertions` | ✓ | 断言列表（见下） |
| `note` | | 补充说明：为什么期望这个（写 PRD 依据，方便回溯） |

示例（节选自 `01_intent/cases.yaml`）：

```yaml
stage: 01_intent
name: 意图识别与路由
target: PRD 5.1 → runtime/router.py
status: ready
cases:
  - id: ir_001
    name: 研究指令+公司名 → 研究任务
    input:
      message: "帮我研究下兆易创新"
    expect:
      assertions:
        - path: route
          op: eq
          value: research_task
    note: PRD 5.1 规则快速通道：明确研究指令 + 一个公司 → 研究任务
```

## 断言一览

每条断言 = `{path, op, value}`。`path` 为点号路径，支持数组下标（`entity.symbol`、`value.1.1`）。

| op | 语义 |
|---|---|
| `eq` / `ne` | 相等 / 不等 |
| `in` / `not_in` | 值在/不在列表内（value 为 list） |
| `contains` / `not_contains` | 字符串或列表包含/不包含 |
| `gt` / `ge` / `lt` / `le` | 数值比较 |
| `approx` | 浮点近似（默认容差 0.01，可加 `tol` 覆盖） |
| `regex` | 正则匹配（value 为 pattern） |
| `exists` / `not_exists` | 路径存在且非 None / 不存在或为 None |
| `not_empty` | 字符串/列表/字典非空 |
| `len_ge` / `len_le` | 长度 ≥ / ≤ value |

**LLM 环节纪律**：只断言结构化字段（枚举值、数值区间、字段存在性），不断言自由文本全文——随机输出用 `in` / `contains` / `regex` 收口。

## run.py 的约定

每个环节的 `run.py` 固定骨架（抄最近似环节改 `execute` 即可）：

```python
import asyncio, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]      # yiyu-agent/
sys.path.insert(0, str(ROOT))
from evaluation.stage.common import run_stage

async def execute(case_input: dict) -> dict:
    """调用本环节真实实现，返回结构化结果（供断言）。"""
    ...

if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
```

三条约定：

1. **用例字段按新 PRD 写**（稳定验收标准，不随实现摇摆）。现状与 PRD 的字段差异
   在 `execute` 里做**临时映射**，标注 `# TODO(改造): ...`，该环节改造完成时删除映射。
   基线跑出来的红色用例 = 现状与 PRD 的 gap = 改造清单。
2. **复杂语义用派生指标**。需要"每题都有 priority"这类元素级断言时，在 `execute`
   里用 Python 算成派生字段（如 `all_have_priority: true`），断言打派生字段。
   断言引擎保持傻瓜化，智能放在 execute（可断点、可调试）。
3. **待建环节**：`execute` 里 import 目标模块，`ImportError` 抛
   `NotImplementedError` → 该组用例整体 SKIP（退出码 2），提示"环节待建，评测集已就位"。
   `pending` 环节的 cases.yaml 即未来实现的**接口契约**。

## 工作流（评测驱动开发的循环）

```
① 评测集就位（用户给 → 按本格式放入对应环节 cases.yaml）
② 跑基线：python evaluation/stage/<环节>/run.py
      红色用例 = gap，逐条对应改造项
③ 改造该环节代码；run.py 的映射层同步删减
④ 复跑到全绿 = 环节验收通过
⑤ 之后每次改动跑一遍 = 回归保障（不碰该环节的改动，跑一遍就知道有没有误伤）
```

可选参数：`--skip-live` 跳过 `live: true` 的用例（断网/无 key 时用）。

## 环节清单与状态

| 目录 | 环节（PRD） | 被测对象 | 状态 | 说明 |
|---|---|---|---|---|
| `01_intent` | 5.1 意图识别与路由 | `runtime/router.py` | ready | execute 含 intent→两路径 的临时映射 |
| `02_entity` | 5.2 实体解析 | `toolkit/entity/resolver.py` | ready | PRD 六分类字段（alias_type 等）待改造接通 |
| `03_classify` | 5.3 商业模式初判与分类 | `toolkit/entity/classify.py` | ready | 用独立临时缓存，不污染正式缓存 |
| `04_granularity` | 5.3 环节② 研究粒度决策 | `runtime/granularity.py` | **pending** | 用例即接口契约；含护栏用例（校验器直测） |
| `05_plan` | 5.3 环节④ 研究计划生成 | `runtime/plan.py` | **pending** | 重写 planner.py 的目标契约；断言打派生指标 |
| `06_metrics` | 第6章 指标字典 | `bus_router/formulas_core.py` | ready | 纯函数离线可跑；三态（OK/NA/NC/DEGRADED）全覆盖 |
| `07_cognition` | 记忆管理·认知抽取 | `store/cognition_store.py` | ready | 先覆盖 extract；recall 待认知库改造后补 |
| `08_sufficiency` | 5.5 硬规则准出 | `toolkit/delivery/submit_conclusion.py` | ready | R1–R5 逐条 + 边界（否定语境）用例 |

新增环节三步：建目录 → 抄最近似环节的 `run.py` → 写 `cases.yaml`。
