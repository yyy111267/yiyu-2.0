"""
E 类用例评测：公司画像与 Adapter 路由（10 条）
  正向：E-01 ~ E-05
  防御：E-D1 ~ E-D5（pass^k=5）

运行：
  PYTHONPATH=. python3 tests/eval_profiler_e.py
  PYTHONPATH=. python3 tests/eval_profiler_e.py --defence-k 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from runtime.preloop.profiler import (
    ADAPTER_CATALOG,
    FALLBACK_ADAPTER_ID,
    TOKEN_BUDGET_INITIAL,
    TokenBudgetState,
    _make_fallback_profile,
    _parse_and_validate,
    build_unit_profiles,
    clear_cache,
)
from runtime.schemas import (
    AliasType,
    BusinessSegment,
    CompanyFacts,
    CurrentEntitySchema,
    EntitySource,
    FinancialSnapshot,
    GranularityDecision,
    GranularityMode,
    InfoRichness,
    ResearchUnit,
    DifferentialDimension,
    SourceTrace,
)

logging.basicConfig(level=logging.WARNING)


# ════════════════════════════════════════════════════════════════
# 测试基础设施
# ════════════════════════════════════════════════════════════════

@dataclass
class Result:
    case_id: str
    desc: str
    verdict: str
    p0_hits: list[str] = field(default_factory=list)
    p1_hits: list[str] = field(default_factory=list)
    notes: list[str]   = field(default_factory=list)


def _verdict(p0, p1):
    if p0: return "FAIL"
    if not p1: return "PASS"
    return "PARTIAL" if len(p1) <= 2 else "FAIL"


def _llm(responses: list[dict]):
    state = {"calls": 0}
    async def chat_json(system, user, temperature=0.1, **kw):
        idx = min(state["calls"], len(responses) - 1)
        state["calls"] += 1
        return responses[idx]
    m = MagicMock()
    m.chat_json = chat_json
    m._state = state
    return m


def _make_entity(name, security_id, market="A"):
    code, exchange = security_id.split(".") if "." in security_id else (security_id, "SH")
    return CurrentEntitySchema(
        canonical_name=name, security_id=security_id,
        code=code, exchange=exchange, primary_market=market,
        entity_source=EntitySource.EXPLICIT, alias_type=AliasType.ABBREVIATION,
    )


def _make_facts(entity, segments_raw, richness=InfoRichness.A, fv="v1", snap=None):
    segs = [BusinessSegment(
        name=s["name"], revenue_share=s.get("revenue_share"),
        customer=s.get("customer"), charging=s.get("charging"),
        competition=s.get("competition"), capex_profile=s.get("capex_profile"),
    ) for s in segments_raw]
    return CompanyFacts(
        entity=entity,
        one_line_business=f"以{segments_raw[0]['name']}为主的公司",
        segments=segs,
        financial_snapshot=snap or FinancialSnapshot(
            revenue_ttm=50_000, gross_margin=0.45, ocf=10_000, capex=2_000),
        info_richness=richness,
        source_trace=[SourceTrace(field="segments", source="年报", source_level="S")],
        facts_version=fv,
    )


def _make_gran(entity_name, units_raw, fv="v1"):
    units = [ResearchUnit(
        id=u["id"], scope=u["scope"], reason=u.get("reason", "整体研究"),
        differential_dimensions=[DifferentialDimension(d) for d in u.get("dims", [])],
    ) for u in units_raw]
    mode = GranularityMode.SPLIT if len(units) > 1 else GranularityMode.WHOLE
    return GranularityDecision(
        mode=mode, units=units, source="年报",
        confidence=0.9, facts_version=fv,
    )


# ── 合法 LLM 响应构造器 ─────────────────────────────────────────

def _good_resp(adapter="consumer_brand", stage="成熟", charging="一次性销售",
               capital="轻资产", cycle="弱周期", chain="应用",
               secondary=None, pending=False):
    return {
        "development_stage": {"value": stage,    "evidence": "近3年营收CAGR稳定", "confidence": 0.85},
        "charging":          {"value": charging, "evidence": "年报披露收费模式",   "confidence": 0.9},
        "capital_intensity": {"value": capital,  "evidence": "固定资产占比<5%",    "confidence": 0.8},
        "cycle":             {"value": cycle,    "evidence": "需求相对稳定",        "confidence": 0.75},
        "value_chain":       {"value": chain,    "evidence": "产品处于应用层",      "confidence": 0.85},
        "selected_adapter":  adapter,
        "secondary_adapter": secondary,
        "selection_reason":  f"命中{adapter}适用条件",
        "pending_verify":    pending,
    }


# ── 冻结夹具 ───────────────────────────────────────────────────

_ZHIJI = _make_entity("兆易创新", "603986.SH")
_ZHIJI_FACTS = _make_facts(_ZHIJI, [
    {"name": "NOR Flash", "revenue_share": 0.65, "charging": "一次性销售",
     "customer": "消费电子/工业", "competition": "华邦/旺宏", "capex_profile": "轻资产（Fabless）"},
    {"name": "MCU",       "revenue_share": 0.25, "charging": "一次性销售",
     "customer": "工业/汽车", "competition": "中颖/ST", "capex_profile": "轻资产（Fabless）"},
], fv="zhiji_v1")

_TENCENT = _make_entity("腾讯控股", "00700.HK", "HK")
_TENCENT_FACTS = _make_facts(_TENCENT, [
    {"name": "增值服务（游戏）", "revenue_share": 0.50, "charging": "虚拟道具/订阅",
     "customer": "个人玩家", "capex_profile": "轻资产"},
    {"name": "网络广告",       "revenue_share": 0.17, "charging": "按效果计费",
     "customer": "广告主",   "capex_profile": "轻资产"},
    {"name": "金融科技与企业服务", "revenue_share": 0.32, "charging": "费率抽成/SaaS订阅",
     "customer": "个人/企业", "capex_profile": "中等（监管合规）"},
], fv="tencent_v1")

_ZHIJI_GRAN = _make_gran("兆易创新", [{"id": "u_company", "scope": "公司整体"}], "zhiji_v1")
_TENCENT_GRAN = _make_gran("腾讯控股", [
    {"id": "u_game",   "scope": "游戏业务",     "dims": ["profit_model"],
     "reason": "盈利模式为虚拟道具付费，与广告/金融不同"},
    {"id": "u_ad",     "scope": "网络广告",     "dims": ["profit_model"],
     "reason": "流量变现模式，护城河来自微信生态"},
    {"id": "u_fintech","scope": "金融科技与企业服务", "dims": ["capital_input", "valuation_logic"],
     "reason": "重监管合规资本投入，估值受监管周期影响"},
], "tencent_v1")


# ════════════════════════════════════════════════════════════════
# 正向用例
# ════════════════════════════════════════════════════════════════

async def run_E01() -> Result:
    """E-01 单元画像与合法选型：5 维齐全 + 合法 Adapter ID。"""
    clear_cache()
    profiles = await build_unit_profiles(
        _ZHIJI_FACTS, _ZHIJI_GRAN,
        _llm([_good_resp("semiconductor", stage="成熟", chain="零部件")]),
    )
    p0, p1 = [], []
    assert len(profiles) == 1
    up = profiles[0]

    # P0: 5 维标签 value 在枚举范围内
    errors = up.company_profile.validate_enum_values()
    for e in errors:
        p0.append(f"枚举越界: {e}")

    # P0: selected_adapter 合法
    if up.selected_adapter not in ADAPTER_CATALOG:
        p0.append(f"selected_adapter='{up.selected_adapter}' 不在目录")

    # P0: adapter_fallback=False（正常路径不走降级）
    if up.adapter_fallback:
        p0.append("正常路径不应有 adapter_fallback=True")

    # P1: selection_reason 非空
    if not up.selection_reason:
        p1.append("selection_reason 为空")

    # P1: 5 维 evidence 均非空
    for dim_name in ("development_stage", "charging", "capital_intensity", "cycle", "value_chain"):
        tag = getattr(up.company_profile, dim_name)
        if not tag.evidence.strip():
            p1.append(f"{dim_name}.evidence 为空")

    return Result("E-01", "单元画像与合法选型", _verdict(p0, p1), p0, p1)


async def run_E02() -> Result:
    """E-02 split per-unit 差异化路由：三单元各独立画像与选型。"""
    clear_cache()
    profiles = await build_unit_profiles(
        _TENCENT_FACTS, _TENCENT_GRAN,
        _llm([
            _good_resp("digital_software_platform", stage="成熟", charging="广告", chain="平台"),  # u_game
            _good_resp("digital_software_platform", stage="成熟", charging="广告", chain="平台"),  # u_ad
            _good_resp("digital_software_platform", stage="成熟", charging="按量计费", chain="平台"),  # u_fintech
        ]),
    )
    p0, p1 = [], []

    # P0: 三单元各产出完整画像
    if len(profiles) != 3:
        p0.append(f"期望 3 个 UnitProfile，得到 {len(profiles)}")

    # P0: 无遗漏，每个 unit_id 对应一个 profile
    expected_ids = {"u_game", "u_ad", "u_fintech"}
    actual_ids   = {p.unit_id for p in profiles}
    missing = expected_ids - actual_ids
    if missing:
        p0.append(f"缺少单元的画像: {missing}")

    # P0: 每个 profile 的 Adapter 合法
    for up in profiles:
        if up.selected_adapter not in ADAPTER_CATALOG:
            p0.append(f"unit={up.unit_id} selected_adapter='{up.selected_adapter}' 不在目录")

    # P1: 三单元均非 fallback（夹具 LLM 都给合法输出）
    fallbacks = [p.unit_id for p in profiles if p.adapter_fallback]
    if fallbacks:
        p1.append(f"以下单元走了回退: {fallbacks}")

    return Result("E-02", "split per-unit 差异化路由", _verdict(p0, p1), p0, p1)


async def run_E03() -> Result:
    """E-03 业务交叉双 Adapter + pending_verify 标记。"""
    clear_cache()
    dual_resp = _good_resp("digital_software_platform", secondary="semiconductor", pending=True)
    profiles = await build_unit_profiles(
        _ZHIJI_FACTS, _ZHIJI_GRAN, _llm([dual_resp]),
    )
    up = profiles[0]
    p0, p1 = [], []

    # P0: 两项 Adapter 均合法
    if up.selected_adapter not in ADAPTER_CATALOG:
        p0.append(f"selected_adapter='{up.selected_adapter}' 不在目录")
    if up.secondary_adapter and up.secondary_adapter not in ADAPTER_CATALOG:
        p0.append(f"secondary_adapter='{up.secondary_adapter}' 不在目录")

    # P1: pending_verify 存在
    if not up.pending_verify:
        p1.append("双 Adapter 场景应有 pending_verify=True")
    if not up.secondary_adapter:
        p1.append("双 Adapter 场景 secondary_adapter 为空")

    return Result("E-03", "双 Adapter 待验证", _verdict(p0, p1), p0, p1)


async def run_E04() -> Result:
    """E-04 重复路由一致（pass^5 在防御用例里；此处做同 mock 5 次确定性验证）。"""
    # 用 force_refresh 绕缓存，直接重复调 LLM
    client = _llm([_good_resp("semiconductor")] * 10)
    adapters, unit_ids_set = [], set()
    for _ in range(5):
        clear_cache()
        profiles = await build_unit_profiles(
            _ZHIJI_FACTS, _ZHIJI_GRAN, client, force_refresh=True
        )
        adapters.append(profiles[0].selected_adapter)
        unit_ids_set.add(profiles[0].unit_id)

    p0, p1 = [], []
    if len(set(adapters)) > 1:
        p0.append(f"5 次路由 Adapter 不一致: {adapters}")
    if len(unit_ids_set) > 1:
        p1.append(f"unit_id 不一致: {unit_ids_set}")

    return Result("E-04", "重复路由一致（5次）", _verdict(p0, p1), p0, p1)


async def run_E05() -> Result:
    """E-05 主选 + 备选配置齐全。"""
    clear_cache()
    resp = _good_resp("consumer_brand", secondary="advanced_manufacturing", pending=False)
    profiles = await build_unit_profiles(_ZHIJI_FACTS, _ZHIJI_GRAN, _llm([resp]))
    up = profiles[0]
    p0, p1 = [], []

    # P1: 主选备选齐全且不重复
    if not up.secondary_adapter:
        p1.append("secondary_adapter 为空，未配置备选")
    elif up.secondary_adapter == up.selected_adapter:
        p1.append(f"备选与主选重复: {up.secondary_adapter}")
    if up.secondary_adapter and up.secondary_adapter not in ADAPTER_CATALOG:
        p1.append(f"secondary_adapter='{up.secondary_adapter}' 不在目录")

    return Result("E-05", "主选 + 备选配置", _verdict(p0, p1), p0, p1)


# ════════════════════════════════════════════════════════════════
# 防御用例
# ════════════════════════════════════════════════════════════════

async def run_E_D1() -> Result:
    """E-D1 幻觉 Adapter ID 被拦截，带错误退回重选，上限 2 次后回退通用。"""
    clear_cache()
    hallucin = _good_resp("adapter_semiconductor_v9")  # 不存在的 ID
    # 第 1/2 次幻觉 → 第 3 次合法（重试上限 2 次后用第 3 次，否则回退）
    profiles = await build_unit_profiles(
        _ZHIJI_FACTS, _ZHIJI_GRAN,
        _llm([hallucin, hallucin, _good_resp("semiconductor")]),
    )
    up = profiles[0]
    p0, p1 = [], []

    # P0: 幻觉 ID 未进入下游
    if up.selected_adapter == "adapter_semiconductor_v9":
        p0.append("幻觉 ID 进入了最终结果，拦截失效")

    # P0: 最终结果是合法 Adapter（重选成功或回退通用）
    if up.selected_adapter not in ADAPTER_CATALOG:
        p0.append(f"最终 selected_adapter='{up.selected_adapter}' 仍不合法")

    return Result("E-D1", "幻觉 Adapter ID 被拦截", _verdict(p0, p1), p0, p1)


async def run_E_D2() -> Result:
    """E-D2 枚举越界标签回退通用 Adapter + pending_verify 标记。"""
    clear_cache()
    invalid_enum = {
        "development_stage": {"value": "爆发期", "evidence": "增速很快", "confidence": 0.9},
        "charging":          {"value": "一次性销售", "evidence": "年报", "confidence": 0.9},
        "capital_intensity": {"value": "轻资产", "evidence": "固定资产低", "confidence": 0.8},
        "cycle":             {"value": "弱周期", "evidence": "稳定", "confidence": 0.8},
        "value_chain":       {"value": "应用", "evidence": "软件应用层", "confidence": 0.8},
        "selected_adapter": "consumer_brand",
        "secondary_adapter": None,
        "selection_reason": "x",
        "pending_verify": False,
    }
    # 第 1/2 次越界 → 第 3 次合法（触发回退通用）
    profiles = await build_unit_profiles(
        _ZHIJI_FACTS, _ZHIJI_GRAN,
        _llm([invalid_enum, invalid_enum, _good_resp("semiconductor")]),
    )
    up = profiles[0]
    p0, p1 = [], []

    # P0: 越界标签（"爆发期"）未写入交付画像
    stage_val = up.company_profile.development_stage.value
    if stage_val == "爆发期":
        p0.append("越界标签「爆发期」进入了最终画像，校验失效")

    # P0: 回退通用 Adapter 后 pending_verify=True
    if up.adapter_fallback and not up.pending_verify:
        p0.append("回退通用 Adapter 后应标记 pending_verify=True")

    return Result("E-D2", "枚举越界回退通用 Adapter", _verdict(p0, p1), p0, p1)


async def run_E_D3() -> Result:
    """E-D3 低置信维度列入 unverified_tags，不驱动强结论。"""
    clear_cache()
    low_conf_resp = {
        "development_stage": {"value": "高增长",  "evidence": "CAGR 40%",  "confidence": 0.9},
        "charging":          {"value": "订阅",    "evidence": "ARR 占60%",  "confidence": 0.8},
        "capital_intensity": {"value": "轻资产",  "evidence": "固定资产低",  "confidence": 0.7},
        "cycle":             {"value": "成长周期", "evidence": "AI 周期波动", "confidence": 0.45},  # < 0.6
        "value_chain":       {"value": "应用",    "evidence": "软件层",      "confidence": 0.5},   # < 0.6
        "selected_adapter": "digital_software_platform",
        "secondary_adapter": None,
        "selection_reason": "AI早期命中 digital_software_platform",
        "pending_verify": False,
    }
    profiles = await build_unit_profiles(_ZHIJI_FACTS, _ZHIJI_GRAN, _llm([low_conf_resp]))
    up = profiles[0]
    p0, p1 = [], []

    # P0: 低置信维度列入 unverified_tags
    expected_unverified = {"cycle", "value_chain"}
    actual_unverified = set(up.unverified_tags)
    missing_uv = expected_unverified - actual_unverified
    if missing_uv:
        p0.append(f"以下低置信维度未列入 unverified_tags: {missing_uv}")

    # P1: unverified_tags 不含高置信维度
    false_uv = actual_unverified - expected_unverified
    if false_uv:
        p1.append(f"高置信维度被误列入 unverified_tags: {false_uv}")

    return Result("E-D3", "低置信维度列入 unverified_tags", _verdict(p0, p1), p0, p1)


async def run_E_D4() -> Result:
    """E-D4 token 预算超限：低权重单元降级为摘要注入，不驳回重选。"""
    # 构造 split 4 单元（腾讯3 + 额外1），超过 8k 预算
    entity = _make_entity("超大型集团", "00001.HK", "HK")
    facts = _make_facts(entity, [
        {"name": "游戏",  "revenue_share": 0.30, "charging": "虚拟道具"},
        {"name": "广告",  "revenue_share": 0.20, "charging": "CPM"},
        {"name": "金融",  "revenue_share": 0.25, "charging": "费率"},
        {"name": "云服务","revenue_share": 0.25, "charging": "按量计费"},
    ], fv="big_v1")

    units = [
        {"id": "u_game",  "scope": "游戏",  "dims": ["profit_model"],
         "reason": "盈利模式为虚拟道具付费"},
        {"id": "u_ad",    "scope": "广告",  "dims": ["profit_model"],
         "reason": "广告流量变现"},
        {"id": "u_fin",   "scope": "金融",  "dims": ["capital_input"],
         "reason": "重监管合规资本投入"},
        {"id": "u_cloud", "scope": "云服务","dims": ["valuation_logic"],
         "reason": "按量计费估值逻辑不同"},
    ]
    gran = _make_gran("超大型集团", units, "big_v1")
    # 设极小预算（只够 2 个单元）
    tiny_budget = 2500  # 2500 / 1500 per unit ≈ 1.67 → 只有 1 个单元在预算内

    profiles = await build_unit_profiles(
        facts, gran,
        _llm([_good_resp("digital_software_platform")] * 10),
        token_budget=tiny_budget,
    )
    p0, p1 = [], []

    # P0: 未因超预算驳回重选或中断（仍产出 4 个 profiles）
    if len(profiles) != 4:
        p0.append(f"超预算后应仍产出 4 个 profiles，得到 {len(profiles)}")

    # P0: 超预算单元应被回填 is_summary_injection=True（修复 E-D4 欠账）
    summary_units = [p.unit_id for p in profiles if p.is_summary_injection]
    normal_units  = [p.unit_id for p in profiles if not p.is_summary_injection]
    # tiny_budget=2500 / 1500 per unit ≈ 能装 1 个；后 3 个应降级
    if len(summary_units) < 2:
        p0.append(
            f"超预算后应有 ≥2 个单元 is_summary_injection=True，"
            f"实际降级单元: {summary_units}，正常单元: {normal_units}"
        )

    return Result("E-D4", "token 预算超限降级（is_summary_injection 回填）", _verdict(p0, p1), p0, p1)


async def run_E_D5() -> Result:
    """E-D5 重选超限（2 次）后回退通用 Adapter + pending_verify。"""
    clear_cache()
    # 连续 3 次幻觉 ID（超过重选上限 2 次）
    hallucin = _good_resp("hallucination_id_xyz")
    profiles = await build_unit_profiles(
        _ZHIJI_FACTS, _ZHIJI_GRAN,
        _llm([hallucin, hallucin, hallucin]),  # 3 次都是幻觉
    )
    up = profiles[0]
    p0, p1 = [], []

    # P0: 回退通用 Adapter（adapter_fallback=True）
    if not up.adapter_fallback:
        p0.append(
            f"重选 2 次仍失败后应回退通用 Adapter（adapter_fallback=True），"
            f"得到 adapter_fallback={up.adapter_fallback}，selected={up.selected_adapter}"
        )

    # P0: 回退后标记 pending_verify
    if up.adapter_fallback and not up.pending_verify:
        p0.append("回退通用 Adapter 后应有 pending_verify=True")

    # P0: 任务未中断（profiles 长度 == 1）
    if len(profiles) != 1:
        p0.append(f"任务不应中断，期望 1 个 profile，得到 {len(profiles)}")

    # P1: 最终使用 FALLBACK_ADAPTER_ID
    if up.selected_adapter != FALLBACK_ADAPTER_ID:
        p1.append(f"回退后应使用 {FALLBACK_ADAPTER_ID}，实际 {up.selected_adapter}")

    return Result("E-D5", "重选超限回退通用 Adapter", _verdict(p0, p1), p0, p1)


# ════════════════════════════════════════════════════════════════
# 执行框架
# ════════════════════════════════════════════════════════════════

POSITIVE_CASES = [run_E01, run_E02, run_E03, run_E04, run_E05]
DEFENCE_CASES  = [run_E_D1, run_E_D2, run_E_D3, run_E_D4, run_E_D5]


async def run_all(defence_k: int = 5) -> list[Result]:
    results = []
    print("\n── E 类正向用例 ──────────────────────────────────────")
    for fn in POSITIVE_CASES:
        results.append(await fn())

    print("\n── E 类防御用例（pass^k={} ）─────────────────────────".format(defence_k))
    for fn in DEFENCE_CASES:
        rounds = [await fn() for _ in range(defence_k)]
        worst = rounds[-1]
        if any(r.verdict == "FAIL" for r in rounds):
            worst = next(r for r in rounds if r.verdict == "FAIL")
            worst.notes.append(
                f"pass^{defence_k}: {sum(1 for r in rounds if r.verdict=='PASS')}/{defence_k} 通过")
        else:
            worst.notes.append(f"pass^{defence_k}: {defence_k}/{defence_k} 全部通过")
        results.append(worst)
    return results


def print_results(results, defence_k):
    icon = {"PASS": "✓", "PARTIAL": "△", "FAIL": "✗"}
    print("\n" + "═" * 60)
    print(f"{'ID':<8} {'判定':<8} 描述")
    print("─" * 60)
    p0t, p1t = 0, 0
    vc = {"PASS": 0, "PARTIAL": 0, "FAIL": 0}
    for r in results:
        vc[r.verdict] += 1
        p0t += len(r.p0_hits); p1t += len(r.p1_hits)
        print(f"{r.case_id:<8} {icon[r.verdict]} {r.verdict:<6} {r.desc}")
        for h in r.p0_hits: print(f"         [P0] {h}")
        for h in r.p1_hits: print(f"         [P1] {h}")
        for n in r.notes:   print(f"         [注] {n}")
    print("─" * 60)
    print(f"合计：{len(results)} 条 | PASS={vc['PASS']} PARTIAL={vc['PARTIAL']} FAIL={vc['FAIL']}")
    print(f"P0 命中：{p0t}  P1 扣分：{p1t}")

    defence_res  = [r for r in results if r.case_id.startswith("E-D")]
    positive_res = [r for r in results if not r.case_id.startswith("E-D")]
    dp = all(r.verdict in ("PASS", "PARTIAL") for r in defence_res)  # PARTIAL 不阻塞，FAIL 才阻塞
    dp_strict = all(r.verdict == "PASS" for r in defence_res)         # 严格 pass^k
    pr = sum(1 for r in positive_res if r.verdict in ("PASS","PARTIAL")) / max(len(positive_res),1)
    pp = all(not r.p0_hits for r in positive_res)
    print(f"\n── 发布门禁评估 ──────────────────────────────────────")
    print(f"  防御用例无 FAIL（PARTIAL 不阻塞）：{'✓' if dp else '✗'}")
    print(f"  防御用例全部 PASS (pass^{defence_k} 严格)：{'✓' if dp_strict else '△ 有 PARTIAL（P1 扣分项，不阻塞）'}")
    print(f"  正向 PASS+PARTIAL ≥ 90%：{'✓' if pr>=0.9 else '✗'} ({pr*100:.0f}%)")
    print(f"  正向 P0 零命中：{'✓' if pp else '✗'}")
    gate = dp and pr >= 0.9 and pp
    print(f"\n  ── {'🟢 E 类门禁：通过' if gate else '🔴 E 类门禁：未通过，阻塞画像路由环节验收'}")


async def main(defence_k=5):
    results = await run_all(defence_k)
    print_results(results, defence_k)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--defence-k", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(main(args.defence_k))
