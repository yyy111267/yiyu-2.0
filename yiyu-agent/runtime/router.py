"""
意图路由器 - 根据用户输入自动匹配 Skill。

支持三种模式：
1. 显式指定：用户在 API 请求里带 skill_name → 直接用，不走路由。
2. LLM 语义识别：调用 LLM 分类意图（主力，理解复杂语义，如"茅台值得买吗"）。
3. 关键词匹配：LLM 失败/低置信时的零成本兜底。

路由优先级：显式 > LLM 语义（confidence≥阈值）> 关键词 > 默认兜底（chat 闲聊）。

意图类别：
- stock_research → deep-research：带计算指标的标的研究
- knowledge_qa → knowledge_qa：通用投研知识问答（不带计算，纯 LLM + RAG）
- quick-screen / private-company / holdings-track / trade-review：既有业务类
- chat（skill=None）：闲聊/其他（工具集为空，避免误用计算工具）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RouteResult:
    """路由结果"""
    skill: Optional[str]  # 命中的 skill id；None 表示纯闲聊
    matched_by: str = ""  # 命中方式：explicit / llm / keyword / default
    confidence: float = 0.0


# 关键词规则（按命中优先级排序，先匹配先用）
# 每条： (编译后的正则, skill_id, 置信度)
_RULES: list[tuple[re.Pattern, str, float]] = [
    # 深度研究：明确要"研究/分析"+ 标的
    (re.compile(r"(深度|详细|认真)?\s*(研究|分析|调研).{0,6}(腾讯|阿里|茅台|比亚迪|[0-9]{6}|[A-Z]{2,5})", re.IGNORECASE),
     "deep-research", 0.9),
    (re.compile(r"(研究|分析)一下.{0,10}(股票|标的|公司)", re.IGNORECASE), "deep-research", 0.8),
    (re.compile(r"(六关|四大师|镜子测试|护城河|安全边际)", re.IGNORECASE), "deep-research", 0.85),

    # 快速筛选
    (re.compile(r"(快速)?(筛选|筛选一下|选股|过滤).{0,10}(标的|股票|公司)", re.IGNORECASE), "quick-screen", 0.85),
    (re.compile(r"(低估值|高股息|破净).{0,6}(的|有哪些)", re.IGNORECASE), "quick-screen", 0.75),

    # 未上市公司
    (re.compile(r"(未上市|一级市场|pre-?ipo|创业公司|初创)", re.IGNORECASE), "private-company", 0.85),

    # 持仓跟踪
    (re.compile(r"(我的|当前)?\s*(持仓|组合|仓位)(怎么样|情况|状态)?", re.IGNORECASE), "holdings-track", 0.85),
    (re.compile(r"(加仓|减仓|清仓|调仓)", re.IGNORECASE), "holdings-track", 0.7),

    # 交易复盘
    (re.compile(r"(复盘|回顾|总结).{0,10}(交易|买卖|操作)", re.IGNORECASE), "trade-review", 0.85),
    (re.compile(r"(上次|最近).{0,4}(买|卖).{0,6}对不对", re.IGNORECASE), "trade-review", 0.7),
]


# LLM 语义识别置信度阈值：低于则降级到关键词
_LLM_CONFIDENCE_THRESHOLD = 0.6

# LLM 分类器可输出的 skill 白名单（防止 LLM 编造不存在的 skill）
_LLM_SKILLS = {
    "deep-research", "quick-screen", "private-company",
    "holdings-track", "trade-review", "knowledge_qa",
}

LLM_ROUTER_PROMPT = """你是投研助手的目的分类器。判断用户输入属于哪一类，只输出 JSON，不要输出其他文字。

类别定义（skill 字段取括号内的值）：
- deep-research：对某只股票/公司做深度研究，需要财报、指标计算、估值、护城河等分析
  （如"分析一下腾讯"、"茅台值得买吗"、"XX的基本面怎么样"）
- quick-screen：按条件快速筛选股票/标的（如"筛选低估值股票"）
- private-company：未上市公司的研究（如"XX还没上市，分析下"）
- holdings-track：持仓/组合跟踪（如"我的持仓怎么样"、"帮我看看仓位"）
- trade-review：交易复盘（如"复盘我上次的操作"）
- knowledge_qa：通用投资知识问答，不针对特定公司做深度研究
  （如"什么是ROE"、"DCF怎么理解"、"怎么看护城河"）
- chat：闲聊、问候或其他（如"你好"、"谢谢"、"今天天气"）

输出格式：
{{"skill": "<类别>", "confidence": 0.0-1.0, "reasoning": "<一句话说明>"}}"""


def route(user_message: str, explicit_skill: Optional[str] = None) -> RouteResult:
    """
    同步关键词路由（零成本兜底）。显式指定优先，然后关键词匹配。

    Args:
        user_message: 用户原始输入
        explicit_skill: 用户/API 显式指定的 skill（优先级最高）

    Returns:
        RouteResult: 命中的 skill（可能为 None 表示闲聊）
    """
    # 1. 显式指定优先
    if explicit_skill:
        logger.debug(f"路由：显式指定 {explicit_skill}")
        return RouteResult(skill=explicit_skill, matched_by="explicit", confidence=1.0)

    # 2. 关键词匹配
    for pattern, skill_id, conf in _RULES:
        if pattern.search(user_message):
            logger.debug(f"路由：关键词命中 {skill_id} (conf={conf})")
            return RouteResult(skill=skill_id, matched_by="keyword", confidence=conf)

    # 3. 兜底：纯闲聊
    logger.debug("路由：未命中，默认闲聊")
    return RouteResult(skill=None, matched_by="default", confidence=0.0)


async def route_async(
    llm_client,
    user_message: str,
    explicit_skill: Optional[str] = None,
    history: Optional[list[dict]] = None,
) -> RouteResult:
    """
    异步路由：LLM 语义识别为主，关键词为兜底。

    优先级：显式 > LLM 语义（confidence≥阈值）> 关键词 > 闲聊。
    LLM 调用失败或低置信时自动降级到关键词路由。

    Args:
        llm_client: LLMClient 实例（需支持 chat_json）
        user_message: 用户原始输入
        explicit_skill: 用户/API 显式指定的 skill
        history: 最近对话历史（可选，[{role, content}]）

    Returns:
        RouteResult
    """
    # 1. 显式指定优先（同步语义）
    if explicit_skill:
        return route(user_message, explicit_skill)

    # 2. LLM 语义识别
    try:
        ctx = ""
        if history:
            ctx = "\n最近对话:\n" + "\n".join(
                f"  {m.get('role', 'user')}: {str(m.get('content', ''))[:200]}"
                for m in history[-3:]
            )
        user = f"{ctx}\n用户消息: {user_message}".strip() if ctx else f"用户消息: {user_message}"
        data = await llm_client.chat_json(
            system=LLM_ROUTER_PROMPT,
            user=user,
            temperature=0.1,
        )
        skill = str(data.get("skill", "")).strip().lower()
        confidence = float(data.get("confidence", 0.0) or 0.0)

        if skill in _LLM_SKILLS and confidence >= _LLM_CONFIDENCE_THRESHOLD:
            logger.debug(f"路由：LLM 命中 {skill} (conf={confidence:.2f})")
            return RouteResult(skill=skill, matched_by="llm", confidence=confidence)

        logger.debug(
            f"路由：LLM 结果未采用 skill={skill!r} conf={confidence:.2f}，降级关键词"
        )
    except Exception as e:
        logger.warning(f"路由：LLM 语义识别失败，降级关键词: {e}")

    # 3. 关键词兜底
    return route(user_message)


# 可路由的 skill 列表（供 UI 展示 / 健康检查）
def available_skills() -> list[str]:
    """返回路由器认识的所有 skill id。"""
    return sorted({skill for _, skill, _ in _RULES} | {"knowledge_qa"})
