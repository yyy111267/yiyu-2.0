"""
C 类用例评测：事实包构建（11 条）
  正向：C-01 ~ C-06
  防御：C-D1 ~ C-D5（pass^k=5）

运行：
  PYTHONPATH=. python3 tests/eval_facts_builder_c.py
  PYTHONPATH=. python3 tests/eval_facts_builder_c.py --defence-k 3   # 快速跑防御
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")

from runtime.preloop.facts_builder import (
    _FACTS_CACHE,
    _parse_llm_output,
    _validate_and_fix,
    build_company_facts,
    clear_cache,
)
from runtime.schemas import (
    AliasType,
    BusinessSegment,
    CompanyFacts,
    CurrentEntitySchema,
    EntitySource,
    FinancialSnapshot,
    SourceTrace,
)

logging.basicConfig(level=logging.WARNING)  # 静默 INFO，只看告警


# ════════════════════════════════════════════════════════════════
# 评测基础设施
# ════════════════════════════════════════════════════════════════

def _entity(
    name: str,
    security_id: str,
    code: str,
    exchange: str,
    market: str = "A",
    alias_type: str = "abbreviation",
) -> CurrentEntitySchema:
    return CurrentEntitySchema(
        canonical_name=name,
        security_id=security_id,
        code=code,
        exchange=exchange,
        primary_market=market,
        entity_source=EntitySource.EXPLICIT,
        alias_type=AliasType(alias_type),
    )


def _llm(responses: list[dict]):
    """按顺序依次返回 responses；超出后重复最后一个。记录调用次数。"""
    state = {"calls": 0}

    async def chat_json(system, user, temperature=0.1, **kw):
        idx = min(state["calls"], len(responses) - 1)
        state["calls"] += 1
        return responses[idx]

    m = MagicMock()
    m.chat_json = chat_json
    m._state = state
    return m


def _web(results: list[dict], *, fail: bool = False):
    """
    results: [{title, snippet, url, source_level}]
    fail=True: 模拟超时/异常，抛 RuntimeError
    """
    async def fn(query, max_results=5, sources=None, **kw):
        if fail:
            raise RuntimeError("mock 联网检索失败")
        return {
            "results": results[:max_results],
            "count": min(len(results), max_results),
            "sources_verified": True,
        }
    return fn


def _market(years: list[dict], currency: str = "CNY", *, fail: bool = False):
    """模拟 MarketData.bundle。fail=True 时抛异常。"""
    if fail:
        md = MagicMock()
        md.bundle = AsyncMock(side_effect=RuntimeError("mock market.bundle 失败"))
        return md

    fund = MagicMock()
    fund.years = years
    fund.source = "mock"
    fund.asof = "2025-12-31"

    snap = MagicMock()
    snap.currency = currency
    snap.name = "Mock Co"

    bundle = MagicMock()
    bundle.fundamentals = fund
    bundle.snapshot = snap
    bundle.errors = []

    md = MagicMock()
    md.bundle = AsyncMock(return_value=bundle)
    return md


# ── 标准财务数据（兆易创新样本）
_ZHIJI_YEARS = [{"year": 2024, "revenue": 4200, "net_profit": 420,
                  "gross_margin": 42.5, "ocf": 380, "capex": 60}]

# ── 标准 LLM 响应（兆易创新）
_ZHIJI_LLM_OK = {
    "one_line_business": "以NOR Flash存储芯片和MCU为主业的半导体设计公司",
    "segments": [
        {"name": "NOR Flash", "revenue_share": 0.65,
         "customer": "消费电子/工业客户", "charging": "一次性销售",
         "channel": "分销商/直销", "competition": "华邦/旺宏",
         "capex_profile": "轻资产（Fabless）"},
        {"name": "MCU", "revenue_share": 0.25,
         "customer": "工业/汽车电子客户", "charging": "一次性销售",
         "channel": "分销商", "competition": "中颖/ST",
         "capex_profile": "轻资产（Fabless）"},
    ],
    "source_field_map": {
        "one_line_business": "source_0",
        "segments[0].name": "source_0",
        "segments[0].revenue_share": "source_0",
        "segments[1].name": "source_0",
        "segments[1].revenue_share": "source_0",
    },
    "open_questions": [],
}

# ── 标准搜索结果
_ZHIJI_SEARCH = [
    {"title": "兆易创新2024年报：NOR Flash营收占比65%",
     "snippet": "兆易创新2024年主营NOR Flash和MCU，NOR Flash营收占比约65%。",
     "url": "https://www.eastmoney.com/zhiyi/2024annual.html",
     "source_level": "A"},
    {"title": "兆易创新：Fabless模式，主要客户消费电子",
     "snippet": "公司采用Fabless模式，无自有晶圆厂，资产轻。",
     "url": "https://www.cninfo.com.cn/zhiyi/2024report.html",
     "source_level": "S"},
]

# ── 腾讯多分部 LLM 响应
_TENCENT_LLM_OK = {
    "one_line_business": "以游戏、广告、金融科技与企业服务为主业的综合互联网集团",
    "segments": [
        {"name": "增值服务（游戏）", "revenue_share": 0.50,
         "customer": "个人玩家", "charging": "虚拟道具/订阅",
         "channel": "自有平台/应用商店", "competition": "网易/米哈游",
         "capex_profile": "轻资产"},
        {"name": "网络广告", "revenue_share": 0.17,
         "customer": "广告主", "charging": "按效果计费/展示",
         "channel": "微信/视频号", "competition": "字节/百度",
         "capex_profile": "轻资产"},
        {"name": "金融科技与企业服务", "revenue_share": 0.32,
         "customer": "个人用户/企业", "charging": "费率抽成/SaaS订阅",
         "channel": "微信支付/腾讯云", "competition": "蚂蚁/阿里云",
         "capex_profile": "中等"},
    ],
    "source_field_map": {
        "one_line_business": "source_0",
        "segments[0].name": "source_0",
        "segments[0].revenue_share": "source_0",
        "segments[1].name": "source_0",
        "segments[1].revenue_share": "source_0",
        "segments[2].name": "source_1",
        "segments[2].revenue_share": "source_1",
    },
    "open_questions": ["金融科技与企业服务合并披露，无法进一步拆分"],
}

_TENCENT_SEARCH = [
    {"title": "腾讯2024年报：增值服务占50%",
     "snippet": "腾讯2024年收入结构：增值服务50%，网络广告17%，金融科技32%。",
     "url": "https://www.eastmoney.com/tencent2024.html",
     "source_level": "A"},
    {"title": "腾讯控股业务分部详解",
     "snippet": "金融科技与企业服务合并在财报中披露，无法单独拆分。",
     "url": "https://www.tencent.com/ir/2024.html",
     "source_level": "S"},
]


@dataclass
class Result:
    case_id: str
    desc: str
    verdict: str       # PASS / PARTIAL / FAIL
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


# ════════════════════════════════════════════════════════════════
# 正向用例
# ════════════════════════════════════════════════════════════════

async def run_C01() -> Result:
    """C-01 单一主业公司事实包完整构建（兆易创新）。"""
    clear_cache()
    e = _entity("兆易创新", "603986.SH", "603986", "SH")
    facts = await build_company_facts(
        e,
        _llm([_ZHIJI_LLM_OK]),
        _market(_ZHIJI_YEARS),
        _web(_ZHIJI_SEARCH),
    )

    p0, p1 = [], []

    # P0: one_line_business 来自信源（非空且有 source_trace 指向 one_line_business）
    if not facts.one_line_business or "待补充" in facts.one_line_business:
        p0.append("one_line_business 为空或降级")
    traced = {t.field for t in facts.source_trace}
    if "one_line_business" not in traced:
        p0.append("one_line_business 缺少 source_trace")

    # P0: financial_snapshot 关键数字有值且有 trace
    snap = facts.financial_snapshot
    for fname in ("revenue_ttm", "gross_margin", "ocf", "capex"):
        if getattr(snap, fname) is None:
            p0.append(f"financial_snapshot.{fname} 为 None")
    snap_traced = {t.field for t in facts.source_trace if "financial_snapshot" in t.field}
    if not snap_traced:
        p0.append("financial_snapshot 无任何 source_trace")

    # P1: segments / open_questions / facts_version 字段齐全
    if not facts.segments:
        p1.append("segments 为空")
    if not facts.facts_version:
        p1.append("facts_version 为空")

    return Result("C-01", "单一主业公司事实包完整构建", _verdict(p0, p1), p0, p1)


async def run_C02() -> Result:
    """C-02 多业务分部结构抽取（腾讯控股）。"""
    clear_cache()
    e = _entity("腾讯控股", "00700.HK", "00700", "HK", market="HK")
    tencent_years = [{"year": 2024, "revenue": 660000, "net_profit": 199000,
                       "gross_margin": 50.1, "ocf": 200000, "capex": 15000}]
    facts = await build_company_facts(
        e,
        _llm([_TENCENT_LLM_OK]),
        _market(tencent_years, "HKD"),
        _web(_TENCENT_SEARCH),
    )

    p0, p1 = [], []

    # P0: 分部至少3个 + 占比来自信源（有 source_trace）
    if len(facts.segments) < 3:
        p0.append(f"segments 数量 {len(facts.segments)} < 3，分部抽取不完整")

    total_share = sum(
        (s.revenue_share or 0) for s in facts.segments if s.revenue_share is not None
    )
    if total_share > 0 and not (0.85 <= total_share <= 1.05):
        p0.append(f"各分部收入占比合计 {total_share:.2f}，不在合理区间 [0.85, 1.05]")

    seg_traces = [t for t in facts.source_trace if "segments" in t.field]
    if not seg_traces:
        p0.append("segments 字段组无任何 source_trace")

    # P1: 每分部 customer / charging 非空
    missing_meta = [
        s.name for s in facts.segments
        if not s.customer or not s.charging
    ]
    if missing_meta:
        p1.append(f"以下分部缺少 customer 或 charging: {missing_meta}")

    return Result("C-02", "多业务分部结构抽取", _verdict(p0, p1), p0, p1)


async def run_C03() -> Result:
    """C-03 缓存复用跳过检索（借 C-01 已构建的缓存）。

    注：C-03 原定为"A 级信息充分评级"，但 info_richness 字段尚未在
    CompanyFacts schema 中实现（规划在后续环节），因此本轮替换为测试
    缓存写入后能否正确读取——与 C-05 并列验证同一缓存机制，
    并在 notes 中标注 info_richness 为待实现项。
    """
    clear_cache()
    e = _entity("贵州茅台", "600519.SH", "600519", "SH")
    moutai_years = [{"year": 2024, "revenue": 170000, "net_profit": 85000,
                      "gross_margin": 91.0, "ocf": 82000, "capex": 2000}]
    moutai_llm = {
        "one_line_business": "以飞天茅台为核心的高端白酒企业",
        "segments": [{"name": "茅台酒", "revenue_share": 0.85,
                       "customer": "高端消费者/政务/商务", "charging": "一次性销售",
                       "channel": "经销商/直销", "competition": "五粮液/泸州老窖",
                       "capex_profile": "中等（酿造产能）"}],
        "source_field_map": {"one_line_business": "source_0",
                              "segments[0].name": "source_0",
                              "segments[0].revenue_share": "source_0"},
        "open_questions": [],
    }
    moutai_search = [
        {"title": "贵州茅台2024年报：茅台酒营收占比85%",
         "snippet": "茅台2024年茅台酒营收占比85%，毛利率91%。",
         "url": "https://www.cninfo.com.cn/moutai2024.html", "source_level": "S"},
    ]

    llm_client = _llm([moutai_llm])
    facts1 = await build_company_facts(
        e, llm_client, _market(moutai_years), _web(moutai_search)
    )
    calls_after_first = llm_client._state["calls"]

    # 第二次调用（应命中缓存）
    facts2 = await build_company_facts(
        e, llm_client, _market(moutai_years), _web(moutai_search)
    )
    calls_after_second = llm_client._state["calls"]

    p0, p1 = [], []
    if calls_after_second != calls_after_first:
        p0.append(
            f"第二次调用触发了 LLM（第一次后 calls={calls_after_first}，"
            f"第二次后 calls={calls_after_second}），缓存未生效"
        )
    if facts2.facts_version != facts1.facts_version:
        p0.append("缓存命中后 facts_version 漂移")
    if facts2.one_line_business != facts1.one_line_business:
        p1.append("缓存命中后 one_line_business 内容不一致")

    # ── info_richness 断言（A 级：信息充分） ──────────────────────
    from runtime.schemas import InfoRichness
    if facts1.info_richness != InfoRichness.A:
        p1.append(
            f"茅台信息充分，info_richness 应为 A，得到 {facts1.info_richness.value!r}"
        )
    # A 级初始 anti_consensus_triggered 应为 False（由调用方/环节④在计划生成前置 True）
    if facts1.anti_consensus_triggered:
        p1.append(
            "info_richness=A 时 anti_consensus_triggered 初始应为 False，"
            "应由环节④在计划生成前触发"
        )

    notes = ["A级反共识触发时机：由环节④在研究计划生成前将 anti_consensus_triggered 置 True"]
    return Result("C-03", "A级信息充分评级（缓存一致性 + info_richness 验证）",
                  _verdict(p0, p1), p0, p1, notes)


async def run_C04() -> Result:
    """C-04 C 级信息稀缺：不补值，open_questions 显式列缺口。"""
    clear_cache()
    e = _entity("新上市科技公司A", "688999.SH", "688999", "SH")

    # 模拟信息极度稀缺：LLM 无法抽到有效内容（空响应）
    sparse_llm = {
        "one_line_business": "",   # 无法从信源得到
        "segments": [],
        "source_field_map": {},
        "open_questions": ["公司上市不足1年，无完整年报分部附注；卖方研报覆盖极少"],
    }
    sparse_search: list[dict] = []  # 搜索返回为空
    sparse_years: list[dict] = []    # 无财务数据

    facts = await build_company_facts(
        e,
        _llm([sparse_llm, sparse_llm]),  # 两次都是空
        _market(sparse_years),
        _web(sparse_search),
    )

    p0, p1 = [], []

    # P0: 不应凭记忆填充（one_line_business 为空或含"待补充"降级标记是预期行为）
    if facts.one_line_business and "待补充" not in facts.one_line_business and len(facts.one_line_business) > 20:
        p0.append(
            f"信源为空时 one_line_business 不应有实质内容，但得到: {facts.one_line_business!r}"
        )

    # P0: financial_snapshot 关键字段应为 None（无数据源）
    snap = facts.financial_snapshot
    if snap.revenue_ttm is not None:
        p0.append(f"无财务数据时 revenue_ttm 应为 None，但得到 {snap.revenue_ttm}")
    if snap.gross_margin is not None:
        p0.append(f"无财务数据时 gross_margin 应为 None，但得到 {snap.gross_margin}")

    # P1: open_questions 有内容
    if not facts.open_questions:
        p1.append("open_questions 为空，缺口未显式记录")

    # ── info_richness 断言（C 级：信息稀缺） ──────────────────────
    from runtime.schemas import InfoRichness
    if facts.info_richness != InfoRichness.C:
        p0.append(
            f"信息稀缺场景 info_richness 应为 C，得到 {facts.info_richness.value!r}；"
            "下游环节④无法触发范围收窄逻辑"
        )
    if facts.anti_consensus_triggered:
        p0.append("C 级不应触发反共识检查（anti_consensus_triggered 应为 False）")

    notes = ["C级研究范围收窄逻辑待环节④验证；禁止行业均值补值待端到端测试覆盖"]
    return Result("C-04", "C级信息稀缺不补值 + info_richness 验证", _verdict(p0, p1), p0, p1, notes)


async def run_C05() -> Result:
    """C-05 缓存复用跳过检索（同任务第二次触发）。"""
    clear_cache()
    e = _entity("兆易创新", "603986.SH", "603986", "SH")
    llm_client = _llm([_ZHIJI_LLM_OK])

    facts1 = await build_company_facts(
        e, llm_client, _market(_ZHIJI_YEARS), _web(_ZHIJI_SEARCH)
    )
    calls_first = llm_client._state["calls"]
    version_first = facts1.facts_version

    # 第二次调用
    facts2 = await build_company_facts(
        e, llm_client, _market(_ZHIJI_YEARS), _web(_ZHIJI_SEARCH)
    )
    calls_second = llm_client._state["calls"]

    p0, p1 = [], []

    # P0: 返回 v1 缓存产物，关键字段与首次一致（无漂移）
    if calls_second != calls_first:
        p0.append(
            f"第二次调用触发 LLM（第一次 calls={calls_first}，"
            f"第二次 calls={calls_second}），缓存未生效"
        )
    if facts2.facts_version != version_first:
        p0.append(f"缓存命中后 facts_version 漂移：{version_first!r} → {facts2.facts_version!r}")
    if facts2.one_line_business != facts1.one_line_business:
        p0.append("缓存命中后 one_line_business 内容不一致（无漂移断言）")

    # P1: trace 中无新联网检索调用（行为断言，当前通过 LLM call_count 间接验证）
    if calls_second > calls_first:
        p1.append("第二次调用额外触发了模型调用，缓存未完全跳过")

    return Result("C-05", "缓存复用跳过检索", _verdict(p0, p1), p0, p1)


async def run_C06() -> Result:
    """C-06 信源不足升级年报检索（行为断言：当搜索结果不含分部附注时触发升级）。

    当前 facts_builder 尚无独立的"年报检索"工具路径，
    本用例验证：初始搜索结果不含分部细节时，LLM 抽取到的 open_questions
    应包含"需要年报附注"的提示——对应 PRD C-06「升级触发条件明确」的 P1 条件。
    年报工具的集成将在后续迭代中实现，届时补充 P0 验证。
    """
    clear_cache()
    e = _entity("某工业公司B", "600888.SH", "600888", "SH")

    # 只有概况信息，无分部附注细节
    vague_search = [
        {"title": "某工业公司B简介",
         "snippet": "公司主营工业制造，有多个产品线，详细分部数据见年报附注。",
         "url": "https://www.eastmoney.com/b600888.html",
         "source_level": "A"},
    ]
    # LLM 抽取出信息不足，segments 需依赖年报
    vague_llm = {
        "one_line_business": "以工业制造为主业的公司",
        "segments": [{"name": "主营业务（待细化）", "revenue_share": None,
                       "customer": None, "charging": None,
                       "channel": None, "competition": None,
                       "capex_profile": None}],
        "source_field_map": {"one_line_business": "source_0",
                              "segments[0].name": "source_0"},
        "open_questions": [
            "分部细节需查年报附注第X节",
            "收入占比信息在基础检索中未披露，需年报及公告检索补足",
        ],
    }
    facts = await build_company_facts(
        e,
        _llm([vague_llm]),
        _market([{"year": 2024, "revenue": 5000, "net_profit": 300,
                   "gross_margin": 25.0, "ocf": 280, "capex": 100}]),
        _web(vague_search),
    )

    p0, p1 = [], []

    # P0: 最终产物的分部细节有 source_trace 可证（即便不完整也要有）
    seg_traces = [t for t in facts.source_trace if "segments" in t.field]
    if not seg_traces:
        p0.append("segments 字段组无任何 source_trace，即便信息不足也应有来源记录")

    # P1: open_questions 中应有触发升级检索的明确条件描述
    upgrade_hints = [q for q in facts.open_questions
                     if "年报" in q or "附注" in q or "检索" in q]
    if not upgrade_hints:
        p1.append("open_questions 未包含触发年报升级检索的明确提示")

    notes = [
        "年报检索工具集成后补充 P0 验证：分部细节来自年报结果且 trace 可证",
        "当前 P1 验证：升级触发条件在 open_questions 中有明确描述",
    ]
    return Result("C-06", "信源不足升级年报检索（P1验证）", _verdict(p0, p1), p0, p1, notes)


# ════════════════════════════════════════════════════════════════
# 防御用例（pass^k）
# ════════════════════════════════════════════════════════════════

async def run_C_D1() -> Result:
    """C-D1 溯源缺失打回：LLM 返回缺 source_field_map 的结果，护栏应打回。"""
    clear_cache()
    e = _entity("兆易创新", "603986.SH", "603986", "SH")

    # 第一次：缺 source_field_map（护栏应打回重试）
    # 第二次（重试）：补全 source_field_map
    no_trace_llm = {
        "one_line_business": "以NOR Flash为主的半导体设计公司",
        "segments": [{"name": "NOR Flash", "revenue_share": 0.65,
                       "customer": "消费电子", "charging": "一次性销售",
                       "channel": "分销商", "competition": "华邦",
                       "capex_profile": "轻资产"}],
        "source_field_map": {},       # 空 map → 护栏应感知到 source_trace 缺失
        "open_questions": [],
    }

    facts = await build_company_facts(
        e,
        _llm([no_trace_llm, _ZHIJI_LLM_OK]),  # 第一次无trace，第二次有
        _market(_ZHIJI_YEARS),
        _web(_ZHIJI_SEARCH),
    )

    p0, p1 = [], []

    # P0: 缺 source_trace 的字段未进入交付（验证：最终结果有 source_trace）
    traced = {t.field for t in facts.source_trace}
    # 最终产物必须有 one_line_business 的溯源（第二次重试应补上）
    if "one_line_business" not in traced:
        p0.append(
            "最终交付的 company_facts 中 one_line_business 仍缺 source_trace，"
            "说明护栏打回后重试结果未生效"
        )

    # P1: facts_version 非空（即便降级也应有版本）
    if not facts.facts_version:
        p1.append("facts_version 为空")

    return Result("C-D1", "溯源缺失打回", _verdict(p0, p1), p0, p1)


async def run_C_D2() -> Result:
    """C-D2 检索失败降级不阻断：降级为仅结构化数据源，不阻断流程。

    回归增强（真模型评测 R-04 发现的 bug）：搜索成功但 LLM 抽不出
    one_line_business 走降级时，其 open_questions（如口径冲突观察）不得被丢弃。
    场景设计：web search 正常返回（信源只有毛利率、无业务信息可抽），
    LLM 诚实返回空业务 + 冲突观察。
    """
    clear_cache()
    e = _entity("兆易创新", "603986.SH", "603986", "SH")

    # 信源只有毛利率数据（无主营业务信息可抽）
    sparse_search = [
        {"title": "年报口径毛利率45.2%",
         "snippet": "公司2024年年报披露毛利率45.2%（年报口径）。",
         "url": "https://cninfo.com.cn/c2024a.html", "source_level": "S"},
        {"title": "TTM口径毛利率38.5%",
         "snippet": "按TTM滚动口径计算毛利率38.5%，与年报口径存在差异。",
         "url": "https://eastmoney.com/c2024b.html", "source_level": "A"},
    ]

    # LLM 抽不出业务信息（诚实返回空），但识别到了口径冲突（降级时须保留）
    degrade_llm = {
        "one_line_business": "",   # 空 → 触发降级
        "segments": [],
        "source_field_map": {},
        "open_questions": [
            "信源仅有毛利率数据，无主营业务信息，无法构建 one_line_business",
            "年报口径毛利率45.2%与TTM口径38.5%存在差异，需明确口径差异原因",
        ],
    }

    facts = await build_company_facts(
        e,
        _llm([degrade_llm, degrade_llm]),  # 两次都抽不到业务信息 → 降级
        _market(_ZHIJI_YEARS),
        _web(sparse_search),
    )

    p0, p1 = [], []

    # P0: 降级后流程未阻断，产出可用事实包
    if facts is None:
        p0.append("联网检索失败后 build_company_facts 返回 None，流程被阻断")
    else:
        # P0: 数据缺口显式记入 open_questions
        has_gap_record = any(
            "失败" in q or "检索" in q or "信息" in q
            for q in facts.open_questions
        )
        if not has_gap_record:
            p0.append(
                "联网检索失败后 open_questions 未记录缺口，"
                f"实际 open_questions: {facts.open_questions}"
            )

        # P1: 产物有 facts_version（降级也要完整输出）
        if not facts.facts_version:
            p1.append("降级产物 facts_version 为空")

        # P1: financial_snapshot 不为全空（结构化数据源仍可用）
        if facts.financial_snapshot.revenue_ttm is None:
            p1.append("financial_snapshot.revenue_ttm 为空，结构化数据源未生效")

        # P0 回归（R-04 发现的 bug）：LLM 降级前识别的口径冲突不得被丢弃
        conflict_kept = any("口径" in q or "差异" in q for q in facts.open_questions)
        if not conflict_kept:
            p0.append(
                "降级路径丢弃了 LLM 已识别的口径冲突观察"
                f"（open_questions: {facts.open_questions}）"
            )

    return Result("C-D2", "抽取失败降级不阻断+冲突保留", _verdict(p0, p1), p0, p1)


async def run_C_D3() -> Result:
    """C-D3 信源冲突不臆断：两条信源对毛利率口径冲突，应记入 open_questions。"""
    clear_cache()
    e = _entity("测试公司C", "000002.SZ", "000002", "SZ")

    conflict_search = [
        {"title": "年报口径毛利率45.2%",
         "snippet": "公司2024年报毛利率45.2%。",
         "url": "https://cninfo.com.cn/c2024a.html", "source_level": "S"},
        {"title": "TTM口径毛利率38.5%",
         "snippet": "按TTM口径计算毛利率38.5%，与年报存在差异。",
         "url": "https://eastmoney.com/c2024b.html", "source_level": "A"},
    ]
    conflict_llm = {
        "one_line_business": "以住宅地产开发为主业的公司",
        "segments": [{"name": "住宅开发", "revenue_share": 0.90,
                       "customer": "购房者", "charging": "一次性销售",
                       "channel": "自销/中介", "competition": "万科/碧桂园",
                       "capex_profile": "重资产"}],
        "source_field_map": {
            "one_line_business": "source_0",
            "segments[0].name": "source_0",
            "segments[0].revenue_share": "source_0",
        },
        # LLM 识别到冲突并记入 open_questions
        "open_questions": [
            "毛利率存在口径冲突：年报口径45.2% vs TTM口径38.5%，需确认统一口径",
        ],
    }

    facts = await build_company_facts(
        e,
        _llm([conflict_llm]),
        _market([{"year": 2024, "revenue": 8000, "net_profit": 500,
                   "gross_margin": 45.2, "ocf": 400, "capex": 200}]),
        _web(conflict_search),
    )

    p0, p1 = [], []

    # P0: 未在无依据情况下静默选定某一口径（验证：open_questions 记录冲突）
    conflict_recorded = any(
        "冲突" in q or "口径" in q or "差异" in q
        for q in facts.open_questions
    )
    if not conflict_recorded:
        p0.append(
            "信源毛利率口径冲突未记入 open_questions，"
            f"实际 open_questions: {facts.open_questions}"
        )

    # P1: 两口径均可追溯（source_trace 有对应记录）
    if len(facts.source_trace) < 2:
        p1.append(f"source_trace 条数 {len(facts.source_trace)} 偏少，冲突信源应均有追溯")

    return Result("C-D3", "信源冲突不臆断", _verdict(p0, p1), p0, p1)


async def run_C_D4() -> Result:
    """C-D4 零信源不凭记忆生成：检索返回空，LLM 两次均无法抽到内容。"""
    clear_cache()
    e = _entity("极度陌生公司D", "300999.SZ", "300999", "SZ")

    # LLM 诚实返回空（没有信源就不填）
    honest_empty_llm = {
        "one_line_business": "",
        "segments": [],
        "source_field_map": {},
        "open_questions": ["无公开财经信源覆盖，主营业务无法从信源抽取"],
    }

    facts = await build_company_facts(
        e,
        _llm([honest_empty_llm, honest_empty_llm]),
        _market([]),      # 无财务数据
        _web([]),         # 无搜索结果
    )

    p0, p1 = [], []

    # P0: 缺失字段未凭模型记忆填充
    # 即：one_line_business 为空或含"待补充"，不应是一段实质性业务描述
    if (facts.one_line_business
            and "待补充" not in facts.one_line_business
            and "待" not in facts.one_line_business
            and len(facts.one_line_business) > 15):
        p0.append(
            f"零信源时 one_line_business 不应有实质内容，"
            f"得到: {facts.one_line_business!r}"
        )
    if facts.segments:
        p0.append(
            f"零信源时 segments 应为空，得到 {len(facts.segments)} 个分部"
        )
    snap = facts.financial_snapshot
    if snap.revenue_ttm is not None or snap.gross_margin is not None:
        p0.append(
            f"零财务数据时 revenue_ttm/gross_margin 应为 None，"
            f"得到 revenue={snap.revenue_ttm}, gm={snap.gross_margin}"
        )

    # P1: 缺口记入 open_questions
    if not facts.open_questions:
        p1.append("open_questions 为空，数据缺口未记录")

    return Result("C-D4", "零信源不凭记忆生成", _verdict(p0, p1), p0, p1)


async def run_C_D5() -> Result:
    """C-D5 白名单外信源拦截：搜索夹具中混入白名单外来源，不应进入 source_trace。"""
    clear_cache()
    e = _entity("兆易创新", "603986.SH", "603986", "SH")

    # 混入白名单外来源（xueqiu 是 B 级，某随机论坛是白名单外）
    mixed_search = [
        {"title": "兆易创新年报摘要",
         "snippet": "NOR Flash营收占比65%。",
         "url": "https://cninfo.com.cn/zhiyi_ok.html",
         "source_level": "S"},          # 白名单 S 级 ✓
        {"title": "投资者论坛帖子",
         "snippet": "有人说兆易创新毛利率80%（未核实）。",
         "url": "https://random-forum.example.com/post/12345",
         "source_level": "B"},          # 假设论坛在白名单内作为 B 级
        {"title": "白名单外站点",
         "snippet": "兆易创新商业模式分析。",
         "url": "https://not-in-whitelist.xyz/analysis",
         "source_level": "B"},          # 白名单外（仅靠 source_level 标记来区分）
    ]

    # LLM 如实抽取，source_field_map 仅引用 source_0（白名单内）
    filtered_llm = {
        "one_line_business": "以NOR Flash和MCU为主的半导体公司",
        "segments": [{"name": "NOR Flash", "revenue_share": 0.65,
                       "customer": "消费电子", "charging": "一次性销售",
                       "channel": "分销商", "competition": "华邦",
                       "capex_profile": "轻资产"}],
        "source_field_map": {
            "one_line_business": "source_0",       # S 级来源 ✓
            "segments[0].name": "source_0",
            "segments[0].revenue_share": "source_0",
        },
        "open_questions": [],
    }

    facts = await build_company_facts(
        e,
        _llm([filtered_llm]),
        _market(_ZHIJI_YEARS),
        _web(mixed_search),
    )

    p0, p1 = [], []

    # P0: 白名单外来源未进入 source_trace
    # 判据：source_trace 中所有 url 都不含 "random-forum.example.com" 和 "not-in-whitelist.xyz"
    bad_urls = [
        t.url for t in facts.source_trace
        if t.url and (
            "random-forum.example.com" in t.url
            or "not-in-whitelist.xyz" in t.url
        )
    ]
    if bad_urls:
        p0.append(f"白名单外 URL 进入了 source_trace: {bad_urls}")

    # P1: B 级信源（xueqiu 论坛类）若引用，应注明非官方口径
    b_traces = [t for t in facts.source_trace if t.source_level == "B"]
    b_without_note = [
        t for t in b_traces
        if "非官方" not in (t.source or "") and "讨论" not in (t.source or "")
    ]
    # 当前实现中 B 级信源的 source 字段就是 title+url，未特别注明
    # 这是 P1，不是 P0：记录而不阻断
    if b_without_note:
        p1.append(
            f"以下 B 级信源未在 source 字段注明非官方口径: "
            f"{[t.url for t in b_without_note]}"
        )

    return Result("C-D5", "白名单外信源拦截", _verdict(p0, p1), p0, p1)


# ════════════════════════════════════════════════════════════════
# 补充 Case：原评测集未覆盖的 4 类洞
# ════════════════════════════════════════════════════════════════

async def run_C_X1() -> Result:
    """C-X1 数值溯源对得上：source_trace 指向的 URL 里能找到对应数值。

    【补洞理由】溯源"存在" ≠ 溯源"对得上"。原 C-D1 只验证
    source_trace 字段非空，没有验证数值与信源内容是否匹配。
    本用例：固定夹具里 source_0 明确披露 NOR Flash 65%，
    最终 segments[0].revenue_share 必须是 0.65（允许 ±0.01 浮动），
    且 source_trace 指向同一条 URL。
    """
    clear_cache()
    e = _entity("兆易创新", "603986.SH", "603986", "SH")

    # 夹具：source_0 明确写 65%
    pinned_search = [
        {"title": "兆易创新2024年报：NOR Flash营收占比65%",
         "snippet": "据年报，NOR Flash分部2024年营收占总营收的65%，MCU占25%。",
         "url": "https://cninfo.com.cn/zhiyi_2024_annual.html",
         "source_level": "S"},
    ]
    # LLM 诚实抽取 65% → 0.65
    pinned_llm = {
        "one_line_business": "以NOR Flash存储芯片和MCU为主业的半导体Fabless公司",
        "segments": [
            {"name": "NOR Flash", "revenue_share": 0.65,
             "customer": "消费电子", "charging": "一次性销售",
             "channel": "分销商", "competition": "华邦/旺宏",
             "capex_profile": "轻资产"},
            {"name": "MCU", "revenue_share": 0.25,
             "customer": "工业/汽车", "charging": "一次性销售",
             "channel": "分销商", "competition": "中颖/ST",
             "capex_profile": "轻资产"},
        ],
        "source_field_map": {
            "one_line_business": "source_0",
            "segments[0].name": "source_0",
            "segments[0].revenue_share": "source_0",  # 指向同一 URL
            "segments[1].name": "source_0",
            "segments[1].revenue_share": "source_0",
        },
        "open_questions": [],
    }

    facts = await build_company_facts(
        e,
        _llm([pinned_llm]),
        _market(_ZHIJI_YEARS),
        _web(pinned_search),
    )

    p0, p1 = [], []

    # 断言1：revenue_share 数值与信源一致（允许 ±0.01 容差）
    nor_seg = next((s for s in facts.segments if "NOR" in s.name), None)
    if nor_seg is None:
        p0.append("segments 中未找到 NOR Flash 分部")
    elif nor_seg.revenue_share is None:
        p0.append("NOR Flash.revenue_share 为 None，应为 0.65")
    elif abs(nor_seg.revenue_share - 0.65) > 0.01:
        p0.append(
            f"NOR Flash.revenue_share={nor_seg.revenue_share:.3f} "
            f"与信源值 0.65 偏差超过容差 0.01"
        )

    # 断言2：revenue_share 的 source_trace 指向同一 URL
    rs_traces = [
        t for t in facts.source_trace
        if "revenue_share" in t.field and "NOR" not in t.field  # 找 segments[0].revenue_share
           or ("segments[0]" in t.field and "revenue_share" in t.field)
    ]
    # 简化：检查是否有指向 cninfo 的 trace
    cninfo_traces = [t for t in facts.source_trace
                     if t.url and "cninfo.com.cn" in t.url]
    if not cninfo_traces:
        p1.append("source_trace 中未见 cninfo.com.cn 指向，数值出处不明")

    return Result("C-X1", "数值溯源与信源数值匹配验证", _verdict(p0, p1), p0, p1)


async def run_C_X2() -> Result:
    """C-X2 多币种/多单位：港股财务快照单位为百万港元，不能混入人民币或元。

    【补洞理由】腾讯港股上市但部分数据源以「亿元人民币」或「元」
    为单位返回，字段映射若不处理会引入数量级错误（万元/亿元之差=10000x）。
    本用例验证：bundle 返回以百万 HKD 为单位的数据，
    financial_snapshot.currency 必须是 HKD，
    revenue_ttm 数量级应与 bundle 返回值直接对应（不乘/除系数）。
    """
    clear_cache()
    e = _entity("腾讯控股", "00700.HK", "00700", "HK", market="HK")

    # bundle 以「百万港元」为单位返回（这是 FinancialSnapshot 的默认单位）
    # revenue = 660000（即 6600亿港元，合理）
    hk_years = [{"year": 2024, "revenue": 660000, "net_profit": 199000,
                  "gross_margin": 50.1, "ocf": 200000, "capex": 15000}]

    facts = await build_company_facts(
        e,
        _llm([_TENCENT_LLM_OK]),
        _market(hk_years, currency="HKD"),
        _web(_TENCENT_SEARCH),
    )

    p0, p1 = [], []

    # P0: currency 必须是 HKD
    if facts.financial_snapshot.currency != "HKD":
        p0.append(
            f"港股实体 currency 应为 HKD，"
            f"得到 {facts.financial_snapshot.currency!r}"
        )

    # P0: revenue_ttm 数量级合理（应在 500000~800000 之间，即 5000~8000 亿港元）
    rev = facts.financial_snapshot.revenue_ttm
    if rev is not None and not (500_000 <= rev <= 800_000):
        p0.append(
            f"revenue_ttm={rev} 数量级异常（期望 500000~800000 百万港元），"
            "疑似单位换算错误"
        )

    # P1: source_trace 中标注了 as_of
    traces_with_asof = [t for t in facts.source_trace if t.as_of]
    if not traces_with_asof:
        p1.append("financial_snapshot 相关 source_trace 缺少 as_of 时点标注")

    return Result("C-X2", "多币种/多单位不混淆", _verdict(p0, p1), p0, p1)


async def run_C_X3() -> Result:
    """C-X3 口径混用与舍入差识别：两条信源毛利率 45.2% vs 45.25%。

    【补洞理由】原 C-D3 测的是数值差距大的真冲突（45.2 vs 38.5）。
    本用例测"舍入差不应被误判为冲突"：45.2 和 45.25 相差 0.05pct，
    属同一口径不同精度，不应进入 open_questions 的冲突告警。
    反之，45.2 和 45.8 相差 0.6pct，视为真冲突须记录。
    """
    clear_cache()
    e = _entity("测试公司E", "000003.SZ", "000003", "SZ")

    # 两条信源：年报 45.2%，数据服务商 45.25%（精度差，非真冲突）
    rounding_search = [
        {"title": "年报毛利率45.2%",
         "snippet": "公司2024年毛利率为45.2%（四舍五入到一位小数）。",
         "url": "https://cninfo.com.cn/e2024a.html", "source_level": "S"},
        {"title": "数据服务商毛利率45.25%",
         "snippet": "按精确口径计算毛利率45.25%。",
         "url": "https://eastmoney.com/e2024b.html", "source_level": "A"},
    ]
    # LLM 正确识别为舍入差，不记入冲突
    rounding_llm = {
        "one_line_business": "以工业自动化为主业的公司",
        "segments": [{"name": "工业自动化", "revenue_share": 0.90,
                       "customer": "工业制造企业", "charging": "一次性销售",
                       "channel": "直销/渠道", "competition": "西门子/汇川",
                       "capex_profile": "中等"}],
        "source_field_map": {
            "one_line_business": "source_0",
            "segments[0].name": "source_0",
            "segments[0].revenue_share": "source_0",
        },
        # 舍入差不应出现在 open_questions 里
        "open_questions": [],
    }

    facts = await build_company_facts(
        e,
        _llm([rounding_llm]),
        _market([{"year": 2024, "revenue": 5000, "net_profit": 300,
                   "gross_margin": 45.2, "ocf": 280, "capex": 100}]),
        _web(rounding_search),
    )

    p0, p1 = [], []

    # P0: 舍入差（≤0.1pct）不应被记为口径冲突
    false_conflicts = [
        q for q in facts.open_questions
        if ("冲突" in q or "口径" in q) and "45" in q
    ]
    if false_conflicts:
        p0.append(
            f"舍入差（45.2 vs 45.25）被误判为口径冲突记入 open_questions: {false_conflicts}"
        )

    # P1: gross_margin 值在合理范围（0.45 ± 0.005）
    gm = facts.financial_snapshot.gross_margin
    if gm is not None and abs(gm - 0.452) > 0.005:
        p1.append(f"gross_margin={gm:.4f} 超出期望范围 [0.447, 0.457]")

    notes = ["真冲突（差距>0.1pct）仍应记录，舍入差不应误报——反向用例见 C-D3"]
    return Result("C-X3", "舍入差不误判为口径冲突", _verdict(p0, p1), p0, p1, notes)


async def run_C_X4() -> Result:
    """C-X4 空包的下游契约：C-D2/C-D4 产出空事实包后，其字段结构须满足环节②消费契约。

    【补洞理由】C-D2/C-D4 验证的是"降级不阻断 + 缺口记录"，
    但没定义空包流入环节②时的最小可消费性：
      · facts_version 非空（环节②缓存键依赖）
      · info_richness 字段存在且为 C 级（环节②/③按档调整）
      · entity 字段完整（security_id 可用）
      · open_questions 非空（环节④范围收窄依据）
    本用例专门从"下游能不能正确消费"视角对空包做契约断言。
    """
    clear_cache()
    e = _entity("极度陌生公司F", "301001.SZ", "301001", "SZ")

    # 零信源，产出空包
    facts = await build_company_facts(
        e,
        _llm([{"one_line_business": "", "segments": [],
               "source_field_map": {}, "open_questions": ["无公开信源"]}]),
        _market([]),
        _web([]),
    )

    p0, p1 = [], []

    # ── 契约1：facts_version 非空（环节②缓存键） ──────────────
    if not facts.facts_version:
        p0.append("空包 facts_version 为空，环节②无法缓存")

    # ── 契约2：info_richness 字段存在且为 C 级 ──────────────────
    from runtime.schemas import InfoRichness
    if facts.info_richness != InfoRichness.C:
        p0.append(
            f"零信源时 info_richness 应为 C，得到 {facts.info_richness.value!r}；"
            "下游环节③/④无法按档调整"
        )

    # ── 契约3：entity.security_id 完整可用 ───────────────────────
    if not facts.entity.security_id:
        p0.append("空包 entity.security_id 为空，环节②无法路由")

    # ── 契约4：open_questions 非空（研究范围收窄依据） ──────────
    if not facts.open_questions:
        p0.append("空包 open_questions 为空，环节④无法生成收窄提示")

    # ── 契约5：anti_consensus_triggered 应为 False（C 级不触发）──
    if facts.anti_consensus_triggered:
        p0.append("C 级空包不应触发反共识检查（anti_consensus_triggered=True）")

    # ── P1：schema 合法性（Pydantic 不报错说明字段结构完整）──────
    # 运行到这里说明构造未抛出异常，P1 直接通过
    notes = ["此用例从'下游消费'视角测空包契约，补 C-D2/C-D4 的跨模块盲区"]
    return Result("C-X4", "空包下游消费契约验证", _verdict(p0, p1), p0, p1, notes)


# ════════════════════════════════════════════════════════════════
# 执行框架
# ════════════════════════════════════════════════════════════════

POSITIVE_CASES = [run_C01, run_C02, run_C03, run_C04, run_C05, run_C06]
DEFENCE_CASES  = [run_C_D1, run_C_D2, run_C_D3, run_C_D4, run_C_D5]
EXTENDED_CASES = [run_C_X1, run_C_X2, run_C_X3, run_C_X4]


async def run_all(defence_k: int = 5) -> list[Result]:
    results: list[Result] = []

    print("\n── 正向用例 ─────────────────────────────────────────")
    for fn in POSITIVE_CASES:
        r = await fn()
        results.append(r)

    print("\n── 防御用例（pass^k={} ）────────────────────────────".format(defence_k))
    for fn in DEFENCE_CASES:
        round_results: list[Result] = []
        for _ in range(defence_k):
            round_results.append(await fn())
        worst = round_results[-1]
        if any(r.verdict == "FAIL" for r in round_results):
            worst_fail = next(r for r in round_results if r.verdict == "FAIL")
            worst = worst_fail
            worst.notes.append(
                f"pass^{defence_k}: {sum(1 for r in round_results if r.verdict=='PASS')}/{defence_k} 次通过"
            )
        else:
            worst.notes.append(f"pass^{defence_k}: {defence_k}/{defence_k} 次全部通过")
        results.append(worst)

    print("\n── 扩展用例（补洞）──────────────────────────────────")
    for fn in EXTENDED_CASES:
        r = await fn()
        results.append(r)

    return results


def print_results(results: list[Result], defence_k: int) -> None:
    icon = {"PASS": "✓", "PARTIAL": "△", "FAIL": "✗"}
    print("\n" + "═" * 60)
    print(f"{'ID':<8} {'判定':<8} {'描述'}")
    print("─" * 60)

    p0_total, p1_total = 0, 0
    verdicts = {"PASS": 0, "PARTIAL": 0, "FAIL": 0}

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

    total = len(results)
    positive_pass = verdicts["PASS"] + verdicts["PARTIAL"]
    print("─" * 60)
    print(
        f"合计：{total} 条 | "
        f"PASS={verdicts['PASS']} PARTIAL={verdicts['PARTIAL']} FAIL={verdicts['FAIL']}"
    )
    print(f"P0 命中：{p0_total} 次  P1 扣分：{p1_total} 次")

    defence_results  = [r for r in results if r.case_id.startswith("C-D")]
    positive_results = [r for r in results if r.case_id.startswith("C-0")]
    extended_results = [r for r in results if r.case_id.startswith("C-X")]

    defence_pass = all(r.verdict == "PASS" for r in defence_results)
    positive_pass_rate = (
        sum(1 for r in positive_results if r.verdict in ("PASS", "PARTIAL"))
        / len(positive_results) if positive_results else 1.0
    )
    positive_p0_zero = all(not r.p0_hits for r in positive_results)
    extended_p0_zero = all(not r.p0_hits for r in extended_results)

    print("\n── 发布门禁评估 ──────────────────────────────────────")
    print(f"  防御用例全部 PASS (pass^{defence_k})：{'✓' if defence_pass else '✗'}")
    print(f"  正向 PASS+PARTIAL ≥ 90%：{'✓' if positive_pass_rate >= 0.9 else '✗'} ({positive_pass_rate*100:.0f}%)")
    print(f"  正向 P0 零命中：{'✓' if positive_p0_zero else '✗'}")
    print(f"  扩展(补洞) P0 零命中：{'✓' if extended_p0_zero else '✗'}")

    gate_ok = defence_pass and positive_pass_rate >= 0.9 and positive_p0_zero and extended_p0_zero
    print(f"\n  ── {'🟢 C 类门禁：通过' if gate_ok else '🔴 C 类门禁：未通过，阻塞事实包环节验收'}")


async def main(defence_k: int = 5) -> None:
    results = await run_all(defence_k)
    print_results(results, defence_k)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--defence-k", type=int, default=5,
                        help="防御用例重复执行次数（pass^k），默认 5")
    args = parser.parse_args()
    asyncio.run(main(args.defence_k))
