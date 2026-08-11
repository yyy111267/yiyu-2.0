# evaluation/ — Agent 评测

评测覆盖四层：
1. **硬规则回归**：`test_hard_rules.py`（三条硬规则守住）
2. **安全攻击回归**：`gate_bypass.py`（Prompt 注入/越权绕过）
3. **回答质量评测集**：`cases/answer_quality/`（PRD 11.3 的 18 个核心用例）
4. **用户侧质量评测**：`quality.py` + `cases/quality/`（结论读起来好不好，100 分制）

## 结构
```
evaluation/
├── cases/
│   ├── answer_quality/          回答质量评测集（Eval01–18）
│   │   ├── deep_research_1.yaml    Eval01–09
│   │   ├── deep_research_2.yaml    Eval10–15, 17, 18
│   │   └── private_company.yaml    Eval16
│   ├── quality/                 用户侧质量用例（好/坏/中样本回归）
│   └── conflict_priority.yaml   个人方法论优先级（旧格式）
├── schema.py                    用例数据模型 + YAML 加载
├── metrics.py                   三大指标（硬规则/关键字段有源/效率）
├── judges.py                    判定器（关键词 + LLM 行为打分）
├── quality.py                   用户侧质量打分引擎（10 项指标，100 分制）
├── runner.py                    执行器（live / offline 两模式）
├── run_eval.py                  批量入口（出报告 + badcase 入库）
├── run_quality.py               质量评测入口（单条打分 / 批量用例）
├── run_all.py                   一键全量评测
├── test_hard_rules.py           硬规则回归
└── gate_bypass.py               安全攻击回归
```

## 快速开始

```bash
# 一键全量（hard_rules + 安全攻击 + 评测集 live）
.venv/bin/python -m evaluation.run_all

# 只跑评测集（live：真实调 LLM）
.venv/bin/python -m evaluation.run_eval

# 只跑评测集（offline：测判定器，不调 LLM）
.venv/bin/python -m evaluation.run_eval --offline --conclusion "结论文本"

# 定向跑
.venv/bin/python -m evaluation.run_eval --skill deep-research
.venv/bin/python -m evaluation.run_eval --filter eval17,eval18

# 用户侧质量评测：给一段结论打分
.venv/bin/python -m evaluation.run_quality --conclusion "结论文本……"

# 用户侧质量评测：批量跑 quality 用例（出平均分 + JSON 报告）
.venv/bin/python -m evaluation.run_quality
```

## 用户侧质量评测（100 分制）

评的是**最终研报结论读起来好不好**（不管数字对错）：十项指标各 10 分，
纯规则实现（离线、零成本，不调 LLM）。

| 指标 | 打分方式 |
|------|----------|
| Q1 结构清晰 | 数小标题/分点 |
| Q2 结论先行 | 开头 300 字内有无判断词 |
| Q3 通顺不重复 | 套话是否反复出现（≥3 次扣分） |
| Q4 长短合适 | 字数区间（理想 400–2000） |
| Q5 无空话 | 空话词计数（众所周知/综上所述…） |
| F1 术语有解释 | 术语是否配了"即/就是/括号注释" |
| F2 有大白话 | 有无"打个比方"式转述 |
| F3 不吓人 | 有无"仅供参考/不构成投资建议" |
| F4 告诉下一步 | 有无行动指引（建议您/可以关注…） |
| F5 不堆黑话 | 术语密度（>6% 判黑话轰炸） |

评级：A≥90 / B≥80 / C≥70 / D≥60 / F<60。
质量用例格式（`expect.quality.min_score/max_score` 限定期望区间）：

```yaml
id: quality01
sample_conclusion: |
  结论文本……
expect:
  quality:
    min_score: 80      # 好样本：必须达到
    max_score: 50      # 坏样本：必须不超过
```

## 用例格式（五段式）

```yaml
id: eval01
name: 实体消歧
skill: deep-research
desc: ...
input:
  message: "用户输入"
expect:
  must_output: [必须出现的关键词]      # 关键词判定
  must_not_output: [禁止出现的关键词]  # 关键词判定
  must_do: [行为要求]                 # LLM 打分 0-4，均值≥3 通过
  hard_fail: [硬失败项]               # 任一命中即判负
```

## 判定规则（PRD 11.4）

- 任一 hard_fail 命中 → 硬失败，用例判负；
- must_output 全部命中 且 must_not_output 全部未命中 → 关键词判定通过；
- must_do 由 LLM 逐条打分，均值 ≥3 → 行为判定通过；无 LLM 时跳过；
- 硬失败项不因其他项通过而豁免。

## Badcase 回流

失败的用例自动写入 `store/repos/badcase_repo.py` 的 `badcases` 表：

```bash
# 查看未修复 badcase
.venv/bin/python -c "
from store.repos.badcase_repo import BadcaseRepo
r = BadcaseRepo()
print(r.stats())
print(r.list_unfixed(limit=10))"

# 标记已修复
.venv/bin/python -c "
from store.repos.badcase_repo import BadcaseRepo
BadcaseRepo().mark_fixed('<badcase_id>')"
```

复盘节奏建议：每轮迭代后跑一次 live 评测，看 `badcases` 表的 `error_type` 分布，
优先修出现最多的失败类型（改 prompt / 补工具 / 调阈值）。
