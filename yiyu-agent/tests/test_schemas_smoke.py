"""快速冒烟测试 runtime/schemas.py 的关键校验点"""
from runtime.schemas import (
    CurrentEntitySchema, AliasType, EntitySource,
    CompanyFacts, FinancialSnapshot, BusinessSegment, SourceTrace, InfoRichness,
    GranularityDecision, GranularityMode, ResearchUnit, DifferentialDimension,
    CompanyProfile, ProfileTag, UnitProfile,
    ResearchPlan, ResearchQuestion, DimensionCoverage, PlanMetadata,
    ResearchDimension, QuestionLevel, Priority, QuestionStatus, DimensionMode,
    PreloopResult,
)
from pydantic import ValidationError

passed = 0
failed = 0


def check(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  ✓ {name}")
        passed += 1
    except Exception as e:
        print(f"  ✗ {name}: {e}")
        failed += 1


# ── 公共实体 ──────────────────────────────────────────────
_entity = CurrentEntitySchema(
    canonical_name="腾讯控股", security_id="00700.HK",
    code="00700", exchange="HK", primary_market="HK",
    entity_source=EntitySource.EXPLICIT, alias_type=AliasType.ABBREVIATION,
)

def _all_dim_coverage():
    dims = list(ResearchDimension)
    return [
        DimensionCoverage(dimension=d, mode=DimensionMode.QUESTION, covered_by=f"Q0{i+1}")
        for i, d in enumerate(dims)
    ]

def _make_q(qid, dim, level=QuestionLevel.COMPANY, unit_id=None):
    return ResearchQuestion(
        question_id=qid, level=level, unit_id=unit_id,
        dimension=dim, priority=Priority.P0,
        question="测试问题正文（超过5字）",
        decision_relevance="影响核心判断",
        required_evidence=["证据A"], falsification=["反证X"],
        completion_rule="形成倾向判断",
    )


# ── 1. CurrentEntitySchema ──────────────────────────────
print("\n[1] CurrentEntitySchema")

def t_entity_ok():
    e = CurrentEntitySchema(
        canonical_name="兆易创新", security_id="603986.SH",
        code="603986", exchange="SH", primary_market="A",
        entity_source=EntitySource.EXPLICIT, alias_type=AliasType.ABBREVIATION,
    )
    assert e.canonical_name == "兆易创新"

def t_entity_brand_no_scope():
    try:
        CurrentEntitySchema(
            canonical_name="阿里妈妈", security_id="09988.HK",
            code="09988", exchange="HK", primary_market="HK",
            entity_source=EntitySource.EXPLICIT,
            alias_type=AliasType.BRAND_OR_SUBSIDIARY,
            scope_note=None,
        )
        raise AssertionError("应该报错")
    except ValidationError:
        pass

check("entity 正常创建", t_entity_ok)
check("brand_or_subsidiary 缺 scope_note 报错", t_entity_brand_no_scope)


# ── 2. CompanyFacts ──────────────────────────────────────
print("\n[2] CompanyFacts")

def t_facts_ok():
    f = CompanyFacts(
        entity=_entity,
        one_line_business="以游戏、广告为主业的互联网集团",
        segments=[BusinessSegment(name="游戏", revenue_share=0.3)],
        info_richness=InfoRichness.A,
        source_trace=[SourceTrace(field="segments", source="年报2024", source_level="S")],
    )
    v = f.compute_version()
    assert len(v) == 16
    assert f.facts_version == v

def t_facts_no_trace():
    try:
        CompanyFacts(
            entity=_entity,
            one_line_business="以游戏为主业",
            segments=[BusinessSegment(name="游戏")],
            info_richness=InfoRichness.B,
            source_trace=[],
        )
        raise AssertionError("应该报错")
    except ValidationError:
        pass

def t_facts_richness_reset():
    """B/C 级设 anti_consensus_triggered=True 应被 validator 重置为 False。"""
    f = CompanyFacts(
        entity=_entity,
        one_line_business="以游戏为主业",
        info_richness=InfoRichness.B,
        anti_consensus_triggered=True,  # 误用 → 应被重置
        source_trace=[SourceTrace(field="one_line_business", source="x", source_level="S")],
    )
    assert f.anti_consensus_triggered is False

check("facts 正常创建 + compute_version", t_facts_ok)
check("facts 缺 source_trace 报错", t_facts_no_trace)
check("B级误设反共识标记被重置", t_facts_richness_reset)


# ── 3. GranularityDecision ──────────────────────────────
print("\n[3] GranularityDecision")

def t_whole_ok():
    g = GranularityDecision(
        mode=GranularityMode.WHOLE,
        units=[ResearchUnit(id="u_company", scope="公司整体", reason="整体研究")],
        source="年报分部信息", confidence=0.85, facts_version="abc123",
    )
    assert g.unit_ids == ["u_company"]
    assert not g.needs_sotp

def t_split_ok():
    g = GranularityDecision(
        mode=GranularityMode.SPLIT,
        units=[
            ResearchUnit(id="u_game", scope="游戏", reason="盈利模式与估值逻辑不同",
                         differential_dimensions=[DifferentialDimension.PROFIT_MODEL]),
            ResearchUnit(id="u_fintech", scope="金融科技", reason="资本投入与监管逻辑独立",
                         differential_dimensions=[DifferentialDimension.CAPITAL_INPUT]),
        ],
        source="年报", confidence=0.8, facts_version="abc123",
    )
    assert g.needs_sotp

def t_split_one_unit_fails():
    try:
        GranularityDecision(
            mode=GranularityMode.SPLIT,
            units=[ResearchUnit(id="u1", scope="A", reason="整体研究",
                                differential_dimensions=[DifferentialDimension.PROFIT_MODEL])],
            source="x", confidence=0.8, facts_version="x",
        )
        raise AssertionError("应该报错")
    except ValidationError:
        pass

def t_split_no_diff_dim_fails():
    try:
        GranularityDecision(
            mode=GranularityMode.SPLIT,
            units=[
                ResearchUnit(id="u1", scope="A", reason="盈利模式不同",
                             differential_dimensions=[DifferentialDimension.PROFIT_MODEL]),
                ResearchUnit(id="u2", scope="B", reason="业务较多",
                             differential_dimensions=[]),
            ],
            source="x", confidence=0.8, facts_version="x",
        )
        raise AssertionError("应该报错")
    except ValidationError:
        pass

check("granularity whole 正常", t_whole_ok)
check("granularity split 多单元正常", t_split_ok)
check("split 仅1个单元报错", t_split_one_unit_fails)
check("split 单元缺 differential_dimensions 报错", t_split_no_diff_dim_fails)


# ── 4. CompanyProfile / UnitProfile ─────────────────────
print("\n[4] CompanyProfile / UnitProfile")

def _make_cp(cycle_conf=0.75):
    return CompanyProfile(
        development_stage=ProfileTag(value="成熟", evidence="CAGR 8%", confidence=0.9),
        charging=ProfileTag(value="广告", evidence="年报", confidence=0.8),
        capital_intensity=ProfileTag(value="轻资产", evidence="固定资产占比低", confidence=0.7),
        cycle=ProfileTag(value="弱周期", evidence="用户黏性强", confidence=cycle_conf),
        value_chain=ProfileTag(value="平台", evidence="双边平台", confidence=0.75),
    )

def t_profile_unverified():
    cp = _make_cp(cycle_conf=0.5)
    assert "cycle" in cp.unverified_tags

def t_unit_profile_syncs():
    cp = _make_cp(cycle_conf=0.5)
    up = UnitProfile(unit_id="u_ad", company_profile=cp,
                     selected_adapter="G4", selection_reason="平台命中G4")
    assert "cycle" in up.unverified_tags

def t_profile_evidence_empty_fails():
    try:
        ProfileTag(value="成熟", evidence="", confidence=0.9)
        raise AssertionError("应该报错")
    except ValidationError:
        pass

check("低置信度维度进 unverified_tags", t_profile_unverified)
check("UnitProfile 自动同步 unverified_tags", t_unit_profile_syncs)
check("evidence 为空字符串报错", t_profile_evidence_empty_fails)


# ── 5. ResearchPlan ──────────────────────────────────────
print("\n[5] ResearchPlan")

def t_plan_ok():
    dims = list(ResearchDimension)
    questions = [_make_q(f"Q0{i+1}", d) for i, d in enumerate(dims)]
    plan = ResearchPlan(
        goal="判断腾讯生意质量与估值位置",
        questions=questions,
        dimension_coverage=_all_dim_coverage(),
        metadata=PlanMetadata(planner_model="deepseek-v3", facts_version="abc123"),
    )
    assert len(plan.p0_questions()) == 7
    assert plan.warn_if_oversized() == []

def t_plan_missing_dim_fails():
    try:
        dims = list(ResearchDimension)[:-1]  # 少一维
        questions = [_make_q(f"Q0{i+1}", d) for i, d in enumerate(dims)]
        coverage = [
            DimensionCoverage(dimension=d, mode=DimensionMode.QUESTION, covered_by=f"Q0{i+1}")
            for i, d in enumerate(dims)
        ]
        ResearchPlan(
            goal="测试", questions=questions, dimension_coverage=coverage,
            metadata=PlanMetadata(planner_model="m", facts_version="x"),
        )
        raise AssertionError("应该报错")
    except ValidationError:
        pass

def t_exempted_no_reason_fails():
    try:
        DimensionCoverage(
            dimension=ResearchDimension.VALUATION,
            mode=DimensionMode.EXEMPTED,
            covered_by=None, reason=None,
        )
        raise AssertionError("应该报错")
    except ValidationError:
        pass

def t_segment_no_unit_id_fails():
    try:
        ResearchQuestion(
            question_id="Q01", level=QuestionLevel.SEGMENT, unit_id=None,
            dimension=ResearchDimension.MOAT, priority=Priority.P0,
            question="游戏护城河是否可持续",
            decision_relevance="x", required_evidence=["x"],
            falsification=["y"], completion_rule="z",
        )
        raise AssertionError("应该报错")
    except ValidationError:
        pass

def t_validate_unit_refs():
    q = _make_q("Q01", ResearchDimension.MOAT, QuestionLevel.SEGMENT, "u_nonexistent")
    dims = list(ResearchDimension)
    questions = [q] + [_make_q(f"Q0{i+2}", d) for i, d in enumerate(dims[1:])]
    plan = ResearchPlan(
        goal="判断护城河是否可持续", questions=questions,
        dimension_coverage=_all_dim_coverage(),
        metadata=PlanMetadata(planner_model="m", facts_version="x"),
    )
    errors = plan.validate_unit_refs(["u_game", "u_ad"])
    assert len(errors) == 1 and "u_nonexistent" in errors[0]

def t_plan_warn_p0_oversized():
    """P0 超过10个时 warn_if_oversized 返回告警"""
    dims = list(ResearchDimension)
    base_q = [_make_q(f"Q0{i+1}", d) for i, d in enumerate(dims)]
    # 补充到12个P0
    extra = [
        ResearchQuestion(
            question_id=f"QX{i}", level=QuestionLevel.COMPANY, unit_id=None,
            dimension=ResearchDimension.MOAT, priority=Priority.P0,
            question=f"额外P0问题{i}超过5字",
            decision_relevance="x", required_evidence=["x"],
            falsification=["y"], completion_rule="z",
        )
        for i in range(5)
    ]
    coverage = _all_dim_coverage()
    plan = ResearchPlan(
        goal="验证P0问题数量超限告警机制", questions=base_q + extra,
        dimension_coverage=coverage,
        metadata=PlanMetadata(planner_model="m", facts_version="x"),
    )
    warnings = plan.warn_if_oversized()
    assert any("P0" in w for w in warnings)

check("plan 7维全覆盖正常创建", t_plan_ok)
check("plan 缺少维度报错", t_plan_missing_dim_fails)
check("exempted 缺 reason 报错", t_exempted_no_reason_fails)
check("segment 问题缺 unit_id 报错", t_segment_no_unit_id_fails)
check("validate_unit_refs 检测非法引用", t_validate_unit_refs)
check("P0 超限时 warn_if_oversized 告警", t_plan_warn_p0_oversized)


# ── 6. PreloopResult ─────────────────────────────────────
print("\n[6] PreloopResult")

def _make_preloop(unit_profiles_count=1):
    facts = CompanyFacts(
        entity=_entity, one_line_business="以游戏为主业",
        segments=[BusinessSegment(name="游戏")],
        info_richness=InfoRichness.A,
        source_trace=[SourceTrace(field="segments", source="年报", source_level="S")],
        facts_version="abc123",
    )
    gran = GranularityDecision(
        mode=GranularityMode.WHOLE,
        units=[ResearchUnit(id="u_company", scope="公司整体", reason="整体研究")],
        source="x", confidence=0.9, facts_version="abc123",
    )
    cp = _make_cp()
    profiles = [
        UnitProfile(unit_id=f"u_company", company_profile=cp,
                    selected_adapter="G4", selection_reason="平台命中G4")
        for _ in range(unit_profiles_count)
    ]
    dims = list(ResearchDimension)
    questions = [_make_q(f"Q0{i+1}", d) for i, d in enumerate(dims)]
    plan = ResearchPlan(
        goal="研究腾讯生意质量", questions=questions,
        dimension_coverage=_all_dim_coverage(),
        metadata=PlanMetadata(planner_model="m", facts_version="abc123"),
    )
    return _entity, facts, gran, profiles, plan

def t_preloop_ok():
    entity, facts, gran, profiles, plan = _make_preloop(1)
    result = PreloopResult(entity=entity, facts=facts, granularity=gran,
                           unit_profiles=profiles, plan=plan)
    s = result.summary()
    assert s["granularity_mode"] == "whole"
    assert s["p0_count"] == 7
    assert s["unit_count"] == 1

def t_preloop_unit_mismatch_fails():
    try:
        entity, facts, gran, profiles, plan = _make_preloop(2)  # 多传一个profile
        PreloopResult(entity=entity, facts=facts, granularity=gran,
                      unit_profiles=profiles, plan=plan)
        raise AssertionError("应该报错")
    except ValidationError:
        pass

check("PreloopResult 正常创建 + summary", t_preloop_ok)
check("unit_profiles 数量不一致报错", t_preloop_unit_mismatch_fails)


# ── 汇总 ─────────────────────────────────────────────────
print(f"\n{'='*40}")
total = passed + failed
print(f"通过 {passed} / {total}  {'✓ 全部通过' if failed == 0 else f'✗ {failed} 个失败'}")
