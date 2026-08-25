# ============================================================
# 可信公式函数库 · 全局（formulas_core.py）
# 定位：有标准口径、且容易被 LLM 算错的价值投资指标（"口径冻结层"）。
# 被两处调用：
#   1) base_pack 自动计算时，引擎按 yaml 的 formula 名查 REGISTRY
#   2) LLM 在 run_code 沙箱里现算非标指标时，优先 import 本库可信函数，
#      不自己手写公式（防口径漂移）
# 设计铁律：
#   · 每个函数返回 MetricResult（三态 + 溯源），不返回裸数字
#   · 三态严格区分：OK / NOT_APPLICABLE(不适用，正常别当缺陷) /
#                   NOT_COMPUTABLE(数据缺失，可告警) / DEGRADED(用了近似口径)
#   · 绝不静默返回 None/0；缺失显式 NC、不适用显式 NA、近似显式 DEGRADED
#   · 有口径陷阱的（ROIC/TTM差分/正常化盈利/应计项）口径写死在函数里
#   · A股特有陷阱（累计数差分）在此层处理，不暴露给上层
# 不在这里的东西：
#   · 纯非标、临时、无标准口径的计算 → LLM 在 run_code 里现写
#   · 数据获取 → 取数层，此处只接收已取到的数据
# ============================================================

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Sequence


# ============================================================
# 结果契约：所有函数返回 MetricResult
# ============================================================

class Status(str, Enum):
    OK = "OK"                          # 正常算出，可解读
    NOT_APPLICABLE = "NOT_APPLICABLE"  # 指标对本公司不适用——正常，别当缺陷
    NOT_COMPUTABLE = "NOT_COMPUTABLE"  # 数据缺失导致算不出——数据问题，可告警
    DEGRADED = "DEGRADED"              # 用了 fallback/近似口径（解读降权）


@dataclass
class MetricResult:
    value: Optional[Any]               # float / dict / list，随指标而定
    status: Status
    reason: str = ""
    unit: str = ""
    inputs_used: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)   # 口径/期间/数据源

    @property
    def ok(self) -> bool:
        return self.status == Status.OK

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "status": self.status.value,
            "reason": self.reason,
            "unit": self.unit,
            "inputs_used": self.inputs_used,
            "provenance": self.provenance,
        }


# 便捷构造器
def OK(value: Any, unit: str = "", **kw: Any) -> MetricResult:
    return MetricResult(value, Status.OK, unit=unit, **kw)


def NA(reason: str) -> MetricResult:
    return MetricResult(None, Status.NOT_APPLICABLE, reason=reason)


def NC(reason: str) -> MetricResult:
    return MetricResult(None, Status.NOT_COMPUTABLE, reason=reason)


def DEGRADED(value: Any, reason: str, unit: str = "", **kw: Any) -> MetricResult:
    return MetricResult(value, Status.DEGRADED, reason=reason, unit=unit, **kw)


# ============================================================
# 公式注册表：yaml 的 formula 名 → 函数。装饰器就近注册，不漏不重。
# ============================================================

REGISTRY: dict[str, Callable[..., MetricResult]] = {}


def formula(name: str):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


# ============================================================
# 数值防御
# ============================================================

def _f(v: Any) -> float | None:
    """宽松数值解析：MetricResult 取 .value；None/''/'--'/'nan' → None。"""
    if isinstance(v, MetricResult):
        v = v.value
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "None", "nan", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _series(v: Any) -> list[float]:
    """把输入规范成数值列表（@series 字段）。非数值跳过。

    序列约定：**按时间升序（最早在前，最新在后）**。
    """
    if isinstance(v, MetricResult):
        v = v.value
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        result: list[float] = []
        for i in v:
            fv = _f(i)
            if fv is not None:
                result.append(fv)
        return result
    fv = _f(v)
    return [fv] if fv is not None else []


def _quarter_tag(period: str) -> int | None:
    """从期间字符串提取季度序号（1-4），容错 '2025Q3'/'2025-Q3'/'Q3 2025'。"""
    m = re.search(r"Q\s*([1-4])", str(period), flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


# ============================================================
# 1) ROIC —— 口径写死，最容易被 LLM 算错的指标
#    LLM 常见错误：分母乱用（总资产 vs 投入资本 vs 净资产）、
#                  分子用净利润而非 NOPAT、忘了扣超额现金
# ============================================================

@formula("roic")
def roic(ebit: Any, tax_rate: Any, total_debt: Any, total_equity: Any,
         cash: Any = 0.0, excess_cash_ratio: float = 1.0) -> MetricResult:
    """投入资本回报率 = NOPAT / 投入资本（%）。

    口径写死：NOPAT = EBIT × (1 − tax_rate)（用息税前，不用净利润）；
    投入资本 = 有息负债 + 股东权益 − 超额现金。
    tax_rate：调用者应传近 3 年有效税率均值；只有法定税率时传法定并注明
    （引擎在 provenance 里记录来源，DEGRADED 由调用侧标）。
    """
    e, tr = _f(ebit), _f(tax_rate)
    d, eq = _f(total_debt), _f(total_equity)
    if e is None or tr is None or d is None or eq is None:
        return NC("ROIC 缺少 ebit/tax_rate/total_debt/total_equity 之一")
    nopat = e * (1 - tr)
    invested = d + eq - (_f(cash) or 0.0) * excess_cash_ratio
    if invested <= 0:
        return NA(f"投入资本非正({invested:.0f})，ROIC 无经济含义")
    return OK(
        nopat / invested * 100, unit="pct",
        inputs_used={"nopat": round(nopat, 2), "invested_capital": round(invested, 2)},
        provenance={"caliber": "NOPAT=EBIT×(1−tax)；投入资本=有息负债+权益−超额现金"},
    )


# ============================================================
# 2) TTM 差分 —— A股财报是累计数，最隐蔽的坑
#    A股季报是"年初至今累计"；LLM 常直接把三季报数字当 Q3 单季，全错。
#    正确：先按期间标签差分成单季，再滚动求 TTM。
# ============================================================

@formula("quarterly_from_cumulative")
def quarterly_from_cumulative(cumulative_series: Sequence[tuple]) -> MetricResult:
    """累计数 → 单季值序列（按期间标签差分，跨年确定不猜）。

    输入：[('2025Q1', 累计值), ('2025Q2', 累计值), ...] 按时间升序。
    每年 Q1 的单季 = 其累计本身；其余单季 = 本期累计 − 上期累计。
    """
    if not cumulative_series:
        return NC("累计序列为空")
    s = sorted(cumulative_series, key=lambda x: str(x[0]))
    singles: list[tuple] = []
    skipped_oldest = None
    for i, (period, cum) in enumerate(s):
        if cum is None:
            return NC(f"{period} 累计值缺失，无法差分")
        q = _quarter_tag(str(period))
        if q is None:
            return NC(f"期间格式无法识别: {period}（须含 Q1-Q4）")
        if q == 1:
            singles.append((period, cum))
        else:
            if i == 0:
                skipped_oldest = period  # 最老一期非 Q1：缺上一累计值无法差分，跳过
                continue
            prev_cum = s[i - 1][1]
            if prev_cum is None:
                return NC(f"{period} 前一累计缺失，无法差分")
            singles.append((period, cum - prev_cum))
    if not singles:
        return NC("差分后无有效单季")
    return OK(
        singles,
        inputs_used={"quarters": len(singles), "skipped_oldest": skipped_oldest},
        provenance={"caliber": "累计数按期间标签差分为单季（Q1=累计本身）"},
    )


@formula("ttm_from_cumulative")
def ttm_from_cumulative(cumulative_series: Sequence[tuple]) -> MetricResult:
    """最新 TTM（滚动12个月）= 最近4个单季之和。

    输入：[('2025Q1', 累计值), ...] 按时间升序，至少覆盖最近5个季度点。
    口径陷阱：直接用"最新累计"当 TTM 会漏掉上年尾段。
    """
    if not cumulative_series or len(cumulative_series) < 5:
        return NC("TTM 需至少5个季度累计点以还原4个单季")
    s = sorted(cumulative_series, key=lambda x: str(x[0]))
    singles: list[tuple] = []
    for i, (period, cum) in enumerate(s):
        if cum is None:
            return NC(f"{period} 累计值缺失，无法差分")
        q = _quarter_tag(str(period))
        if q is None:
            return NC(f"期间格式无法识别: {period}（须含 Q1-Q4）")
        if q == 1:
            singles.append((period, cum))
        else:
            if i == 0:
                continue  # 最老一期非 Q1：缺上一累计值无法差分，跳过（不影响最近4个单季）
            prev_cum = s[i - 1][1]
            if prev_cum is None:
                return NC(f"{period} 前一累计缺失，无法差分")
            singles.append((period, cum - prev_cum))
    if len(singles) < 4:
        return NC("差分后单季不足4个")
    last4 = singles[-4:]
    ttm = sum(v for _, v in last4)
    return OK(
        ttm,
        inputs_used={"last4_singles": last4},
        provenance={"caliber": "累计数差分为单季后滚动求和"},
    )


# ============================================================
# 3) 正常化盈利 —— 价值投资抗周期口径（Graham/Greenwald）
# ============================================================

@formula("normalized_earnings")
def normalized_earnings(earnings_series: Sequence[Optional[float]],
                        min_years: int = 7, method: str = "mean") -> MetricResult:
    """正常化盈利 = 过去 N 年盈利均值/中位数（抗单年波动）。

    判断注入约定：LLM 负责把一次性损益从序列中剔除后传入；本函数只做均值/中位数。
    周期股必须用此口径，禁用单年。
    """
    valid = _series(earnings_series)
    if len(valid) < min_years:
        return NC(f"正常化盈利需≥{min_years}年，仅{len(valid)}年")
    norm = statistics.median(valid) if method == "median" else statistics.mean(valid)
    note = ""
    if any(v < 0 for v in valid):
        note = "序列含亏损年，正常化盈利已纳入，解读需结合周期位置"
    return OK(
        norm,
        reason=note,
        inputs_used={"years_used": len(valid), "method": method},
        provenance={"caliber": f"过去{len(valid)}年盈利{method}"},
    )


@formula("normalized_pe")
def normalized_pe(price: Any, normalized_earnings_ps: Any) -> MetricResult:
    """正常化 PE = 股价 / 正常化每股盈利（判断在调用侧，本函数只做除法）。"""
    p, eps = _f(price), _f(normalized_earnings_ps)
    if p is None or eps in (None, 0):
        return NC("正常化PE 缺少 price 或 normalized_earnings_ps")
    return OK(p / eps, provenance={"caliber": "股价/正常化每股盈利"})


# ============================================================
# 4) 三情景估值 kernel —— 算术固定，假设由 LLM 提供（Tier B）
# ============================================================

@formula("three_scenario")
def three_scenario(base_revenue: Any, shares_diluted: Any, current_price: Any,
                   assumptions: Sequence[dict]) -> MetricResult:
    """三情景估值：收入→净利→市值→目标价→上行空间/隐含IRR，算术固定。

    assumptions: [{"name": "保守/中性/乐观", "revenue_cagr": 0.05,
                   "net_margin": 0.20, "exit_pe": 15, "horizon_years": 3,
                   "rationale": "LLM 必须给的理由"}, ...]
    LLM 只提供假设，算术必须由本 kernel 执行，保证全公司口径一致可比。
    """
    rev0, shr, px = _f(base_revenue), _f(shares_diluted), _f(current_price)
    if rev0 is None or shr in (None, 0) or px in (None, 0):
        return NC("三情景估值缺少 base_revenue/shares_diluted/current_price")
    if not assumptions:
        return NC("未提供情景假设（应由 LLM 生成）")
    out: dict[str, dict] = {}
    for a in assumptions:
        name = a.get("name")
        cagr = _f(a.get("revenue_cagr"))
        margin = _f(a.get("net_margin"))
        pe = _f(a.get("exit_pe"))
        years = int(a.get("horizon_years") or 3)
        rationale = str(a.get("rationale") or "")
        if name is None or cagr is None or margin is None or pe in (None, 0) or years <= 0:
            return NC(f"情景 {name or '?'} 假设不完整（须含 revenue_cagr/net_margin/exit_pe/horizon_years）")
        rev = rev0 * (1 + cagr) ** years
        ni = rev * margin
        mv = ni * pe
        target = mv / shr
        out[name] = {
            "future_revenue": round(rev, 2),
            "future_net_income": round(ni, 2),
            "target_mv": round(mv, 2),
            "target_price": round(target, 2),
            "upside_pct": round((target / px - 1) * 100, 1),
            "implied_irr_pct": round(((target / px) ** (1 / years) - 1) * 100, 1),
            "rationale": rationale,
        }
    return OK(
        out,
        inputs_used={"base_revenue": rev0, "current_price": px, "scenarios": len(out)},
        provenance={"caliber": "净利×退出PE→市值→目标价；算术固定，假设由LLM提供"},
    )


@formula("scenario_valuation")
def scenario_valuation(scenarios: Sequence[dict], price: Any = None) -> MetricResult:
    """情景概率加权期望值（value × probability，概率须加总≈1）。"""
    if not scenarios or not isinstance(scenarios, (list, tuple)):
        return NC("scenario_valuation 缺少情景列表")
    total_p = 0.0
    weighted = 0.0
    items: list[dict] = []
    for s in scenarios:
        v = _f(s.get("value"))
        p = _f(s.get("probability"))
        if v is None or p is None:
            return NC("情景须含 value 和 probability")
        total_p += p
        weighted += v * p
        items.append({"label": s.get("label", ""), "value": v, "probability": p})
    if abs(total_p - 1.0) > 0.01:
        return NC(f"情景概率须加总≈1，实际 {total_p:.3f}")
    result: dict[str, Any] = {
        "method": "scenario_weighted",
        "expected_value": round(weighted, 2),
        "scenarios": items,
        "probability_sum": round(total_p, 4),
    }
    cur = _f(price)
    if cur is not None and cur != 0:
        result["premium_over_price_pct"] = round((weighted - cur) / cur * 100, 2)
    return OK(result, provenance={"caliber": "value×probability 概率加权期望"})


# ============================================================
# 5) 通用比率 / 差值 / 增速（core.yaml 引用）
# ============================================================

@formula("ratio")
def ratio(a: Any, b: Any) -> MetricResult:
    """通用比率（乘数或百分比由 unit 标注解释）。分母为 0 → 不适用。"""
    x, y = _f(a), _f(b)
    if x is None or y is None:
        return NC("ratio 缺少分子或分母")
    if y == 0:
        return NA("分母为0，比率无定义")
    return OK(x / y)


@formula("sum_components")
def sum_components(*values: Any) -> MetricResult:
    """分量求和（财险综合成本率 = 赔付率 + 费用率等）。任一分量缺失 → 不可计算。"""
    raw = list(values)
    xs = [_f(v) for v in raw]
    if any(x is None for x in xs):
        return NC("求和存在缺失分量")
    return OK(sum(xs), provenance={"caliber": "各分量直接相加"})


@formula("spread")
def spread(a: Any, b: Any) -> MetricResult:
    """差值（ROIC−WACC 经济利润差 / 准许ROE−实际ROE / 股息率−国债利差）。"""
    x, y = _f(a), _f(b)
    if x is None or y is None:
        return NC("spread 缺少两输入之一")
    return OK(x - y)


@formula("yoy_growth")
def yoy_growth(current: Any, prior: Any) -> MetricResult:
    """同比增速 %。prior 为 0 或缺失 → 不适用。"""
    c, p = _f(current), _f(prior)
    if c is None or p is None:
        return NC("yoy_growth 缺少 current 或 prior")
    if p == 0:
        return NA("上年基数为0，同比增速无意义")
    return OK((c - p) / abs(p) * 100, unit="pct")


@formula("cagr")
def cagr(values: Sequence[Any]) -> MetricResult:
    """复合增速 CAGR（首尾值，小数）。需 ≥2 个数值且首值 > 0。"""
    xs = _series(values)
    if len(xs) < 2:
        return NC("CAGR 需要 ≥2 个数值")
    first, last = xs[0], xs[-1]
    if first <= 0 or last <= 0:
        return NA("序列首/尾值非正，CAGR 无意义")
    return OK((last / first) ** (1 / (len(xs) - 1)) - 1)


@formula("incremental_roic")
def incremental_roic(ebit_change: Any, invested_capital_change: Any) -> MetricResult:
    """增量 ROIC = 新增 EBIT / 新增投入资本（%）。"""
    e, ic = _f(ebit_change), _f(invested_capital_change)
    if e is None or ic is None:
        return NC("增量ROIC 缺少 ebit_change 或 invested_capital_change")
    if ic == 0:
        return NA("新增投入资本为0，增量ROIC 无意义")
    return OK(e / ic * 100, unit="pct")


# ============================================================
# 6) 现金质量 / 每股价值 / 生存力（core.yaml 引用）
# ============================================================

@formula("fcf_conversion")
def fcf_conversion(operating_cash_flow: Any, capital_expenditure: Any,
                   net_income: Any) -> MetricResult:
    """FCF/净利润 = (OCF − capex) / 净利润（利润能沉淀多少真钱）。"""
    ocf, capex, ni = _f(operating_cash_flow), _f(capital_expenditure), _f(net_income)
    if ocf is None or ni is None:
        return NC("FCF/净利润 缺少 operating_cash_flow 或 net_income")
    if ni == 0:
        return NA("净利润为0，FCF/净利润 无意义")
    fcf = ocf - (capex or 0)
    return OK(fcf / ni)


@formula("accruals")
def accruals(net_income: Any, operating_cash_flow: Any, total_assets: Any) -> MetricResult:
    """应计项占比 = (净利润 − 经营现金流) / 总资产（%），盈余质量经典信号。"""
    ni, ocf, ta = _f(net_income), _f(operating_cash_flow), _f(total_assets)
    if ni is None or ocf is None or ta is None:
        return NC("应计项缺少 net_income/operating_cash_flow/total_assets")
    if ta == 0:
        return NA("总资产为0，应计项占比无意义")
    return OK((ni - ocf) / ta * 100, unit="pct")


@formula("owner_earnings")
def owner_earnings(net_income: Any, depreciation_amortization: Any,
                   maintenance_capex: Any, working_capital_change: Any) -> MetricResult:
    """所有者收益 = 净利 + 折旧摊销 − 维持性capex − 增量营运资本（巴菲特口径）。"""
    ni = _f(net_income)
    if ni is None:
        return NC("所有者收益缺少 net_income")
    return OK(ni + (_f(depreciation_amortization) or 0)
              - (_f(maintenance_capex) or 0) - (_f(working_capital_change) or 0))


@formula("stdev")
def stdev(values: Sequence[Any]) -> MetricResult:
    """标准差（样本）。需要 ≥2 个数值。"""
    xs = _series(values)
    if len(xs) < 2:
        return NC("标准差需要 ≥2 个数值")
    return OK(statistics.stdev(xs), unit="pct")


@formula("coefficient_of_variation")
def coefficient_of_variation(values: Sequence[Any]) -> MetricResult:
    """变异系数 = 标准差 / 均值（%），衡量稳定性。"""
    xs = _series(values)
    if len(xs) < 2:
        return NC("变异系数需要 ≥2 个数值")
    mean = sum(xs) / len(xs)
    if mean == 0:
        return NA("均值为0，变异系数无意义")
    return OK(statistics.stdev(xs) / mean * 100, unit="pct")


@formula("net_debt_to_ebitda")
def net_debt_to_ebitda(total_debt: Any, cash: Any, ebitda: Any) -> MetricResult:
    """净债/EBITDA = (有息负债 − 现金) / EBITDA。EBITDA 非正 → 不适用。"""
    d, c, e = _f(total_debt), _f(cash), _f(ebitda)
    if d is None or e is None:
        return NC("净债/EBITDA 缺少 total_debt 或 ebitda")
    if e <= 0:
        return NA("EBITDA 非正，净债/EBITDA 无意义")
    return OK((d - (c or 0)) / e)


@formula("buyback_value_creation")
def buyback_value_creation(common_stock_repurchased: Any, avg_price: Any,
                           intrinsic_value_estimate: Any) -> MetricResult:
    """回购价值创造：回购价 vs 内在价值，低于内在价值回购创造价值。

    返回 {per_share_spread_pct, shares_repurchased, value_created}。
    高位回购（回购价 > 内在价值）= 摧毁价值（value_created 为负）。
    """
    spent, ap, iv = _f(common_stock_repurchased), _f(avg_price), _f(intrinsic_value_estimate)
    if spent is None or ap in (None, 0) or iv is None:
        return NC("回购价值创造缺少 common_stock_repurchased/avg_price/intrinsic_value_estimate")
    shares = spent / ap
    created = (iv - ap) * shares
    return OK({
        "per_share_spread_pct": round((iv - ap) / ap * 100, 2),
        "shares_repurchased": round(shares, 4),
        "value_created": round(created, 2),
    }, provenance={"caliber": "(内在价值−回购价)×回购股数；负值=摧毁价值"})


# ============================================================
# 7) 半核心库：银行/公用特有口径（G1b/G2a 引用）
# ============================================================

@formula("interest_coverage")
def interest_coverage(ebit: Any, interest_expense: Any) -> MetricResult:
    """利息保障倍数 = EBIT / 利息支出。

    口径陷阱（茅台踩过）：A股「财务费用」可能为负（利息收入>支出，如净现金公司），
    此时利息支出≈0，公司几乎无偿债压力 → 应判「无偿债压力」而非算出负倍数打危险红旗。
    """
    e, ie = _f(ebit), _f(interest_expense)
    if e is None or ie is None:
        return NC("利息保障倍数缺少 ebit 或 interest_expense")
    # 财务费用≤0：净利息收入，无有息负担，倍数无意义（不是危险，是极安全）
    if ie <= 0:
        return NA(f"利息支出为{ie:.0f}（≤0，净利息收入/无有息负担），"
                  "利息保障无意义——属净现金公司，偿债无压力")
    return OK(e / ie, unit="x",
              provenance={"caliber": "EBIT/利息支出；财务费用≤0时判NA（净现金公司）"})


@formula("passthrough")
def passthrough(v: Any) -> MetricResult:
    """透传：指标本身就是披露比率（如资本充足率），无需计算。"""
    x = _f(v)
    if x is None:
        return NC("passthrough 输入缺失")
    return OK(x)


@formula("nim")
def nim(net_interest_income: Any, average_interest_earning_assets: Any) -> MetricResult:
    """净息差 = 净利息收入 / 平均生息资产（%，银行核心盈利指标）。"""
    nii, aea = _f(net_interest_income), _f(average_interest_earning_assets)
    if nii is None or aea is None:
        return NC("净息差缺少 net_interest_income 或 average_interest_earning_assets")
    if aea == 0:
        return NA("平均生息资产为0，净息差无意义")
    return OK(nii / aea * 100, unit="pct")


@formula("dupont_bank")
def dupont_bank(net_income: Any, average_total_assets: Any,
                average_total_equity: Any) -> MetricResult:
    """ROE 杜邦分解（银行版）：ROE = ROA × 权益乘数。返回三个分量。"""
    ni, ta, eq = _f(net_income), _f(average_total_assets), _f(average_total_equity)
    if ni is None or ta is None or eq is None:
        return NC("ROE杜邦分解缺少 net_income/average_total_assets/average_total_equity")
    if ta == 0 or eq == 0:
        return NA("总资产或权益为0，杜邦分解无意义")
    roa = ni / ta
    mult = ta / eq
    return OK({
        "roe": round(ni / eq * 100, 2),
        "roa": round(roa * 100, 2),
        "equity_multiplier": round(mult, 2),
    }, unit="pct", provenance={"caliber": "ROE=ROA×权益乘数"})


@formula("utilization_rate")
def utilization_rate(actual_output: Any, capacity: Any) -> MetricResult:
    """产能利用率 = 实际产出 / 产能（%）。"""
    a, c = _f(actual_output), _f(capacity)
    if a is None or c is None:
        return NC("产能利用率缺少 actual_output 或 capacity")
    if c == 0:
        return NA("产能为0，产能利用率无意义")
    return OK(a / c * 100, unit="pct")


@formula("weighted_funding_cost")
def weighted_funding_cost(interest_expense: Any, total_debt: Any) -> MetricResult:
    """加权融资成本 = 利息支出 / 有息负债总额（%，须低于准许收益率）。"""
    ie, td = _f(interest_expense), _f(total_debt)
    if ie is None or td is None:
        return NC("加权融资成本缺少 interest_expense 或 total_debt")
    if td == 0:
        return NA("有息负债为0，加权融资成本无意义")
    return OK(ie / td * 100, unit="pct")


@formula("dividend_coverage")
def dividend_coverage(operating_cash_flow: Any, interest_expense: Any,
                      maintenance_capex: Any, total_dividends_paid: Any) -> MetricResult:
    """股息覆盖率 = OCF / (利息 + 维持性capex + 股息)，>1.5 稳健。"""
    ocf, ie, mc, dp = (_f(operating_cash_flow), _f(interest_expense),
                       _f(maintenance_capex), _f(total_dividends_paid))
    if ocf is None or dp is None:
        return NC("股息覆盖率缺少 operating_cash_flow 或 total_dividends_paid")
    burden = (ie or 0) + (mc or 0) + dp
    if burden <= 0:
        return NA("(利息+维持性capex+股息) 为0，股息覆盖率无意义")
    return OK(ocf / burden)


# ============================================================
# 8) 估值辅助（step7 交叉验证）
# ============================================================

@formula("pe_percentile")
def pe_percentile(pe_history: Sequence[Any], current_pe: Any) -> MetricResult:
    """当前 PE 在历史 PE 序列中的分位（0~100，越小越便宜）。"""
    hist = _series(pe_history)
    cur = _f(current_pe)
    if len(hist) < 5 or cur is None:
        return NC("PE 分位需 ≥5 个历史点且当前 PE 非空")
    below = sum(1 for h in hist if h <= cur)
    return OK(below / len(hist) * 100, unit="pct")


@formula("reverse_dcf")
def reverse_dcf(current_price: Any, fcf_series: Sequence[Any], shares: Any,
                discount_rate: Any) -> MetricResult:
    """反推 DCF：当前价格隐含的永续增速 g（简化单阶段模型）。

    注意：这是"市场预期反推"，与 calc.valuation_dcf（显式预测期折现）是两回事。
    """
    p, shares_v, r = _f(current_price), _f(shares), _f(discount_rate)
    fcf_list = _series(fcf_series)
    if p is None or r in (None, 0) or shares_v in (None, 0) or not fcf_list:
        return NC("反推DCF 缺少 current_price/fcf_series/shares/discount_rate")
    fcf_ps = fcf_list[-1] / shares_v
    if p + fcf_ps == 0:
        return NA("股价与每股FCF 之和为0，隐含增速无解")
    g = (p * r - fcf_ps) / (p + fcf_ps)
    return OK({
        "implied_growth": round(g * 100, 2),
        "fcf_ps": round(fcf_ps, 4),
        "price": round(p, 2),
        "discount_rate": round(r * 100, 1),
    }, provenance={"caliber": "P=FCF(1+g)/(r−g) 反解 g"})


# ============================================================
# 9) 近似推导库 —— 数据源缺字段时用相邻字段补算（DEGRADED 的正式用法）
#    供两处使用：① base_pack 引擎在 _map_fields 里内联同口径推导并登记近似；
#    ② 沙箱 LLM 在 run_code 里 import 本库做补算。返回一律标 DEGRADED，
#    结论层据此降权解读，绝不与精确值同等对待。
# ============================================================

@formula("ebit_approx")
def ebit_approx(operating_profit: Any, interest_expense: Any,
                total_profit: Any = None) -> MetricResult:
    """EBIT 近似 = 营业利润 + 财务费用（A股财报最常见近似）。

    无营业利润时退化为「利润总额 + 财务费用」。数据源未直接披露 EBIT 时用，
    返回 DEGRADED（口径近似，解读降权）。
    """
    op, ie, tp = _f(operating_profit), _f(interest_expense), _f(total_profit)
    # 财务费用为负（净利息收入）时视为无利息支出，避免 EBIT 被调小失真
    ie = max(ie, 0.0) if ie is not None else None
    if op is not None and ie is not None:
        return DEGRADED(
            op + ie,
            "EBIT 用「营业利润+财务费用」近似（非直接披露）",
            provenance={"caliber": "营业利润+财务费用(负值按0)"},
        )
    if tp is not None and ie is not None:
        return DEGRADED(
            tp + ie,
            "EBIT 用「利润总额+财务费用」近似（非直接披露）",
            provenance={"caliber": "利润总额+财务费用(负值按0)"},
        )
    return NC("EBIT 近似缺少 operating_profit/interest_expense（或 total_profit）")


@formula("ebitda_approx")
def ebitda_approx(ebit: Any, depreciation_amortization: Any = None) -> MetricResult:
    """EBITDA 近似 = EBIT + 折旧摊销；无折旧摊销时以 EBIT 退化（DEGRADED）。"""
    e = _f(ebit)
    if e is None:
        return NC("EBITDA 近似缺少 ebit")
    dep = _f(depreciation_amortization)
    if dep is None:
        return DEGRADED(e, "EBITDA 无折旧摊销，以 EBIT 退化近似", unit="currency",
                        provenance={"caliber": "EBIT（缺折旧摊销退化）"})
    return DEGRADED(e + dep, "EBITDA = EBIT + 折旧摊销（近似）", unit="currency",
                    provenance={"caliber": "EBIT+折旧摊销"})


@formula("ocf_approx")
def ocf_approx(net_income: Any, depreciation_amortization: Any) -> MetricResult:
    """经营现金流近似 = 净利润 + 折旧摊销（间接法第一近似，忽略营运资本变动）。

    仅当数据源没有 OCF 时用；涉及盈余质量（OCF/净利润）的判断须降权。
    """
    ni, dep = _f(net_income), _f(depreciation_amortization)
    if ni is None or dep is None:
        return NC("OCF 近似缺少 net_income 或 depreciation_amortization")
    return DEGRADED(
        ni + dep,
        "OCF 用净利润+折旧摊销近似（忽略营运资本变动）",
        provenance={"caliber": "净利润+折旧摊销（间接法第一近似）"},
    )


# ============================================================
# 索引：REGISTRY → (name, 一行口径)。供 run_code 空调用返回给 LLM。
# ============================================================

def formula_index() -> list[dict]:
    return [
        {"name": k, "doc": (v.__doc__ or "").strip().splitlines()[0] if v.__doc__ else ""}
        for k, v in sorted(REGISTRY.items())
    ]


def available_trusted_formulas() -> list[str]:
    """LLM 写脚本前先看这里有哪些可信函数，能调现成的就别自己手写。"""
    return sorted(REGISTRY.keys())


# 公开面：沙箱里 `from formulas_core import *` 拿到的名字
__all__ = [
    "MetricResult", "Status", "OK", "NA", "NC", "DEGRADED",
    "REGISTRY", "formula", "formula_index", "available_trusted_formulas",
    "roic", "quarterly_from_cumulative", "ttm_from_cumulative",
    "normalized_earnings", "normalized_pe", "three_scenario", "scenario_valuation",
    "ratio", "sum_components", "spread", "yoy_growth", "cagr", "incremental_roic",
    "fcf_conversion", "accruals", "owner_earnings", "stdev",
    "coefficient_of_variation", "net_debt_to_ebitda", "buyback_value_creation",
    "passthrough", "nim", "dupont_bank", "utilization_rate",
    "weighted_funding_cost", "dividend_coverage", "pe_percentile", "reverse_dcf",
    "ebit_approx", "ebitda_approx", "ocf_approx",
]
