from runtime.loop import AgentLoop
from runtime.state import AgentState, Observation


def test_light_data_requires_usable_market_or_calc_result():
    state = AgentState(
        session_id="test-light-data",
        user_message="计算茅台的pe",
        active_skill="light-data",
    )

    state.add_observation(Observation(
        source="entity.resolve",
        content={"resolved": True},
        success=True,
    ))
    assert not AgentLoop._has_light_data_evidence(state)

    state.add_observation(Observation(
        source="market.get_snapshot",
        content={"status": "degraded", "error": "未能获取快照"},
        success=True,
    ))
    assert not AgentLoop._has_light_data_evidence(state)

    state.add_observation(Observation(
        source="calc.metric",
        content={"success": True, "metric_id": "pe_ttm", "value": 24.0},
        success=True,
    ))
    assert AgentLoop._has_light_data_evidence(state)
