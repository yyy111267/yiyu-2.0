"""
确定性估值计算工具 —— 禁止模型心算的关键。

所有估值/增长/现金跑道计算必须走本工具，保证可复现、可审计。
LLM 只负责输入参数与解读输出，不参与数值计算本身。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from toolkit.base import Tool, ToolResult, ToolSchema, ReadOnlyTool


# ── 纯函数实现（可单测）────────────────────────────────────

def dcf_valuation(
    fcf_projections: list[float],
    terminal_growth: float,
    discount_rate: float,
    *,
    terminal_fcf: Optional[float] = None,
) -> dict:
    """
    现金流折现（DCF）估值。

    Args:
        fcf_projections: 显式预测期各年自由现金流（顺序，单位与终端一致）。
        terminal_growth: 永续增长率（如 0.03 表示 3%）。须小于 discount_rate。
        discount_rate: 折现率（WACC，如 0.09 表示 9%）。
        terminal_fcf: 终端年份 FCF；若不传则用预测期最后一年的值。

    Returns:
        dict: 预测期现值、终值现值、企业总价值、各年贡献。
    """
    if discount_rate <= 0:
        raise ValueError("折现率必须 > 0")
    if terminal_growth >= discount_rate:
        raise ValueError("永续增长率必须小于折现率（否则终值发散）")
    if not fcf_projections:
        raise ValueError("预测期 FCF 列表不能为空")

    # 显式预测期现值
    pv_explicit = 0.0
    year_pvs: list[dict] = []
    for i, fcf in enumerate(fcf_projections, start=1):
        pv = fcf / ((1 + discount_rate) ** i)
        pv_explicit += pv
        year_pvs.append({"year": i, "fcf": fcf, "pv": round(pv, 2)})

    # 终值（Gordon 模型）
    last_fcf = terminal_fcf if terminal_fcf is not None else fcf_projections[-1]
    terminal_value = last_fcf * (1 + terminal_growth) / (discount_rate - terminal_growth)
    n = len(fcf_projections)
    pv_terminal = terminal_value / ((1 + discount_rate) ** n)

    enterprise_value = pv_explicit + pv_terminal

    return {
        "method": "DCF",
        "explicit_pv": round(pv_explicit, 2),
        "terminal_value": round(terminal_value, 2),
        "terminal_pv": round(pv_terminal, 2),
        "enterprise_value": round(enterprise_value, 2),
        "assumptions": {
            "years": n,
            "terminal_growth": terminal_growth,
            "discount_rate": discount_rate,
            "terminal_fcf_used": last_fcf,
        },
        "year_breakdown": year_pvs,
    }


def growth_rate(values: list[float], *, periods: int = 1) -> dict:
    """
    计算增长率（CAGR 或简单增长率）。

    periods=1 时计算最后一期对上一期的简单增长率；
    periods>1 时计算 CAGR。
    """
    if len(values) < 2:
        raise ValueError("至少需要 2 个值")
    if periods < 1:
        raise ValueError("periods 须 >= 1")
    if periods >= len(values):
        raise ValueError("periods 不能超过数据长度-1")

    start = values[-(periods + 1)]
    end = values[-1]
    if start <= 0:
        return {"method": "growth_rate", "error": "起始值 <= 0，增长率无意义", "start": start, "end": end}

    if periods == 1:
        rate = (end - start) / start
    else:
        rate = (end / start) ** (1 / periods) - 1

    return {
        "method": "growth_rate",
        "periods": periods,
        "start": start,
        "end": end,
        "rate": round(rate, 6),
        "rate_pct": round(rate * 100, 2),
    }


def cash_runway(cash: float, monthly_burn: float) -> dict:
    """现金跑道（剩余可支撑月数）。G5/未盈利标的的关键估值锚。"""
    if monthly_burn <= 0:
        return {"method": "cash_runway", "error": "月烧钱率须 > 0", "cash": cash, "burn": monthly_burn}
    months = cash / monthly_burn
    return {
        "method": "cash_runway",
        "cash": cash,
        "monthly_burn": monthly_burn,
        "months": round(months, 1),
        "years": round(months / 12, 2),
        "healthy": months >= 18,  # SKILL.md 中 G5 硬阈值
    }


# ── Tool 封装（供 LLM 调用）────────────────────────────────

class ValuationDCFTool(ReadOnlyTool):
    """DCF 估值工具。"""

    schema = ToolSchema(
        name="calc.valuation_dcf",
        description=(
            "基于自由现金流折现（DCF）模型计算企业估值。传入预测期各年 FCF、永续增长率、折现率，"
            "返回预测期现值、终值现值、企业总价值。永续增长率必须小于折现率。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "fcf_projections": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "显式预测期各年自由现金流（如 [100, 110, 120]）",
                },
                "terminal_growth": {"type": "number", "description": "永续增长率，如 0.03"},
                "discount_rate": {"type": "number", "description": "折现率(WACC)，如 0.09"},
                "terminal_fcf": {"type": "number", "description": "终端年 FCF，可选，默认用预测期最后一年"},
            },
            "required": ["fcf_projections", "terminal_growth", "discount_rate"],
        },
        read_only=True,
        max_chars=3000,
    )

    async def execute(
        self,
        fcf_projections: list[float],
        terminal_growth: float,
        discount_rate: float,
        terminal_fcf: Optional[float] = None,
        **kwargs: Any,
    ) -> dict:
        return dcf_valuation(
            fcf_projections, terminal_growth, discount_rate, terminal_fcf=terminal_fcf
        )


class GrowthRateTool(ReadOnlyTool):
    """增长率计算工具。"""

    schema = ToolSchema(
        name="calc.growth_rate",
        description="计算一组数值序列的增长率。periods=1 简单增长率，>1 为 CAGR。",
        parameters={
            "type": "object",
            "properties": {
                "values": {"type": "array", "items": {"type": "number"}, "description": "数值序列（按时间正序）"},
                "periods": {"type": "integer", "description": "间隔期数，默认 1", "default": 1},
            },
            "required": ["values"],
        },
        read_only=True,
        max_chars=1500,
    )

    async def execute(self, values: list[float], periods: int = 1, **kwargs: Any) -> dict:
        return growth_rate(values, periods=periods)


class CashRunwayTool(ReadOnlyTool):
    """现金跑道计算工具（G5/未盈利标的专用）。"""

    schema = ToolSchema(
        name="calc.cash_runway",
        description="计算现金跑道（剩余可支撑月数）。G5/未盈利标的的关键估值锚，healthy 标准为 >=18 个月。",
        parameters={
            "type": "object",
            "properties": {
                "cash": {"type": "number", "description": "当前现金及等价物"},
                "monthly_burn": {"type": "number", "description": "月度净烧钱率（正数）"},
            },
            "required": ["cash", "monthly_burn"],
        },
        read_only=True,
        max_chars=1000,
    )

    async def execute(self, cash: float, monthly_burn: float, **kwargs: Any) -> dict:
        return cash_runway(cash, monthly_burn)


CALC_TOOLS: list[Tool] = [ValuationDCFTool(), GrowthRateTool(), CashRunwayTool()]
