"""三条硬规则回归测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from runtime.loop import AgentLoop
from runtime.state import AgentState
from store.cognition_store import CognitionStore
from toolkit.delivery.submit_conclusion import validate_conclusion


def test_methodology_requires_confirm():
    """未确认的候选条目不得进入方法库、不得参与回答。"""
    with TemporaryDirectory() as directory:
        store = CognitionStore(f"sqlite:///{Path(directory) / 'memory.db'}")
        cards = store.extract_atoms(
            "护城河源于品牌定价权，说明竞争优势可持续。",
            user_id="hard-rule-user",
        )
        assert cards
        assert store.repo.list_atoms("hard-rule-user") == []


def test_personal_overrides_standard():
    """冲突时结论遵循个人方法论，且必须说明与标准框架的差异。"""
    loop = AgentLoop(None, None, None, None)
    state = AgentState(
        session_id="hard-rule", user_message="x", active_skill="deep-research",
    )
    state.context["hard_constraints"] = [
        {"id": "personal-1", "statement": "高负债公司不纳入投资范围"},
    ]
    ok, _ = loop._validate_hard_constraints(state, "标准框架认为可配置，建议买入。")
    assert not ok
    ok, _ = loop._validate_hard_constraints(
        state,
        "投资底线对比：原前提是高负债不投；本次证据与之冲突；"
        "模型倾向遵循个人底线，不给出买入结论。",
    )
    assert ok


def test_mirror_test_gate():
    """镜子测试 5 句话不完整时，不给买入结论。"""
    result = validate_conclusion(
        conclusion="建议买入。AI 置信度高，但投资确定性需自行判断。",
        tier="G1",
        info_richness="A",
        mirror_json='{"mirror_test": ["1", "2", "3"]}',
    )
    assert not result.passed
    assert "R2_no_buy_on_fail_or_short_mirror" in result.violated_rules
