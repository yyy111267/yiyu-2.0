"""
意图路由器（PRD 5.1 两路径版）—— 只分两条路，分类越简单越准。

- light_answer  快问快答：不启动研究引擎直接回答（概念 / 单点数据 / 时效行情 / 观点闲聊）
- research_task 正式研究：启动完整研究流程（多步取证 + 留存结构化产物）

两条判定依据（皆真才建研究任务）：①是否需要多步工具编排 ②是否需要留存结构化产物。
一真一假的模糊输入默认轻回答——宁漏判为轻，不误判为重（误进重流程直接伤响应速度）。

识别模式（优先级）：
1. 显式指定：API 带 skill，优先；
2. 规则快速通道（route，零 LLM 零成本）：形态一眼可辨的输入直接判——
   多标的对比 / 概念解释 / 研究指令（动词+对象）/ 归因深挖 / 买卖判断；
3. LLM 语义（route_async）：模糊输入兜底，输出结构化两路径结果；
4. 默认：light_answer。

升级机制（PRD 5.1）：同标的连续查询 query_streak ≥ 3 → suggest_research=True，
轻回答附带「生成完整研究」入口；纯标的名也走轻回答并常驻该入口。

兼容性：保留 intent / skill / matched_by / confidence 字段供下游（loop / chat API）
平滑过渡；intent 是 light 路径的细分标签（knowledge / chitchat）与 research 路径的
主标签（research / compare），不再作为顶层分类。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from toolkit.entity.mention import extract_candidates

logger = logging.getLogger(__name__)

# 同标的连续查询达到该次数 → 主动建议升级完整研究（PRD 5.1）
RESEARCH_SUGGEST_STREAK = 3

# 路由结果枚举（PRD 5.1）
LIGHT_ANSWER = "light_answer"
RESEARCH_TASK = "research_task"


@dataclass
class RouteResult:
    """路由结果（PRD 5.1 结构 + 兼容字段）。"""
    route_result: str = ""                # light_answer / research_task（主结果）
    entity_candidates: list[str] = field(default_factory=list)  # 标的候选（透传 5.2）
    route_reason: str = ""                # 判定理由（结构化，非自由文本）
    query_streak: int = 0                 # 同标的连续查询计数
    suggest_research: bool = False        # 轻回答是否附带「生成完整研究」入口

    # ── 兼容字段（下游 loop / chat API 在用，平滑过渡期保留）──
    intent: str = ""                      # 细分标签：research/compare/knowledge/chitchat
    skill: Optional[str] = None           # 承接 skill id（deep-research / knowledge_qa / None）
    matched_by: str = ""                  # explicit / rule / llm / default
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "route_result": self.route_result,
            "entity_candidates": self.entity_candidates,
            "route_reason": self.route_reason,
            "query_streak": self.query_streak,
            "suggest_research": self.suggest_research,
            "intent": self.intent,
            "skill": self.skill,
            "matched_by": self.matched_by,
            "confidence": self.confidence,
        }


# ── 意图枚举与 skill 映射（兼容层，供显式指定与下游过渡）──────────────────
INTENT_DEFINITIONS: dict[str, str] = {
    "research": "对某个具体标的做深度研究/分析（财报、估值、护城河、买卖判断）",
    "compare": "对两个及以上标的做对比、比较、选择",
    "screen": "按量化条件筛选候选标的，不针对某个具体标的",
    "recommend": "让系统推荐/选股，未指定标的也无明确条件",
    "knowledge": "投资知识/概念/方法论问答，不针对具体公司做深度计算",
    "calculate": "明确的数字计算诉求（收益率、复利、估值速算等）",
    "portfolio": "用户自己的持仓/组合/仓位管理",
    "review": "回顾/复盘历史交易决策",
    "chitchat": "闲聊、问候、无关话题或其他无法归类",
}

_INTENT_TO_SKILL: dict[str, Optional[str]] = {
    "research": "deep-research",
    "compare": "deep-research",
    "screen": "quick-screen",
    "recommend": None,
    "knowledge": "knowledge_qa",
    "calculate": None,
    "portfolio": "holdings-track",
    "review": "trade-review",
    "chitchat": None,
}

_SKILL_TO_INTENT: dict[str, str] = {
    "deep-research": "research",
    "private-company": "research",
    "quick-screen": "screen",
    "knowledge_qa": "knowledge",
    "holdings-track": "portfolio",
    "trade-review": "review",
}

# LLM 分类置信度阈值：低于则降级到规则路由
_LLM_CONFIDENCE_THRESHOLD = 0.6


def resolve_skill(intent: str, user_message: str = "") -> Optional[str]:
    """把意图解析为可执行的 skill id（intent → skill 的薄层）。"""
    intent = (intent or "").strip().lower()
    if intent == "research" and _PRIVATE_COMPANY_PATTERN.search(user_message or ""):
        return "private-company"
    return _INTENT_TO_SKILL.get(intent)


# ── 规则快速通道（两路径，按优先级；零 LLM）─────────────────────────────

# ① 多标的对比（放最前，避免被研究动词抢走）—— 需多步编排 + 留存产物 → research_task
_COMPARE_RULES: list[tuple[re.Pattern, float]] = [
    (re.compile(r"(对比|比较|pk|vs\.?)\s*.{0,12}(更|哪个|谁|区别|优劣)", re.IGNORECASE), 0.85),
    (re.compile(r".{1,8}\s*(和|与|跟|vs\.?)\s*.{1,8}\s*(哪个|谁|对比|比较|区别)", re.IGNORECASE), 0.75),
]

# ② 概念解释 —— 直接作答 → light_answer（优先于研究动词：「什么是基本面分析」是概念）
_CONCEPT_RE = re.compile(
    r"(什么是|什么意思|什么叫|是什么|怎么(?:理解|看|用)|如何(?:理解|看|使用))",
    re.IGNORECASE,
)

# ③ 研究指令（动词 + 对象/标的）→ research_task
_RESEARCH_VERB_RE = re.compile(
    r"(深度|详细|认真|系统地)?\s*(研究|分析|调研|解读|评估|深挖|看看|看下)"
)

# ④ 归因深挖（为什么/什么原因）—— 需多源证据编排 → research_task
_CAUSAL_RE = re.compile(r"(为什么|什么原因|怎么回事|为啥|缘由|归因)")

# ⑤ 买卖判断 —— 需深度研究支撑 → research_task
_BUY_JUDGMENT_RE = re.compile(
    r"(值得买|该不该买|能不能买|值不值得|该买吗|可以买吗|能买吗|要不要买)"
)

# ⑥ 深度研究词 —— 明确的深研信号 → research_task
_DEEP_DIVE_RE = re.compile(
    r"(基本面怎么样|基本面如何|财报分析|深度分析|护城河|安全边际|内在价值|估值分析|生意质量)"
)

# 知识提示词（light 细分标签用，不影响两路径判定）
_KNOWLEDGE_HINT_RE = re.compile(
    r"(\b(?:roi|roe|pe|pb|dcf|roic)\b|复利|现金流折现|护城河|安全边际|内在价值|自由现金流)",
    re.IGNORECASE,
)

# 研究类标的属性：未上市/一级市场 → 用 private-company 技能承接
_PRIVATE_COMPANY_PATTERN = re.compile(
    r"((?:未|没|尚未|还未)\s*上市|拟上市|一级市场|pre-?ipo|创业公司|初创|startup|融资轮|天使轮|风投)",
    re.IGNORECASE,
)


def _classify_two_path(message: str,
                       candidates: list[str] | None = None) -> tuple[str, str, float]:
    """规则快速通道：消息 → (路径, 理由, 置信度)。零 LLM。

    置信约定：≥0.75 的判定在 route_async 中直接短路（不花 LLM 成本）。
    """
    for pattern, conf in _COMPARE_RULES:
        if pattern.search(message):
            return RESEARCH_TASK, "多标的对比：需多步编排与留存结构化产物", conf
    if _CONCEPT_RE.search(message):
        return LIGHT_ANSWER, "概念解释类：可直接作答", 0.85
    if _RESEARCH_VERB_RE.search(message):
        return RESEARCH_TASK, "明确研究指令（研究/分析动词 + 对象）", 0.85
    if _CAUSAL_RE.search(message):
        return RESEARCH_TASK, "归因深挖：需多源证据编排（财报/分部/行业）", 0.8
    if _BUY_JUDGMENT_RE.search(message):
        return RESEARCH_TASK, "买卖判断：需深度研究支撑，不裸答", 0.75
    if _DEEP_DIVE_RE.search(message):
        return RESEARCH_TASK, "深度研究信号（基本面/护城河/估值分析）", 0.8
    if candidates:
        # 纯标的指称（无任何研究/概念信号）：规则快速通道直判轻回答，零 LLM
        return LIGHT_ANSWER, "纯标的指称：轻回答（概况/询问关注点，常驻研究入口）", 0.8
    return LIGHT_ANSWER, "无研究信号，默认轻回答（宁轻勿重）", 0.5


def _light_intent(message: str) -> tuple[str, Optional[str]]:
    """light 路径细分标签（兼容下游 skill 选择）。"""
    if _CONCEPT_RE.search(message) or _KNOWLEDGE_HINT_RE.search(message):
        return "knowledge", "knowledge_qa"
    return "chitchat", None


def _to_intent_skill(route_result: str, message: str) -> tuple[str, Optional[str]]:
    """路径 → (细分 intent, skill) 兼容映射。"""
    if route_result == RESEARCH_TASK:
        if any(p.search(message) for p, _ in _COMPARE_RULES):
            return "compare", resolve_skill("compare", message)
        return "research", resolve_skill("research", message)
    return _light_intent(message)


def _next_streak(candidates: list[str], current_entity: Optional[dict],
                 query_streak: int) -> int:
    """同标的连续查询计数（PRD 5.1 升级机制）。

    - 本轮出现与 current_entity 不同的新标的 → 计数重置为 1；
    - 同标的（候选命中当前实体）或延续话题（无新指称）→ 计数 +1；
    - 无 current_entity：有候选从 1 起算，无候选为 0。
    """
    if not current_entity:
        return 1 if candidates else 0
    cur = {str(current_entity.get("name", "")),
           str(current_entity.get("symbol", "")),
           str(current_entity.get("symbol", "")).split(".")[0]}
    cur.discard("")
    if candidates and not any(c in cur for c in candidates):
        return 1
    return int(query_streak or 0) + 1


def _finalize(user_message: str, route_result: str, reason: str,
              matched_by: str, confidence: float,
              current_entity: Optional[dict], query_streak: int,
              intent: str, skill: Optional[str],
              candidates: Optional[list[str]] = None) -> RouteResult:
    """统一组装：候选提取 + streak 升级 + 兼容字段。"""
    if candidates is None:
        candidates = extract_candidates(user_message)
    streak = _next_streak(candidates, current_entity, query_streak)
    suggest = route_result == LIGHT_ANSWER and streak >= RESEARCH_SUGGEST_STREAK
    return RouteResult(
        route_result=route_result,
        entity_candidates=candidates,
        route_reason=reason,
        query_streak=streak,
        suggest_research=suggest,
        intent=intent,
        skill=skill,
        matched_by=matched_by,
        confidence=confidence,
    )


# ── LLM 语义识别（模糊输入兜底；结构化两路径输出）────────────────────────

LLM_ROUTER_PROMPT = """你是投研助手的意图路由器。判断用户这句话应该走哪条路径，只输出 JSON，不要输出其他文字。

两条路径：
- research_task：正式研究。判定依据（两条都满足才选它）：①需要多步工具编排取证（财报、分部数据、行业对比、多源验证）②需要留存结构化研究产物（估值判断、护城河结论、买卖参考）。
  例：「研究下兆易创新」「比亚迪和长城汽车哪个更值得投」「看下兆易创新Q3毛利率为什么降」
- light_answer：快问快答。概念解释、单点数据、时效行情、闲聊观点等，可单轮或少量工具直接回答。
  例：「PE是什么」「兆易创新现在多少倍PE」「兆易创新」

判断要点：
1. 一真一假的模糊输入默认 light_answer（宁漏判为轻，不误判为重）。
2. 只判路由：不建议具体怎么研究、不回答问题本身。

输出格式：
{{"route_result": "research_task 或 light_answer", "confidence": 0.0-1.0, "reasoning": "<一句话说明>"}}"""


def _normalize_explicit(value: str) -> tuple[str, Optional[str]]:
    """显式指定的值可能是 skill id 或 intent 值；统一成 (intent, skill)。"""
    v = (value or "").strip()
    if not v:
        return "", None
    if v in _SKILL_TO_INTENT:            # 是 skill id
        return _SKILL_TO_INTENT[v], v
    if v in _INTENT_TO_SKILL:            # 是 intent 值
        return v, _INTENT_TO_SKILL[v]
    return "", v                         # 未知：透传为 skill（保持兼容）


def route(user_message: str, explicit_skill: Optional[str] = None, *,
          current_entity: Optional[dict] = None,
          query_streak: int = 0) -> RouteResult:
    """
    同步路由（规则快速通道，零 LLM 零网络）。

    Args:
        user_message: 用户原始输入
        explicit_skill: 用户/API 显式指定的 skill（优先级最高）
        current_entity: 会话当前实体 {"symbol": ..., "name": ...}（升级计数用）
        query_streak: 同标的连续查询计数（会话状态透传）

    Returns:
        RouteResult: route_result 两路径 + entity_candidates + 升级信号
    """
    # 1. 显式指定优先
    if explicit_skill:
        intent, skill = _normalize_explicit(explicit_skill)
        route_result = RESEARCH_TASK if intent in ("research", "compare") else LIGHT_ANSWER
        logger.debug(f"路由：显式指定 skill={skill} → {route_result}")
        return _finalize(user_message, route_result, f"显式指定 skill={skill}",
                         "explicit", 1.0, current_entity, query_streak, intent, skill)

    # 2. 规则快速通道（两路径判定）
    candidates = extract_candidates(user_message)
    route_result, reason, conf = _classify_two_path(user_message, candidates)
    matched_by = "rule" if conf >= 0.6 else "default"
    intent, skill = _to_intent_skill(route_result, user_message)
    logger.debug(f"路由：规则 {matched_by} → {route_result}（{reason}）")
    return _finalize(user_message, route_result, reason, matched_by, conf,
                     current_entity, query_streak, intent, skill,
                     candidates=candidates)


async def route_async(
    llm_client,
    user_message: str,
    explicit_skill: Optional[str] = None,
    history: Optional[list[dict]] = None,
    *,
    current_entity: Optional[dict] = None,
    query_streak: int = 0,
) -> RouteResult:
    """
    异步路由：LLM 语义识别为主（模糊输入），规则快速通道兜底。

    优先级：显式 > 规则强信号 > LLM 语义（confidence≥阈值）> 默认轻回答。
    """
    # 1. 显式指定优先（同步语义）
    if explicit_skill:
        return route(user_message, explicit_skill,
                     current_entity=current_entity, query_streak=query_streak)

    # 2. 规则强信号直接短路（高置信规则不花 LLM 成本）
    candidates = extract_candidates(user_message)
    route_result, reason, conf = _classify_two_path(user_message, candidates)
    if conf >= 0.75:
        intent, skill = _to_intent_skill(route_result, user_message)
        return _finalize(user_message, route_result, reason, "rule", conf,
                         current_entity, query_streak, intent, skill,
                         candidates=candidates)

    # 3. LLM 语义识别（两路径）
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
        llm_route = str(data.get("route_result", "")).strip().lower()
        confidence = float(data.get("confidence", 0.0) or 0.0)
        reasoning = str(data.get("reasoning", "")).strip() or "LLM 语义判定"

        if llm_route in (LIGHT_ANSWER, RESEARCH_TASK) and confidence >= _LLM_CONFIDENCE_THRESHOLD:
            intent, skill = _to_intent_skill(llm_route, user_message)
            logger.debug(f"路由：LLM → {llm_route} (conf={confidence:.2f})")
            return _finalize(user_message, llm_route, reasoning, "llm", confidence,
                             current_entity, query_streak, intent, skill)
        logger.debug(f"路由：LLM 结果未采用 {llm_route!r} conf={confidence:.2f}，回落规则")
    except Exception as e:  # noqa: BLE001 - LLM 失败回落规则
        logger.warning(f"路由：LLM 语义识别失败，回落规则: {e}")

    # 4. 规则兜底
    intent, skill = _to_intent_skill(route_result, user_message)
    return _finalize(user_message, route_result, reason, "default", conf,
                     current_entity, query_streak, intent, skill,
                     candidates=candidates)


def available_intents() -> list[str]:
    """返回路由器认识的意图枚举值。"""
    return sorted(_INTENT_TO_SKILL)


def available_skills() -> list[str]:
    """返回路由器可解析出的所有 skill id（供 UI 展示 / 健康检查）。"""
    return sorted({s for s in _INTENT_TO_SKILL.values() if s} | {"private-company"})
