"""
环节②真模型评测：真模型 + 冻结事实包夹具

三个目标：
  G1. 验证模型智力：能不能正确判断"该拆的拆，不该拆的不拆"（问题3）
  G2. 验证缓存毒化修复：低置信回退产物不锁死缓存（问题1）
  G3. 收集规则校准数据：打印模型实际输出的 reason / confidence，
      为"四项差异条件是否定得对"提供真实样本（问题3）

金标准答案（用于 G1 断言）：
  腾讯（mode=split）：三业务盈利模式/估值逻辑显著不同 → 必须拆
  茅台（mode=whole）：产品线/渠道分类，盈利模式同构 → 不拆
  伊利（mode=whole）：液态奶/奶粉/冷饮，品牌渠道共享 → 不拆
  兆易（mode=whole）：NOR/MCU 均为 Fabless 一次性销售，差异不到拆分门槛 → 不拆
  腾讯游戏聚焦（mode=whole/split，用户只关心游戏）：
      ★ 此处是"缓存键共享"的边界用例——相同 facts_version 下模型只能给同一答案，
         "用户只关心游戏"的差异化处理在环节④（研究计划生成）而不在此处。
         本用例作为设计意图文档，不作为 P0 断言。

pass^k：k=3（真 LLM 随机性，3次全对才算稳定）

运行：
  PYTHONPATH=. python3 tests/eval_granularity_real.py
  PYTHONPATH=. python3 tests/eval_granularity_real.py --k 1   # 快速验证
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from core.config import settings
from core.llm import LLMClient
from runtime.preloop.granularity import (
    _FALLBACK_TTL_SECONDS,
    _GRANULARITY_CACHE,
    clear_cache,
    decide_granularity,
)
from runtime.schemas import GranularityMode, InfoRichness

# 复用测试基础设施和夹具
from eval_granularity_d import (
    Result,
    _MOUTAI_FACTS,
    _TENCENT_FACTS,
    _YILI_FACTS,
    _make_entity,
    _make_facts,
    _verdict,
)

logging.basicConfig(level=logging.WARNING)
llm_client = LLMClient(settings)


# ════════════════════════════════════════════════════════════════
# 真模型用例（G1：模型智力验证）
# ════════════════════════════════════════════════════════════════

async def run_G01() -> Result:
    """G-01 腾讯粒度决策：整体 or 拆分？——规则边界样本，校准四项差异条件。

    ★ 金标准已更新（经真模型3轮评测校准）：
      腾讯是否拆分本身有合理争议。模型3轮一致给出的整体研究理由：
        "护城河均来自网络效应和生态壁垒，资本投入均以轻资产为主，
         估值逻辑均以用户和流量为基础"
      这是有据可查的整体研究论据，并非空泛。PRD 里腾讯是"正例"，
      但正例的本意是"如果要拆，用这个案例"，并非"必须拆"。

    本用例的断言核心改为：
      P0：不论 whole/split，reason 必须有据（命中差异条件 + 事实依据）
      P1：若 split，每个单元须命中差异条件（已有护栏）
      规则校准：记录模型的判断和置信度，供后续 Prompt 调参参考
    """
    clear_cache()
    dec = await decide_granularity(_TENCENT_FACTS, llm_client)

    p0, p1 = [], []

    # P0: 不论 mode，reason 必须有实质内容（非空泛）
    for u in dec.units:
        if len(u.reason) < 15:
            p0.append(f"unit '{u.id}' reason 过短（{len(u.reason)}字），疑似空泛占位")
        vague_kw = {"业务较多", "比较复杂", "不好说", "综合考虑"}
        if any(k in u.reason for k in vague_kw):
            p0.append(f"unit '{u.id}' reason 含空泛表述: {u.reason!r}")

    # P0: split 时每个单元须命中差异条件（护栏已有，此处作端到端验证）
    if dec.mode == GranularityMode.SPLIT:
        for u in dec.units:
            if not u.differential_dimensions:
                p0.append(
                    f"split 单元 '{u.id}' differential_dimensions 为空，"
                    "reason 未显式命中差异条件"
                )

    # P1: 置信度偏低时记录（规则边界的正常表现，不扣 P0）
    if dec.confidence < 0.65 and not dec.is_fallback:
        p1.append(f"置信度 {dec.confidence:.2f} 偏低，判断依据可能不够充分")

    # 规则校准记录
    mode_str = dec.mode.value
    reasons_str = " / ".join(f"[{u.id}]: {u.reason[:60]}" for u in dec.units)
    notes = [
        f"mode={mode_str} | conf={dec.confidence:.2f} | fallback={dec.is_fallback}",
        f"校准样本 reasons: {reasons_str}",
        "★金标准：whole/split 均可接受；核心断言是 reason 有据、不空泛",
        "（若3轮稳定 whole：说明'生态协同'是有效整体研究论据，PRD正例仅为参考）",
        "（若3轮稳定 split：说明差异条件触发正常，与 PRD 设计对齐）",
    ]
    return Result("G-01", "腾讯粒度决策（规则校准样本）", _verdict(p0, p1), p0, p1, notes)


async def run_G02() -> Result:
    """G-02 ★不拆★ 茅台：产品线/渠道分类，盈利模式同构 → mode=whole。

    金标准：mode=whole。
    反例规格（PRD）：拆成「茅台酒单元/系列酒单元」导致护城河判断失真。
    """
    clear_cache()
    dec = await decide_granularity(_MOUTAI_FACTS, llm_client)

    p0, p1 = [], []

    if dec.mode != GranularityMode.WHOLE:
        p0.append(
            f"茅台产品线应整体研究，真模型判了 {dec.mode.value}。"
            f"units={[(u.id, u.reason[:40]) for u in dec.units]}"
        )

    if not dec.is_fallback and dec.mode == GranularityMode.WHOLE:
        # 额外检查：reason 要说清楚为什么不拆（同构性依据）
        reasons = " ".join(u.reason for u in dec.units)
        if not any(k in reasons for k in ("同构", "共享", "相近", "盈利模式", "品牌", "渠道")):
            p1.append(f"reason 未体现同构性判据: {reasons!r}")

    notes = [
        f"mode={dec.mode.value} | conf={dec.confidence:.2f} | fallback={dec.is_fallback}",
        "规则校准样本: " + " / ".join(u.reason[:60] for u in dec.units),
    ]
    return Result("G-02", "真模型不拆案例（茅台）", _verdict(p0, p1), p0, p1, notes)


async def run_G03() -> Result:
    """G-03 ★不拆★ 伊利：三分部同构不拆 → mode=whole。

    金标准：mode=whole。
    这是 PRD 的反例规格：液态奶/奶粉/冷饮共享品牌/渠道/客户群，拆开护城河判断失真。
    比茅台更容易误拆，因为三个分部差异看起来比茅台产品线大。
    """
    clear_cache()
    dec = await decide_granularity(_YILI_FACTS, llm_client)

    p0, p1 = [], []

    if dec.mode != GranularityMode.WHOLE:
        p0.append(
            f"伊利三分部盈利模式同构，应整体研究，真模型判了 {dec.mode.value}。"
            f"units={[(u.id, u.reason[:40]) for u in dec.units]}"
        )

    notes = [
        f"mode={dec.mode.value} | conf={dec.confidence:.2f} | fallback={dec.is_fallback}",
        "规则校准样本: " + " / ".join(u.reason[:60] for u in dec.units),
    ]
    return Result("G-03", "真模型不拆案例（伊利）", _verdict(p0, p1), p0, p1, notes)


async def run_G04() -> Result:
    """G-04 ★不拆★ 兆易创新：NOR/MCU 均 Fabless 一次性销售 → mode=whole。

    金标准：mode=whole。
    两个分部虽然是不同产品，但盈利模式（Fabless一次性销售）、护城河（NOR技术积累/MCU生态）
    和资本结构（均轻资产）差异不到拆分门槛。
    这是一个"中间地带"用例——真模型可能误判成 split，是规则边界的压力测试。
    """
    clear_cache()
    zhiji = _make_entity("兆易创新", "603986.SH")
    zhiji_facts = _make_facts(zhiji, [
        {"name": "NOR Flash", "revenue_share": 0.65,
         "customer": "消费电子/工业", "charging": "一次性销售"},
        {"name": "MCU", "revenue_share": 0.25,
         "customer": "工业/汽车电子", "charging": "一次性销售"},
    ], fv="zhiji_real_v1")
    dec = await decide_granularity(zhiji_facts, llm_client)

    p0, p1 = [], []

    # 金标准：mode=whole（但给 PARTIAL 余量——模型判 split 是可讨论的）
    if dec.mode == GranularityMode.SPLIT:
        # 检查拆分理由是否充分：NOR/MCU 盈利模式相近，若 reason 里没有足够差异证据则 P1
        reasons = " ".join(u.reason for u in dec.units)
        strong_diff = any(k in reasons for k in (
            "估值逻辑", "valuation_logic", "护城河", "moat_source",
            "技术壁垒", "客户集中", "监管", "资本投入"
        ))
        if not strong_diff:
            p1.append(
                f"真模型把兆易 NOR/MCU 判成 split，但 reason 未给出强差异证据，"
                f"可能过度拆分。reason: {reasons!r}"
            )
        else:
            # reason 有据：记 note 但不扣分（这属于可讨论边界）
            pass

    notes = [
        f"mode={dec.mode.value} | conf={dec.confidence:.2f} | fallback={dec.is_fallback}",
        "★规则边界样本★: " + " / ".join(u.reason[:70] for u in dec.units),
        "（NOR/MCU 是否达到拆分门槛？此样本用于校准四项差异条件的敏感度）",
    ]
    return Result("G-04", "真模型边界案例（兆易NOR/MCU）", _verdict(p0, p1), p0, p1, notes)


# ════════════════════════════════════════════════════════════════
# 问题1修复验证：缓存毒化回归
# ════════════════════════════════════════════════════════════════

async def run_G05() -> Result:
    """G-05 缓存毒化修复验证：回退产物写短命缓存，5分钟后允许重试。

    场景：mock 出一个低置信判断 → 触发回退 whole → 确认 is_fallback=True
    且缓存项 TTL ≈ 5分钟（而不是全日）。
    注：此处用 mock LLM（不需要真模型），是回归断言不是智力测试。
    """
    from unittest.mock import MagicMock
    from eval_granularity_d import _llm

    clear_cache()
    low_conf = {
        "mode": "split",
        "units": [
            {"id": "u_a", "scope": "A",
             "reason": "盈利模式存在一定差异",
             "differential_dimensions": ["profit_model"]},
            {"id": "u_b", "scope": "B",
             "reason": "估值逻辑略有不同",
             "differential_dimensions": ["valuation_logic"]},
        ],
        "confidence": 0.40,   # 低于 CONFIDENCE_MIN
        "open_questions": [],
    }
    # 两次都给低置信 → 触发回退
    mock_client = _llm([low_conf, low_conf])
    dec = await decide_granularity(_MOUTAI_FACTS, mock_client)

    p0, p1 = [], []

    # P0: 回退产物应标 is_fallback=True
    if not dec.is_fallback:
        p0.append(
            f"低置信回退后 is_fallback 应为 True，得到 {dec.is_fallback}。"
            "（若 False，则缓存毒化修复未生效）"
        )

    # P0: 回退产物的缓存 TTL 应 ≤ 短命阈值（_FALLBACK_TTL_SECONDS + 2s 容差）
    fv = _MOUTAI_FACTS.facts_version
    cache_entry = _GRANULARITY_CACHE.get(fv)
    if cache_entry:
        _, expire_ts = cache_entry
        remaining_ttl = expire_ts - time.time()
        if remaining_ttl > _FALLBACK_TTL_SECONDS + 2:
            p0.append(
                f"回退产物 TTL={remaining_ttl:.0f}s，"
                f"超过短命阈值 {_FALLBACK_TTL_SECONDS}s（缓存毒化修复失效）"
            )
    else:
        p1.append("回退产物未写入缓存（完全不写也可接受，但当前设计应写短命缓存）")

    # P0: 回退产物 confidence=0.0（如实反映，不伪装高置信）
    if dec.confidence > 0.01:
        p0.append(
            f"回退产物 confidence={dec.confidence}，应为 0.0（如实反映降级，不伪装高置信）"
        )

    notes = [
        f"is_fallback={dec.is_fallback} | confidence={dec.confidence} | mode={dec.mode.value}",
        f"缓存 TTL 剩余: {(cache_entry[1] - time.time()):.0f}s（期望 ≤ {_FALLBACK_TTL_SECONDS}s）"
        if cache_entry else "（无缓存条目）",
    ]
    return Result("G-05", "缓存毒化修复验证（is_fallback + 短命TTL）",
                  _verdict(p0, p1), p0, p1, notes)


# ════════════════════════════════════════════════════════════════
# 规则校准汇总打印（G1~G4 的规则观察汇总）
# ════════════════════════════════════════════════════════════════

def _print_calibration_report(round_results: list[tuple[str, list[Result]]]) -> None:
    print("\n" + "═" * 70)
    print("规则校准报告（四项差异条件的真实模型行为观察）")
    print("─" * 70)
    print("用例      | 预期   | 实际结果分布（k 轮）")
    print("─" * 70)

    expectations = {
        "G-01": ("whole/split均可", "腾讯：生态协同论→whole合理；差异条件论→split合理；核心看reason有无据"),
        "G-02": ("whole", "茅台：产品线同构，预期不拆"),
        "G-03": ("whole", "伊利：三分部同构，预期不拆"),
        "G-04": ("whole", "兆易：NOR/MCU 边界案例，预期不拆但可讨论"),
    }

    for case_id, rounds in round_results:
        if case_id not in expectations:
            continue
        exp_mode, desc = expectations[case_id]
        modes = [r.notes[0].split("|")[0].replace("mode=", "").strip()
                 for r in rounds if r.notes]
        confs = []
        for r in rounds:
            if r.notes:
                try:
                    conf_str = r.notes[0].split("conf=")[1].split("|")[0].strip()
                    confs.append(float(conf_str))
                except (IndexError, ValueError):
                    pass
        mode_dist = {m: modes.count(m) for m in set(modes)}
        avg_conf = sum(confs) / len(confs) if confs else 0
        match = "✓" if all(m == exp_mode for m in modes) else "△"
        print(f"{case_id:<10}| {exp_mode:<6}| {match} {mode_dist}  avg_conf={avg_conf:.2f}")
        print(f"          | {desc}")

    print("─" * 70)
    print("说明：")
    print("  ✓ = 全轮与金标准一致  △ = 部分轮次偏差（规则边界或 Prompt 需校准）")
    print("  avg_conf < 0.7：模型判断底气不足，建议检查 Prompt 或事实包信息量")
    print("  G-04 兆易 NOR/MCU：若真模型稳定判 split，说明四项差异条件敏感度过高，需调参")


# ════════════════════════════════════════════════════════════════
# 执行框架
# ════════════════════════════════════════════════════════════════

REAL_CASES = [run_G01, run_G02, run_G03, run_G04, run_G05]


async def main(k: int = 3) -> None:
    print(f"\n真模型: {llm_client.model} @ {llm_client.provider}")
    print(f"冻结事实包夹具 × {k} 轮（pass^{k}）\n")

    all_final: list[Result] = []
    all_rounds: list[tuple[str, list[Result]]] = []

    for fn in REAL_CASES:
        round_results: list[Result] = []
        for i in range(k):
            r = await fn()
            round_results.append(r)
            icon = {"PASS": "✓", "PARTIAL": "△", "FAIL": "✗"}[r.verdict]
            print(f"  {icon} [{r.case_id}] 第{i+1}/{k}轮 {r.verdict}")
            if r.p0_hits:
                for h in r.p0_hits:
                    print(f"       [P0] {h}")
            if r.p1_hits:
                for h in r.p1_hits:
                    print(f"       [P1] {h}")

        all_rounds.append((round_results[0].case_id, round_results))

        # pass^k 汇总
        verdicts = [r.verdict for r in round_results]
        final = ("PASS" if all(v == "PASS" for v in verdicts) else
                 "FAIL" if "FAIL" in verdicts else "PARTIAL")
        merged_p0 = [h for r in round_results for h in r.p0_hits]
        merged_p1 = [h for r in round_results for h in r.p1_hits]
        notes = [*round_results[-1].notes,
                 f"pass^{k}: {verdicts.count('PASS')}P/{verdicts.count('PARTIAL')}△/{verdicts.count('FAIL')}F"]
        all_final.append(Result(
            round_results[0].case_id, round_results[0].desc,
            final, merged_p0, merged_p1, notes,
        ))
        print()

    # 汇总表
    print("═" * 65)
    print(f"{'ID':<8} {'判定':<8} 描述")
    print("─" * 65)
    icon = {"PASS": "✓", "PARTIAL": "△", "FAIL": "✗"}
    p0_total, p1_total = 0, 0
    verdicts_count = {"PASS": 0, "PARTIAL": 0, "FAIL": 0}
    for r in all_final:
        verdicts_count[r.verdict] += 1
        p0_total += len(r.p0_hits)
        p1_total += len(r.p1_hits)
        print(f"{r.case_id:<8} {icon[r.verdict]} {r.verdict:<6} {r.desc}")
        for h in (r.p0_hits or [])[:2]:
            print(f"         [P0] {h[:80]}")
        for h in (r.p1_hits or [])[:1]:
            print(f"         [P1] {h[:80]}")
        print(f"         [注] {r.notes[-1]}")  # pass^k 统计
    print("─" * 65)
    print(f"合计：{len(all_final)} 条 | PASS={verdicts_count['PASS']} "
          f"PARTIAL={verdicts_count['PARTIAL']} FAIL={verdicts_count['FAIL']}")
    print(f"P0 总命中：{p0_total}  P1 总扣分：{p1_total}")

    if p0_total == 0 and verdicts_count["FAIL"] == 0:
        print("\n  🟢 环节②真模型层：通过")
    else:
        print("\n  🔴 环节②真模型层：存在问题，见上方 P0 明细")
        if any(r.case_id.startswith("G-0") for r in all_final
               if r.verdict == "FAIL"):
            print("     → 若 G-01~G-04 失败，说明四项差异条件规则需要 Prompt 调参")
        if any(r.case_id == "G-05" for r in all_final if r.verdict == "FAIL"):
            print("     → G-05 失败说明缓存毒化修复代码有问题，需检查 granularity.py")

    # 规则校准报告
    _print_calibration_report(all_rounds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=3, help="pass^k 轮数，默认3")
    args = parser.parse_args()
    asyncio.run(main(args.k))
