"""
第一层评测：真模型 + 冻结数据（模型智力测试）

目的：把"模型智力"和"数据脏不脏"分开测。
  · 数据（搜索结果、财务 bundle）→ 仍用冻结夹具（与 C 类用例完全相同）
  · LLM 抽取 → 换成真实 DeepSeek
回答的问题：模型到底会不会想？
  1. 能不能从真实网页文本里抽对数字（R-01/R-02）
  2. 能不能判断信源可信度等级（R-03）
  3. 能不能识别真冲突 vs 舍入差（R-04/R-05）
  4. 零信源时会不会凭记忆编造（R-06 —— 防幻觉关键题）

运行：
  PYTHONPATH=. python3 tests/eval_facts_builder_real.py
  PYTHONPATH=. python3 tests/eval_facts_builder_real.py --k 1      # 只跑1轮（省钱快速验证）
  PYTHONPATH=. python3 tests/eval_facts_builder_real.py --k 3      # 默认3轮 pass^k

成本：每用例最多 2 次 LLM 调用 × 6 用例 × k 轮 ≈ 最多 36 次调用（DeepSeek 约几毛钱）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from core.config import settings
from core.llm import LLMClient

from runtime.preloop.facts_builder import build_company_facts, clear_cache
from runtime.schemas import CurrentEntitySchema, AliasType, EntitySource, InfoRichness

# 冻结夹具直接复用 C 类评测（数据层不变）
from eval_facts_builder_c import (
    _ZHIJI_SEARCH, _ZHIJI_YEARS,
    _TENCENT_SEARCH,
    _entity, _market, _web, _verdict, Result,
)

logging.basicConfig(level=logging.WARNING)

llm_client = LLMClient(settings)


# ════════════════════════════════════════════════════════════════
# 用例定义（真模型 + 冻结数据）
# ════════════════════════════════════════════════════════════════

async def run_R01() -> Result:
    """R-01 数值抽取：真模型从冻结网页文本抽 NOR 65% / MCU 25%。

    夹具 snippet 明确写"NOR Flash分部2024年营收占总营收的65%，MCU占25%"。
    模型若只会"看着像半导体公司就填"，占比会飘；真正读文本的模型能抽准。
    容差 ±0.02（LLM 对 65% → 0.65 的转换允许小数点误差）。
    """
    clear_cache()
    e = _entity("兆易创新", "603986.SH", "603986", "SH")
    facts = await build_company_facts(e, llm_client, _market(_ZHIJI_YEARS), _web(_ZHIJI_SEARCH))

    p0, p1 = [], []

    if not facts.segments:
        p0.append("segments 为空：模型未能从冻结网页抽出任何分部")
    else:
        nor = next((s for s in facts.segments if "NOR" in s.name or "存储" in s.name), None)
        mcu = next((s for s in facts.segments if "MCU" in s.name or "微控" in s.name), None)

        if nor is None:
            p1.append(f"未识别出 NOR Flash 分部，实际分部: {[s.name for s in facts.segments]}")
        elif nor.revenue_share is None:
            p1.append("NOR Flash.revenue_share 为 None，模型未从文本抽出占比")
        elif abs(nor.revenue_share - 0.65) > 0.02:
            p0.append(
                f"NOR Flash.revenue_share={nor.revenue_share:.3f}，"
                f"信源明确写 65%，偏差超容差 ±0.02 —— 模型未忠实抽取"
            )

        if mcu is None:
            p1.append(f"未识别出 MCU 分部，实际分部: {[s.name for s in facts.segments]}")
        elif mcu.revenue_share is not None and abs(mcu.revenue_share - 0.25) > 0.02:
            p1.append(f"MCU.revenue_share={mcu.revenue_share:.3f}，信源写 25%")

    if not facts.one_line_business or "待补充" in facts.one_line_business:
        p1.append(f"one_line_business 降级: {facts.one_line_business!r}")

    return Result("R-01", "真模型数值抽取（NOR 65%/MCU 25%）", _verdict(p0, p1), p0, p1)


async def run_R02() -> Result:
    """R-02 多分部抽取：腾讯 3 分部（增值服务50% / 广告17% / 金融科技32%）。

    夹具只有两条 snippet，第二条明确说"金融科技与企业服务合并且无法单独拆分"。
    好模型应抽出 3 个分部（金融科技整体作为一个分部），而不是硬拆成 4 个。
    """
    clear_cache()
    e = _entity("腾讯控股", "00700.HK", "00700", "HK", market="HK")
    tencent_years = [{"year": 2024, "revenue": 660000, "net_profit": 199000,
                       "gross_margin": 50.1, "ocf": 200000, "capex": 15000}]
    facts = await build_company_facts(
        e, llm_client, _market(tencent_years, "HKD"), _web(_TENCENT_SEARCH))

    p0, p1 = [], []

    if len(facts.segments) < 2:
        p0.append(f"分部数 {len(facts.segments)} < 2，多业务公司抽取不完整: "
                  f"{[s.name for s in facts.segments]}")

    # 占比总和合理（0.85~1.05），有占比的分部
    shares = [s.revenue_share for s in facts.segments if s.revenue_share is not None]
    if shares:
        total = sum(shares)
        if not (0.85 <= total <= 1.05):
            p1.append(f"分部占比合计 {total:.2f}，偏离 1.0 较远（信源: 50/17/32）")

    # 金融科技合并披露 → 模型应把它当一个分部，或记 open_question
    fintech_seg = [s for s in facts.segments
                   if "金融" in s.name or "企业服务" in s.name]
    if not fintech_seg:
        p1.append(f"未识别金融科技与企业服务分部: {[s.name for s in facts.segments]}")

    return Result("R-02", "真模型多分部抽取（腾讯3分部）", _verdict(p0, p1), p0, p1)


async def run_R03() -> Result:
    """R-03 信源可信度判断：one_line_business 应溯源到 S/A 级信源。

    夹具混入 3 条：S 级 cninfo（年报）、A 级 eastmoney、B 级 random-forum。
    好模型的关键数字应引用 S/A 级信源；引用 B 级论坛帖（"毛利率80%未核实"）
    作为业务事实来源属信源误判。
    """
    clear_cache()
    e = _entity("兆易创新", "603986.SH", "603986", "SH")

    mixed_search = [
        {"title": "兆易创新2024年报：NOR Flash营收占比65%",
         "snippet": "据年报，NOR Flash分部2024年营收占总营收的65%，MCU占25%。",
         "url": "https://cninfo.com.cn/zhiyi_2024_annual.html", "source_level": "S"},
        {"title": "东方财富研报摘要",
         "snippet": "兆易创新为Fabless模式半导体设计公司。",
         "url": "https://eastmoney.com/zhiyi_note.html", "source_level": "A"},
        {"title": "投资者论坛帖子",
         "snippet": "有人说兆易创新毛利率80%（未核实，论坛传言）。",
         "url": "https://random-forum.example.com/post/12345", "source_level": "B"},
    ]

    facts = await build_company_facts(e, llm_client, _market(_ZHIJI_YEARS), _web(mixed_search))

    p0, p1 = [], []

    # P0: B 级论坛 URL 不应成为结论性字段的溯源
    forum_traced = [
        t for t in facts.source_trace
        if t.url and "random-forum" in t.url
    ]
    if forum_traced:
        p0.append(f"论坛帖（B级/白名单外）被用作溯源: {[t.field for t in forum_traced]}")

    # P0: "毛利率80%" 这种未核实数字不应出现在事实包
    def _mentions_80(x) -> bool:
        if x is None:
            return False
        if isinstance(x, (int, float)):
            return abs(x - 80) < 0.5 or abs(x - 0.80) < 0.005
        return "80" in str(x)

    gm = facts.financial_snapshot.gross_margin
    if _mentions_80(gm):
        p0.append(f"gross_margin={gm}，疑似采信了论坛'毛利率80%'传言（真实值约42.5%）")
    for s in facts.segments:
        if _mentions_80(s.revenue_share):
            p0.append(f"分部 {s.name}.revenue_share={s.revenue_share}，疑似采信论坛传言")

    # P1: 结论性字段应溯源到 S/A 级
    good_traced = [
        t for t in facts.source_trace
        if t.url and ("cninfo" in t.url or "eastmoney" in t.url)
    ]
    if not good_traced:
        p1.append("未见任何指向 S/A 级信源（cninfo/eastmoney）的溯源")

    return Result("R-03", "真模型信源可信度判断", _verdict(p0, p1), p0, p1)


async def run_R04() -> Result:
    """R-04 真冲突识别：年报毛利率 45.2% vs TTM 38.5%（差 6.7pct）。

    两个数都对但口径不同——好的处理是记入 open_questions 说明口径差异，
    而不是静默选一个，也不是当成数据错误。
    注：夹具附带主营业务信息（走正常抽取路径，不触发降级）；
    降级路径下的冲突保留由 C-D2 升级版覆盖。
    """
    clear_cache()
    e = _entity("测试公司C", "000002.SZ", "000002", "SZ")

    conflict_search = [
        {"title": "测试公司C年报摘要",
         "snippet": "公司主营住宅地产开发，2024年年报披露毛利率45.2%（年报口径）。",
         "url": "https://cninfo.com.cn/c2024a.html", "source_level": "S"},
        {"title": "TTM口径毛利率38.5%",
         "snippet": "按TTM滚动口径计算毛利率38.5%，与年报口径存在差异。",
         "url": "https://eastmoney.com/c2024b.html", "source_level": "A"},
    ]

    facts = await build_company_facts(
        e, llm_client,
        _market([{"year": 2024, "revenue": 8000, "net_profit": 500,
                   "gross_margin": 45.2, "ocf": 400, "capex": 200}]),
        _web(conflict_search),
    )

    p0, p1 = [], []

    conflict_qs = [q for q in facts.open_questions
                   if any(k in q for k in ("冲突", "口径", "差异", "不一致"))]
    if not conflict_qs:
        p0.append(
            "信源毛利率口径冲突（45.2% vs 38.5%）未记入 open_questions，"
            "模型未识别或静默择一 —— 违反'不臆断'铁律"
        )

    return Result("R-04", "真模型冲突识别（45.2 vs 38.5）", _verdict(p0, p1), p0, p1)


async def run_R05() -> Result:
    """R-05 舍入差不误报：年报 45.2% vs 精确 45.25%（差 0.05pct）。

    与 R-04 对照：这俩是同一口径不同精度，不应升级为'冲突'告警。
    模型若把所有数值差异都报冲突 → 噪音淹没真问题。
    """
    clear_cache()
    e = _entity("测试公司E", "000003.SZ", "000003", "SZ")

    rounding_search = [
        {"title": "年报毛利率45.2%",
         "snippet": "公司2024年毛利率45.2%（四舍五入到一位小数）。",
         "url": "https://cninfo.com.cn/e2024a.html", "source_level": "S"},
        {"title": "数据服务商毛利率45.25%",
         "snippet": "按精确口径计算毛利率45.25%。",
         "url": "https://eastmoney.com/e2024b.html", "source_level": "A"},
    ]

    facts = await build_company_facts(
        e, llm_client,
        _market([{"year": 2024, "revenue": 5000, "net_profit": 300,
                   "gross_margin": 45.2, "ocf": 280, "capex": 100}]),
        _web(rounding_search),
    )

    p0, p1 = [], []

    false_conflicts = [
        q for q in facts.open_questions
        if ("冲突" in q or "矛盾" in q) and "45" in q
        # 注意：单纯提"口径差异"不算误报（模型谨慎说明精度差是合理的），
        # 只有标为"冲突/矛盾"才算噪音
    ]
    if false_conflicts:
        p0.append(f"舍入差（45.2 vs 45.25）被误判为冲突: {false_conflicts}")

    return Result("R-05", "真模型舍入差识别（45.2 vs 45.25）", _verdict(p0, p1), p0, p1)


async def run_R06() -> Result:
    """R-06 零信源不编造：搜索为空时，模型不得凭记忆写业务描述。

    ★ 防幻觉关键题 ★
    测试对象是知名公司（兆易创新）——模型训练语料里肯定有它的信息。
    PRD 要求：信源为空 → 不凭模型记忆填充。
    真实风险：模型很可能顺手写出"以NOR Flash为主的半导体设计公司"。
    这题暴露的是"记忆泄漏"风险，第一层评测里最可能出现 FAIL 的地方。
    """
    clear_cache()
    e = _entity("兆易创新", "603986.SH", "603986", "SH")

    facts = await build_company_facts(
        e, llm_client, _market([]), _web([])  # 零搜索结果 + 零财务数据
    )

    p0, p1 = [], []

    # P0: one_line_business 若含具体业务描述（而非"待补充"占位）= 凭记忆编造
    ol = facts.one_line_business or ""
    if ol and "待补充" not in ol and len(ol) > 10:
        # 已知兆易创新的业务关键词——若出现说明用了训练记忆
        memory_keywords = ["NOR", "Flash", "存储", "MCU", "半导体", "芯片", "Fabless"]
        hits = [k for k in memory_keywords if k in ol]
        if hits:
            p0.append(
                f"零信源时 one_line_business 含模型记忆内容（命中关键词 {hits}）: {ol!r}"
            )
        else:
            p1.append(f"零信源时 one_line_business 非空且非占位: {ol!r}")

    # P0: segments 不得凭记忆出现
    if facts.segments:
        p0.append(f"零信源时 segments 非空（{len(facts.segments)} 个）——记忆泄漏")

    # P0: 财务数字不得凭记忆出现
    snap = facts.financial_snapshot
    if snap.revenue_ttm is not None or snap.gross_margin is not None:
        p0.append(f"零数据源时财务字段有值: rev={snap.revenue_ttm}, gm={snap.gross_margin}")

    # P1: 缺口被记录
    if not facts.open_questions:
        p1.append("零信源时 open_questions 为空")

    return Result("R-06", "真模型零信源不编造（防幻觉关键题）", _verdict(p0, p1), p0, p1)


REAL_CASES = [run_R01, run_R02, run_R03, run_R04, run_R05, run_R06]


# ════════════════════════════════════════════════════════════════
# 执行框架：pass^k + 逐轮明细（供权重校准）
# ════════════════════════════════════════════════════════════════

async def main(k: int = 3) -> None:
    print(f"\n真模型: {llm_client.model} @ {llm_client.provider}")
    print(f"冻结数据夹具 × {k} 轮（pass^{k}）\n")

    all_results: list[Result] = []

    for fn in REAL_CASES:
        round_results: list[Result] = []
        for i in range(k):
            r = await fn()
            round_results.append(r)
            icon = {"PASS": "✓", "PARTIAL": "△", "FAIL": "✗"}[r.verdict]
            print(f"  {icon} [{r.case_id}] 第{i+1}/{k}轮 {r.verdict}")
            for h in r.p0_hits:
                print(f"       [P0] {h}")
            for h in r.p1_hits:
                print(f"       [P1] {h}")

        # pass^k 汇总：全部 PASS 才 PASS；有 FAIL 则 FAIL；否则 PARTIAL
        verdicts = [r.verdict for r in round_results]
        if all(v == "PASS" for v in verdicts):
            final = "PASS"
        elif "FAIL" in verdicts:
            final = "FAIL"
        else:
            final = "PARTIAL"

        merged_p0 = [h for r in round_results for h in r.p0_hits]
        merged_p1 = [h for r in round_results for h in r.p1_hits]
        merged = Result(
            round_results[0].case_id, round_results[0].desc, final,
            merged_p0, merged_p1,
            notes=[f"pass^{k}: {verdicts.count('PASS')}PASS/"
                   f"{verdicts.count('PARTIAL')}PARTIAL/{verdicts.count('FAIL')}FAIL"],
        )
        all_results.append(merged)
        print()

    # ── 汇总 ──────────────────────────────────────────────
    print("═" * 60)
    print(f"{'ID':<8} {'判定':<8} 描述")
    print("─" * 60)
    verdicts = {"PASS": 0, "PARTIAL": 0, "FAIL": 0}
    for r in all_results:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
        icon = {"PASS": "✓", "PARTIAL": "△", "FAIL": "✗"}[r.verdict]
        print(f"{r.case_id:<8} {icon} {r.verdict:<6} {r.desc}")
        for h in r.p0_hits[:3]:
            print(f"         [P0] {h[:90]}")
        if len(r.p0_hits) > 3:
            print(f"         ... 共 {len(r.p0_hits)} 条 P0")
        for h in r.p1_hits[:2]:
            print(f"         [P1] {h[:90]}")
        print(f"         [注] {r.notes[0]}")

    total = len(all_results)
    print("─" * 60)
    print(f"合计：{total} 条 | PASS={verdicts['PASS']} "
          f"PARTIAL={verdicts['PARTIAL']} FAIL={verdicts['FAIL']}")
    print(f"P0 总命中：{sum(len(r.p0_hits) for r in all_results)} 次（真模型幻觉/误判证据）")
    print(f"P1 总扣分：{sum(len(r.p1_hits) for r in all_results)} 次")

    p0_total = sum(len(r.p0_hits) for r in all_results)
    if verdicts["FAIL"] == 0 and p0_total == 0:
        print("\n  🟢 第一层（真模型+冻结数据）：通过")
    else:
        print("\n  🔴 第一层：存在真实模型智力缺陷，见上方 P0 明细")
        print("     → 这些是评分规则校准和 Prompt 迭代的输入，不是管道 bug")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=3, help="重复轮数（pass^k），默认 3")
    args = parser.parse_args()
    asyncio.run(main(args.k))
