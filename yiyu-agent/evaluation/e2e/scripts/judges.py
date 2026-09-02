"""
评测判定器。

两类判定：
1. 关键词判定（离线，零成本）：must_output / must_not_output / hard_fail
   用关键词/短语在结论中做包含检查。
2. LLM 行为判定（可选，需 LLMClient）：must_do 逐条打分（0-4 分）。
   无 LLM 时标记 skipped。

通过规则（对应 PRD 11.4）：
- 任一 hard_fail 命中 → 硬失败，整个用例判负。
- must_output 全部命中 且 must_not_output 全部未命中 → 通过。
- must_do 判定：LLM 逐条打分，平均 ≥3 视为通过；无 LLM 时跳过。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from evaluation.e2e.scripts.schema import EvalCase

logger = logging.getLogger(__name__)

_JUDGE_PROMPT = """你是评测判定器。判断 Agent 的回答是否满足要求，只输出 JSON。

研究任务：{task}
Agent 回答：{answer}

要求：{requirement}

评分标准（0-4）：
- 4 完全满足：回答明确体现了该行为
- 3 基本满足：回答间接体现了该行为
- 2 部分满足：仅部分体现，有明显遗漏
- 1 轻微体现：仅擦边
- 0 未体现：完全没做到

只输出 JSON：{{"score": 0-4, "reason": "<一句话理由>"}}"""


@dataclass
class JudgeResult:
    passed: bool = False
    hard_fail: list[str] = field(default_factory=list)
    keyword_hits: dict[str, list[str]] = field(default_factory=dict)
    behavior_scores: dict[str, Any] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    detail: str = ""


class KeywordJudge:
    """关键词判定（离线）。"""

    def check(self, case: EvalCase, conclusion: str) -> JudgeResult:
        res = JudgeResult(detail="关键词判定")
        text = conclusion or ""

        # hard_fail：任一命中即硬失败
        for kw in case.hard_fail:
            if kw and kw in text:
                res.hard_fail.append(kw)
        if res.hard_fail:
            res.passed = False
            res.detail = f"硬失败命中: {', '.join(res.hard_fail)}"
            return res

        # must_output：全部命中才算过
        missing_out = [kw for kw in case.must_output if kw and kw not in text]
        # must_output_any：至少一个命中才算过
        missing_any = [kw for kw in case.must_output_any if kw and kw not in text]
        any_ok = (not case.must_output_any) or len(missing_any) < len(case.must_output_any)
        # must_not_output：任一出现即不过
        forbidden_hit = [kw for kw in case.must_not_output if kw and kw in text]

        res.keyword_hits = {
            "missing_must_output": missing_out,
            "missing_must_output_any": missing_any,
            "forbidden_present": forbidden_hit,
        }
        res.passed = (not missing_out) and any_ok and (not forbidden_hit)
        if not res.passed:
            parts = []
            if missing_out:
                parts.append(f"缺必输出: {missing_out}")
            if not any_ok:
                parts.append(f"缺任一输出: {missing_any}")
            if forbidden_hit:
                parts.append(f"含禁输出: {forbidden_hit}")
            res.detail = "; ".join(parts)
        return res


class LLMBehaviorJudge:
    """LLM 行为判定（must_do 逐条打分）。"""

    def __init__(self, llm_client) -> None:
        self.llm = llm_client

    async def check(self, case: EvalCase, conclusion: str) -> JudgeResult:
        res = JudgeResult(detail="LLM 行为判定")
        if not case.must_do or not conclusion:
            res.skipped = case.must_do or ["无 must_do 要求"]
            return res

        try:
            async def _judge_one(req: str) -> tuple[str, dict]:
                """单条 must_do 判定；异常降级为 0 分并保留原因，不影响其余条目。"""
                try:
                    data = await self.llm.chat_json(
                        system=_JUDGE_PROMPT.format(
                            task=case.input_message,
                            answer=conclusion[:3000],
                            requirement=req,
                        ),
                        user="请判定。",
                        temperature=0.0,
                    )
                    score = float(data.get("score", 0) or 0)
                    return req, {"score": score, "reason": data.get("reason", "")}
                except Exception as e:
                    logger.warning("must_do 单条判定失败(%s): %s", req, e)
                    return req, {"score": 0.0, "reason": f"判定失败: {e}"}

            # 逐条并发：must_do 之间相互独立，串行会把每用例多耗 N 次 RTT。
            for req, item in await asyncio.gather(
                *(_judge_one(r) for r in case.must_do)
            ):
                res.behavior_scores[req] = item
            avg = (
                sum(v["score"] for v in res.behavior_scores.values())
                / len(res.behavior_scores)
                if res.behavior_scores
                else 0.0
            )
            res.passed = avg >= 3.0
            res.detail = f"must_do 平均分 {avg:.2f}/4"
            return res
        except Exception as e:
            logger.warning(f"LLM 判定失败，跳过 must_do: {e}")
            res.skipped = case.must_do
            return res
