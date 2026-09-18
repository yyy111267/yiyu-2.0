"""研究目标和反共识信号的确定性优先级守卫。"""

from runtime.plan import (
    _enforce_anti_consensus, _enforce_goal_priority, validate_plan_payload,
)


def _plan():
    return validate_plan_payload({"user_goal": "主要风险在哪里", "questions": [
        {"question": "增长能否持续", "priority": "P0", "dimension": "growth"},
        {"question": "盈利能力如何", "priority": "P0", "dimension": "profitability"},
        {"question": "估值是否合理", "priority": "P0", "dimension": "valuation"},
        {"question": "政策风险是什么", "priority": "P0", "dimension": "risk"},
        {"question": "分红是否持续", "priority": "P0", "dimension": "management"},
        {"question": "单一大客户依赖是否恶化", "priority": "P2", "dimension": "risk"},
    ]})


def test_anti_consensus_customer_concentration_cannot_sink_to_p2():
    plan = _plan()
    _enforce_anti_consensus(plan, "增长主要来自单一大客户，前五大客户占比异常上升")
    assert plan.questions[-1].priority == "P1"


def test_risk_goal_makes_risk_dominant_and_moves_dividend_to_background():
    plan = _plan()
    _enforce_goal_priority(plan, "主要风险在哪里")
    p0 = plan.p0_questions
    assert sum(q.dimension == "risk" or "风险" in q.question for q in p0) * 2 >= len(p0)
    assert next(q for q in plan.questions if "分红" in q.question).priority == "P2"
