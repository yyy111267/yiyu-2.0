"""环节评测：04_granularity · 循环前处理环节②研究粒度决策（PRD 5.3 环节② granularity）。

一条命令：
    python evaluation/stage/04_granularity/run.py

（原目录为旧 PRD 商业模式分类占位 pending，已升级为 granularity 正式评测。）
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage
from evaluation.mock_tools.preloop_mocks import _make_llm_client, make_entity
from runtime.schemas import BusinessSegment, CompanyFacts, InfoRichness
from runtime.preloop import granularity


def _build_facts(facts_in: dict) -> CompanyFacts:
    """从 cases.yaml 的 facts 字典构造 CompanyFacts（自动算 facts_version）。"""
    entity = make_entity(
        facts_in.get("symbol", "000000.SZ"),
        facts_in.get("name", "测试公司"),
        facts_in.get("exchange", ""))
    segs = [BusinessSegment(name=s["name"], revenue_share=float(s.get("ratio", 0.0)) / 100.0
                             if float(s.get("ratio", 0.0)) > 1 else float(s.get("ratio", 0.0)))
            for s in facts_in.get("segments", [])]
    facts = CompanyFacts(
        entity=entity,
        one_line_business=facts_in["one_line_business"],
        segments=segs,
        info_richness=InfoRichness(facts_in.get("info_richness", "A")),
    )
    facts.compute_version()
    return facts


async def execute(case_input: dict) -> dict:
    granularity.clear_cache()
    facts = _build_facts(case_input["facts"])
    llm_client = _make_llm_client(case_input.get("llm_responses", [{}]))
    force_refresh = bool(case_input.get("force_refresh", False))

    dec = await granularity.decide_granularity(
        facts, llm_client, force_refresh=force_refresh)
    out = dec.model_dump() if hasattr(dec, "model_dump") else dict(dec)

    # 缓存探针：同 facts_version 第二次决策应命中缓存（LLM 不再调用）
    if case_input.get("cache_probe"):
        calls_before = llm_client._call_count[0]
        dec2 = await granularity.decide_granularity(facts, llm_client)
        calls_after = llm_client._call_count[0]
        out["_cache_hit_second_call"] = (calls_after == calls_before)

    # 刷新探针：force_refresh 同 facts 仍应重新调用 LLM
    if case_input.get("refresh_probe"):
        calls_before = llm_client._call_count[0]
        await granularity.decide_granularity(facts, llm_client, force_refresh=True)
        calls_after = llm_client._call_count[0]
        out["_refreshed_recalled_llm"] = (calls_after > calls_before)

    return out


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
