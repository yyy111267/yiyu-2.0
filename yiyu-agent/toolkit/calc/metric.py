"""
calc.metric —— 单指标按需计算工具（agent 主导范式）。

════════════════════════════════════════════════════════════════════
为什么有这个工具（与 base_pack 的本质区别）
════════════════════════════════════════════════════════════════════
旧范式 base_pack 是「前置闸门」：classify → 一次性把某商业模式写死的
14+5+4 个指标全算一遍 → 塞给 LLM。问题：
  1. 把「该看哪些指标」写死在 yaml，剥夺了 agent 判断力；
  2. 大量算不上的指标返回 NC，还有边界 case 算错（如茅台利息保障 -139x），
     带着「权威档位标签」把错数塞给 LLM，比不给还危险；
  3. 串行阻塞、时延高。

新范式（本工具）把控制权还给 LLM：
  · LLM 先判断「这家什么生意、这个时点该重点看什么」；
  · 需要某个指标时，调 calc.metric(metric_id 或 formula+inputs) 单点计算；
  · 口径易错的指标（ROIC/CCC/TTM/正常化）仍走 formulas_core 冻结函数——
    「防算错」的护栏保留，「写死算什么」的枷锁去掉；
  · 缺数据 → 返回精确的取数需求（缺哪个字段、该取哪张报表）；
  · 非标指标 → LLM 去 calc.run_code 沙箱现算。

一句话：base_pack 是「上菜前的固定套餐」，calc.metric 是「随叫随到的计算器」。
本工具不废弃 base_pack（旧链路保留），是它的 agent 化替代。

════════════════════════════════════════════════════════════════════
两种调用方式
════════════════════════════════════════════════════════════════════
A) 按 metric_id 算（借用某商业模式菜单里的标准指标定义，含 bands 解读档位）：
     calc.metric(symbol="600519.SH", metric_id="roic", group="G1a")
B) 按 formula+inputs 直接算（LLM 自己指定公式与字段，最灵活）：
     calc.metric(symbol="600519.SH", formula="ratio",
                 inputs={"a": "net_profit", "b": "revenue"})

两种方式都：自动取数（复用 market.bundle + 字段映射）→ 调冻结函数 →
返回 {value, status(OK/NA/NC/DEGRADED), band, caliber, reason, 缺失字段}。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from toolkit.base import ReadOnlyTool, ToolSchema

# 复用 base_pack 已经写好的、经过验证的基础件（不重复造轮子）：
# 字段映射、input 解析、档位判定、公式库加载、指标定义加载。
from toolkit.calc.base_pack import (
    _get_market_data,
    _judge_band,
    _load_formula_registry,
    _load_metric_defs,
    _map_fields,
    _resolve_input,
)

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_BUS_ROUTER_DIR = _ROOT / "bus_router"

# 取哪张报表的提示（缺字段时给 LLM 精确取数指引，而非只报字段名）
_FIELD_SOURCE_HINT: dict[str, str] = {
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
    """单指标按需计算工具（agent 主导，替代 base_pack 的前置全算）。"""

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
            "返回：value + status（OK可解读/NOT_APPLICABLE不适用别当缺陷/"
            "NOT_COMPUTABLE数据缺失/DEGRADED近似口径需降权）+ band 档位 + caliber 口径 + "
            "缺数据时的精确取数需求（缺哪个字段、该取哪张报表）。\n"
            "缺字段不要心算——按返回的 missing 去补数或进 calc.run_code 沙箱现算。"
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
        timeout_seconds=60,
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

        try:
            bundle = await _get_market_data().bundle(symbol)
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"取数失败: {e}", "symbol": symbol}

        fields = _map_fields(bundle)
        result = _compute_one(metric_id or None, formula or None, inputs, group, fields)
        result["symbol"] = symbol
        result["group"] = group
        return result


METRIC_TOOLS: list[ReadOnlyTool] = [MetricTool()]
