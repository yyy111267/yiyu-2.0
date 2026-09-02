"""产品边界与固定兜底话术。

这里放代码级边界，不依赖 prompt 自觉执行。路由识别到 out_of_scope 或命中
尚未交付的 skill 时，API 直接返回这些模板，避免进入研究循环后产生超范围承诺。
"""

from __future__ import annotations

OUT_OF_SCOPE = "out_of_scope"

BOUNDARY_TEMPLATES: dict[str, str] = {
    "price_prediction": (
        "我不能预测短期涨跌、给目标价或替你做买卖决策。"
        "我可以改为帮你做基本面研究、风险拆解、估值假设核对和证伪条件梳理。"
    ),
    "trade_decision": (
        "我不能直接替你决定买入、卖出、加仓或清仓。"
        "我可以帮你把投资理由、反方证据、关键风险和需要验证的数据列清楚。"
    ),
    "unsupported_task": (
        "这个能力当前还没有交付，暂时不能承诺执行。"
        "当前可用的是标的研究、投资知识问答和多业务公司 SOTP 分析。"
    ),
    "non_research": (
        "这个问题超出了当前投研助手的产品范围。"
        "我可以围绕公司基本面、财务质量、行业竞争、估值假设和风险证伪来帮你分析。"
    ),
}


def boundary_message(reason: str = "non_research") -> str:
    """返回稳定的产品边界话术。"""
    return BOUNDARY_TEMPLATES.get(reason) or BOUNDARY_TEMPLATES["non_research"]

