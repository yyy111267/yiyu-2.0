"""
评测指标（三大类）：

1. 硬规则通过率：结论是否被硬规则校验放行（delivery.submit_conclusion）。
2. 关键字段有源率：结论中出现的关键数字，能否溯源到工具观测。
3. 耗时与成本：单次研究耗时 / token 用量（衡量效率）。

供 run_eval.py 汇总报告，以及后续 badcase 归因。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


# ── 1. 关键字段有源检测 ──────────────────────────────────────────────
# 结论中的数字（可带单位：亿/万/元/%/倍/元/股），用于检查是否有来源标注
_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*(亿|万|%|％|元|块|倍|美元|港元|人民币)?"
)
# 来源标注形态：财报/年报/公告/工具名/网页/URL/来源/数据源/口径
_SOURCE_PATTERN = re.compile(
    r"(财报|年报|公告|来源|数据源|工具|估值|口径|财务|现金流量|资产负债表|利润表|"
    r"http|www\.|\.com|\.cn|东财|雪球|巨潮|交易所|SEC)",
    re.IGNORECASE,
)


@dataclass
class TraceStats:
    """一次研究运行的可观测指标。"""
    steps: int = 0
    tokens_used: int = 0
    duration_sec: float = 0.0
    tool_calls: int = 0
    tool_sources: list[str] = field(default_factory=list)  # 已成功取数的工具名
    hard_rules_passed: Optional[bool] = None               # 硬规则校验结果
    conclusion: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


def compute_key_figure_sourced(conclusion: str, tool_sources: list[str]) -> dict[str, Any]:
    """
    计算「关键数字有源率」：

    - 若结论无任何数字 → 返回 "no_figures"（不参与该指标）。
    - 若结论含数字但无来源标注 → 判定 "unverified"（高风险，可能心算报数）。
    - 若结论含数字且出现来源标注 → 判定 "sourced"。
    - 若结论含数字、有来源标注且工具确实取过数 → "sourced+tool"（最强）。

    返回 dict：count / labeled / status / detail。
    """
    numbers = _NUMBER_PATTERN.findall(conclusion)
    has_figures = len(numbers) > 0
    has_source_label = bool(_SOURCE_PATTERN.search(conclusion))
    has_tool_data = len(tool_sources) > 0

    if not has_figures:
        return {"count": 0, "labeled": 0, "status": "no_figures", "detail": "结论无关键数字"}

    count = len(numbers)
    if has_source_label and has_tool_data:
        return {"count": count, "labeled": count, "status": "sourced_tool",
                "detail": "数字带来源标注且工具已取数"}
    if has_source_label:
        return {"count": count, "labeled": count, "status": "sourced",
                "detail": "数字带来源标注（未确认工具取数）"}
    return {"count": count, "labeled": 0, "status": "unverified",
            "detail": "结论含数字但无来源标注，疑似模型心算报数"}


def compute_metrics(stats: TraceStats) -> dict[str, Any]:
    """汇总单次运行的三类指标。"""
    fig = compute_key_figure_sourced(stats.conclusion, stats.tool_sources)
    return {
        "hard_rules_passed": stats.hard_rules_passed,
        "key_figures": fig,
        "efficiency": {
            "steps": stats.steps,
            "tokens_used": stats.tokens_used,
            "duration_sec": round(stats.duration_sec, 2),
            "tool_calls": stats.tool_calls,
        },
    }
