"""认知库 6.2 记忆闭环的离线回归测试。"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from runtime.loop import AgentLoop
from runtime.state import AgentState
from store.cognition_store import CognitionStore


def _store():
    directory = TemporaryDirectory()
    return directory, CognitionStore(f"sqlite:///{Path(directory.name) / 'memory.db'}")


class _StaticLLM:
    """脚本化 LLM：固定返回一个 chat_json 响应；可选抛错。"""

    def __init__(self, response=None, error: bool = False):
        self._response = response
        self._error = error
        self.prompts: list[str] = []

    async def chat_json(self, system, user, temperature=0.1, **kwargs):
        self.prompts.append(user)
        if self._error:
            raise RuntimeError("llm down")
        return self._response


def test_candidate_requires_confirmation():
    directory, store = _store()
    try:
        cards = store.extract_atoms("护城河源于品牌定价权，说明竞争优势可持续。", user_id="u1")
        assert cards
        assert store.repo.list_atoms("u1") == []
    finally:
        directory.cleanup()


def test_company_memory_becomes_verification_question():
    directory, store = _store()
    try:
        store.confirm_cards([{
            "card_id": "card-1", "action": "confirm", "candidate": {
                "statement": "存货增长主要由主动备货驱动", "category": "风险",
                "type": "cognition", "subject_scope": "company", "symbol": "603986.SH",
                "verification_status": "needs_recheck",
            },
        }], user_id="u1")
        memory = store.prepare_research_memory(user_id="u1", symbol="603986.SH")
        questions = store.cognitions_to_questions(memory["company_assertions"])
        assert len(questions) == 1
        assert questions[0]["memory_mode"] == "verify"
        assert "验证既有判断" in questions[0]["question"]
    finally:
        directory.cleanup()


def test_active_memory_usage_is_reported_as_injected():
    directory, store = _store()
    try:
        result = store.confirm_cards([{
            "card_id": "card-1", "action": "confirm", "candidate": {
                "statement": "现金流质量重于利润规模",
                "category": "选股标准",
                "type": "cognition",
                "subject_scope": "general",
                "content": "适用条件：成熟企业。\n证伪条件：现金流短期异常但经营质量未恶化。",
            },
        }], user_id="u1")
        usage = store.repo.get_usage(result[0]["id"], user_id="u1")
        assert usage is not None
        assert usage["injected"] is True
    finally:
        directory.cleanup()


def test_edit_keeps_agent_variant_and_user_priority():
    directory, store = _store()
    try:
        original = {
            "statement": "高负债公司也值得重仓", "category": "风险", "type": "cognition",
            "content": "适用条件：现金流稳定。\n证伪条件：负债导致偿债风险上升。",
        }
        store.confirm_cards([{
            "card_id": "card-1", "action": "edit", "candidate": original,
            "edited": {
                "statement": "负债率过高的公司不纳入投资范围", "is_hard_constraint": True,
                "content": "适用条件：有息负债持续偏高。\n证伪条件：去杠杆后现金流改善。",
            },
        }], user_id="u1")
        items = store.repo.list_atoms("u1")
        assert len(items) == 2
        user_item = next(item for item in items if item["owner"] == "user")
        assert user_item["variant_of"]
        directory_items = store.prepare_research_memory(user_id="u1")["cognition_directory"]
        assert directory_items[0]["owner"] == "user"
    finally:
        directory.cleanup()


def test_preference_stays_out_of_research_questions():
    directory, store = _store()
    try:
        preferences = store.extract_preferences("我偏好低负债公司，报告请简短。", user_id="u1")
        assert preferences
        store.merge_preferences(preferences, user_id="u1")
        memory = store.prepare_research_memory(user_id="u1", symbol="603986.SH")
        assert memory["preferences"]
        assert memory["company_assertions"] == []
    finally:
        directory.cleanup()


def test_hard_constraint_requires_explicit_comparison_for_positive_conclusion():
    loop = AgentLoop(None, None, None, None)
    state = AgentState(session_id="s1", user_message="x", active_skill="deep-research")
    state.context["hard_constraints"] = [{"id": "ca-1", "statement": "负债率超过60%绝不买"}]
    ok, _ = loop._validate_hard_constraints(state, "建议买入，基本面改善。")
    assert not ok
    ok, _ = loop._validate_hard_constraints(
        state, "建议买入。投资底线对比：原前提是负债率超过60%不买；本次证据显示负债率下降；模型倾向为前提不成立。",
    )
    assert ok


_VALID_CONTENT = (
    "适用条件：毛利率显著高于同业。\n不适用于：周期性高毛利。\n"
    "判断逻辑：护城河需跨周期稳定与ROIC支撑。\n证伪条件：毛利率回落至行业均值且份额下滑。"
)


def test_extract_candidates_uses_llm_and_user_message():
    directory, store = _store()
    try:
        llm = _StaticLLM({"candidates": [{
            "type": "cognition",
            "statement": "高毛利不等于护城河，需跨周期稳定性验证",
            "content": _VALID_CONTENT,
            "category": "护城河", "subject_scope": "general",
            "source_quote": "高毛利不等于护城河，得看ROIC。",
        }]})
        cards = asyncio.run(store.extract_candidates(
            "研究结论：毛利率长期高于行业均值。",
            user_id="u1", user_message="高毛利不等于护城河，得看ROIC。",
            llm_client=llm,
        ))
        assert len(cards) == 1
        assert cards[0]["status"] == "pending_confirmation"
        assert cards[0]["owner"] == "agent"
        assert cards[0]["content"] == _VALID_CONTENT
        # 用户原话必须进入抽取输入（PRD 六类识别信号的载体）
        assert any("高毛利不等于护城河，得看ROIC。" in p for p in llm.prompts)
        assert store.repo.list_atoms("u1") == []  # 未确认仍不写库
    finally:
        directory.cleanup()


def test_extract_candidates_empty_result_is_trusted():
    directory, store = _store()
    try:
        llm = _StaticLLM({"candidates": []})
        cards = asyncio.run(store.extract_candidates(
            "兆易创新2026Q1营收41.88亿元，同比增长119.38%。",
            user_id="u1", llm_client=llm,
        ))
        assert cards == []  # LLM 判定无认知 → 不得降级正则把数字句抽成认知
    finally:
        directory.cleanup()


def test_extract_candidates_falls_back_on_llm_error():
    directory, store = _store()
    try:
        cards = asyncio.run(store.extract_candidates(
            "护城河源于品牌定价权，说明竞争优势可持续。",
            user_id="u1", llm_client=_StaticLLM(error=True),
        ))
        assert cards  # 正则兜底仍出候选
    finally:
        directory.cleanup()


def test_extract_candidates_filters_invalid_llm_items():
    directory, store = _store()
    try:
        cards = asyncio.run(store.extract_candidates(
            "结论", user_id="u1", symbol="603986.SH", company_name="兆易创新",
            llm_client=_StaticLLM({"candidates": [
                {"type": "preference", "statement": "我偏好简短报告"},
                {"type": "cognition", "statement": "兆易创新存货增长是主动备货",
                 "content": "适用条件：x。\n证伪条件：y。", "subject_scope": "company"},
                {"type": "cognition", "statement": "一" * 41,
                 "content": "适用条件：x。\n证伪条件：y。", "subject_scope": "general"},
                {"type": "cognition", "statement": "现金流质量重于利润规模",
                 "content": "适用条件：成熟企业。", "subject_scope": "general"},
                {"type": "cognition", "statement": "存货增长主要由主动备货驱动",
                 "content": "适用条件：订单可见度高。", "subject_scope": "company"},
            ]}),
        ))
        assert len(cards) == 1
        assert cards[0]["statement"] == "存货增长主要由主动备货驱动"
        assert cards[0]["symbol"] == "603986.SH"
        assert cards[0]["verification_status"] == "needs_recheck"
    finally:
        directory.cleanup()
