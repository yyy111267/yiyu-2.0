"""
metric_base —— calc 层的共享基础件（不注册任何工具，纯函数模块）。

calc.metric / calc.metrics（agent 主导的新范式）与 calc.run_code（断网沙箱）
都建立在这几个函数上：

  _load_formula_registry  加载 bus_router 的冻结公式库（core + 组模块）
  _load_metric_defs       加载指标定义（内置 _CORE_METRIC_DEFS + 组 yaml 覆盖）
  _map_fields             统一数据包 → 内部字段字典（含近似口径登记）
  _resolve_input          解析 yaml inputs 引用（支持 @series/@yoy/@latest）
  _judge_band             数值 → 档位 label
  _build_data_pack        统一数据包 → run_code 沙箱的 DATA

历史包袱已清除：旧的 calc.base_pack「前置全算一包指标」工具已删除
（它依赖的 bus_router/G*/base_pack.yaml、core.yaml、G*_metrics.yaml
随业务路由重构一并删除）。指标由 calc.metric(s) 按需单点计算，
沙箱数据包由 run_code 按 symbol 现组装。

铁律（与 formulas_core 对齐）：
  · 指标只由冻结函数算，LLM 不心算；
  · 三态严格区分：OK / NOT_APPLICABLE / NOT_COMPUTABLE / DEGRADED；
  · 字段缺失显式标 NC，绝不静默返回 0/None；
  · 近似口径（EBIT/EBITDA 退化推导）必须登记进 fields["_approx_fields"]，
    不得当精确值使用。
"""

from __future__ import annotations

import importlib
import logging
import sys
import time
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# ── 路径约定 ────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]                     # yiyu-agent/
_BUS_ROUTER_DIR = _ROOT / "bus_router"


# ── 公式库加载：把 bus_router 加到 sys.path，import formulas_core + formulas_<group> ──
# 注：目前 bus_router 下只有 formulas_core.py，formulas_<group>.py 已随重构删除，
# 所以所有 group 实际都走同一套 core 冻结公式。组公式将来补上时这里无需改动。

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
        logger.warning("metric_base: 公式库加载失败（group=%s）: %s", group, e)
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
        logger.warning("metric_base: yaml 读取失败 %s: %s", path, e)
        return {}


# core 指标定义：bus_router/core.yaml 已删除，这份内置定义就是当前唯一来源
# （_load_metric_defs 仍保留读 core.yaml 的分支，方便将来 yaml 回归时覆盖）。
# 每条含 formula/inputs/unit/direction/bands/why，与 formulas_core 的冻结函数对齐。
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


# ── 字段映射：market.bundle → 指标计算字段字典 ──────────────────
# market.fundamentals.years 的键 → 指标 inputs 引用的字段名
# 不全的字段留缺（冻结函数返回 NC，诚实标注数据缺口）

# westock 已扩展提取 zcfz/lrb/xjll/sum 明细字段，这里全部映射进字段字典。
# 缺失字段自然不在 dict 里（_resolve_input 返回 None → 冻结函数 NC，诚实标注）。
_FIELD_ALIASES: dict[str, str] = {
    "revenue": "revenue",
    "revenue_yoy": "revenue_yoy",
    "net_profit": "net_profit",
    "net_profit_parent": "net_profit_parent",
    "net_profit_parent_yoy": "net_profit_parent_yoy",
    "gross_margin": "gross_margin",
    "roe": "roe",
    "ocf": "operating_cash_flow",       # years 用 ocf，指标 inputs 用 operating_cash_flow
    "capex": "capital_expenditure",     # years 用 capex，指标 inputs 用 capital_expenditure
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
    "assets": "total_assets",           # westock years 键是 assets → inputs 用 total_assets
    "equity": "total_equity",           # westock years 键是 equity → inputs 用 total_equity
    "ebitda": "ebitda",
    "fcff": "fcff",
}


def _map_fields(bundle: Any) -> dict[str, Any]:
    """把 MarketBundle 的 fundamentals.years 映射成指标计算字段字典。

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
    # 铁律：近似必须显式登记进 _approx_fields，调用方据此把相关指标
    # 从 OK 降级为 DEGRADED，绝不静默当精确值。
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




# ── 沙箱数据包组装：统一取数缓存 → run_code 的 DATA ──────────────
# run_code 沙箱断网只读，LLM 在里面只能用这些已抓到的字段写脚本补算
# （相邻字段近似、SOTP 分部加总等），不能自己造数字。
# 近似字段在 _approx_fields 里登记，run_code 侧会提示降权。

def _build_data_pack(symbol: str, group: str, bundle: Any, fields: dict) -> dict:
    """组装 run_code 可读的数据包（顶层扁平 {字段: 最新值} + {字段__series: 序列}）。

    内容 = fundamentals.years 的全部原始键（不只单个指标用到的）+ snapshot
    + _map_fields 推导字段（tax_rate/ebitda/ebit 近似等）+ _meta。
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
        for attr in (
            "pe", "pe_dynamic", "pe_static", "pe_ttm", "ps_ttm", "pb", "market_cap", "float_market_cap",
            "price", "industry", "listing_date", "name", "currency",
        ):
            v = getattr(snap, attr, None)
            if v is not None:
                pack.setdefault(attr, v)
    # _map_fields 推导字段（tax_rate/ebitda/ebit 近似等；跳过内部近似登记）
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
