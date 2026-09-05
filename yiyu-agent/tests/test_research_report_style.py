"""用户研究报告的成文合同回归。"""

import asyncio

from runtime.assembler import PromptAssembler
from runtime.loop import AgentLoop
from runtime.state import AgentState
from toolkit.delivery.submit_conclusion import validate_conclusion


def test_synthesis_contract_keeps_coaching_structure_and_positive_sample():
    state = AgentState(
        session_id="style-contract",
        user_message="研究贵州茅台",
        active_skill="deep-research",
        pinned_skill="deep-research",
    )
    state.context.update({"_prompt_stage": "synthesis", "skill_phase": 3})

    prompt = asyncio.run(PromptAssembler().build(state))

    for phrase in (
        "小渔的结论与核心矛盾",
        "市场分歧",
        "什么会改变判断",
        "这次值得留下的投资认知",
        "留给你的问题",
        "好生意、经营变化和好价格是三道独立判断",
    ):
        assert phrase in prompt.system


def test_user_facing_report_passes_and_internal_jargon_is_rejected():
    readable = (
        "小渔的结论：公司基本盘仍然稳健，但渠道信号转弱，需要继续观察。\n\n"
        "市场分歧：乐观者相信品牌能够穿越周期；谨慎者担心渠道压力会传导到收入。\n\n"
        "什么会改变判断：如果批价、库存和预付款同步走弱，就需要重新评估。\n\n"
        "这次值得留下的投资认知：好公司不等于任何价格都值得买。"
    )
    assert validate_conclusion(readable).passed

    leaked = readable + "\nROIC 30%（base_pack，冻结口径）。"
    result = validate_conclusion(leaked)
    assert not result.passed
    assert "R3_no_internal_jargon" in result.violated_rules


def test_report_coach_payload_uses_real_report_sections_only():
    report = """## 小渔的结论
公司基本盘稳健，但渠道仍需观察。

## 市场分歧
乐观者相信品牌可以穿越周期；谨慎者担心渠道压力会传导到收入。

## 这次值得留下的投资认知
好生意、经营变化和好价格是三道独立判断。

## 留给你的问题
如果未来两年利润不增长，你会如何调整自己的估值要求？
"""

    assert AgentLoop._extract_coach_payload(report) == {
        "core_conflict": "乐观者相信品牌可以穿越周期；谨慎者担心渠道压力会传导到收入",
        "takeaway": "好生意、经营变化和好价格是三道独立判断",
        "reflection_question": "如果未来两年利润不增长，你会如何调整自己的估值要求？",
    }
    assert AgentLoop._extract_coach_payload("## 市场分歧\n只有一个分歧。") is None
