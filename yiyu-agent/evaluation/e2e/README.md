# 端到端评测（e2e）

> 产品经理视角：这里考的是「**整车路测**」——不问中间步骤，直接给 AI 一个完整研究任务，看它最后交出来的东西行不行。
> 对应 `evaluation/README.md` 里说的「端到端评测」。

---

## 目录结构

```
e2e/
├── README.md            ← 本文件
├── skills/              ← 端到端考卷对应的能力地图
├── datasets/            ← 考卷库（按用途分四摞）
│   ├── benchmark/       ← 正式标准试卷（上线门槛）
│   ├── quality/         ← 报告质量考卷
│   ├── smoke/           ← 冒烟题（快速自检）
│   └── regression/      ← 历史错题本（防回退）
├── rubrics/             ← 评分标准（质量分制 + 硬规则）
├── mock_tools/          ← 端到端专用假工具（目前复用上层 mock_tools/）
├── scripts/             ← 考试程序（跑题、判卷、出分）
└── reports/             ← 成绩单（每次考试结果存档）
    ├── benchmark/
    ├── quality/
    ├── smoke/
    └── regression/
```

---

## 一次完整路测长什么样

1. 从 `datasets/` 抽一道题（比如「研究贵州茅台的投资价值」）。
2. `scripts/runner.py` 把题喂给真实的以渔研究流程。
3. 研究跑完，产出一份报告 + 完整思考轨迹。
4. `scripts/judges.py`、`scripts/quality.py` 按 `rubrics/` 的标准判卷。
5. `scripts/metrics.py` 统计通过率、分数。
6. 结果写入 `reports/` 对应子目录，留档复盘。

---

## 四个跑分入口

| 你想知道… | 命令 | 成绩单位置 |
|---|---|---|
| 达到上线基线了吗 | `python evaluation/e2e/scripts/run_eval.py` | `reports/benchmark/` |
| 报告质量好不好 | `python evaluation/e2e/scripts/run_quality.py` | `reports/quality/` |
| 基本功能没挂（建议） | `python evaluation/e2e/scripts/run_smoke.py` | `reports/smoke/` |
| 老 bug 没回来（建议） | `python evaluation/e2e/scripts/run_regression.py` | `reports/regression/` |
| 一键全跑 | `python evaluation/e2e/scripts/run_all.py` | 各子目录 |

> 注：`run_smoke.py` / `run_regression.py` 为建议新增脚本，`smoke/`、`regression/` 目前为待填充的考卷占位目录。
