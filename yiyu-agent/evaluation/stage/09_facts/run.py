"""环节评测：09_facts · 循环前处理环节①公司事实构建（PRD 5.2 facts_builder）。

一条命令：
    python evaluation/stage/09_facts/run.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage
from evaluation.mock_tools.preloop_mocks import (
    _make_llm_client, _make_market_data, _make_web_search, make_entity,
)
from runtime.preloop import facts_builder


async def execute(case_input: dict) -> dict:
    facts_builder.clear_cache()  # 用例间隔离，避免跨用例缓存污染

    entity = make_entity(
        case_input["symbol"], case_input["name"], case_input.get("exchange", ""))
    llm_client = _make_llm_client(case_input.get("llm_responses", [{}]))
    market_data = _make_market_data(case_input.get("market_years", []))
    web_search_fn = _make_web_search(case_input.get("web_results", []))

    force_refresh = bool(case_input.get("force_refresh", False))
    facts = await facts_builder.build_company_facts(
        entity, llm_client, market_data, web_search_fn,
        force_refresh=force_refresh)

    out = facts.model_dump() if hasattr(facts, "model_dump") else dict(facts)

    # 缓存探针：cache_probe 用例需在「无 force_refresh」下第二次调用确认命中（LLM 不再调用）
    if case_input.get("cache_probe"):
        calls_before = llm_client._call_count[0]
        facts2 = await facts_builder.build_company_facts(
            entity, llm_client, market_data, web_search_fn, force_refresh=False)
        calls_after = llm_client._call_count[0]
        out["_cache_hit_second_call"] = (
            facts2.facts_version == facts.facts_version and calls_after == calls_before)

    return out


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
