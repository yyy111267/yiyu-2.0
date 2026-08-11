# ============================================================
# G1a 品牌消费 · 组特有公式（formulas_G1a.py）
# 定位：只放"本组特有口径"的函数；通用函数一律在 formulas_core.py。
#       —— 防止 roic/ratio/cagr 等通用函数在每组复制一份、悄悄改口径。
# 约定：
#       · import formulas_core 的 REGISTRY 后由 @formula 注册进同一个注册表
#         （REGISTRY 是共享对象，沙箱里 `from formulas_G1a import REGISTRY`
#          拿到的是 core+G1a 合并的完整表）
#       · 全部返回 MetricResult（三态 + 溯源），不返回裸数字
#       · 新增本组特有指标 → 在此实现 + @formula 注册
# ============================================================

from __future__ import annotations

from typing import Any

# re-export core 全部公开函数（__all__ 控制的干净导入）：
# 沙箱里 `from formulas_G1a import roic, ccc` 一次拿完整函数集
from formulas_core import *  # noqa: F401,F403  (re-export)
from formulas_core import (  # noqa: F401  (构造器/工具，供本文件与用户代码用)
    DEGRADED, MetricResult, NA, NC, OK, _f, _series, formula,
)


# ── G1a 特有：量价拆分 ────────────────────────────────────

@formula("price_volume_decomposition")
def price_volume_decomposition(revenue: Any, sales_volume: Any) -> MetricResult:
    """量价拆分：拆出"价格贡献"和"销量贡献"（提价驱动 > 放量驱动 > 靠铺货）。

    输入为两期序列（时间升序）：revenue=[上年营收, 本年营收]，sales_volume=[上年销量, 本年销量]。
    混合项归入价格贡献（品牌公司惯例解读）。
    """
    rev = _series(revenue)
    vol = _series(sales_volume)
    if len(rev) < 2 or len(vol) < 2:
        return NC("量价拆分需上年+本年两期营收与销量")
    r1, r0 = rev[-1], rev[-2]
    q1, q0 = vol[-1], vol[-2]
    if r0 == 0 or q0 == 0:
        return NA("上年营收或销量为0，量价拆分无意义")
    total = r1 / r0 - 1
    volume = q1 / q0 - 1
    price = total - volume
    return OK({
        "revenue_growth_pct": round(total * 100, 2),
        "volume_contribution_pct": round(volume * 100, 2),
        "price_contribution_pct": round(price * 100, 2),
    }, provenance={"caliber": "价格贡献=收入增速−销量增速（混合项归价格）"})


# ── G1a 特有：现金转换周期 ───────────────────────────────

@formula("ccc")
def ccc(inventory: Any, accounts_receivable: Any, accounts_payable: Any,
        revenue: Any, cogs: Any) -> MetricResult:
    """现金转换周期（天）= DIO + DSO − DPO。品牌消费应为负（先收钱后发货）。"""
    inv, ar, ap = _f(inventory), _f(accounts_receivable), _f(accounts_payable)
    rev, cogs_v = _f(revenue), _f(cogs)
    if rev in (None, 0) or cogs_v in (None, 0):
        return NC("CCC 缺少 revenue 或 cogs（或为0）")
    days_rev = 365 / rev
    days_cogs = 365 / cogs_v
    dio = (inv or 0) * days_cogs
    dso = (ar or 0) * days_rev
    dpo = (ap or 0) * days_cogs
    return OK(round(dio + dso - dpo, 1), unit="days",
              provenance={"caliber": "DIO+DSO−DPO；存货/应付按COGS、应收按收入周转"})


# ── G1a 特有：费效比（含 DEGRADED 用例）───────────────────

@formula("incremental_revenue_per_expense")
def incremental_revenue_per_expense(revenue: Any, selling_expense: Any) -> MetricResult:
    """费效比：每 1 元销售费用换来多少收入（越高品牌自然拉力越强）。

    理想口径是增量（Δ营收/Δ销售费用）；只有单期营收时退化为费用率倒数，
    标 DEGRADED（口径近似，解读降权）。
    """
    rev = _f(revenue)
    se = _series(selling_expense)
    if rev is None or not se:
        return NC("费效比缺少 revenue 或 selling_expense")
    s = se[-1]
    if s == 0:
        return NA("销售费用为0，费效比无意义")
    value = rev / s
    if len(se) < 2:
        return DEGRADED(value, "只有单期数据，退化为费用率倒数（非增量口径）")
    return OK(value, provenance={"caliber": "营收/最新一期销售费用"})


# ── G1a 特有：ROIC 剔除超额现金 ──────────────────────────

@formula("roic_ex_excess_cash")
def roic_ex_excess_cash(ebit: Any, total_assets: Any, cash: Any,
                        current_liabilities: Any) -> MetricResult:
    """ROIC（剔除超额现金）：投入资本 ≈ 总资产 − 现金 − 流动负债（简化口径）。"""
    e, ta, ca, cl = _f(ebit), _f(total_assets), _f(cash), _f(current_liabilities)
    if e is None or ta is None or cl is None:
        return NC("ROIC(剔超额现金) 缺少 ebit/total_assets/current_liabilities")
    invested = ta - (ca or 0) - cl
    if invested <= 0:
        return NA(f"投入资本非正({invested:.0f})，ROIC 无经济含义")
    return OK(e / invested * 100, unit="pct",
              inputs_used={"invested_capital": round(invested, 2)},
              provenance={"caliber": "投入资本≈总资产−现金−流动负债"})
