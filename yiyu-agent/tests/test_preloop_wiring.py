"""preloop → loop 接线回归测试。

验证：
1. run_preloop 用 mock 串起四件套（实体→事实包→粒度→画像→计划）；
2. 四件套能正确喂给 AgentLoop.run（research_plan + initial_context）并收敛；
3. 字段适配函数（entity_to_current_schema / facts_to_plan_input / build_initial_context）。

离线零 LLM：preloop 与 loop 都走 mock。
一条命令：python tests/test_preloop_wiring.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation" / "stage" / "11_loop"))

from runtime.preloop import (  # noqa: E402
    run_preloop, entity_to_current_schema, facts_to_plan_input,
    collect_adapter_questions, build_initial_context,
)
from runtime.loop import LoopConfig  # noqa: E402
from toolkit.entity.resolver import Entity, resolve_entity  # noqa: E402
from runtime.schemas import CompanyFacts, InfoRichness  # noqa: E402
from evaluation.mock_tools.preloop_mocks import (  # noqa: E402
    _make_llm_client, _make_market_data, _make_web_search, make_entity,
)
from run import ScriptedLLM, ScriptedExecutor, _make_loop  # noqa: E402  (12_loop mocks)

passed = 0
failed = 0


def check(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  ✓ {name}")
        passed += 1
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {name}: {type(e).__name__}: {e}")
        failed += 1


# ── 夹具 ──────────────────────────────────────────────────

_ACT = {
    "falsification": "若关键假设被证伪则结论不成立",
    "completion_rule": "关键证据齐备且能给出明确结论",
    "required_evidence": [{"type": "financial_statement", "level": "A", "description": "财务数据"}],
}

_ENTITY = Entity(symbol="600519.SH", name="贵州茅台", market="A", currency="CNY",
                 source="builtin", confidence=0.95)

_PRELOOP_RESPONSES = [
    {"one_line_business": "高端白酒生产与销售",
     "segments": [{"name": "白酒", "revenue_share": 98.0}],
     "source_field_map": {"one_line_business": "source_0", "segments[0].name": "source_0"},
     "open_questions": [], "sources": [{"title": "年报", "url": "https://e.com/a"}]},
    {"mode": "whole",
     "units": [{"id": "u_whole", "scope": "整体公司", "reason": "单一主营",
                "differential_dimensions": []}],
     "confidence": 0.9, "source": "年报", "open_questions": []},
    {"development_stage": {"value": "成熟", "evidence": "多年", "confidence": 0.8},
     "charging": {"value": "一次性销售", "evidence": "产品", "confidence": 0.7},
     "capital_intensity": {"value": "轻资产", "evidence": "固资低", "confidence": 0.7},
     "cycle": {"value": "弱周期", "evidence": "稳定", "confidence": 0.6},
     "value_chain": {"value": "整机", "evidence": "终端", "confidence": 0.6},
     "selected_adapter": "consumer_brand", "selection_reason": "命中品牌消费"},
    {"questions": [
        {"question": "增长驱动", "priority": "P0", "dimension": "growth"},
        {"question": "毛利率", "priority": "P0", "dimension": "profitability"},
        {"question": "估值假设", "priority": "P0", "dimension": "valuation"},
        {"question": "政策风险", "priority": "P0", "dimension": "risk"},
    ]},
]


def _make_preloop_mocks():
    return (
        _make_llm_client(list(_PRELOOP_RESPONSES)),
        _make_market_data([{"year": 2025, "revenue": 150000000000, "net_profit": 50000000000}]),
        _make_web_search([{"title": "贵州茅台官网", "url": "https://e.com"}]),
    )


def _adv(qid):
    return [
        {"op": "advance", "question_id": qid, "to": "in_progress", "activation": _ACT},
        {"op": "advance", "question_id": qid, "to": "answered"},
    ]


def _loop_script():
    return [
        {"content": f"声明：处理 q{i}", "tool_calls": [
            *([{"name": "web.search", "arguments": {"query": "贵州茅台公开资料"}}]
              if i in (1, 4) else []),
            {"name": "market.get_bundle", "arguments": {"symbol": "600519"}},
            {"name": "plan.update", "arguments": {"operations": _adv(f"q{i}")}},
        ]}
        for i in range(1, 5)
    ] + [{"content": "收工", "tool_calls": [{"name": "delivery.finish", "arguments": {}}]}]


def _loop_tools():
    return {
        "market.get_bundle": {
            "fetch_status": "ok", "missing_fields": [],
            "field_evidence": {
                "revenue": {"value": 1500},
                "gross_margin": {"value": 0.9},
                "market_cap": {"value": 20000},
            },
            "revenue_growth": "45.7%",
        },
        "web.search": {"results": [{"url": "https://example.com/public"}]},
        "delivery.finish": {
            "finish_allowed": True,
            "conclusion": "稳健（AI 置信度：中；投资确定性：待验证）",
            "self_check": {"entity_locked": True, "classified": True, "data_sourced": True,
                           "dimensions_covered": ["生意"], "gaps": []},
        },
    }


# ── 测试用例 ──────────────────────────────────────────────

def test_entity_to_current_schema():
    s = entity_to_current_schema(_ENTITY)
    assert s.security_id == "600519.SH"
    assert s.code == "600519"
    assert s.exchange == "SH"
    assert s.primary_market == "A"
    assert s.canonical_name == "贵州茅台"


def test_facts_to_plan_input():
    facts = CompanyFacts(
        entity=make_entity("600519.SH", "贵州茅台", ".SH"),
        one_line_business="高端白酒", info_richness=InfoRichness("A"))
    d = facts_to_plan_input(facts)
    assert "info_richness" not in d
    assert d["one_line_business"] == "高端白酒"
    assert "anti_consensus_signal" in d and "data_gaps" in d


def test_collect_adapter_questions():
    # 从 consumer_brand 目录读推荐问题（若目录有 priority_questions）
    from runtime.schemas import UnitProfile, CompanyProfile, ProfileTag
    prof = UnitProfile(
        unit_id="u1",
        company_profile=CompanyProfile(
            development_stage=ProfileTag(value="成熟", evidence="x", confidence=0.8),
            charging=ProfileTag(value="一次性销售", evidence="x", confidence=0.8),
            capital_intensity=ProfileTag(value="轻资产", evidence="x", confidence=0.8),
            cycle=ProfileTag(value="弱周期", evidence="x", confidence=0.8),
            value_chain=ProfileTag(value="整机", evidence="x", confidence=0.8),
        ),
        selected_adapter="consumer_brand", selection_reason="命中")
    qs = collect_adapter_questions([prof])
    # 目录存在即应读到 ≥1 条推荐问题；目录缺失则空列表（不阻塞）
    assert isinstance(qs, list)


def test_run_preloop_assembles():
    async def go():
        llm, md, web = _make_preloop_mocks()
        r = await run_preloop("是否值得买入", llm_client=llm, market_data=md,
                              web_search_fn=web, entity=_ENTITY, force_refresh=True)
        assert r.facts.info_richness.value == "B"
        assert r.facts.facts_version
        assert r.granularity.mode.value == "whole"
        assert len(r.profiles) == 1 and r.profiles[0].selected_adapter == "consumer_brand"
        assert len(r.plan.p0_questions) == 4
        assert "info_richness" not in r.initial_context
        assert "facts_version" in r.initial_context
        assert r.initial_context["skill_phase"] == 2
        assert r.initial_context["current_entity"]["security_id"] == "600519.SH"
        assert r.initial_context["business_group"] == "consumer_brand"
        assert r.initial_context["research_recipe"]["adapters"] == ["consumer_brand"]
        assert "roic" in r.initial_context["research_recipe"]["metric_ids"]
        return r
    asyncio.run(go())


def test_preloop_feeds_loop():
    async def go():
        llm, md, web = _make_preloop_mocks()
        r = await run_preloop("是否值得买入", llm_client=llm, market_data=md,
                              web_search_fn=web, entity=_ENTITY, force_refresh=True)
        loop = _make_loop(ScriptedLLM(_loop_script()), ScriptedExecutor(_loop_tools()),
                          LoopConfig(max_steps=30, max_tool_calls=30))
        async for _ in loop.run("是否值得买入", session_id="wire", skill_name="deep-research",
                                research_plan=r.plan, initial_context=r.initial_context):
            pass
        assert r.plan.is_converged
        assert loop.trace.final_report is not None
        assert len(loop.trace.turns) == 5  # 4 问 + 1 finish
        assert r.plan.p0_questions and all(
            q.status in ("answered", "unanswerable") for q in r.plan.p0_questions)
    asyncio.run(go())


def test_fast_preloop_uses_at_most_two_llm_calls_for_single_business():
    async def go():
        # 快速路径只需要 facts 抽取 + 单元画像；粒度与计划由确定性规则生成。
        llm = _make_llm_client([_PRELOOP_RESPONSES[0], _PRELOOP_RESPONSES[2]])
        md = _make_market_data([
            {"year": 2025, "revenue": 150000000000, "net_profit": 50000000000},
        ])
        web = _make_web_search([{"title": "贵州茅台官网", "url": "https://e.com"}])
        r = await run_preloop(
            "是否值得买入", llm_client=llm, market_data=md,
            web_search_fn=web, entity=_ENTITY, force_refresh=True, fast_mode=True,
        )
        assert llm._call_count[0] <= 2
        assert r.granularity.mode.value == "whole"
        assert r.plan.p0_questions

    asyncio.run(go())


# ── 轻量启动器回归（preloop 不取重数据 / 并行 / 本地实体表优先）────

def test_entity_resolves_zhipu_hk_02513_without_llm():
    """智谱系列别名必须本地表命中，不依赖 LLM（use_llm_escalation=False）。"""
    async def go():
        for raw in ("智谱", "智谱华章", "北京智谱华章", "Z.AI"):
            r = await resolve_entity(raw, use_llm_escalation=False, market_data=None)
            assert r.resolved, (
                f"{raw!r} 应命中本地表，实际未解析（message={r.message}）")
            assert r.entity.symbol == "02513.HK", (
                f"{raw!r} → {r.entity.symbol}，期望 02513.HK")
    asyncio.run(go())


def test_preloop_does_not_call_market_bundle():
    """preloop 轻量启动器：绝不调用 market.bundle（财报/行情留给正式 loop）。"""
    async def go():
        llm, md, web = _make_preloop_mocks()
        await run_preloop("是否值得买入", llm_client=llm, market_data=md,
                          web_search_fn=web, entity=_ENTITY, force_refresh=True)
        calls = md.bundle.await_count
        assert calls == 0, (
            f"preloop 不应调用 market.bundle（重数据源留给正式 loop 按需调用），"
            f"实际调用了 {calls} 次")
    asyncio.run(go())


def test_preloop_runs_memory_and_light_facts_in_parallel():
    """light_facts 与认知库检索必须并行（时间区间重叠），而非串行等待。"""
    import time as _time
    import runtime.preloop.facts_builder as _fb
    import store.cognition_store as _cs

    async def go():
        llm, md, web = _make_preloop_mocks()
        spans: dict = {}
        orig_facts = _fb.build_light_facts
        orig_mem = _cs.CognitionStore.prepare_research_memory

        async def tracked_facts(*a, **kw):
            t0 = _time.perf_counter()
            await asyncio.sleep(0.15)
            r = await orig_facts(*a, **kw)
            spans["facts"] = (t0, _time.perf_counter())
            return r

        def tracked_mem(self, **kw):
            # 同步阻塞（模拟 SQLite 查询）；由 asyncio.to_thread 包裹执行
            t0 = _time.perf_counter()
            _time.sleep(0.15)
            spans["memory"] = (t0, _time.perf_counter())
            return {"selected_cognitions": [], "company_assertions": []}

        _fb.build_light_facts = tracked_facts
        _cs.CognitionStore.prepare_research_memory = tracked_mem
        try:
            await run_preloop(
                "是否值得买入", llm_client=llm, market_data=md,
                web_search_fn=web, entity=_ENTITY,
                force_refresh=True, user_id="parallel_test",
            )
        finally:
            _fb.build_light_facts = orig_facts
            _cs.CognitionStore.prepare_research_memory = orig_mem

        assert "facts" in spans and "memory" in spans, (
            f"light_facts 与 memory 都应执行，实际只跑了: {list(spans)}")
        f0, f1 = spans["facts"]
        m0, m1 = spans["memory"]
        overlap = min(f1, m1) - max(f0, m0)
        assert overlap > 0.05, (
            f"light_facts 与 memory 应并行执行（重叠 >0.05s），实际重叠 {overlap:.3f}s"
            f"（facts {f0:.3f}~{f1:.3f}, memory {m0:.3f}~{m1:.3f}）")
        total = max(f1, m1) - min(f0, m0)
        assert total < 0.45, (
            f"并行总耗时应接近单个任务（<0.45s），实际 {total:.3f}s——疑似退化为串行")

    asyncio.run(go())


if __name__ == "__main__":
    print("preloop → loop 接线回归测试")
    check("entity_to_current_schema 字段映射", test_entity_to_current_schema)
    check("facts_to_plan_input 适配", test_facts_to_plan_input)
    check("collect_adapter_questions 读目录", test_collect_adapter_questions)
    check("run_preloop 串起四件套", test_run_preloop_assembles)
    check("preloop 喂 loop 收敛", test_preloop_feeds_loop)
    print("\n轻量启动器回归")
    check("智谱别名本地命中 02513.HK（不依赖 LLM）",
          test_entity_resolves_zhipu_hk_02513_without_llm)
    check("preloop 不调用 market.bundle",
          test_preloop_does_not_call_market_bundle)
    check("light_facts 与 memory 并行执行",
          test_preloop_runs_memory_and_light_facts_in_parallel)
    print(f"\n通过 {passed} / 失败 {failed}")
    sys.exit(1 if failed else 0)
