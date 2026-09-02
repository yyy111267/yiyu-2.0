"""每日成本/延迟/工具成功率统计（消费 Run Trace 落盘数据）。

用法：python scripts/daily_stats.py [trace_dir]
默认 trace_dir = ./data/traces

Trace 文件格式：每行一个 JSON（jsonl），含 session_id/steps_taken/tokens_used/duration_sec/工具调用等字段。
Agent 负责人补齐生产 Trace 落盘后，此脚本即可产出日报。
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def analyze(trace_dir: Path) -> dict:
    files = sorted(trace_dir.glob("*.jsonl"))
    if not files:
        return {"total_runs": 0, "message": "无 Trace 文件"}

    runs = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except Exception:
                continue

    if not runs:
        return {"total_runs": 0, "message": "Trace 文件无有效记录"}

    total_tokens = sum(r.get("tokens_used", 0) for r in runs)
    durations = [r.get("duration_sec", 0) for r in runs]
    steps = [r.get("steps_taken", 0) for r in runs]
    tool_calls = sum(len(r.get("tool_calls", [])) for r in runs)
    tool_failures = sum(
        1 for r in runs for tc in r.get("tool_calls", [])
        if not tc.get("success", True)
    )

    # P95 延迟
    sorted_dur = sorted(durations)
    p95_idx = int(len(sorted_dur) * 0.95)
    p95 = sorted_dur[p95_idx] if sorted_dur and p95_idx < len(sorted_dur) else 0

    # 按日聚合
    by_date = defaultdict(lambda: {"runs": 0, "tokens": 0})
    for r in runs:
        day = (r.get("timestamp") or r.get("created_at") or "")[:10]
        if day:
            by_date[day]["runs"] += 1
            by_date[day]["tokens"] += r.get("tokens_used", 0)

    return {
        "report_time": datetime.now().isoformat(),
        "total_runs": len(runs),
        "total_tokens": total_tokens,
        "avg_tokens_per_run": total_tokens // len(runs) if runs else 0,
        "avg_duration_sec": sum(durations) / len(durations) if durations else 0,
        "p95_duration_sec": p95,
        "avg_steps": sum(steps) / len(steps) if steps else 0,
        "total_tool_calls": tool_calls,
        "tool_failure_rate": f"{tool_failures}/{tool_calls}" if tool_calls else "0/0",
        "by_date": dict(by_date),
    }


def main():
    trace_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/traces")
    report = analyze(trace_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
