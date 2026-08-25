"""
base_pack 执行器 —— 商业模式分类与定性研究之间的"传送带"。

定位：classify（商业模式分类）与 checklist/masters（定性研究）之间的缺口。
  classify 决定 group → 本模块按 group 算地基指标 → 六关/四大师基于算好的档位说话。

链路：
  symbol → (可选)classify_company 拿 group → market.bundle 取数
  → 字段映射 → 加载 bus_router/G{group}/base_pack.yaml + core.yaml + G{group}_metrics.yaml
  → 按 formula + inputs 调 formulas_core/formulas_{group} 冻结函数
  → 按 bands 判档位 → 输出"档位+数值+三态"prompt block

铁律（与 formulas_core 对齐）：
  · 指标只由冻结函数算，LLM 不心算；
  · 三态严格区分：OK / NOT_APPLICABLE / NOT_COMPUTABLE / DEGRADED；
  · 字段缺失显式标 NC，绝不静默返回 0/None。

作为工具 calc.base_pack 暴露给 LLM，在 company.classify 之后、六关分析之前调用。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from toolkit.base import ReadOnlyTool, ToolSchema

logger = logging.getLogger(__name__)

# ── 路径约定 ────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]                     # yiyu-agent/
_BUS_ROUTER_DIR = _ROOT / "bus_router"
# data_pack 桥：与 calc.run_code 共用同一数据包（run_code 沙箱的唯一输入源）
_DATA_PACK_PATH = Path(os.environ.get("YIYU_DATA_PACK") or (_ROOT / "var" / "data_pack.json"))


# ── 惰性依赖加载（避免循环 import + 测试可注入）──────────────────

_market_data_instance: Any = None


def _get_market_data():
    global _market_data_instance
    if _market_data_instance is None:
        from core.config import settings
        from toolkit.market.market import MarketData

        _market_data_instance = MarketData(settings)
    return _market_data_instance


async def _classify(symbol: str) -> dict:
    """调 classify_company 拿 group（有 90 天缓存，零成本优先）。失败降级 other。"""
    try:
        from toolkit.entity.classify import classify_company

        result = await classify_company(symbol)
        return result.to_dict()
    except Exception as e:  # noqa: BLE001 - 分类失败不阻塞，降级为 other
        logger.warning("base_pack: classify 失败（降级 other）: %s", e)
        return {"symbol": symbol, "group": "other", "stage": "unknown",
                "confidence": 0.0, "by": "error", "needs_review": True,
                "is_conglomerate": False, "sotp_tier": 0,
                "reasoning": f"分类失败：{e}"}


# ── 公式库加载：把 bus_router 加到 sys.path，import formulas_core + formulas_<group> ──

_registry_cache: dict[str, dict] = {}   # group → REGISTRY（已合并 core+group）


def _load_formula_registry(group: str) -> dict:
    """加载 formulas_core + formulas_<group>，返回合并后的 REGISTRY。

    组模块顶部 `from formulas_core import *` 会触发 core 公式注册；
    组特有 @formula 再叠加进同一个 REGISTRY 对象。
    缺组模块时降级为仅 core。
    """
    if group in _registry_cache:
        return _registry_cache[group]

    added = False
    if str(_BUS_ROUTER_DIR) not in sys.path:
        sys.path.insert(0, str(_BUS_ROUTER_DIR))
        added = True
    try:
        import formulas_core  # noqa: F401  触发 @formula 注册

        registry = formulas_core.REGISTRY
        mod_name = f"formulas_{group}"
        mod_path = _BUS_ROUTER_DIR / f"{mod_name}.py"
        if mod_path.exists():
            importlib.import_module(mod_name)  # 触发组特有 @formula 注册
        _registry_cache[group] = registry
        return registry
    except Exception as e:  # noqa: BLE001
        logger.warning("base_pack: 公式库加载失败（group=%s）: %s", group, e)
        # 降级：尝试仅 core
        try:
            import formulas_core  # noqa: F401

            _registry_cache[group] = formulas_core.REGISTRY
            return formulas_core.REGISTRY
        except Exception:
            _registry_cache[group] = {}
            return {}
    finally:
        if added:
            try:
                sys.path.remove(str(_BUS_ROUTER_DIR))
            except ValueError:
                pass


# ── 指标定义加载 ────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("base_pack: yaml 读取失败 %s: %s", path, e)
        return {}


# core 指标定义 fallback：core.yaml 头部是给 LLM 看的裸文本（非合法 YAML，
# classify.py 也是靠 try/except 跳过它），metrics 段解析不出来时用这份内置定义。
# 内容与 core.yaml 的 metrics 段一致（formula/inputs/bands/direction）。
_CORE_METRIC_DEFS: dict[str, dict] = {
    "roic": {"id": "roic", "name": "投入资本回报率", "formula": "roic",
             "inputs": ["ebit", "tax_rate", "total_debt", "total_equity", "cash"],
             "unit": "pct", "direction": "higher_better",
             "bands": [
                 {"max": 8, "label": "毁灭价值", "severity": "high"},
                 {"min": 8, "max": 15, "label": "一般"},
                 {"min": 15, "max": 25, "label": "良好"},
                 {"min": 25, "label": "优秀"},
             ],
             "why": "价值投资第一指标——赚的钱能否覆盖资本成本"},
    "roic_vs_wacc": {"id": "roic_vs_wacc", "name": "ROIC减WACC(经济利润差)",
                     "formula": "spread", "inputs": ["roic", "wacc"],
                     "unit": "pct", "direction": "higher_better",
                     "why": "正值才是真正创造价值；持续为正才有护城河"},
    "incremental_roic": {"id": "incremental_roic", "name": "增量ROIC",
                         "formula": "incremental_roic",
                         "inputs": ["ebit_change", "invested_capital_change"],
                         "unit": "pct", "direction": "higher_better",
                         "why": "新投入的每一块钱回报如何——比存量ROIC更看未来"},
    "ocf_to_net_income": {"id": "ocf_to_net_income", "name": "经营现金流/净利润",
                          "formula": "ratio",
                          "inputs": ["operating_cash_flow", "net_profit"],
                          "unit": "x", "direction": "higher_better",
                          "bands": [
                              {"max": 0.7, "label": "利润含水", "severity": "high"},
                              {"min": 0.7, "max": 1.0, "label": "偏弱", "severity": "mid"},
                              {"min": 1.0, "label": "健康"},
                          ],
                          "why": "长期<1说明利润没有现金支撑"},
    "fcf_to_net_income": {"id": "fcf_to_net_income", "name": "自由现金流/净利润",
                          "formula": "fcf_conversion",
                          "inputs": ["operating_cash_flow", "capital_expenditure",
                                     "net_profit"],
                          "unit": "x", "direction": "higher_better",
                          "why": "扣掉维持性投资后，利润能沉淀多少真金白银"},
    "accruals_ratio": {"id": "accruals_ratio", "name": "应计项占比",
                       "formula": "accruals",
                       "inputs": ["net_profit", "operating_cash_flow", "total_assets"],
                       "unit": "pct", "direction": "lower_better",
                       "why": "应计项越高，盈余质量越差"},
    "owner_earnings": {"id": "owner_earnings", "name": "所有者收益",
                       "formula": "owner_earnings",
                       "inputs": ["net_profit", "depreciation_amortization",
                                  "maintenance_capex", "working_capital_change"],
                       "unit": "currency", "direction": "higher_better",
                       "why": "巴菲特口径——真正可分配给股东的钱"},
    "diluted_shares_cagr": {"id": "diluted_shares_cagr", "name": "稀释股本复合增速",
                            "formula": "cagr",
                            "inputs": ["weighted_average_shares_diluted@series"],
                            "unit": "pct", "direction": "lower_better",
                            "bands": [
                                {"max": 0, "label": "净回购(股东友好)"},
                                {"min": 0, "max": 2, "label": "轻微摊薄"},
                                {"min": 2, "label": "显著摊薄", "severity": "mid"},
                            ],
                            "why": "摊薄吃掉增长"},
    "sbc_to_revenue": {"id": "sbc_to_revenue", "name": "股权激励占收入比",
                       "formula": "ratio",
                       "inputs": ["stock_based_compensation", "revenue"],
                       "unit": "pct", "direction": "lower_better",
                       "why": "SBC是隐形成本"},
    "net_debt_to_ebitda": {"id": "net_debt_to_ebitda", "name": "净债/EBITDA",
                           "formula": "net_debt_to_ebitda",
                           "inputs": ["total_debt", "cash", "ebitda"],
                           "unit": "x", "direction": "lower_better",
                           "bands": [
                               {"max": 1, "label": "稳健"},
                               {"min": 1, "max": 3, "label": "正常"},
                               {"min": 3, "max": 4, "label": "偏高", "severity": "mid"},
                               {"min": 4, "label": "高风险", "severity": "high"},
                           ],
                           "why": "还债需要几年"},
    "interest_coverage": {"id": "interest_coverage", "name": "利息保障倍数",
                          "formula": "interest_coverage",
                          "inputs": ["ebit", "interest_expense"],
                          "unit": "x", "direction": "higher_better",
                          "bands": [
                              {"max": 2, "label": "危险", "severity": "high"},
                              {"min": 2, "max": 5, "label": "一般"},
                              {"min": 5, "label": "安全"},
                          ],
                          "why": "赚的钱够不够付利息"},
    "capex_intensity": {"id": "capex_intensity", "name": "资本开支强度",
                        "formula": "ratio",
                        "inputs": ["capital_expenditure", "revenue"],
                        "unit": "pct", "direction": "neutral",
                        "why": "方向由行业决定，底座不预设"},
    "roic_volatility_10y": {"id": "roic_volatility_10y", "name": "ROIC十年波动率",
                            "formula": "stdev",
                            "inputs": ["roic@series_10y"],
                            "unit": "pct", "direction": "lower_better",
                            "bands": [
                                {"max": 3, "label": "高度可预测"},
                                {"min": 3, "max": 8, "label": "中等"},
                                {"min": 8, "label": "不可预测", "severity": "high"},
                            ],
                            "why": "波动过大→无法定义安全边际"},
    "revenue_volatility_10y": {"id": "revenue_volatility_10y", "name": "收入十年变异系数",
                               "formula": "coefficient_of_variation",
                               "inputs": ["revenue@series_10y"],
                               "unit": "pct", "direction": "lower_better",
                               "why": "收入越稳，未来现金流越可估"},
    # ── step7 估值输入（依赖 LLM 提供假设，字段齐才可算，缺则 NC）──
    "normalized_pe": {"id": "normalized_pe", "name": "正常化PE",
                      "formula": "normalized_pe",
                      "inputs": ["current_price", "normalized_earnings_ps"],
                      "unit": "x", "direction": "lower_better",
                      "why": "跨周期均值，剔周期扰动"},
    "pe_historical_percentile": {"id": "pe_historical_percentile",
                                 "name": "当前PE历史分位",
                                 "formula": "pe_percentile",
                                 "inputs": ["pe@series", "pe"],
                                 "unit": "pct", "direction": "lower_better",
                                 "why": "安全边际锚"},
    "reverse_dcf": {"id": "reverse_dcf", "name": "反推DCF",
                    "formula": "reverse_dcf",
                    "inputs": ["current_price", "operating_cash_flow@series",
                               "weighted_average_shares_diluted", "discount_rate"],
                    "unit": "pct", "direction": "neutral",
                    "why": "当前价隐含多少永续增速"},
}


def _load_metric_defs(group: str) -> dict[str, dict]:
    """合并 core + G{group} 的 metrics 段 → {id: metric_def}。

    core 定义：先试 core.yaml 的 metrics 段（成功则覆盖内置 fallback）；
    失败用 _CORE_METRIC_DEFS（core.yaml 头部是裸文本，非合法 YAML，预期会失败）。
    组定义：bus_router/G{group}/G{group}_metrics.yaml（目录化）或 G{group}.yaml（平铺）。
    """
    defs: dict[str, dict] = dict(_CORE_METRIC_DEFS)

    core_yaml = _BUS_ROUTER_DIR / "core.yaml"
    core_metrics = _load_yaml(core_yaml).get("metrics", []) or []
    for m in core_metrics:
        mid = m.get("id")
        if mid:
            defs[mid] = m

    # 组特有指标（三种布局都试：目录内 _metrics / 目录内同名 / 平铺同名）
    for p in (
        _BUS_ROUTER_DIR / group / f"{group}_metrics.yaml",
        _BUS_ROUTER_DIR / group / f"{group}.yaml",
        _BUS_ROUTER_DIR / f"{group}.yaml",
    ):
        if p.exists():
            for m in _load_yaml(p).get("metrics", []) or []:
                mid = m.get("id")
                if mid:
                    defs[mid] = m  # 组定义覆盖同名 core 定义（override 语义）
            break
    return defs


def _load_pack(group: str) -> dict:
    """加载 base_pack.yaml，返回其内容。缺则返回空（仅算 core）。"""
    for p in (
        _BUS_ROUTER_DIR / group / "base_pack.yaml",
        _BUS_ROUTER_DIR / f"{group}_base_pack.yaml",
    ):
        if p.exists():
            return _load_yaml(p)
    return {}


# ── 字段映射：market.bundle → base_pack 字段字典 ────────────────
# market.fundamentals.years 的键 → base_pack required_raw_fields
# 不全的字段留缺（冻结函数返回 NC，诚实标注数据缺口）

# years 键 → base_pack 字段名
# westock 已扩展提取 zcfz/lrb/xjll/sum 明细字段，这里全部映射进 base_pack 字段字典。
# 缺失字段自然不在 dict 里（_resolve_input 返回 None → 冻结函数 NC，诚实标注）。
_FIELD_ALIASES: dict[str, str] = {
    "revenue": "revenue",
    "net_profit": "net_profit",
    "gross_margin": "gross_margin",
    "roe": "roe",
    "ocf": "operating_cash_flow",       # years 用 ocf，base_pack 用 operating_cash_flow
    "capex": "capital_expenditure",     # years 用 capex，base_pack 用 capital_expenditure
    "debt_ratio": "debt_ratio",
    # ── westock 明细字段（zcfz/lrb/xjll/sum）──
    "ebit": "ebit",
    "cash": "cash",
    "contract_liability": "contract_liability",
    "inventory": "inventory",
    "total_debt": "total_debt",
    "fixed_assets": "fixed_assets",
    "current_liabilities": "current_liabilities",
    "accounts_receivable": "accounts_receivable",
    "accounts_payable": "accounts_payable",
    "cogs": "cogs",
    "selling_expense": "selling_expense",
    "rd_expense": "rd_expense",
    "interest_expense": "interest_expense",
    "assets": "total_assets",           # westock years 键是 assets → base_pack 用 total_assets
    "equity": "total_equity",           # westock years 键是 equity → base_pack 用 total_equity
    "ebitda": "ebitda",
    "fcff": "fcff",
}


def _map_fields(bundle: Any) -> dict[str, Any]:
    """把 MarketBundle 的 fundamentals.years 映射成 base_pack 字段字典。

    返回 {field_name: value} —— 单值取最新年，序列取全部年（升序）。
    缺失字段不在字典里（_resolve_input 会返回 None → NC）。
    """
    fields: dict[str, Any] = {}
    fund = getattr(bundle, "fundamentals", None)
    if not fund or not getattr(fund, "years", None):
        return fields

    # years 按年降序（最新在前），转成升序便于取序列
    years = sorted(fund.years, key=lambda y: str(y.get("year", "")))
    for src_key, dst_key in _FIELD_ALIASES.items():
        vals = [y.get(src_key) for y in years if y.get(src_key) is not None]
        if not vals:
            continue
        # 单值 = 最新；同时保留序列供 @series 取用
        fields[dst_key] = vals[-1]
        fields[f"{dst_key}__series"] = vals  # 内部序列缓存

    # ── 推导字段（数据源没直接给，但可由已有字段算）──
    # tax_rate：有效税率 = (利润总额 − 归母净利) / 利润总额（含少数股东与税）
    tax_series: list[float] = []
    for y in years:
        tp = y.get("total_profit")
        np_ = y.get("net_profit")
        if tp and np_ is not None and tp > 0:
            tax_series.append((tp - np_) / tp)
    if tax_series:
        fields["tax_rate"] = tax_series[-1]
        fields["tax_rate__series"] = tax_series

    # ebitda ≈ ebit + 折旧摊销（折旧缺失时退化为 ebit）
    ebit_series = [y.get("ebit") for y in years if y.get("ebit") is not None]
    if ebit_series:
        fields["ebitda"] = ebit_series[-1]
        fields["ebitda__series"] = ebit_series

    # ── 近似推导（数据源缺字段时用相邻字段近似）──
    # 铁律：近似必须显式登记进 _approx_fields，_compute_pack 组装结果时
    # 会把依赖这些字段的指标从 OK 降级为 DEGRADED，绝不静默当精确值。
    approx: dict[str, str] = {}
    # ebit ≈ 营业利润 + 财务费用（A股财报 EBIT 常见近似）；
    # 无营业利润时退化「利润总额 + 财务费用」。
    if "ebit" not in fields:
        ebit_parts: list[float | None] = []
        for y in years:
            op, ie, tp = (y.get("operating_profit"), y.get("interest_expense"),
                          y.get("total_profit"))
            # 财务费用为负（净利息收入）时视为无利息支出，避免 EBIT 被调小失真
            ie = max(ie, 0.0) if ie is not None else None
            if op is not None and ie is not None:
                ebit_parts.append(op + ie)
            elif tp is not None and ie is not None:
                ebit_parts.append(tp + ie)
            else:
                ebit_parts.append(None)
        ebit_parts = [v for v in ebit_parts if v is not None]
        if ebit_parts:
            fields["ebit"] = ebit_parts[-1]
            fields["ebit__series"] = ebit_parts
            approx["ebit"] = (
                "EBIT 未直接披露，用「营业利润+财务费用」"
                "（无营业利润则「利润总额+财务费用」）近似")
    # ebitda 缺折旧摊销时退化为 ebit
    if "ebitda" not in fields and "ebit" in fields:
        fields["ebitda"] = fields["ebit"]
        fields["ebitda__series"] = fields.get("ebit__series", [fields["ebit"]])
        approx["ebitda"] = "EBITDA 未直接披露且无折旧摊销，以 EBIT 退化近似"
    fields["_approx_fields"] = approx

    # 快照里的字段
    snap = getattr(bundle, "snapshot", None)
    if snap:
        if snap.pe is not None:
            fields["pe"] = snap.pe
        if snap.pb is not None:
            fields["pb"] = snap.pb
        if snap.market_cap is not None:
            fields["market_cap"] = snap.market_cap
        if snap.price is not None:
            fields["current_price"] = snap.price
    return fields


# ── input 引用解析 ──────────────────────────────────────────

def _resolve_input(ref: str, fields: dict, computed: dict[str, Any]) -> Any:
    """解析指标 input 引用，支持三种来源 + 后缀：

    1. 其他已算指标 id（如 roic_vs_wacc 引用 roic）→ 取 .value
    2. 原始字段名（如 ebit）→ 取最新值
    3. 后缀：
       @series / @series_10y → 序列（升序）
       @latest → 最新值（默认）
       @yoy → 上年同期（序列倒数第二）
    """
    if not ref:
        return None
    name, _, suffix = ref.partition("@")
    # ① 先查已算指标
    if name in computed:
        val = computed[name].value
    else:
        # ② 查原始字段：带序列后缀的优先用序列缓存；单值用最新
        series_key = f"{name}__series"
        if suffix in ("series", "series_10y", "yoy") and series_key in fields:
            val = fields[series_key]
        elif name in fields:
            val = fields[name]
        elif series_key in fields:
            val = fields[series_key]
        else:
            return None

    if not suffix:
        # 无后缀：传单值（最新）。需要序列的指标在 yaml inputs 里显式写 @series。
        # 冻结函数的 _f() 遇到 list 会转 None，所以这里必须取最新，不能原样传 list。
        if isinstance(val, list):
            return val[-1] if val else None
        return val
    if suffix == "latest":
        if isinstance(val, list):
            return val[-1] if val else None
        return val
    if suffix in ("series", "series_10y"):
        if isinstance(val, list):
            return val[-10:] if suffix == "series_10y" else val
        return [val] if val is not None else []
    if suffix == "yoy":
        if isinstance(val, list) and len(val) >= 2:
            return val[-2]
        return None
    # 未知后缀 → 取最新
    if isinstance(val, list):
        return val[-1] if val else None
    return val


# ── 档位判定 ────────────────────────────────────────────────

def _judge_band(value: Any, bands: list[dict]) -> Optional[str]:
    """value 落在哪个 band 的 [min, max) 区间。返回 label。"""
    if value is None or not isinstance(value, (int, float)):
        return None
    for b in bands or []:
        lo = b.get("min", float("-inf"))
        hi = b.get("max", float("inf"))
        if lo <= value < hi:
            return b.get("label", "")
    return None


def _approx_inputs_hit(inputs_spec: Any, approx_fields: dict) -> list[str]:
    """metric 的 inputs 里命中了近似字段的字段名列表（升序去重）。

    支持 yaml inputs 的两种形态：列表 [field1, field2] 与 dict {k: ref}。
    ref 形如 'ebit@latest'，按 @ 切出字段名。
    """
    refs: list[str] = []
    if isinstance(inputs_spec, dict):
        refs = [v for v in inputs_spec.values() if isinstance(v, str)]
    elif isinstance(inputs_spec, list):
        refs = [v for v in inputs_spec if isinstance(v, str)]
    hit = {ref.partition("@")[0] for ref in refs if ref.partition("@")[0] in approx_fields}
    return sorted(hit)


# ── 核心执行 ────────────────────────────────────────────────

def _compute_pack(
    symbol: str,
    group: str,
    classification: dict,
    fields: dict,
) -> dict:
    """执行 base_pack 计算，返回结构化结果。

    步骤：
      1. 加载公式库 + 指标定义 + pack 清单
      2. 两轮计算（第二轮补算依赖第一轮结果的指标）
      3. 档位判定
    """
    registry = _load_formula_registry(group)
    defs = _load_metric_defs(group)
    pack = _load_pack(group)

    # 待算指标 id 清单（core_metrics + step2 + step7）。
    # red_flag_precheck 是组合条件（condition 列表，非 formula），不做自动计算，
    # 由 LLM 基于已算指标判断，或后续做条件引擎。
    ids_to_compute: list[str] = []
    for key in ("core_metrics", "step2_business_essence", "step7_valuation"):
        ids_to_compute.extend(pack.get(key, []) or [])
    # pack 为空时降级为 core 全量
    if not ids_to_compute:
        ids_to_compute = [mid for mid in defs if mid in (
            "roic", "ocf_to_net_income", "fcf_to_net_income", "accruals_ratio",
            "net_debt_to_ebitda", "owner_earnings",
        )]

    computed: dict[str, Any] = {}  # id → MetricResult
    # 两轮：第二轮让依赖第一轮结果的指标（如 roic_vs_wacc 依赖 roic）能算
    for _round in range(2):
        progressed = False
        for mid in ids_to_compute:
            if mid in computed:
                continue
            mdef = defs.get(mid)
            if not mdef:
                computed[mid] = _MissingResult(f"指标定义缺失: {mid}")
                continue
            fname = mdef.get("formula")
            fn = registry.get(fname) if fname else None
            if not fn:
                computed[mid] = _MissingResult(
                    f"公式 '{fname}' 不在注册表（core+{group}）")
                continue
            # 解析 inputs
            inputs_spec = mdef.get("inputs", [])
            if isinstance(inputs_spec, dict):
                # contract_liability_yoy 的 inputs 是 {current: ..@latest, prior: ..@yoy}
                kwargs = {k: _resolve_input(v, fields, computed)
                          for k, v in inputs_spec.items()}
            else:
                # 列表形式 [field1, field2] → 按函数参数名位置传
                # 但 REGISTRY 函数签名用的是具名参数，这里按 spec 顺序取值
                # 约定：inputs 列表顺序与函数参数顺序一致
                vals = [_resolve_input(r, fields, computed) for r in inputs_spec]
                # 取函数参数名
                try:
                    import inspect
                    sig = inspect.signature(fn)
                    # sig.parameters 迭代产生参数名字符串
                    params = [p for p in sig.parameters
                              if sig.parameters[p].default is inspect.Parameter.empty]
                    kwargs = {params[i]: vals[i]
                              for i in range(min(len(params), len(vals)))}
                except Exception:  # noqa: BLE001
                    kwargs = {}
            try:
                result = fn(**kwargs)
                computed[mid] = result
                progressed = True
            except Exception as e:  # noqa: BLE001
                computed[mid] = _MissingResult(f"{fname} 执行异常: {e}")
                progressed = True
        if not progressed:
            break  # 无进展，退出

    # 档位判定 + 组装结果
    results: list[dict] = []
    missing_fields: set[str] = set()
    for mid in ids_to_compute:
        mdef = defs.get(mid, {})
        r = computed.get(mid)
        if r is None:
            results.append({"id": mid, "name": mdef.get("name", mid),
                            "status": "NOT_COMPUTABLE", "reason": "未计算"})
            continue
        value = getattr(r, "value", None)
        status = getattr(r, "status", None)
        status_val = status.value if hasattr(status, "value") else str(status)
        reason = getattr(r, "reason", "") or ""
        # 近似口径降级：metric 的任一 input 字段用了近似推导 → OK 降为 DEGRADED
        approx_fields = fields.get("_approx_fields") or {}
        if status_val == "OK" and approx_fields:
            used_approx = _approx_inputs_hit(mdef.get("inputs", []), approx_fields)
            if used_approx:
                status_val = "DEGRADED"
                reason = f"{reason}；" if reason else ""
                reason += "输入字段用近似口径: " + ", ".join(used_approx)
        band = _judge_band(value, mdef.get("bands", [])) if value is not None else None
        results.append({
            "id": mid,
            "name": mdef.get("name", mid),
            "value": value,
            "status": status_val,
            "band": band,
            "unit": mdef.get("unit", ""),
            "reason": reason,
            "caliber": (getattr(r, "provenance", {}) or {}).get("caliber", ""),
            "why": mdef.get("why", ""),
            "severity": next((b.get("severity") for b in (mdef.get("bands") or [])
                              if _judge_band(value, [b])), None),
        })
        if status_val == "NOT_COMPUTABLE" and "缺少" in reason:
            # 收集缺失字段
            for f in _FIELD_ALIASES.values():
                if f not in fields and f in (getattr(r, "reason", "") or ""):
                    missing_fields.add(f)

    return {
        "symbol": symbol,
        "group": group,
        "classification": classification,
        "metrics": results,
        "missing_fields": sorted(missing_fields),
    }


# ── data_pack 桥：引擎取到的全部原始字段 → var/data_pack.json ──────
# 目的：把 run_code 沙箱的输入源接通。沙箱断网、只读数据包，LLM 在里面
# 只能用这些已抓到的字段写脚本补算（相邻字段近似、SOTP 分部加总等），
# 不能自己造数字。近似字段在 _approx_fields 里登记，run_code 侧会提示降权。

def _build_data_pack(symbol: str, group: str, bundle: Any, fields: dict) -> dict:
    """组装 run_code 可读的数据包（顶层扁平 {字段: 最新值} + {字段__series: 序列}）。

    内容 = fundamentals.years 的全部原始键（不只 base_pack 用到的）+ snapshot
    + base_pack 推导字段（tax_rate/ebitda/ebit 近似等）+ _meta。
    """
    pack: dict[str, Any] = {}
    fund = getattr(bundle, "fundamentals", None)
    if fund and getattr(fund, "years", None):
        years = sorted(fund.years, key=lambda y: str(y.get("year", "")))
        keys: set[str] = set()
        for y in years:
            keys.update(k for k in y.keys() if k != "year")
        for k in sorted(keys):
            vals = [y[k] for y in years if y.get(k) is not None]
            if not vals:
                continue
            pack[k] = vals[-1]               # 最新值
            pack[f"{k}__series"] = vals      # 序列（时间升序）
    snap = getattr(bundle, "snapshot", None)
    if snap:
        for attr in ("pe", "pb", "market_cap", "price", "name", "currency"):
            v = getattr(snap, attr, None)
            if v is not None:
                pack.setdefault(attr, v)
    # base_pack 推导字段（tax_rate/ebitda/ebit 近似等；跳过内部近似登记）
    # 规则：基础键缺失才补；其 __series 序列在基础键已补时也补（原始键的序列已在上方写入）
    for k, v in fields.items():
        if k == "_approx_fields":
            continue
        if k in pack:
            continue
        if k.endswith("__series"):
            base = k[: -len("__series")]
            if base in pack:
                pack[k] = v
        else:
            pack[k] = v
    pack["_meta"] = {
        "symbol": symbol,
        "group": group,
        "fundamentals_source": getattr(fund, "source", None) if fund else None,
        "asof": getattr(fund, "asof", None) if fund else None,
        "approx_fields": sorted(fields.get("_approx_fields") or {}),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return pack


def _write_data_pack(pack: dict) -> str | None:
    """原子写 var/data_pack.json（临时文件 + rename），返回路径；失败只告警不阻塞。"""
    try:
        var_dir = _ROOT / "var"
        var_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(var_dir), prefix=".data_pack_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(pack, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, _DATA_PACK_PATH)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        return str(_DATA_PACK_PATH)
    except Exception as e:  # noqa: BLE001 - 数据包写失败不阻塞计算
        logger.warning("base_pack: data_pack 写入失败: %s", e)
        return None


# ── 简易 MissingResult（避免 import formulas_core 时的循环依赖）──

class _MissingResult:
    """占位结果：指标因定义/公式缺失而无法计算。"""
    def __init__(self, reason: str):
        self.value = None
        self.status = type("S", (), {"value": "NOT_COMPUTABLE"})()
        self.reason = reason
        self.provenance: dict = {}
        self.unit = ""


# ── prompt block 渲染 ───────────────────────────────────────

def _fmt_value(value: Any, unit: str) -> str:
    if value is None:
        return "NC"
    if isinstance(value, float):
        if unit == "pct":
            return f"{value:.1f}%"
        if unit == "days":
            return f"{value:.0f}天"
        if unit == "x":
            return f"{value:.2f}x"
        if abs(value) >= 1e8:
            return f"{value/1e8:.2f}亿"
        return f"{value:.2f}"
    if isinstance(value, dict):
        parts = [f"{k}={_fmt_value(v, '')}" for k, v in value.items()]
        return "{" + ", ".join(parts) + "}"
    return str(value)


def _render_prompt_block(pack_result: dict) -> str:
    """把计算结果渲染成注入 LLM 上下文的文本块。"""
    sym = pack_result["symbol"]
    group = pack_result["group"]
    cls = pack_result["classification"]
    metrics = pack_result["metrics"]

    lines: list[str] = []
    lines.append(f"【base_pack 地基指标 · {sym}】")
    lines.append(
        f"商业模式: {group} | stage: {cls.get('stage', '?')} | "
        f"分类置信度: {cls.get('confidence', 0):.2f}({cls.get('by', '?')})"
    )
    if cls.get("needs_review"):
        lines.append("⚠ 分类待复核（needs_review=true），指标体系可能不准，结论需谨慎")
    lines.append("")

    ok_count = sum(1 for m in metrics if m["status"] == "OK")
    nc_count = sum(1 for m in metrics if m["status"] == "NOT_COMPUTABLE")
    na_count = sum(1 for m in metrics if m["status"] == "NOT_APPLICABLE")
    lines.append(f"已算 {ok_count} 项 / 数据缺失 {nc_count} 项 / 不适用 {na_count} 项")
    lines.append("")

    for m in metrics:
        status_icon = {"OK": "✓", "NOT_APPLICABLE": "—", "NOT_COMPUTABLE": "✗",
                       "DEGRADED": "≈"}.get(m["status"], "?")
        val_str = _fmt_value(m["value"], m["unit"])
        band_str = f" [{m['band']}]" if m["band"] else ""
        sev_str = f" ⚠{m['severity']}" if m.get("severity") else ""
        lines.append(f"• {m['name']}({m['id']}): {val_str}{band_str} {status_icon}{sev_str}")
        if m["status"] != "OK" and m["reason"]:
            lines.append(f"  原因: {m['reason']}")
        if m["caliber"]:
            lines.append(f"  口径: {m['caliber']}")

    missing = pack_result.get("missing_fields") or []
    if missing:
        lines.append("")
        lines.append(
            "⚠ 数据缺口（以下字段 market.bundle 未提供，相关指标标 NC，"
            "需扩展数据源或 LLM 在 calc.run_code 沙箱里用 web 搜索补取）："
        )
        lines.append("  " + ", ".join(missing))

    lines.append("")
    lines.append(
        "使用说明：六关「好生意」关与四大师估值章节须基于上述档位指标判断，"
        "禁止心算 ROIC/FCF 等口径冻结指标；标 NC 的指标标 grey 并写明缺什么。"
    )
    return "\n".join(lines)


# ── Tool 封装 ───────────────────────────────────────────────

class BasePackTool(ReadOnlyTool):
    """base_pack 执行器工具：按商业模式分组自动计算地基指标。"""

    schema = ToolSchema(
        name="calc.base_pack",
        description=(
            "按商业模式分组自动计算地基指标（base_pack）：ROIC/现金质量/应计项/"
            "G1a 合同负债同比/量价拆分/CCC 等口径冻结指标。\n"
            "在 company.classify 之后、六关分析之前调用。\n"
            "内部流程：classify(拿 group) → market.bundle(取数) → 字段映射 → "
            "调 formulas_core/formulas_<group> 冻结函数 → 按 bands 判档位 → "
            "输出「档位+数值+三态」文本块。\n"
            "输出直接作为六关「好生意」关与四大师估值章节的输入，禁止 LLM 心算这些指标。\n"
            "标 NC(NOT_COMPUTABLE) 的指标表示字段缺失；标 DEGRADED 表示用了近似口径"
            "（EBIT 用营业利润+财务费用等），解读须降权。\n"
            "调用后已把全部原始字段写入 var/data_pack.json：字段缺失时可调 calc.run_code "
            "读数据包用相邻字段近似补算，或 web.search 补数（见 run_code 的 web_data 通道），"
            "不要直接心算补数。\n"
            "多业务公司(is_conglomerate=true)仍可调用，按 primary_group 计算；"
            "SOTP 分部分析另行用 calc.run_code。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码或公司名，如 '600519' / '贵州茅台'",
                },
                "group": {
                    "type": "string",
                    "description": (
                        "商业模式分组（G1a/G1b/G2a/G2b/G3/G4/G5/G6）。"
                        "不传则内部调 company.classify 自动判定（有缓存）。"
                        "若已调过 company.classify，传 group 可省一次分类调用。"
                    ),
                },
            },
            "required": ["symbol"],
        },
        read_only=True,
        max_chars=6000,
        timeout_seconds=120,
    )

    async def execute(self, symbol: str, group: str = "", **kwargs: Any) -> dict:
        symbol = str(symbol or "").strip()
        if not symbol:
            return {"success": False, "error": "symbol 为空"}

        # ① 确定 group（传入优先，否则调 classify）
        classification: dict = {}
        if group:
            classification = {"symbol": symbol, "group": group, "stage": "unknown",
                               "confidence": 1.0, "by": "caller", "needs_review": False,
                               "is_conglomerate": False, "sotp_tier": 0,
                               "reasoning": "调用方显式传入 group"}
        else:
            classification = await _classify(symbol)
            group = classification.get("group", "other")

        # group 不在白名单 → 仅算 core 通用指标
        valid_groups = ("G1a", "G1b", "G2a", "G2b", "G3", "G4", "G5", "G6")
        if group not in valid_groups:
            group = "core"  # 降级：只算 core 通用

        # ② 取数（有缓存）
        try:
            bundle = await _get_market_data().bundle(symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning("base_pack: 取数失败: %s", e)
            return {"success": False, "error": f"market.bundle 失败: {e}",
                    "symbol": symbol, "group": group}

        # ③ 字段映射
        fields = _map_fields(bundle)

        # ④ 计算
        pack_result = _compute_pack(symbol, group, classification, fields)

        # ⑤ 渲染 prompt block
        prompt_block = _render_prompt_block(pack_result)

        # ⑥ data_pack 桥：把全部原始字段落盘，供 calc.run_code 沙箱读取补算
        data_pack_path = None
        try:
            data_pack_path = _write_data_pack(
                _build_data_pack(symbol, group, bundle, fields))
        except Exception as e:  # noqa: BLE001
            logger.warning("base_pack: data_pack 桥失败（不阻塞）: %s", e)

        statuses = [m["status"] for m in pack_result["metrics"]]
        return {
            "success": True,
            "symbol": symbol,
            "group": group,
            "stage": classification.get("stage"),
            "data_status": str(bundle.status.value) if bundle.status else "unknown",
            "metrics_count": len(pack_result["metrics"]),
            "ok_count": statuses.count("OK"),
            "degraded_count": statuses.count("DEGRADED"),
            "nc_count": statuses.count("NOT_COMPUTABLE"),
            "na_count": statuses.count("NOT_APPLICABLE"),
            "missing_fields": pack_result["missing_fields"],
            "approx_fields": sorted(fields.get("_approx_fields") or {}),
            "data_pack": data_pack_path,
            "prompt_block": prompt_block,
        }


BASE_PACK_TOOLS: list[ReadOnlyTool] = [BasePackTool()]
