"""
calc.metric —— 单指标按需计算工具（agent 主导范式）。

════════════════════════════════════════════════════════════════════
为什么是这个范式（被它取代的旧 base_pack 已删除）
════════════════════════════════════════════════════════════════════
旧范式 calc.base_pack 是「前置闸门」：classify → 一次性把某商业模式写死的
14+5+4 个指标全算一遍 → 塞给 LLM。问题：
 1. 把「该看哪些指标」写死在 yaml，剥夺了 agent 判断力；
 2. 大量算不上的指标返回 NC，还有边界 case 算错（如茅台利息保障 -139x），
    带着「权威档位标签」把错数塞给 LLM，比不给还危险；
 3. 串行阻塞、时延高。

（该工具随 bus_router 的 G*/base_pack.yaml 重构一并删除，其通用基础件
保留为 toolkit/calc/metric_base.py，本工具继续复用。）

本工具把控制权还给 LLM：
 · LLM 先判断「这家什么生意、这个时点该重点看什么」；
 · 需要某个指标时，调 calc.metric(metric_id 或 formula+inputs) 单点计算；
 · 口径易错的指标（ROIC/CCC/TTM/正常化）仍走 formulas_core 冻结函数——
   「防算错」的护栏保留，「写死算什么」的枷锁去掉；
 · 缺数据 → 返回精确的取数需求（缺哪个字段、该取哪张报表）；
 · 非标指标 → LLM 去 calc.run_code 沙箱现算。

一句话：旧 base_pack 是「上菜前的固定套餐」，calc.metric 是「随叫随到的计算器」。

════════════════════════════════════════════════════════════════════
两种调用方式
════════════════════════════════════════════════════════════════════
A) 按 metric_id 算（借用某商业模式菜单里的标准指标定义，含 bands 解读档位）：
     calc.metric(symbol="600519.SH", metric_id="roic", group="G1a")
B) 按 formula+inputs 直接算（LLM 自己指定公式与字段，最灵活）：
     calc.metric(symbol="600519.SH", formula="ratio",
                 inputs={"a": "net_profit", "b": "revenue"})

两种方式都：复用 market.get_bundle 形成的统一数据包 → 调冻结函数 →
返回 {value, status(OK/NA/NC/DEGRADED), band, caliber, reason, 缺失字段}。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from toolkit.base import ReadOnlyTool, ToolSchema

# 复用 metric_base 已经写好的、经过验证的基础件（不重复造轮子）：
# 字段映射、input 解析、档位判定、公式库加载、指标定义加载。
from toolkit.calc.metric_base import (
    _judge_band,
    _load_formula_registry,
    _load_metric_defs,
    _map_fields,
    _resolve_input,
)
from toolkit.calc.metric_service import P0_METRIC_IDS, _collect_fields, compute_metric, compute_metrics
from toolkit.market.research_data import get_research_bundle

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_BUS_ROUTER_DIR = _ROOT / "bus_router"

# 取哪张报表的提示（缺字段时给 LLM 精确取数指引，而非只报字段名）
_FIELD_SOURCE_HINT: dict[str, str] = {
    "market_cap": "行情快照；缺失时以最新价 × 已披露总股本派生",
    "price": "行情快照·最新价（须注明交易时点）",
    "total_shares": "上市公告/招股书/最新年报·已发行总股本（核对股份类别与时点）",
    "revenue": "利润表·营业收入",
    "net_profit": "利润表·归母净利润",
    "cogs": "利润表·营业成本",
    "gross_margin": "利润表·(营收−营业成本)/营收",
    "operating_cash_flow": "现金流量表·经营活动产生的现金流量净额",
    "capital_expenditure": "现金流量表·购建固定资产等支付的现金",
    "ebit": "利润表·营业利润+财务费用（或息税前利润）",
    "ebitda": "EBIT+折旧摊销（现金流量表·折旧+摊销）",
    "total_assets": "资产负债表·资产总计",
    "total_equity": "资产负债表·所有者权益合计",
    "total_debt": "资产负债表·短期借款+长期借款+应付债券等有息负债",
    "cash": "资产负债表·货币资金+交易性金融资产",
    "current_liabilities": "资产负债表·流动负债合计",
    "inventory": "资产负债表·存货",
    "accounts_receivable": "资产负债表·应收账款",
    "accounts_payable": "资产负债表·应付账款",
    "contract_liability": "资产负债表·合同负债（预收款）",
    "selling_expense": "利润表·销售费用",
    "interest_expense": "利润表·财务费用（注意可能为负=净利息收入）",
    "tax_rate": "有效税率=(利润总额−归母净利)/利润总额，或财报附注",
    "sales_volume": "年报·主营业务分产品销量（结构化数据源常缺，需查年报文本）",
    "stock_based_compensation": "年报·股份支付费用（A股多数无，美股/港股披露）",
    "weighted_average_shares_diluted": "利润表附注·稀释加权平均股数",
    "depreciation_amortization": "现金流量表·折旧+无形资产摊销",
    "pe": "行情快照·市盈率",
    "pb": "行情快照·市净率",
    "current_price": "行情快照·最新价",
    "normalized_earnings_ps": "需 LLM 先算正常化每股盈利（跨周期均值）后传入",
    "discount_rate": "需 LLM 自估 WACC（无风险利率+β×风险溢价）后传入",
}


def _field_recovery_plan(metrics: list[dict], available_fields: set[str]) -> list[dict]:
    """为无法计算的标准指标生成可执行的补数契约。

    这是给研究循环的下一步，而不是替模型猜一个数：只有 ``not_disclosed``
    才会进入计划。市值缺失被拆成价格和总股本两个可验证输入，避免搜索到
    不同股份类别或过期股本后直接写进结论。
    """
    plan: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for metric in metrics:
        if metric.get("status") != "not_disclosed":
            continue
        metric_id = str(metric.get("metric_id") or "")
        used = {
            str(item.get("field"))
            for item in (metric.get("fields") or [])
            if isinstance(item, dict) and item.get("field")
        }
        for field_name in metric.get("required_fields") or []:
            field_name = str(field_name)
            if field_name in available_fields or field_name in used:
                continue
            key = (metric_id, field_name)
            if key in seen:
                continue
            seen.add(key)
            if field_name == "market_cap":
                plan.append({
                    "metric_id": metric_id,
                    "missing_field": "market_cap",
                    "action": "derive_market_cap",
                    "prerequisites": ["price", "total_shares"],
                    "formula": "market_cap = price × total_shares",
                    "documents": ["listing_announcement", "prospectus", "latest_annual_report"],
                    "validation": "总股本须与价格同一证券类别，且披露时点不早于报告期。",
                })
            else:
                plan.append({
                    "metric_id": metric_id,
                    "missing_field": field_name,
                    "action": "fetch_official_disclosure",
                    "source_hint": _FIELD_SOURCE_HINT.get(field_name, "查最新年报/中报及其附注"),
                    "documents": ["latest_results", "annual_report_or_interim_report"],
                })
    return plan


def _attach_recovery_plan(result: dict, bundle: Any) -> dict:
    """把缺字段的下一步统一放入计算工具结果。"""
    metrics = result.get("metrics") if isinstance(result.get("metrics"), list) else [result]
    plan = _field_recovery_plan(metrics, set(_collect_fields(bundle)))
    if plan:
        result["field_recovery_plan"] = plan
        result["next_action"] = "按 field_recovery_plan 补齐官方披露字段后，重新调用同一 calc 工具。"
    return result


def _describe_missing(reason: str, needed_fields: list[str],
                      have_fields: set[str]) -> list[dict]:
    """把「缺什么」翻译成精确取数需求：字段名 + 该取哪张报表。"""
    missing = [f for f in needed_fields if f not in have_fields]
    return [
        {"field": f, "source_hint": _FIELD_SOURCE_HINT.get(f, "查年报/财务报表")}
        for f in missing
    ]


def _extract_field_names(inputs_spec: Any) -> list[str]:
    """从 metric 的 inputs（list 或 dict）里抽出裸字段名（去掉 @latest 等后缀）。"""
    refs: list[str] = []
    if isinstance(inputs_spec, dict):
        refs = [v for v in inputs_spec.values() if isinstance(v, str)]
    elif isinstance(inputs_spec, list):
        refs = [v for v in inputs_spec if isinstance(v, str)]
    return [r.partition("@")[0] for r in refs]


def _compute_one(
    metric_id: Optional[str],
    formula_name: Optional[str],
    inputs_spec: Any,
    group: str,
    fields: dict,
) -> dict:
    """算单个指标，返回结构化结果（含三态、档位、缺数据取数需求）。"""
    registry = _load_formula_registry(group)
    defs = _load_metric_defs(group)

    mdef: dict = {}
    if metric_id:
        mdef = defs.get(metric_id, {})
        if not mdef:
            return {
                "success": False,
                "error": f"metric_id '{metric_id}' 不在 {group} 的菜单里",
                "hint": "用 calc.menu 查该商业模式有哪些标准指标，"
                        "或改用 formula+inputs 直接指定公式",
            }
        formula_name = mdef.get("formula")
        inputs_spec = mdef.get("inputs", [])

    if not formula_name:
        return {"success": False, "error": "必须提供 metric_id 或 formula"}

    fn = registry.get(formula_name)
    if not fn:
        return {
            "success": False,
            "error": f"公式 '{formula_name}' 不在注册表（core+{group}）",
            "available_formulas": sorted(registry.keys())[:40],
        }

    # 解析 inputs → kwargs
    computed: dict[str, Any] = {}
    if isinstance(inputs_spec, dict):
        kwargs = {k: _resolve_input(v, fields, computed)
                  for k, v in inputs_spec.items()}
    else:
        vals = [_resolve_input(r, fields, computed) for r in (inputs_spec or [])]
        import inspect
        try:
            sig = inspect.signature(fn)
            params = [p for p in sig.parameters
                      if sig.parameters[p].default is inspect.Parameter.empty]
            kwargs = {params[i]: vals[i]
                      for i in range(min(len(params), len(vals)))}
        except Exception:  # noqa: BLE001
            kwargs = {}

    try:
        r = fn(**kwargs)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"{formula_name} 执行异常: {e}"}

    value = getattr(r, "value", None)
    status = getattr(r, "status", None)
    status_val = status.value if hasattr(status, "value") else str(status)
    reason = getattr(r, "reason", "") or ""
    caliber = (getattr(r, "provenance", {}) or {}).get("caliber", "")
    band = _judge_band(value, mdef.get("bands", [])) if value is not None else None
    severity = next(
        (b.get("severity") for b in (mdef.get("bands") or []) if _judge_band(value, [b])),
        None,
    )

    out: dict[str, Any] = {
        "success": True,
        "metric_id": metric_id,
        "name": mdef.get("name", metric_id or formula_name),
        "formula": formula_name,
        "value": value,
        "status": status_val,
        "band": band,
        "severity": severity,
        "unit": mdef.get("unit", getattr(r, "unit", "")),
        "caliber": caliber,
        "reason": reason,
        "why": mdef.get("why", ""),
    }
    # 数据缺失 → 给精确取数需求
    if status_val == "NOT_COMPUTABLE":
        needed = _extract_field_names(inputs_spec)
        out["missing"] = _describe_missing(reason, needed, set(fields.keys()))
        out["next_action"] = (
            "缺字段：可 web.search 补数后进 calc.run_code 沙箱现算，"
            "或若数据源可扩展则补取。切勿心算/编数。"
        )
    return out


class MetricTool(ReadOnlyTool):
    """单指标按需计算工具（agent 主导，取代已删除的 base_pack 前置全算）。"""

    schema = ToolSchema(
        name="calc.metric",
        description=(
            "【agent 主导范式】按需计算单个财务指标——你先判断这家公司这个时点该看什么，"
            "再点名算什么，而不是一次性算一大包。\n"
            "口径易错的指标（ROIC/CCC/TTM差分/正常化盈利/应计项等）由冻结函数保证不算错，"
            "你只负责判断「该看哪些」和「怎么解读档位」。\n"
            "两种用法：\n"
            "① 按标准指标：{symbol, metric_id, group} —— 借用该商业模式菜单里的"
            "标准定义（含解读档位 band）。先用 calc.menu 看某商业模式有哪些标准指标。\n"
            "② 自定义：{symbol, formula, inputs} —— 你自己指定公式名与字段映射，最灵活。"
            "如 formula='ratio', inputs={'a':'net_profit','b':'revenue'} 算净利率。\n"
            "P0 指标批次按指标字典 v1.0 输出：{value, metric_id, caliber_version, "
            "period, source_level, as_of, status, fields}。\n"
            "P0 已支持：pe_ttm / pb / ps_ttm / ev_ebitda / ev_revenue / fcf_yield / "
            "roic / roe / fcf / fcf_margin / capex_intensity / runway。\n"
            "旧菜单指标返回：value + status（OK可解读/NOT_APPLICABLE不适用别当缺陷/"
            "NOT_COMPUTABLE数据缺失/DEGRADED近似口径需降权）+ band 档位 + caliber 口径 + "
            "缺数据时的精确取数需求（缺哪个字段、该取哪张报表）。\n"
            "缺字段不要心算——按返回的 missing 去补数或进 calc.run_code 沙箱现算。\n"
            "本工具不联网取行情；可与 market.get_bundle 同轮调用，编排层会先完成统一取数。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码或公司名，如 600519.SH / 贵州茅台"},
                "metric_id": {
                    "type": "string",
                    "description": "标准指标 id（如 roic / cash_conversion_cycle）。"
                                   "与 formula 二选一。用 calc.menu 查可用 id。",
                },
                "formula": {
                    "type": "string",
                    "description": "公式名（如 ratio / roic / ccc / normalized_pe）。"
                                   "与 metric_id 二选一。空调用 calc.run_code 看可用公式清单。",
                },
                "inputs": {
                    "type": "object",
                    "description": "formula 模式下的字段映射，如 {'a':'net_profit','b':'revenue'}。"
                                   "值支持 @series/@yoy/@latest 后缀。",
                },
                "group": {
                    "type": "string",
                    "description": "商业模式分组（G1a/G1b/G2a/G2b/G3/G4/G5/G6），"
                                   "决定加载哪套 formulas_<group> + 指标菜单。默认 core。",
                    "default": "core",
                },
            },
            "required": ["symbol"],
        },
        read_only=True,
        max_chars=4000,
        timeout_seconds=30,
    )

    async def execute(self, symbol: str, metric_id: str = "", formula: str = "",
                      inputs: Any = None, group: str = "core", **kwargs: Any) -> dict:
        symbol = str(symbol or "").strip()
        if not symbol:
            return {"success": False, "error": "symbol 为空"}
        if not metric_id and not formula:
            return {"success": False, "error": "必须提供 metric_id 或 formula 之一",
                    "hint": "先用 calc.menu 看该商业模式有哪些标准指标"}
        group = group or "core"

        stored = get_research_bundle(symbol)
        if stored is None:
            return {
                "success": False,
                "error": "统一行情数据包不存在，请先调用 market.get_bundle",
                "symbol": symbol,
                "needs_market_data": True,
                "required_market_request": {
                    "symbol": symbol,
                    "metric_ids": [metric_id] if metric_id in P0_METRIC_IDS else [],
                },
            }
        data_pack_id, bundle = stored

        fields = _map_fields(bundle)
        if metric_id in P0_METRIC_IDS and not formula:
            result = compute_metric(bundle, metric_id)
            result["symbol"] = symbol
            result["group"] = group
            # 透传取数层 evidence + missing + fetch_status
            result["field_evidence"] = dict(bundle.field_evidence)
            covered = {str(f.get("field")) for f in result.get("fields", [])}
            result["missing_fields"] = [f for f in bundle.missing_fields if f not in covered]
            result["fetch_status"] = bundle.fetch_status
            result["data_pack_id"] = data_pack_id
            return _attach_recovery_plan(result, bundle)
        result = _compute_one(metric_id or None, formula or None, inputs, group, fields)
        result["symbol"] = symbol
        result["group"] = group
        result["data_pack_id"] = data_pack_id
        return result


class MetricsTool(ReadOnlyTool):
    """一次取数后计算多个标准指标，避免研究配方逐项重复拉行情。"""

    schema = ToolSchema(
        name="calc.metrics",
        description=(
            "批量计算标准指标：复用 market.get_bundle 形成的统一数据包，调用冻结指标服务输出多项结果。"
            "适合执行配方已明确多个标准指标的研究；不支持的非标指标请用 calc.run_code。"
            "本工具不联网取行情，可与 market.get_bundle 同轮调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "证券代码或公司名"},
                "metric_ids": {
                    "type": "array", "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Metric Service 支持的标准指标，例如 ['ps_ttm', 'ev_revenue', 'runway']。",
                },
                "group": {"type": "string", "description": "商业模式分组，仅作审计标记", "default": "core"},
            },
            "required": ["symbol", "metric_ids"],
        },
        read_only=True,
        max_chars=8000,
        timeout_seconds=30,
    )

    async def execute(self, symbol: str, metric_ids: list[str], group: str = "core", **kwargs: Any) -> dict:
        symbol = str(symbol or "").strip()
        ids = list(dict.fromkeys(str(metric_id or "").strip() for metric_id in (metric_ids or []) if metric_id))
        if not symbol:
            return {"success": False, "error": "symbol 为空"}
        if not ids:
            return {"success": False, "error": "metric_ids 至少包含一个指标"}
        unsupported = [metric_id for metric_id in ids if metric_id not in P0_METRIC_IDS]
        if unsupported:
            return {
                "success": False,
                "error": f"以下指标不支持批量标准计算: {', '.join(unsupported)}",
                "supported_metrics": sorted(P0_METRIC_IDS),
            }
        stored = get_research_bundle(symbol)
        if stored is None:
            return {
                "success": False,
                "error": "统一行情数据包不存在，请先调用 market.get_bundle",
                "symbol": symbol,
                "needs_market_data": True,
                "required_market_request": {"symbol": symbol, "metric_ids": ids},
            }
        data_pack_id, bundle = stored

        result = compute_metrics(bundle, ids)
        result.update({
            "symbol": symbol,
            "group": group or "core",
            "field_evidence": dict(bundle.field_evidence),
            "missing_fields": list(bundle.missing_fields),
            "fetch_status": bundle.fetch_status,
            "data_pack_id": data_pack_id,
        })
        return _attach_recovery_plan(result, bundle)


METRIC_TOOLS: list[ReadOnlyTool] = [MetricTool(), MetricsTool()]
