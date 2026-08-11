"""实体识别工具封装 —— 供 LLM 调用的 `entity.resolve`。

流程定位：深度研究的第 1 步。用户输入 → 标准化标的实体（symbol + name + market + currency）。
歧义时返回候选列表（needs_disambiguation=true），LLM 应向用户确认，绝不擅自替用户选择。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from toolkit.base import ReadOnlyTool, Tool, ToolSchema

from .classify import classify_company
from .resolver import resolve_entity

logger = logging.getLogger(__name__)

# 单例缓存：避免每次 tool call 都重建 MarketData（仅 verify_snapshot=True 时使用）
_market_data_instance: Optional[Any] = None


def _get_market_data():
    global _market_data_instance
    if _market_data_instance is None:
        from core.config import settings
        from toolkit.market.market import MarketData

        _market_data_instance = MarketData(settings)
    return _market_data_instance


class EntityResolveTool(ReadOnlyTool):
    """实体识别工具：名称/代码 → 标准化标的。"""

    schema = ToolSchema(
        name="entity.resolve",
        description=(
            "把用户提到的公司/股票名称或代码解析为唯一标的实体（标准化代码+名称+市场+币种）。"
            "支持：A股(600519/贵州茅台)、港股(0700.HK/腾讯)、美股(AAPL/苹果)。"
            "歧义时返回候选列表并 needs_disambiguation=true，此时必须向用户确认后再继续，"
            "不得擅自替用户选择。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "公司名称、股票代码或别名，如 '贵州茅台' / '600519' / '腾讯' / 'AAPL'",
                },
                "verify_snapshot": {
                    "type": "boolean",
                    "description": "是否拉取实时快照核对名称（需要网络，稍慢；默认 false）",
                    "default": False,
                },
                "use_llm_escalation": {
                    "type": "boolean",
                    "description": (
                        "是否在边界情况花少量 LLM 成本自动决策："
                        "① 多候选但投研常识无歧义（如'比亚迪'默认A股）自动选默认；"
                        "② 内置表未命中（新股如'智谱'）联网找代码并自动收录。"
                        "默认 false（纯规则零成本）；用户问新股或歧义标的时可开启。"
                    ),
                    "default": False,
                },
            },
            "required": ["query"],
        },
        read_only=True,
        max_chars=3000,
        timeout_seconds=120,  # 首次构建 A股索引可能需数十秒
    )

    async def execute(self, query: str, verify_snapshot: bool = False,
                      use_llm_escalation: bool = False, **kwargs: Any) -> dict:
        if not query or not query.strip():
            return {"resolved": False, "needs_disambiguation": False,
                    "entity": None, "candidates": [], "raw_input": query,
                    "message": "输入为空"}
        market_data = _get_market_data() if (verify_snapshot or use_llm_escalation) else None
        result = await resolve_entity(query, verify_snapshot=verify_snapshot,
                                      use_llm_escalation=use_llm_escalation,
                                      market_data=market_data)
        return result.to_dict()


class ClassifyCompanyTool(ReadOnlyTool):
    """商业模式识别工具：标的 → 商业模式分组（决定加载哪套 skill 与估值模型）。"""

    schema = ToolSchema(
        name="company.classify",
        description=(
            "识别一个标的所属的商业模式分组（G1a 品牌消费 / G1b 公用事业 / G2a 银行 / "
            "G2b 保险 / G3 周期 / G4 平台软件 / G5 未盈利 / G6 硬件制造），"
            "并判断是否多业务公司。"
            "返回 {group, stage, confidence, by, needs_review, is_conglomerate, "
            "sotp_tier, reasoning}。"
            "路由规则：is_conglomerate=false → 加载对应 group 的单组 skill；"
            "is_conglomerate=true → 加载 sotp-multi-business skill（多业务 SOTP 分析），"
            "sotp_tier（1~4）是数据可得性档位初判，实际档位在分析时修正。"
            "在深度研究第一步（解析实体之后、取数之前）调用。"
            "结果自动缓存（90 天），同一标的再次调用直接返回。"
            "confidence 低于 0.7 或 needs_review=true 时，应对结论保持怀疑并提示用户复核。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码或公司名，如 '600519' / '600519.SH' / '贵州茅台' / 'AAPL'",
                },
                "skip_snapshot": {
                    "type": "boolean",
                    "description": (
                        "是否跳过快照取公司名（true 时直接凭代码+名称判定，避免港股/美股"
                        "快照耗时数十秒；默认 false）。纯代码输入且跳过时 L1 名称表不生效，"
                        "直接走 LLM 判定。"
                    ),
                    "default": False,
                },
            },
            "required": ["symbol"],
        },
        read_only=True,
        max_chars=2000,
        timeout_seconds=120,  # 缓存未命中时 LLM(+可能联网) 需数十秒
    )

    async def execute(self, symbol: str, skip_snapshot: bool = False, **kwargs: Any) -> dict:
        if not symbol or not str(symbol).strip():
            return {"symbol": "", "group": "other", "stage": "unknown",
                    "confidence": 0.0, "by": "none", "needs_review": True,
                    "reasoning": "输入为空"}
        try:
            result = await classify_company(str(symbol), skip_snapshot=skip_snapshot)
            return result.to_dict()
        except Exception as e:  # noqa: BLE001 - 工具层兜底，不中断流程
            logger.exception("company.classify 执行失败: %s", symbol)
            return {"symbol": str(symbol), "group": "other", "stage": "unknown",
                    "confidence": 0.0, "by": "error", "needs_review": True,
                    "reasoning": f"分类失败：{e}"}


ENTITY_TOOLS: list[Tool] = [EntityResolveTool(), ClassifyCompanyTool()]
