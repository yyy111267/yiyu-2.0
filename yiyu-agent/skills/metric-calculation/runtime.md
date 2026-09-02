# 指标计算执行合同

- 标准指标：单项调 `calc.metric`，多项调 `calc.metrics`；不确定指标时先调 `calc.menu`。
- 非标情景、敏感性或 SOTP 才调 `calc.run_code`；标准指标禁止在沙箱里重写公式。
- `not_disclosed` 表示缺字段，按 `field_recovery_plan` 补官方披露；`not_meaningful` 表示不适用，不要补数或解读为负面。
- `degraded` 结果可使用但结论必须降权并写明原因；`self_funded` 表示现金跑道无需计算。
- 数字不得心算或凭记忆填补。结论中只使用带口径版本、期间和字段溯源的结果。
