"""环节评测：07_cognition · 认知抽取（PRD 记忆管理）。

一条命令：
    python evaluation/stage/07_cognition/run.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402


class ScriptedExtractorLLM:
    """按脚本返回 chat_json 响应；可注入故障；记录 prompt 供接线断言。"""

    def __init__(self, response=None, error: bool = False):
        self.response = response
        self.error = error
        self.prompts: list[dict] = []

    async def chat_json(self, system: str, user: str, temperature: float = 0.1, **kwargs):
        self.prompts.append({"system": system, "user": user})
        if self.error:
            raise RuntimeError("mock llm unavailable")
        return self.response


async def execute(case_input: dict) -> dict:
    """调用环节真实实现。

    - 用例含 llm 脚本 → store/cognition_store.py::extract_candidates（LLM 结构化抽取主路径）
    - 无 llm 脚本     → store/cognition_store.py::extract_atoms（正则兜底路径）
    """
    from store.cognition_store import CognitionStore

    store = CognitionStore()  # 抽取为纯函数，不触库
    if "llm_response" in case_input or case_input.get("llm_error"):
        llm = ScriptedExtractorLLM(
            response=case_input.get("llm_response"),
            error=bool(case_input.get("llm_error")),
        )
        atoms = await store.extract_candidates(
            case_input["text"],
            user_id=case_input.get("user_id", "eval_user"),
            symbol=case_input.get("symbol", ""),
            user_message=case_input.get("user_message", ""),
            company_name=case_input.get("company_name", ""),
            llm_client=llm,
        )
        user_message = case_input.get("user_message", "")
        summary_head = case_input["text"].strip()[:30]
        return {
            "atoms": atoms, "count": len(atoms),
            "llm_called": len(llm.prompts),
            # 接线断言：用户原话必须进入抽取输入（PRD 六类信号的载体）
            "prompt_has_user_message": bool(user_message) and any(
                user_message in p["user"] for p in llm.prompts),
            "prompt_has_research_summary": bool(summary_head) and any(
                summary_head in p["user"] for p in llm.prompts),
        }
    atoms = store.extract_atoms(
        case_input["text"],
        user_id=case_input.get("user_id", "eval_user"),
        symbol=case_input.get("symbol", ""),
    )
    return {"atoms": atoms, "count": len(atoms)}


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
