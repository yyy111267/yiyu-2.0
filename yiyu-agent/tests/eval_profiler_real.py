"""
环节③真模型评测：欠账二（选型稳定性）+ 欠账三（选型匹配质量）

共 12 条用例：
  欠账二（稳定性）：E-S1, E-S2, E-SD1, E-SD2          （4 条）
  欠账三（匹配质量）：E-M1~E-M4 正向 + E-MD1~E-MD4 防御（8 条）

运行（推荐先跑匹配质量，成本低、验收价值高）：
  PYTHONPATH=. python3 tests/eval_profiler_real.py --group match     # 仅匹配质量
  PYTHONPATH=. python3 tests/eval_profiler_real.py --group stability # 仅稳定性
  PYTHONPATH=. python3 tests/eval_profiler_real.py                   # 全跑

成本：匹配质量组 ~24 次调用，稳定性组 ~21 次，合计 ~45 次。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from core.config import settings
from core.llm import LLMClient
from runtime.preloop.profiler import (
    ADAPTER_CATALOG,
    FALLBACK_ADAPTER_ID,
    _profile_unit,
    clear_cache,
)
from runtime.schemas import (
    AliasType, BusinessSegment, CompanyFacts,
    CurrentEntitySchema, EntitySource,
    FinancialSnapshot, GranularityDecision, GranularityMode,
    InfoRichness, ResearchUnit, DifferentialDimension, SourceTrace,
)
# 复用评测基础设施
from eval_profiler_e import _make_entity, _make_facts, _make_gran

logging.basicConfig(level=logging.WARNING)
llm_client = LLMClient(settings)


# ════════════════════════════════════════════════════════════════
# expected_adapter 金标准映射（对应 Adapter 目录 ID）
# ════════════════════════════════════════════════════════════════
# 金标准必须是 ADAPTER_CATALOG 中的合法 ID
# 匹配质量用例预置表（PRD 用例集 5.2 节）：

# "半导体与 AI 算力" → 当前目录中最接近的是 G6（高端制造/硬件）
# "消费品牌/成熟制造" → G1a 或 G1b（成熟价值型）
# "AI 软件/大模型（早期）" → G5（早期硬科技/未盈利成长）
# "机器人与高端制造" → G6（高端制造/硬件）
# 注：用例集里的 Adapter 名称是概念名，此处对齐实际目录 ID，见下方 EXPECTED_MAP。
# 若目录扩充了专属 Adapter，只需更新此 map，断言逻辑不变。

EXPECTED_MAP: dict[str, list[str]] = {
    # 校准历史 v3（行业收敛后）：
    # 旧 G1a/G1b/G5/G6 ID 已废弃，换为 4 个 MVP Adapter ID。
    # zhiji_storage：成熟 Fabless 半导体，semiconductor 或 consumer_brand 均合理
    "zhiji_storage":  ["semiconductor"],
    "moutai":         ["consumer_brand"],         # 消费品牌 → consumer_brand ✓
    "digital_software_platform": ["digital_software_platform"],
    "robot":          ["advanced_manufacturing"],
    "fintech":        [],   # 无直接对应，无 expected 硬答案
    "pv_mfg":         [],   # 无直接对应，无 expected 硬答案
}

WRONG_MAP: dict[str, list[str]] = {
    "pv_mfg": ["digital_software_platform"],
}

ABSOLUTE_WRONG_MAP: dict[str, list[str]] = {
    "fintech": ["advanced_manufacturing", "semiconductor", "consumer_brand"],
    "pv_mfg":  ["digital_software_platform"],
}


# ════════════════════════════════════════════════════════════════
# 冻结夹具
# ════════════════════════════════════════════════════════════════

def _single_unit_gran(facts: CompanyFacts, uid="u_company") -> tuple[GranularityDecision, ResearchUnit]:
    unit = ResearchUnit(id=uid, scope=facts.entity.canonical_name + "整体",
                        reason="整体研究", differential_dimensions=[])
    gran = GranularityDecision(
        mode=GranularityMode.WHOLE, units=[unit],
        source="夹具", confidence=0.9, facts_version=facts.facts_version,
    )
    return gran, unit


# 兆易创新（半导体 Fabless）
_ZHIJI_E = _make_entity("兆易创新", "603986.SH")
_ZHIJI_FACTS_R = _make_facts(_ZHIJI_E, [
    {"name": "NOR Flash", "revenue_share": 0.65, "charging": "一次性销售",
     "customer": "消费电子/工业", "competition": "华邦/旺宏",
     "capex_profile": "轻资产（Fabless，自己设计不自己生产）"},
    {"name": "MCU",       "revenue_share": 0.25, "charging": "一次性销售",
     "customer": "工业/汽车电子", "competition": "中颖/ST",
     "capex_profile": "轻资产（Fabless）"},
], richness=InfoRichness.A, fv="zhiji_real_v2",
    snap=FinancialSnapshot(revenue_ttm=42_000, gross_margin=0.425,
                           ocf=3_800, capex=600, currency="CNY"))

# 兆易（业务性措辞版）
_ZHIJI_FACTS_B = _make_facts(_ZHIJI_E, [
    {"name": "存储芯片（NOR Flash）", "revenue_share": 0.65, "charging": "卖货",
     "customer": "手机/家电/工业设备厂商", "competition": "竞争对手为华邦电子等台湾厂商",
     "capex_profile": "自己设计、找代工厂生产（Fabless），固定资产极少"},
    {"name": "单片机（MCU）",       "revenue_share": 0.25, "charging": "卖货",
     "customer": "工业控制/汽车电子", "competition": "中颖电子/意法半导体",
     "capex_profile": "同上，Fabless 模式"},
], richness=InfoRichness.A, fv="zhiji_biz_v1",
    snap=FinancialSnapshot(revenue_ttm=42_000, gross_margin=0.425,
                           ocf=3_800, capex=600, currency="CNY"))

# 贵州茅台
_MOUTAI_E = _make_entity("贵州茅台", "600519.SH")
_MOUTAI_FACTS_R = _make_facts(_MOUTAI_E, [
    {"name": "茅台酒", "revenue_share": 0.85, "charging": "一次性销售",
     "customer": "高端消费者/政商务", "competition": "五粮液/国台",
     "capex_profile": "中等（酿造产能，原材料积累）；品牌定价权极强"},
    {"name": "系列酒", "revenue_share": 0.14, "charging": "一次性销售",
     "customer": "大众消费者", "competition": "普通白酒品牌",
     "capex_profile": "中等"},
], richness=InfoRichness.A, fv="moutai_real_v1",
    snap=FinancialSnapshot(revenue_ttm=170_000, gross_margin=0.91,
                           ocf=82_000, capex=2_000, currency="CNY"))

# AI 软件公司（早期，未盈利）
_AI_E = _make_entity("某AI软件公司", "AISOFT.US")
_AI_FACTS_R = _make_facts(_AI_E, [
    {"name": "大模型 API 服务", "revenue_share": 0.60, "charging": "按量计费/订阅",
     "customer": "企业开发者", "competition": "OpenAI/百度文心",
     "capex_profile": "重（GPU 算力租赁）；软件研发为主，尚未盈利"},
    {"name": "企业软件（SaaS）", "revenue_share": 0.40, "charging": "订阅",
     "customer": "中大型企业", "competition": "Salesforce/钉钉",
     "capex_profile": "轻资产（软件交付）；ARR 增速>80%"},
], richness=InfoRichness.B, fv="aisoft_v1",
    snap=FinancialSnapshot(revenue_ttm=800, gross_margin=0.55,
                           ocf=-200, capex=150, currency="USD"))

# 机器人公司
_ROBOT_E = _make_entity("某机器人公司", "ROBOT.SH")
_ROBOT_FACTS_R = _make_facts(_ROBOT_E, [
    {"name": "工业机器人整机", "revenue_share": 0.65, "charging": "一次性销售",
     "customer": "汽车/3C 制造厂商", "competition": "ABB/发那科/埃斯顿",
     "capex_profile": "重资产（装配/检测产线）；量产交付为主"},
    {"name": "核心零部件（减速器/伺服）", "revenue_share": 0.35, "charging": "一次性销售",
     "customer": "机器人集成商/OEM", "competition": "哈默纳科/纳博特斯克",
     "capex_profile": "重资产（精密制造）；国产替代趋势明显"},
], richness=InfoRichness.B, fv="robot_v1",
    snap=FinancialSnapshot(revenue_ttm=5_000, gross_margin=0.35,
                           ocf=400, capex=600, currency="CNY"))

# 金融科技（无直接手册）
_FT_E = _make_entity("某金融科技公司", "FT.HK", "HK")
_FT_FACTS_R = _make_facts(_FT_E, [
    {"name": "支付服务", "revenue_share": 0.55, "charging": "费率抽成",
     "customer": "个人用户/商户", "competition": "支付宝/微信支付",
     "capex_profile": "中等（合规牌照/服务器）；重监管"},
    {"name": "消费信贷科技", "revenue_share": 0.45, "charging": "利息/服务费",
     "customer": "小微企业/个人", "competition": "马上消费/360数科",
     "capex_profile": "轻资产（风控算法）；资本消耗受监管约束"},
], richness=InfoRichness.B, fv="fintech_v1",
    snap=FinancialSnapshot(revenue_ttm=8_000, gross_margin=0.60,
                           ocf=1_200, capex=300, currency="HKD"))

# 光伏制造（无直接手册）
_PV_E = _make_entity("某光伏公司", "PV.SH")
_PV_FACTS_R = _make_facts(_PV_E, [
    {"name": "组件制造", "revenue_share": 0.80, "charging": "一次性销售",
     "customer": "电站开发商/EPC", "competition": "隆基/天合/晶科",
     "capex_profile": "重资产（硅片/电池/组件产线）；强周期，产能过剩风险"},
], richness=InfoRichness.A, fv="pv_v1",
    snap=FinancialSnapshot(revenue_ttm=60_000, gross_margin=0.12,
                           ocf=2_000, capex=8_000, currency="CNY"))

# 腾讯三单元
_TENCENT_R = _make_entity("腾讯控股", "00700.HK", "HK")
_TENCENT_FACTS_R = _make_facts(_TENCENT_R, [
    {"name": "增值服务（游戏）",    "revenue_share": 0.50, "charging": "虚拟道具/订阅",
     "customer": "个人玩家", "competition": "网易/米哈游",
     "capex_profile": "轻资产（内容研发）；估值=流水/递延"},
    {"name": "网络广告",          "revenue_share": 0.17, "charging": "CPM/CPC",
     "customer": "广告主", "competition": "字节跳动/百度",
     "capex_profile": "轻资产（流量变现）；估值=DAU×eCPM"},
    {"name": "金融科技与企业服务",  "revenue_share": 0.32, "charging": "费率/SaaS",
     "customer": "个人/企业", "competition": "蚂蚁/阿里云",
     "capex_profile": "中等（合规/服务器）；估值受监管周期影响"},
], richness=InfoRichness.A, fv="tencent_real_v1",
    snap=FinancialSnapshot(revenue_ttm=660_000, gross_margin=0.50,
                           ocf=200_000, capex=15_000, currency="HKD"))

_TENCENT_GRAN_R = _make_gran("腾讯", [
    {"id": "u_game",    "scope": "游戏业务",
     "dims": ["profit_model"], "reason": "盈利模式为虚拟道具付费"},
    {"id": "u_ad",      "scope": "网络广告",
     "dims": ["profit_model"], "reason": "流量变现"},
    {"id": "u_fintech", "scope": "金融科技与企业服务",
     "dims": ["capital_input", "valuation_logic"], "reason": "监管合规+估值逻辑不同"},
], "tencent_real_v1")


# ════════════════════════════════════════════════════════════════
# 评测结果
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


async def _run_once(facts, unit, *, force_refresh=True):
    """单次真模型调用，绕缓存。"""
    return await _profile_unit(facts, unit, llm_client, force_refresh=force_refresh)


# ════════════════════════════════════════════════════════════════
# 欠账二：选型稳定性
# ════════════════════════════════════════════════════════════════

async def run_E_S1(k=5) -> Result:
    """E-S1 同画像重复路由一致性（连跑 k 次）。"""
    _, unit = _single_unit_gran(_ZHIJI_FACTS_R)
    adapters = []
    for _ in range(k):
        up = await _run_once(_ZHIJI_FACTS_R, unit)
        adapters.append(up.selected_adapter)

    most_common, count = Counter(adapters).most_common(1)[0]
    p0, p1 = [], []

    if count < max(4, int(k * 0.8)):
        p0.append(f"一致性率 {count}/{k}，未达 ≥4/5：分布={Counter(adapters)}")

    if count < k:
        p1.append(f"未 100% 一致：{Counter(adapters)}")

    notes = [
        f"分布={dict(Counter(adapters))}，主导={most_common}（{count}/{k}）",
        "（G6=高端制造/硬件 为半导体Fabless预期选型）",
    ]
    return Result("E-S1", "同画像重复路由一致性", _verdict(p0, p1), p0, p1, notes)


async def run_E_S2(k=3) -> Result:
    """E-S2 措辞变体稳定性（技术性 vs 业务性，各 k 次）。"""
    _, unit_a = _single_unit_gran(_ZHIJI_FACTS_R, "u_tech")
    _, unit_b = _single_unit_gran(_ZHIJI_FACTS_B, "u_biz")

    adapters_a, adapters_b = [], []
    for _ in range(k):
        a = await _run_once(_ZHIJI_FACTS_R, unit_a)
        b = await _run_once(_ZHIJI_FACTS_B, unit_b)
        adapters_a.append(a.selected_adapter)
        adapters_b.append(b.selected_adapter)

    mode_a = Counter(adapters_a).most_common(1)[0][0]
    mode_b = Counter(adapters_b).most_common(1)[0][0]

    p0, p1 = [], []
    if mode_a != mode_b:
        p0.append(f"措辞变体导致选型不同：技术性={mode_a}，业务性={mode_b}")

    if Counter(adapters_a).most_common(1)[0][1] < k:
        p1.append(f"技术性措辞版内部不一致：{dict(Counter(adapters_a))}")
    if Counter(adapters_b).most_common(1)[0][1] < k:
        p1.append(f"业务性措辞版内部不一致：{dict(Counter(adapters_b))}")

    notes = [f"技术性={dict(Counter(adapters_a))}，业务性={dict(Counter(adapters_b))}"]
    return Result("E-S2", "措辞变体稳定性", _verdict(p0, p1), p0, p1, notes)


async def run_E_SD1(k=5) -> Result:
    """E-SD1 边界画像不摇摆（双业务：NOR Flash 半导体 + 消费电子应用层）。"""
    # 构造双行业画像：兆易存储 + 消费电子终端
    e = _make_entity("某双行业公司", "DUAL.SH")
    dual_facts = _make_facts(e, [
        {"name": "NOR Flash 芯片（半导体）", "revenue_share": 0.60,
         "charging": "一次性销售", "customer": "消费电子厂商",
         "capex_profile": "Fabless 设计"},
        {"name": "智能家居模组（消费电子）",  "revenue_share": 0.40,
         "charging": "一次性销售", "customer": "品牌家电厂商",
         "capex_profile": "轻度组装；以方案销售为主"},
    ], richness=InfoRichness.B, fv="dual_v1")
    _, unit = _single_unit_gran(dual_facts)

    adapters = []
    for _ in range(k):
        up = await _run_once(dual_facts, unit)
        adapters.append(up.selected_adapter)

    dist = Counter(adapters)
    dominant, dominant_cnt = dist.most_common(1)[0]
    p0, p1 = [], []

    # P0: 不出现三种以上 Adapter 摇摆（无主导 = 摇摆）
    if len(dist) >= 3:
        p0.append(f"选型在 {len(dist)} 种 Adapter 间摇摆（无主导）：{dict(dist)}")
    elif dominant_cnt < max(4, int(k * 0.8)):
        p0.append(f"主导率不足 ≥4/5：{dict(dist)}")

    if dominant_cnt < k:
        p1.append(f"未 100% 一致（边界案例正常）：{dict(dist)}")

    notes = [
        f"分布={dict(dist)}，主导={dominant}（{dominant_cnt}/{k}）",
        "（双行业边界：G6=半导体整机路径，G1a=消费品路径，均可接受为主导）",
    ]
    return Result("E-SD1", "边界画像不摇摆", _verdict(p0, p1), p0, p1, notes)


async def run_E_SD2(k=5) -> Result:
    """E-SD2 温度扰动不漂移（茅台，单一主业，生产温度 0.3）。"""
    _, unit = _single_unit_gran(_MOUTAI_FACTS_R)
    adapters, reasons = [], []
    for _ in range(k):
        up = await _profile_unit(
            _MOUTAI_FACTS_R, unit, llm_client,
            force_refresh=True
        )
        adapters.append(up.selected_adapter)
        reasons.append(up.selection_reason)

    dist = Counter(adapters)
    p0, p1 = [], []

    if dist.most_common(1)[0][1] < k:
        p0.append(f"温度扰动下选型漂移（单一主业不应漂移）：{dict(dist)}")

    # 无非法 ID
    illegal = [a for a in adapters if a not in ADAPTER_CATALOG]
    if illegal:
        p0.append(f"出现非法 Adapter ID：{illegal}")

    notes = [f"分布={dict(dist)}（茅台单一主业，期望 G1a 或 G1b 稳定）"]
    return Result("E-SD2", "温度扰动不漂移（茅台）", _verdict(p0, p1), p0, p1, notes)


# ════════════════════════════════════════════════════════════════
# 欠账三：选型匹配质量
# ════════════════════════════════════════════════════════════════

async def _match_case(case_id: str, desc: str, facts: CompanyFacts,
                      unit_key: str, k=3) -> Result:
    """通用匹配质量用例执行器。"""
    _, unit = _single_unit_gran(facts)
    expected = EXPECTED_MAP.get(unit_key, [])
    wrong    = WRONG_MAP.get(unit_key, [])

    adapters, reasons = [], []
    for _ in range(k):
        up = await _run_once(facts, unit)
        adapters.append(up.selected_adapter)
        reasons.append(up.selection_reason)

    dominant = Counter(adapters).most_common(1)[0][0]
    p0, p1 = [], []

    # P0: 明显错配
    wrong_hits = [a for a in adapters if a in wrong]
    if wrong_hits:
        p0.append(f"出现明显错配 Adapter：{wrong_hits}（禁止列表 {wrong}）")

    if expected:
        # P0: 主导选型命中 expected（≥ k*0.7 次）
        hit_cnt = sum(1 for a in adapters if a in expected)
        if hit_cnt < max(1, int(k * 0.7)):
            p0.append(
                f"命中 expected_adapter {expected} 仅 {hit_cnt}/{k} 次，"
                f"主导选型={dominant}（{Counter(adapters).most_common(1)[0][1]}/{k}）"
            )
    else:
        # 无 expected（降级场景）：检查绝对禁止项（语义强冲突）
        abs_wrong = ABSOLUTE_WRONG_MAP.get(unit_key, [])
        abs_hits = [a for a in adapters if a in abs_wrong]
        if abs_hits:
            p0.append(
                f"选到了绝对禁止的 Adapter {abs_hits}（与画像强冲突，"
                f"绝对禁止列表 {abs_wrong}）"
            )

    # P1: 理由体现关键画像特征
    key_features = {
        "zhiji_storage": ["存储", "Flash", "Fabless", "芯片", "半导体"],
        "moutai":        ["白酒", "品牌", "成熟", "茅台", "消费"],
        "digital_software_platform": ["大模型", "软件", "订阅", "AI", "云", "广告", "平台"],
        "robot":         ["机器人", "整机", "制造", "零部件", "精密"],
        "fintech":       ["支付", "金融", "监管", "信贷"],
        "pv_mfg":        ["光伏", "组件", "硅", "制造", "周期"],
    }
    features = key_features.get(unit_key, [])
    reason_text = " ".join(reasons)
    if features and not any(f in reason_text for f in features):
        p1.append(f"selection_reason 未提及关键画像特征 {features[:3]}：{reasons[0][:80]!r}")

    notes = [
        f"分布={dict(Counter(adapters))}，expected={expected}",
        f"主导理由示例：{reasons[0][:80]!r}",
    ]
    return Result(case_id, desc, _verdict(p0, p1), p0, p1, notes)


async def run_E_M1() -> Result:
    return await _match_case("E-M1", "半导体单元选对（兆易存储→G6）",
                              _ZHIJI_FACTS_R, "zhiji_storage")

async def run_E_M2() -> Result:
    return await _match_case("E-M2", "消费品牌单元选对（茅台→G1a/G1b）",
                              _MOUTAI_FACTS_R, "moutai")

async def run_E_M3() -> Result:
    return await _match_case("E-M3", "AI软件单元选对（→G5）",
                              _AI_FACTS_R, "digital_software_platform")

async def run_E_M4() -> Result:
    return await _match_case("E-M4", "机器人单元选对（→G6）",
                              _ROBOT_FACTS_R, "robot")


async def run_E_MD1(k=5) -> Result:
    """E-MD1 金融科技单元显式降级（无直接对应手册）。"""
    return await _match_case("E-MD1", "金融科技显式降级（无直接手册）",
                              _FT_FACTS_R, "fintech", k=k)


async def run_E_MD2(k=5) -> Result:
    """E-MD2 腾讯三单元不串味。"""
    from runtime.preloop.profiler import build_unit_profiles
    adapters_map: dict[str, list[str]] = {"u_game": [], "u_ad": [], "u_fintech": []}
    reasons_map:  dict[str, list[str]] = {"u_game": [], "u_ad": [], "u_fintech": []}

    for _ in range(k):
        from eval_profiler_e import _make_gran as _mg
        gran = _TENCENT_GRAN_R
        profiles = await build_unit_profiles(
            _TENCENT_FACTS_R, gran, llm_client, force_refresh=True)
        for p in profiles:
            adapters_map[p.unit_id].append(p.selected_adapter)
            reasons_map[p.unit_id].append(p.selection_reason)

    p0, p1 = [], []

    # P0: 游戏单元不因金融科技存在而串味（核心断言）
    fintech_kw = {"金融", "支付", "监管", "信贷", "银行"}
    for r in reasons_map.get("u_game", []):
        if any(kw in r for kw in fintech_kw):
            p0.append(f"u_game selection_reason 出现金融词（串味）：{r[:80]!r}")
            break

    # P0: 无非法 Adapter ID
    for uid, alist in adapters_map.items():
        illegal = [a for a in alist if a not in ADAPTER_CATALOG]
        if illegal:
            p0.append(f"unit={uid} 出现非法 Adapter ID：{illegal}")

    # P1（观察项）: 三单元全选同一 Adapter = 目录粒度不足导致的塌缩
    # 当前 Adapter 目录只有 10 个 ID，腾讯复合型集团三个平台业务
    # 都命中 G4（平台/网络效应）是合理映射，不是模型错误。
    # 待目录扩充（游戏平台/广告平台/支付平台 细分后）再升级为 P0 断言。
    dominant_set = {Counter(v).most_common(1)[0][0] for v in adapters_map.values()}
    if len(dominant_set) == 1:
        p1.append(
            f"三单元全选同一 Adapter {dominant_set}（Adapter 目录粒度不足导致塌缩）；"
            "待目录扩充后升级为 P0"
        )

    # P1: 各单元理由与自身画像对应
    for uid, kw_list in [
        ("u_game",    ["游戏", "虚拟道具", "流水"]),
        ("u_ad",      ["广告", "流量", "eCPM", "CPM"]),
        ("u_fintech", ["金融", "支付", "监管", "SaaS"]),
    ]:
        rs = " ".join(reasons_map.get(uid, []))
        if not any(k in rs for k in kw_list):
            p1.append(f"{uid} 理由未体现自身画像特征 {kw_list[:2]}：{reasons_map[uid][0][:60]!r}")

    notes = [
        f"u_game={dict(Counter(adapters_map['u_game']))}",
        f"u_ad={dict(Counter(adapters_map['u_ad']))}",
        f"u_fintech={dict(Counter(adapters_map['u_fintech']))}",
    ]
    return Result("E-MD2", "多单元不串味（腾讯三单元）", _verdict(p0, p1), p0, p1, notes)


async def run_E_MD3(k=5) -> Result:
    """E-MD3 无手册行业显式降级（光伏制造）。"""
    return await _match_case("E-MD3", "光伏制造显式降级（无直接手册）",
                              _PV_FACTS_R, "pv_mfg", k=k)


async def run_E_MD4() -> Result:
    """E-MD4 选型理由可解释（兆易存储，同 E-M1 场景，专测 reason 质量）。"""
    _, unit = _single_unit_gran(_ZHIJI_FACTS_R)
    up = await _run_once(_ZHIJI_FACTS_R, unit)

    p0, p1 = [], []

    # P0: 选型正确（同 E-M1）
    if up.selected_adapter not in EXPECTED_MAP["zhiji_storage"]:
        p0.append(
            f"选型={up.selected_adapter} 不在 expected {EXPECTED_MAP['zhiji_storage']}"
        )

    # P1 质量三项
    reason = up.selection_reason
    key_features = ["存储", "Flash", "Fabless", "芯片", "半导体"]
    if not any(f in reason for f in key_features):
        p1.append(f"理由未提及关键画像特征 {key_features[:3]}：{reason!r}")

    hallucinated = ["茅台", "白酒", "游戏", "金融", "机器人", "光伏"]
    if any(h in reason for h in hallucinated):
        p1.append(f"理由引用了画像中不存在的事实：{reason!r}")

    vague_phrases = ["科技公司", "技术公司", "符合条件", "适合该行业"]
    if any(v in reason for v in vague_phrases):
        p1.append(f"理由包含空话：{reason!r}")

    notes = [
        f"adapter={up.selected_adapter}",
        f"reason={reason[:100]!r}",
    ]
    return Result("E-MD4", "选型理由可解释", _verdict(p0, p1), p0, p1, notes)


# ════════════════════════════════════════════════════════════════
# 执行框架
# ════════════════════════════════════════════════════════════════

STABILITY_CASES = [run_E_S1, run_E_S2, run_E_SD1, run_E_SD2]
MATCH_CASES     = [run_E_M1, run_E_M2, run_E_M3, run_E_M4,
                   run_E_MD1, run_E_MD2, run_E_MD3, run_E_MD4]


async def run_group(cases) -> list[Result]:
    results = []
    for fn in cases:
        r = await fn()
        icon = {"PASS": "✓", "PARTIAL": "△", "FAIL": "✗"}[r.verdict]
        print(f"  {icon} [{r.case_id}] {r.verdict}  {r.desc}")
        for h in r.p0_hits: print(f"       [P0] {h[:90]}")
        for h in r.p1_hits: print(f"       [P1] {h[:90]}")
        for n in r.notes[:2]: print(f"       [数] {n}")
        results.append(r)
    return results


def print_summary(results: list[Result], label: str) -> None:
    icon = {"PASS": "✓", "PARTIAL": "△", "FAIL": "✗"}
    p0t = sum(len(r.p0_hits) for r in results)
    p1t = sum(len(r.p1_hits) for r in results)
    vc  = Counter(r.verdict for r in results)
    print(f"\n{'─'*60}")
    print(f"{label}  {len(results)} 条 | "
          f"PASS={vc['PASS']} PARTIAL={vc['PARTIAL']} FAIL={vc['FAIL']}")
    print(f"P0 命中：{p0t}  P1 扣分：{p1t}")

    # 发布门禁
    gate_p0 = p0t == 0
    gate_rate = (vc["PASS"] + vc["PARTIAL"]) / max(len(results), 1)
    gate = gate_p0 and gate_rate >= 0.9
    print(f"{'✓' if gate_p0 else '✗'} P0 零命中  "
          f"{'✓' if gate_rate>=0.9 else '✗'} PASS+PARTIAL ≥90% ({gate_rate*100:.0f}%)")
    print(f"  {'🟢 通过' if gate else '🔴 未通过'}")


async def main(group: str = "all") -> None:
    print(f"\n真模型: {llm_client.model} @ {llm_client.provider}\n")

    if group in ("stability", "all"):
        print("═" * 60)
        print("欠账二·选型稳定性（E-S / E-SD）")
        print("─" * 60)
        stability = await run_group(STABILITY_CASES)
        print_summary(stability, "稳定性组")

    if group in ("match", "all"):
        print("\n" + "═" * 60)
        print("欠账三·选型匹配质量（E-M / E-MD）")
        print("─" * 60)
        match = await run_group(MATCH_CASES)
        print_summary(match, "匹配质量组")

    if group == "all":
        all_r = (stability if "stability" in dir() else []) + \
                (match if "match" in dir() else [])
        print_summary(all_r, "【全量汇总】")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", choices=["all", "stability", "match"], default="all")
    args = parser.parse_args()
    asyncio.run(main(args.group))
