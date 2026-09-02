"""环节评测：10_profiler · 循环前处理环节③公司画像与适配器选型（PRD 5.3 环节③ profiler）。

一条命令：
    python evaluation/stage/10_profiler/run.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage
from evaluation.mock_tools.preloop_mocks import _make_llm_client, make_entity
from runtime.schemas import (
    BusinessSegment, CompanyFacts, GranularityDecision, GranularityMode, InfoRichness, ResearchUnit,
)
from runtime.preloop import profiler


# 合法默认 5 维标签（避免每个用例在 yaml 重复写全）
_DEFAULT_TAGS = {
    "development_stage": {"value": "成熟", "evidence": "经营多年稳定", "confidence": 0.8},
    "charging":          {"value": "一次性销售", "evidence": "产品制销售", "confidence": 0.7},
    "capital_intensity": {"value": "轻资产", "evidence": "固定资产占比低", "confidence": 0.7},
    "cycle":            {"value": "弱周期", "evidence": "需求稳定", "confidence": 0.6},
    "value_chain":      {"value": "整机", "evidence": "面向终端客户", "confidence": 0.6},
}


def _build_facts() -> CompanyFacts:
    entity = make_entity("000000.SZ", "测试公司", "")
    facts = CompanyFacts(
        entity=entity,
        one_line_business="综合业务公司（评测用）",
        segments=[BusinessSegment(name="主营", ratio=100.0)],
        info_richness=InfoRichness("A"),
    )
    facts.compute_version()
    return facts


def _build_granularity(mode: str, units: list[dict]) -> GranularityDecision:
    ru = [ResearchUnit(id=u["id"], scope=u.get("scope", ""), reason=u.get("scope", "研究单元"))
          for u in units]
    g = GranularityDecision(
        entity=make_entity("000000.SZ", "测试公司", ""),
        mode=GranularityMode(mode),
        units=ru,
        confidence=0.8,
        source="评测构造",
        facts_version="fv_eval",
    )
    return g


def _make_llm_responses(case_input: dict) -> list[dict]:
    """基于默认合法标签 + 用例覆盖，构造 LLM raw 响应。"""
    items = case_input.get("llm_responses", [{}])
    out = []
    for it in items:
        raw = {dim: dict(tag) for dim, tag in _DEFAULT_TAGS.items()}
        for dim, tag in (it.get("override_dims") or {}).items():
            raw[dim] = tag
        raw["selected_adapter"] = it.get("selected_adapter", "consumer_brand")
        raw["selection_reason"] = it.get("selection_reason", "命中适用条件")
        out.append(raw)
    return out


async def execute(case_input: dict) -> dict:
    profiler.clear_cache()
    facts = _build_facts()
    granularity = _build_granularity(
        case_input.get("mode", "whole"), case_input.get("units", [{"id": "u_whole"}]))
    llm_client = _make_llm_client(_make_llm_responses(case_input))

    profiles = await profiler.build_unit_profiles(
        facts, granularity, llm_client, force_refresh=True)

    profiles_out = []
    for p in profiles:
        d = p.model_dump() if hasattr(p, 'model_dump') else dict(p)
        profiles_out.append(d)

    out = {"profiles": profiles_out}

    # fallback 用例：质疑最终每单元均回退到 FALLBACK（adapter_fallback=true）
    if case_input.get("expect_fallback"):
        out["_all_fallback"] = all(p.get("adapter_fallback") for p in profiles_out)

    return out


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
