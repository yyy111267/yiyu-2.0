"""
D 类用例评测：研究粒度决策（10 条）
  正向：D-01 ~ D-05
  防御：D-D1 ~ D-D5（pass^k=5）

运行：
  PYTHONPATH=. python3 tests/eval_granularity_d.py
  PYTHONPATH=. python3 tests/eval_granularity_d.py --defence-k 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from runtime.preloop.granularity import (
    CONFIDENCE_MIN,
    _make_whole_fallback,
    clear_cache,
    decide_granularity,
)
from runtime.schemas import (
    AliasType,
    BusinessSegment,
    CompanyFacts,
    CurrentEntitySchema,
    EntitySource,
    FinancialSnapshot,
    GranularityMode,
    InfoRichness,
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
    verdict: str        # PASS / PARTIAL / FAIL
    p0_hits: list[str] = field(default_factory=list)
    p1_hits: list[str] = field(default_factory=list)
    notes: list[str]   = field(default_factory=list)


def _verdict(p0: list[str], p1: list[str]) -> str:
    if p0:
        return "FAIL"
    if len(p1) == 0:
        return "PASS"
    if len(p1) <= 2:
        return "PARTIAL"
    return "FAIL"


def _llm(responses: list[dict]):
    """顺序依次返回，超出后重复最后一个；记录调用次数。"""
    state = {"calls": 0}
    async def chat_json(system, user, temperature=0.1, **kw):
        idx = min(state["calls"], len(responses) - 1)
        state["calls"] += 1
        return responses[idx]
    m = MagicMock()
    m.chat_json = chat_json
    m._state = state
    return m


def _make_entity(name: str, security_id: str, market: str = "A") -> CurrentEntitySchema:
    code, exchange = security_id.split(".") if "." in security_id else (security_id, "SH")
    return CurrentEntitySchema(
        canonical_name=name, security_id=security_id,
        code=code, exchange=exchange, primary_market=market,
        entity_source=EntitySource.EXPLICIT, alias_type=AliasType.ABBREVIATION,
    )


def _make_facts(
    entity: CurrentEntitySchema,
    segments: list[dict],
    richness: InfoRichness = InfoRichness.A,
    fv: str = "v1",
    open_qs: list[str] | None = None,
) -> CompanyFacts:
    segs = [BusinessSegment(
        name=s["name"],
        revenue_share=s.get("revenue_share"),
        customer=s.get("customer"),
        charging=s.get("charging"),
    ) for s in segments]
    f = CompanyFacts(
        entity=entity,
        one_line_business=f"以{segments[0]['name'] if segments else '主营业务'}为主的公司",
        segments=segs,
        financial_snapshot=FinancialSnapshot(revenue_ttm=100_000, gross_margin=0.45),
        info_richness=richness,
        source_trace=[SourceTrace(field="segments", source="年报", source_level="S")],
        open_questions=open_qs or [],
        facts_version=fv,
    )
    return f


# ── 冻结夹具数据 ─────────────────────────────────────────────────

_TENCENT = _make_entity("腾讯控股", "00700.HK", "HK")
_TENCENT_FACTS = _make_facts(_TENCENT, [
    {"name": "增值服务（游戏）", "revenue_share": 0.50,
     "customer": "个人玩家", "charging": "虚拟道具/订阅",
     "competition": "网易/米哈游/Sony",
     "capex_profile": "轻资产（内容研发）；估值逻辑=流水/递延收入驱动"},
    {"name": "网络广告", "revenue_share": 0.17,
     "customer": "广告主", "charging": "按效果计费/CPM",
     "competition": "字节跳动/百度/阿里妈妈",
     "capex_profile": "轻资产（流量变现）；估值逻辑=DAU×eCPM驱动"},
    {"name": "金融科技与企业服务", "revenue_share": 0.32,
     "customer": "个人用户/企业", "charging": "费率抽成/SaaS订阅",
     "competition": "蚂蚁集团/微众银行/阿里云",
     "capex_profile": "中等（服务器/合规/牌照）；估值逻辑受监管周期影响，适用监管折价"},
], fv="tencent_v1")

_MOUTAI = _make_entity("贵州茅台", "600519.SH")
_MOUTAI_FACTS = _make_facts(_MOUTAI, [
    {"name": "茅台酒", "revenue_share": 0.85, "charging": "一次性销售"},
    {"name": "系列酒", "revenue_share": 0.14, "charging": "一次性销售"},
], fv="moutai_v1")

_YILI = _make_entity("伊利股份", "600887.SH")
_YILI_FACTS = _make_facts(_YILI, [
    {"name": "液态奶", "revenue_share": 0.60, "charging": "一次性销售"},
    {"name": "奶粉", "revenue_share": 0.20, "charging": "一次性销售"},
    {"name": "冷饮", "revenue_share": 0.10, "charging": "一次性销售"},
], fv="yili_v1")

# 正确的 split 响应：腾讯三单元
_TENCENT_SPLIT_RESP = {
    "mode": "split",
    "units": [
        {"id": "u_game", "scope": "游戏业务",
         "reason": "盈利模式为虚拟道具付费，估值逻辑以流水/递延收入驱动，独立于其他分部",
         "differential_dimensions": ["profit_model", "valuation_logic"]},
        {"id": "u_ad", "scope": "网络广告",
         "reason": "盈利模式为广告流量变现，护城河来源（微信生态流量壁垒）与游戏/金融科技不同",
         "differential_dimensions": ["profit_model", "moat_source"]},
        {"id": "u_fintech", "scope": "金融科技与企业服务",
         "reason": "资本投入方式重（监管合规/技术基础设施）、估值逻辑受监管周期影响，显著不同",
         "differential_dimensions": ["capital_input", "valuation_logic"]},
    ],
    "confidence": 0.88,
    "open_questions": ["金融科技与企业服务合并披露，细分拆分依赖附注"],
}

# 整体研究响应
_WHOLE_RESP = {
    "mode": "whole",
    "units": [{"id": "u_company", "scope": "公司整体",
               "reason": "整体研究，各业务盈利模式/护城河/资本结构高度相近",
               "differential_dimensions": []}],
    "confidence": 0.92,
    "open_questions": [],
}


# ════════════════════════════════════════════════════════════════
# 正向用例
# ════════════════════════════════════════════════════════════════

async def run_D01() -> Result:
    """D-01 显著分部差异正确拆分（腾讯）。"""
    clear_cache()
    dec = await decide_granularity(_TENCENT_FACTS, _llm([_TENCENT_SPLIT_RESP]))

    p0, p1 = [], []

    # P0: mode = split
    if dec.mode != GranularityMode.SPLIT:
        p0.append(f"腾讯三分部应判 split，得到 mode={dec.mode.value}")

    # P0: 每单元 reason 命中至少一项差异条件
    for u in dec.units:
        if not u.differential_dimensions:
            p0.append(f"unit '{u.id}' differential_dimensions 为空，reason 未命中差异条件")
        if len(u.reason) < 10:
            p0.append(f"unit '{u.id}' reason 过短（{len(u.reason)}字），疑似空泛")

    # P1: 字段完整
    if dec.confidence <= 0:
        p1.append("confidence 为 0")
    if dec.facts_version != "tencent_v1":
        p1.append(f"facts_version 不匹配，得到 {dec.facts_version!r}")
    if len(dec.units) > 4:
        p1.append(f"units 数量 {len(dec.units)} 超参考上限 4")

    return Result("D-01", "显著分部差异正确拆分（腾讯）", _verdict(p0, p1), p0, p1)


async def run_D02() -> Result:
    """D-02 披露多分类默认整体（茅台产品线/渠道不拆）。"""
    clear_cache()
    dec = await decide_granularity(_MOUTAI_FACTS, _llm([_WHOLE_RESP]))

    p0, p1 = [], []

    if dec.mode != GranularityMode.WHOLE:
        p0.append(f"茅台产品线应整体研究，得到 mode={dec.mode.value}")

    # P1: 单一单元
    if len(dec.units) != 1:
        p1.append(f"整体研究应只有 1 个单元，得到 {len(dec.units)} 个")

    return Result("D-02", "披露多分类默认整体（茅台）", _verdict(p0, p1), p0, p1)


async def run_D03() -> Result:
    """D-03 业务同构不拆分（伊利奶粉/液态奶/冷饮）。"""
    clear_cache()
    whole_reason = {
        "mode": "whole",
        "units": [{"id": "u_company", "scope": "公司整体",
                   "reason": "整体研究，各业务盈利模式/护城河/资本结构高度相近，"
                             "共享同一品牌、渠道与客户群，盈利模式同构",
                   "differential_dimensions": []}],
        "confidence": 0.90,
        "open_questions": [],
    }
    dec = await decide_granularity(_YILI_FACTS, _llm([whole_reason]))

    p0, p1 = [], []

    if dec.mode != GranularityMode.WHOLE:
        p0.append(f"伊利同构业务应整体研究，得到 mode={dec.mode.value}")

    # P1: reason 体现同构性判断
    reasons = " ".join(u.reason for u in dec.units)
    if not any(k in reasons for k in ("同构", "共享", "相近", "品牌", "渠道")):
        p1.append(f"reason 未体现同构性依据: {reasons!r}")

    return Result("D-03", "业务同构不拆分（伊利）", _verdict(p0, p1), p0, p1)


async def run_D04() -> Result:
    """D-04 决策复用不重判（facts_version 未变缓存命中）。"""
    clear_cache()
    client = _llm([_WHOLE_RESP])

    # 第一次：调 LLM
    dec1 = await decide_granularity(_MOUTAI_FACTS, client)
    calls_after_first = client._state["calls"]

    # 第二次：应命中缓存
    dec2 = await decide_granularity(_MOUTAI_FACTS, client)
    calls_after_second = client._state["calls"]

    p0, p1 = [], []

    if calls_after_second != calls_after_first:
        p0.append(
            f"第二次调用触发 LLM（first={calls_after_first}, second={calls_after_second}），"
            "缓存未生效"
        )
    if dec2.mode != dec1.mode:
        p0.append(f"缓存命中后 mode 漂移：{dec1.mode.value} → {dec2.mode.value}")
    if dec2.facts_version != dec1.facts_version:
        p1.append("缓存命中后 facts_version 不一致")

    return Result("D-04", "决策复用不重判", _verdict(p0, p1), p0, p1)


async def run_D05() -> Result:
    """D-05 版本变更触发重判（facts_version v1→v2）。"""
    clear_cache()
    client = _llm([_WHOLE_RESP, _TENCENT_SPLIT_RESP])

    # v1 版本：整体研究
    facts_v1 = _MOUTAI_FACTS  # facts_version = "moutai_v1"
    dec_v1 = await decide_granularity(facts_v1, client)
    calls_v1 = client._state["calls"]

    # v2 版本：新财报改变分部口径（不同 facts_version）
    entity_v2 = _make_entity("贵州茅台", "600519.SH")
    facts_v2 = _make_facts(entity_v2, [
        {"name": "茅台酒", "revenue_share": 0.83, "charging": "一次性销售"},
        {"name": "系列酒", "revenue_share": 0.16, "charging": "一次性销售"},
    ], fv="moutai_v2")  # 新 facts_version

    dec_v2 = await decide_granularity(facts_v2, client)
    calls_v2 = client._state["calls"]

    p0, p1 = [], []

    # P0: v2 触发了新判断（调用次数增加）
    if calls_v2 == calls_v1:
        p0.append(
            "facts_version 变更后未重新调用 LLM，沿用旧版本决策（版本绑定失效）"
        )

    # P0: v2 结果绑定新 facts_version
    if dec_v2.facts_version != "moutai_v2":
        p0.append(f"v2 决策绑定的 facts_version={dec_v2.facts_version!r}，应为 moutai_v2")

    # P1: v1 缓存不受 v2 影响
    dec_v1_again = await decide_granularity(facts_v1, client)
    if dec_v1_again.mode != dec_v1.mode:
        p1.append("v2 判断影响了 v1 缓存（版本隔离失效）")

    return Result("D-05", "版本变更触发重判", _verdict(p0, p1), p0, p1)


# ════════════════════════════════════════════════════════════════
# 防御用例
# ════════════════════════════════════════════════════════════════

async def run_D_D1() -> Result:
    """D-D1 过度拆分被护栏拦截：reason 未命中差异条件的 split 打回。"""
    clear_cache()
    # 第一次：产品线差异（reason 未命中差异条件）
    bad_split = {
        "mode": "split",
        "units": [
            {"id": "u_maotai", "scope": "茅台酒", "reason": "产品线差异明显",
             "differential_dimensions": []},  # 空 → 护栏拦截
            {"id": "u_series", "scope": "系列酒", "reason": "产品线不同",
             "differential_dimensions": []},
        ],
        "confidence": 0.75,
        "open_questions": [],
    }
    # 第二次（重试）：回退 whole
    good_whole = _WHOLE_RESP.copy()

    dec = await decide_granularity(_MOUTAI_FACTS, _llm([bad_split, good_whole]))

    p0, p1 = [], []

    # P0: 过度拆分未放行（应是 whole 或拒绝该 split）
    if dec.mode == GranularityMode.SPLIT:
        bad_units = [u for u in dec.units if not u.differential_dimensions]
        if bad_units:
            p0.append(f"护栏未拦截 differential_dimensions 为空的 split 单元: "
                      f"{[u.id for u in bad_units]}")

    # P1: 最终收敛 whole（重试后合法）
    if dec.mode == GranularityMode.SPLIT:
        p1.append("过度拆分打回后未收敛 whole，仍为 split")

    return Result("D-D1", "过度拆分被护栏拦截", _verdict(p0, p1), p0, p1)


async def run_D_D2() -> Result:
    """D-D2 空泛拆分理由被打回（「业务较多，比较复杂」）。"""
    clear_cache()
    vague_split = {
        "mode": "split",
        "units": [
            {"id": "u_a", "scope": "业务A", "reason": "业务较多，比较复杂",
             "differential_dimensions": ["profit_model"]},
            {"id": "u_b", "scope": "业务B", "reason": "综合考虑",
             "differential_dimensions": ["profit_model"]},
        ],
        "confidence": 0.8,
        "open_questions": [],
    }
    # 重试后回退 whole
    dec = await decide_granularity(
        _MOUTAI_FACTS,
        _llm([vague_split, _WHOLE_RESP]),
    )

    p0, p1 = [], []

    if dec.mode == GranularityMode.SPLIT:
        # 检查是否仍有空泛理由单元
        vague_kw = {"业务较多", "比较复杂", "不好说", "综合考虑"}
        bad_units = [u for u in dec.units
                     if any(k in u.reason for k in vague_kw)]
        if bad_units:
            p0.append(f"空泛理由单元进入了最终结果: "
                      f"{[(u.id, u.reason) for u in bad_units]}")

    return Result("D-D2", "空泛拆分理由被打回", _verdict(p0, p1), p0, p1)


async def run_D_D3() -> Result:
    """D-D3 置信度不足：<0.6 补证据重判，仍不足回退 whole。"""
    clear_cache()
    low_conf_split = {
        "mode": "split",
        "units": [
            {"id": "u_a", "scope": "业务A",
             "reason": "盈利模式存在差异（待核实）",
             "differential_dimensions": ["profit_model"]},
            {"id": "u_b", "scope": "业务B",
             "reason": "估值逻辑不同（待核实）",
             "differential_dimensions": ["valuation_logic"]},
        ],
        "confidence": 0.45,  # 低于 CONFIDENCE_MIN=0.6
        "open_questions": ["分部差异尚待年报核实"],
    }
    # 重试也给低置信 → 最终回退 whole
    dec = await decide_granularity(
        _TENCENT_FACTS,
        _llm([low_conf_split, low_conf_split]),
    )

    p0, p1 = [], []

    # P0: 低置信不直接采用
    if dec.confidence < CONFIDENCE_MIN and dec.mode == GranularityMode.SPLIT:
        p0.append(
            f"置信度 {dec.confidence:.2f} < {CONFIDENCE_MIN}，"
            "低置信 split 未被打回，直接流入下游"
        )

    # P0: 两次低置信后回退 whole
    if dec.mode != GranularityMode.WHOLE:
        p0.append(
            f"两次低置信判断后应回退 whole，得到 mode={dec.mode.value}"
        )

    # P1: 回退原因写入 open_questions
    if dec.mode == GranularityMode.WHOLE:
        has_reason = any("置信" in q or "回退" in q for q in dec.open_questions)
        if not has_reason:
            p1.append("回退 whole 后 open_questions 未记录回退原因")

    return Result("D-D3", "置信度不足回退 whole", _verdict(p0, p1), p0, p1)


async def run_D_D4() -> Result:
    """D-D4 split 单单元矛盾判非法打回。"""
    clear_cache()
    one_unit_split = {
        "mode": "split",
        "units": [
            {"id": "u_only", "scope": "公司整体",
             "reason": "盈利模式独特值得单独研究",
             "differential_dimensions": ["profit_model"]},
        ],
        "confidence": 0.80,
        "open_questions": [],
    }
    # 重试给合法 whole
    dec = await decide_granularity(
        _MOUTAI_FACTS,
        _llm([one_unit_split, _WHOLE_RESP]),
    )

    p0, p1 = [], []

    # P0: split+1单元的矛盾不能流入下游
    if dec.mode == GranularityMode.SPLIT and len(dec.units) == 1:
        p0.append("split 模式下仅 1 个研究单元的逻辑矛盾未被拦截，流入下游")

    return Result("D-D4", "split 单单元矛盾判非法打回", _verdict(p0, p1), p0, p1)


async def run_D_D5() -> Result:
    """D-D5 同版本重复判断一致（pass^k，绕过缓存直判）。"""
    # 直判模式：清缓存后每次都重新调 LLM，验证 LLM 输出一致性
    # 注：deterministic mock 确保一致；真 LLM 评测另见 real 层
    client = _llm([_TENCENT_SPLIT_RESP] * 10)

    modes: list[str] = []
    unit_counts: list[int] = []
    unit_ids_list: list[frozenset] = []

    for _ in range(5):
        clear_cache()  # 绕过缓存，强制每次重新判断
        dec = await decide_granularity(_TENCENT_FACTS, client, force_refresh=True)
        modes.append(dec.mode.value)
        unit_counts.append(len(dec.units))
        unit_ids_list.append(frozenset(dec.unit_ids))

    p0, p1 = [], []

    # P0: mode 5次一致
    if len(set(modes)) > 1:
        p0.append(f"5 次判断 mode 不一致: {modes}")

    # P0: unit 划分 5次一致
    if len(set(unit_ids_list)) > 1:
        p0.append(f"5 次判断 unit 划分不一致: {[sorted(s) for s in unit_ids_list]}")

    # P0: unit 数量一致
    if len(set(unit_counts)) > 1:
        p0.append(f"5 次判断 unit 数量不一致: {unit_counts}")

    return Result("D-D5", "同版本重复判断一致（pass^5）", _verdict(p0, p1), p0, p1)


# ════════════════════════════════════════════════════════════════
# 执行框架
# ════════════════════════════════════════════════════════════════

POSITIVE_CASES = [run_D01, run_D02, run_D03, run_D04, run_D05]
DEFENCE_CASES  = [run_D_D1, run_D_D2, run_D_D3, run_D_D4, run_D_D5]


async def run_all(defence_k: int = 5) -> list[Result]:
    results: list[Result] = []

    print("\n── D 类正向用例 ──────────────────────────────────────")
    for fn in POSITIVE_CASES:
        r = await fn()
        results.append(r)

    print("\n── D 类防御用例（pass^k={} ）─────────────────────────".format(defence_k))
    for fn in DEFENCE_CASES:
        round_results: list[Result] = []
        for _ in range(defence_k):
            round_results.append(await fn())
        worst = round_results[-1]
        if any(r.verdict == "FAIL" for r in round_results):
            worst = next(r for r in round_results if r.verdict == "FAIL")
            worst.notes.append(
                f"pass^{defence_k}: {sum(1 for r in round_results if r.verdict=='PASS')}/{defence_k} 次通过"
            )
        else:
            worst.notes.append(f"pass^{defence_k}: {defence_k}/{defence_k} 次全部通过")
        results.append(worst)

    return results


def print_results(results: list[Result], defence_k: int) -> None:
    icon = {"PASS": "✓", "PARTIAL": "△", "FAIL": "✗"}
    print("\n" + "═" * 60)
    print(f"{'ID':<8} {'判定':<8} {'描述'}")
    print("─" * 60)

    verdicts = {"PASS": 0, "PARTIAL": 0, "FAIL": 0}
    p0_total, p1_total = 0, 0

    for r in results:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
        p0_total += len(r.p0_hits)
        p1_total += len(r.p1_hits)
        print(f"{r.case_id:<8} {icon[r.verdict]} {r.verdict:<6} {r.desc}")
        for h in r.p0_hits:
            print(f"         [P0] {h}")
        for h in r.p1_hits:
            print(f"         [P1] {h}")
        for n in r.notes:
            print(f"         [注] {n}")

    print("─" * 60)
    print(f"合计：{len(results)} 条 | PASS={verdicts['PASS']} "
          f"PARTIAL={verdicts['PARTIAL']} FAIL={verdicts['FAIL']}")
    print(f"P0 命中：{p0_total} 次  P1 扣分：{p1_total} 次")

    defence_res  = [r for r in results if r.case_id.startswith("D-D")]
    positive_res = [r for r in results if not r.case_id.startswith("D-D")]

    defence_pass = all(r.verdict == "PASS" for r in defence_res)
    pos_rate = (
        sum(1 for r in positive_res if r.verdict in ("PASS", "PARTIAL"))
        / len(positive_res) if positive_res else 1.0
    )
    pos_p0_zero = all(not r.p0_hits for r in positive_res)

    print("\n── 发布门禁评估 ──────────────────────────────────────")
    print(f"  防御用例全部 PASS (pass^{defence_k})：{'✓' if defence_pass else '✗'}")
    print(f"  正向 PASS+PARTIAL ≥ 90%：{'✓' if pos_rate >= 0.9 else '✗'} ({pos_rate*100:.0f}%)")
    print(f"  正向 P0 零命中：{'✓' if pos_p0_zero else '✗'}")

    gate_ok = defence_pass and pos_rate >= 0.9 and pos_p0_zero
    print(f"\n  ── {'🟢 D 类门禁：通过' if gate_ok else '🔴 D 类门禁：未通过，阻塞粒度决策环节验收'}")


async def main(defence_k: int = 5) -> None:
    results = await run_all(defence_k)
    print_results(results, defence_k)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--defence-k", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(main(args.defence_k))
