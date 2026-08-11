"""
上下文压缩器 - 长研究会话防 token 溢出。

当 observations 历史过长时，把旧的观察结果摘要合并，
保留最近 N 条原文，更早的压缩为"要点摘要"。

策略（保守，避免丢关键证据）：
1. 阈值：observations 数量 > max_keep * 2 时触发。
2. 保留最近 max_keep 条原文。
3. 更早的：保留 source + success + 截断 content（前 200 字）。
4. 不删除，只压缩——序列化到 checkpoint 时已只留最近 10 条，这里做运行时压缩。
"""

from __future__ import annotations

import logging
from typing import Any

from runtime.state import AgentState, Observation

logger = logging.getLogger(__name__)

# 触发压缩的观察数量阈值
COMPACT_THRESHOLD = 12
# 压缩后保留原文的最近条数
KEEP_RECENT = 5
# 旧条目 content 截断长度
COMPACT_CONTENT_CHARS = 200


def needs_compaction(state: AgentState, threshold: int = COMPACT_THRESHOLD) -> bool:
    """判断是否需要压缩。"""
    return len(state.observations) > threshold


def compact_observations(state: AgentState, keep_recent: int = KEEP_RECENT) -> int:
    """
    压缩 state.observations：旧条目 content 截断为摘要。

    原地修改 state，返回被压缩的条数。

    保守策略：
    - 不删除任何 observation（保留 source/success/error 用于追溯）
    - 只截断过长的 content 文本
    - 最近 keep_recent 条保持原文不动
    """
    total = len(state.observations)
    if total <= COMPACT_THRESHOLD:
        return 0

    compacted = 0
    old_start = max(0, total - keep_recent)
    for i in range(old_start):
        obs = state.observations[i]
        content_str = str(obs.content) if obs.content is not None else ""
        if len(content_str) > COMPACT_CONTENT_CHARS:
            obs.content = content_str[:COMPACT_CONTENT_CHARS] + "...[已压缩]"
            compacted += 1

    if compacted:
        logger.info(
            f"[Session {state.session_id}] 压缩 {compacted} 条旧观察 "
            f"(保留最近 {keep_recent} 条原文)"
        )
    return compacted


def estimate_observations_tokens(state: AgentState) -> int:
    """粗估 observations 占用的 token 数（4 字符 ≈ 1 token）。"""
    total_chars = 0
    for obs in state.observations:
        total_chars += len(obs.source)
        total_chars += len(str(obs.content or ""))
        total_chars += len(obs.error or "")
    return total_chars // 4
