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


# ── 4. 轨迹级指标 ──────────────────────────────────────────────────────
# 从 events 序列分析 Agent 的运行轨迹质量：
# - 重复取数检测（对应「36 分钟」根因：模型失忆重复取同一标的）
# - 状态机合规（对应 run_code 编排修复：工具调用顺序是否满足 phase 门控）
# - 工具失败率、finish 重试次数、调用顺序

from collections import Counter


def _normalize_tool_name(raw: str) -> str:
    """归一化工具名：schema 名（market_get_bundle）→ 注册名（market.get_bundle）。

    只替换第一个下划线为点号（namespace 分隔符）；
    名字部分的下划线保留（如 get_bundle / run_code / base_pack）。
    """
    s = (raw or "").strip()
    if "_" not in s:
        return s
    parts = s.split("_", 1)
    return parts[0] + "." + parts[1]


def _check_state_machine(tool_calls: list[tuple[str, int]]) -> bool:
    """检查工具调用顺序是否满足 deep-research 的 phase 门控。

    合法顺序：entity.resolve → company.classify → (market/calc/web) → delivery.finish
    - 未 resolve 不得 classify
    - 未 classify 不得取数/计算
    - 有 finish 但无任何取数 → 违规（跳过研究直接收尾）
    """
    order = [t for t, _ in tool_calls]
    if not order:
        return True  # 空轨迹不判违规（offline 模式）

    def first_idx(tool_set: set[str]) -> int:
        for i, t in enumerate(order):
            if t in tool_set:
                return i
        return len(order)

    resolve_idx = first_idx({"entity.resolve"})
    classify_idx = first_idx({"company.classify"})
    fetch_idx = first_idx({
        "market.get_bundle", "calc.base_pack", "calc.run_code",
        "web.search", "web.fetch",
    })
    finish_idx = first_idx({"delivery.finish"})

    # classify 在 resolve 之前 → 违规
    if classify_idx < resolve_idx:
        return False
    # 取数/计算在 classify 之前 → 违规
    if fetch_idx < classify_idx:
        return False
    # 有 finish 但无任何取数 → 违规（跳过研究直接收尾）
    if finish_idx < len(order) and fetch_idx == len(order):
        return False
    return True


def compute_trace_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """从事件序列计算轨迹级指标。

    events 格式（runner.py 补全后）：[{"type", "content", "metadata", "timestamp"}, ...]
    """
    if not events:
        return {}

    # 提取工具调用序列与结果
    tool_calls: list[tuple[str, int]] = []   # (tool_name_normalized, step)
    tool_results: list[tuple[str, bool]] = []  # (tool_name, success)
    finish_count = 0

    for evt in events:
        etype = evt.get("type", "")
        meta = evt.get("metadata") or {}
        tool_name = _normalize_tool_name(str(meta.get("tool_name", "")))
        step = int(meta.get("step", 0))

        if etype == "tool_call":
            tool_calls.append((tool_name, step))
            if tool_name == "delivery.finish":
                finish_count += 1
        elif etype == "tool_result":
            tool_results.append((tool_name, bool(meta.get("success", True))))

    # 1. 重复取数检测：同一取数工具被调用 >1 次（模型失忆重复取数）
    fetch_tools = {"market.get_bundle", "calc.base_pack"}
    fetch_seq = [t for t, _ in tool_calls if t in fetch_tools]
    fetch_counts = Counter(fetch_seq)
    dup_fetch = sum(c - 1 for c in fetch_counts.values() if c > 1)

    # 2. 状态机合规
    state_machine_ok = _check_state_machine(tool_calls)

    # 3. 工具失败率
    total_results = len(tool_results)
    failed = sum(1 for _, s in tool_results if not s)
    tool_fail_rate = round(failed / total_results, 4) if total_results else 0.0

    # 4. 工具调用顺序（供报告展示）
    call_sequence = [t for t, _ in tool_calls]

    return {
        "dup_fetch_count": dup_fetch,
        "state_machine_ok": state_machine_ok,
        "tool_fail_rate": tool_fail_rate,
        "finish_attempts": finish_count,
        "tool_call_count": len(tool_calls),
        "tool_call_sequence": call_sequence,
    }
