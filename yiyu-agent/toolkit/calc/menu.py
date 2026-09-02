"""
calc.menu —— 商业模式指标参考菜单（不是必算清单，是给 LLM 的点菜单）。

════════════════════════════════════════════════════════════════════
定位：与已删除的旧 base_pack 的根本区别
════════════════════════════════════════════════════════════════════
旧 base_pack.yaml 是「必算清单」——不管三七二十一把 14+5+4 个指标全算
（该工具与这批 yaml 已随 bus_router 重构删除）。
本工具是「参考菜单」——只告诉 LLM：这个商业模式「通常」关注哪些指标、
各自看什么、有什么口径陷阱，然后由 LLM 自己判断「这家公司这个时点该点哪几个」，
再用 calc.metric 逐个算。

一句话：菜单是「餐厅推荐菜」，不是「强制套餐」。点不点、点几个，LLM 说了算。

════════════════════════════════════════════════════════════════════
为什么菜单里要带「陷阱提示」而不只是指标名
════════════════════════════════════════════════════════════════════
实测发现：茅台 CCC 算出 1407 天，档位判「偏弱」——但这其实是白酒护城河
（基酒窖藏5年+占用经销商预付款），不是缺陷。写死的 band 会误导 LLM。
所以菜单对每个指标标注「解读要点/陷阱」，让 LLM 结合行业常识判断，
而不是盲信一个档位标签。
"""

from __future__ import annotations

from typing import Any

from toolkit.base import ReadOnlyTool, ToolSchema

# ════════════════════════════════════════════════════════════════
# 每个商业模式的「点菜单」：核心生死变量 + 推荐指标（含解读要点/陷阱）
# 指标 id 与 bus_router 各组 yaml 的 metric id 对齐，可直接喂给 calc.metric。
# note 字段 = 解读要点/陷阱，专治「写死档位误导」。
# ════════════════════════════════════════════════════════════════
_MENU: dict[str, dict[str, Any]] = {
    "core": {
        "name": "通用价值投资底座",
        "one_liner": "任何非金融公司都该看的价值投资核心（资本回报/现金质量/生存力/可预测性）",
        "kill_variables": ["ROIC能否持续覆盖资本成本", "利润是不是真金白银"],
        "metrics": [
            {"id": "roic", "看什么": "资本回报的核心，>15%良好>25%优秀",
             "note": "口径已冻结(NOPAT/投入资本)，别用净利润/总资产乱算"},
            {"id": "ocf_to_net_income", "看什么": "利润含金量，长期<1警惕",
             "note": "单年<1不一定坏(如加大预收/存货)，看趋势"},
            {"id": "fcf_to_net_income", "看什么": "扣资本开支后利润沉淀多少真钱", "note": ""},
            {"id": "accruals_ratio", "看什么": "应计项占比，越高盈余质量越差", "note": "财务操纵经典信号"},
            {"id": "net_debt_to_ebitda", "看什么": "还债需几年，>4危险",
             "note": "净现金公司为负=极安全，别误读"},
            {"id": "interest_coverage", "看什么": "EBIT够不够付利息",
             "note": "★净现金公司财务费用为负→判NA(无偿债压力)，不是危险"},
            {"id": "roic_volatility_10y", "看什么": "ROIC十年波动，>8不可预测",
             "note": "波动过大→安全边际无法定义→降级观察(能力圈闸门)"},
        ],
    },
    "G1a": {
        "name": "品牌消费",
        "one_liner": "靠品牌收溢价的复购生意，轻资产、经销商预付款",
        "kill_variables": ["提价能力(还有没有定价权)", "渠道健康度(有没有压货)"],
        "extends": "core",
        "metrics": [
            {"id": "contract_liability_yoy", "看什么": "经销商打款意愿，领先营收1-2季",
             "note": "白酒最强领先指标；连续下滑要警惕需求转弱"},
            {"id": "cash_conversion_cycle", "看什么": "对上下游话语权",
             "note": "★白酒存货周转天数天然很高(基酒窖藏5年)，CCC大不等于差；"
                     "重点看应收(DSO)≈0+占用经销商预付款，这才是护城河。别盲信'偏弱'档位"},
            {"id": "price_volume_split", "看什么": "提价驱动>放量>靠铺货",
             "note": "需两期营收+销量；销量数据结构化源常缺，可能要查年报"},
            {"id": "roic_ex_cash", "看什么": "剔除超额现金的真实经营回报", "note": "品牌消费常年账上巨额现金，不剔会低估ROIC"},
            {"id": "sales_expense_efficiency", "看什么": "每1元销售费用换多少收入(品牌自然拉力)",
             "note": "单期会退化DEGRADED，理想要增量口径"},
        ],
        "valuation": "DCF+ROIC/g 主，正常化PE、股息率分位交叉验证",
    },
    "G1b": {
        "name": "受监管公用事业",
        "one_liner": "政府定价、类债资产，准许收益+高股息",
        "kill_variables": ["准许ROE与实际ROE差", "股息可持续性"],
        "extends": "core",
        "metrics": [
            {"id": "capex_intensity", "看什么": "★公用事业capex越高越好(在扩张资产基数RAB)",
             "note": "与品牌消费相反！FCF为负常是健康的(在建电厂/管网)，别套用'FCF为负=烧钱'"},
            {"id": "dividend_coverage", "看什么": "OCF够不够覆盖利息+维持capex+股息，>1.5稳健", "note": ""},
            {"id": "roic", "看什么": "应接近准许收益率而非越高越好",
             "note": "★ROIC远超准许收益反而可能是监管要压价的信号，别当'越高越好'"},
        ],
        "valuation": "DDM(股息折现)，不是DCF；锚股息率-10Y国债利差",
    },
    "G2a": {
        "name": "银行",
        "one_liner": "赚息差、风险后置的信用中介",
        "kill_variables": ["净息差NIM", "资产质量(不良/拨备)"],
        "extends": None,   # ★金融整体替换 core：毛利率/FCF/capex 概念失效
        "metrics": [
            {"id": "nim", "看什么": "净息差，银行核心盈利", "note": "★不要对银行算 ROIC/FCF/毛利率，会计结构不同，无意义"},
            {"id": "dupont_bank", "看什么": "ROE杜邦=ROA×权益乘数", "note": "看盈利是靠真本事(ROA)还是加杠杆(权益乘数)"},
        ],
        "valuation": "PB-ROE；资产质量触发先查再估值。绝不用简单PE",
        "warning": "extends=null：银行禁用 core 的 ROIC/FCF/毛利率类指标",
    },
    "G2b": {
        "name": "保险",
        "one_liner": "利润来自精算假设，长久期负债",
        "kill_variables": ["NBV及其增速", "精算假设是否被美化"],
        "extends": None,
        "metrics": [
            {"id": "nbv_margin", "看什么": "新业务价值率(寿险)", "note": "结构化源常缺，多在年报/业绩发布"},
            {"id": "combined_ratio", "看什么": "综合成本率(财险)，<100才承保盈利", "note": ""},
        ],
        "valuation": "寿险P/EV、财险PB+综合成本率；★绝不用PE。先查假设再谈估值",
        "warning": "extends=null：保险禁用 core 的 ROIC/FCF/毛利率类指标",
    },
    "G3": {
        "name": "周期",
        "one_liner": "价格外生、随周期波动的价格接受者",
        "kill_variables": ["当前处于景气什么位置", "成本曲线分位"],
        "extends": "core",
        "metrics": [
            {"id": "normalized_earnings", "看什么": "★全周期正常化盈利(禁单年)",
             "note": "周期股顶部低PE最危险(利润顶点)、底部高PE可能是机会，禁止单年利润外推"},
            {"id": "roic", "看什么": "要看全周期平均，不看单年", "note": "单年ROIC对周期股无信息量"},
        ],
        "valuation": "全周期正常化PE / PB vs重置成本；禁单年外推",
    },
    "G4": {
        "name": "平台/软件",
        "one_liner": "网络效应/订阅、边际成本趋零",
        "kill_variables": ["单位经济UE是否成立", "留存(NDR/NRR)"],
        "extends": "core",
        "metrics": [
            {"id": "roic", "看什么": "成熟平台看资本回报", "note": "早期平台可能不适用，看单位经济"},
            {"id": "sbc_to_revenue", "看什么": "★股权激励占收入，SaaS常被低估的隐形成本",
             "note": "很多公司靠加回SBC美化Non-GAAP利润，>5%警惕"},
        ],
        "valuation": "PS/PEG + 单位经济；多业务必须SOTP分部；GMV≠收入",
    },
    "G6": {
        "name": "高端制造",
        "one_liner": "重资产、看产能利用率的制造",
        "kill_variables": ["产能利用率(毛利率第一解释)", "订单出货比book-to-bill"],
        "extends": "core",
        "metrics": [
            {"id": "utilization_rate", "看什么": "产能利用率，重资产盈利第一解释", "note": "产量/设计产能，源常缺需查年报"},
            {"id": "capex_intensity", "看什么": "重资产capex重，看扩产纪律", "note": "折旧刚性，产能过剩时最痛"},
            {"id": "roic", "看什么": "重资产周期看全周期回报", "note": ""},
        ],
        "valuation": "周期调整PE / EV/EBITDA，下行看PB",
    },
}


class MenuTool(ReadOnlyTool):
    """商业模式指标参考菜单（点菜单，非必算清单）。"""

    schema = ToolSchema(
        name="calc.menu",
        description=(
            "查某商业模式「通常关注哪些指标」——这是给你参考的点菜单，不是强制套餐。\n"
            "你先看菜单了解该模式的生死变量+推荐指标+每个指标的解读要点/陷阱，"
            "再自己判断「这家公司这个时点该重点看哪几个」，然后用 calc.metric 逐个算。\n"
            "★重点看每个指标的 note（解读陷阱），别盲信档位标签——"
            "例如白酒 CCC 天然很高不代表差、净现金公司利息保障为负不代表危险、"
            "公用事业 capex 高反而是好事。\n"
            "传 group 看单个模式；不传看全部模式概览。\n"
            "可用 group：G1a品牌消费/G1b公用/G2a银行/G2b保险/G3周期/G4平台/G6制造/core通用底座。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "group": {
                    "type": "string",
                    "description": "商业模式分组(G1a/G1b/G2a/G2b/G3/G4/G6/core)。不传返回全部概览。",
                },
            },
        },
        read_only=True,
        max_chars=6000,
    )

    async def execute(self, group: str = "", **kwargs: Any) -> dict:
        group = (group or "").strip()
        if not group:
            return {
                "success": True,
                "usage": "这是点菜单不是必算清单——按公司实际情况点选指标，再用 calc.metric 算",
                "modes": {g: {"name": m["name"], "one_liner": m["one_liner"],
                              "kill_variables": m.get("kill_variables", [])}
                          for g, m in _MENU.items()},
            }
        m = _MENU.get(group)
        if not m:
            return {"success": False, "error": f"未知 group: {group}",
                    "available": sorted(_MENU.keys())}
        return {"success": True, "group": group, **m,
                "reminder": "这是参考菜单——按公司实际点选，重点看每个指标的 note(解读陷阱)，"
                            "别盲信档位。用 calc.metric(symbol, metric_id, group) 逐个算。"}


MENU_TOOLS: list[ReadOnlyTool] = [MenuTool()]
