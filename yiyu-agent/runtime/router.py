"""
意图路由器 - 识别用户"意图"（intent），再映射到可执行的 Skill（skill）。

设计原则（intent-driven，而非 capability-driven）：
- 意图（intent）回答"用户想干什么"，是从用户诉求归纳出的稳定枚举；
- 技能（skill）回答"用什么能力承接"，由 intent + 标的信息二次解析得出；
- 两层解耦：顶层只做意图分类，skill 的挑选与「行业分组 / 上市状态 / 多业务」等
  细分交给下游（entity.resolve + company.classify + bus_router）。

意图分类的骨架：任务类型 × 是否有具体标的
- 有标的：research（单标的深度研究）、compare（多标的对比）
- 无标的：screen（按条件筛选）、recommend（选股推荐）、knowledge（知识问答）、
          calculate（计算）、portfolio（持仓）、review（复盘）、chitchat（闲聊）

识别模式：
1. 显式指定：API 请求里带 skill（给前端按钮 / 高级调用方用），优先。
2. LLM 语义识别：调用 LLM 分类意图（主力，理解复杂语义）。
3. 关键词匹配：LLM 失败 / 低置信时的零成本兜底（高精度启发式，不做穷举）。

优先级：显式 > LLM 语义（confidence≥阈值）> 关键词 > 默认（chitchat 闲聊）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RouteResult:
    """路由结果。"""
    intent: str = ""                 # 意图（intent 枚举值），主分类结果
    skill: Optional[str] = None      # 解析后的 skill id；None 表示无能力承接（纯对话）
    matched_by: str = ""             # 命中方式：explicit / llm / keyword / default
    confidence: float = 0.0


# ── 意图枚举（字符串常量，source of truth 见 INTENT_DEFINITIONS）───────────
# 意图是从"用户诉求"归纳出来的，不直接等于某个 skill id。
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

# LLM 分类器可输出的意图白名单（防止 LLM 编造不存在的意图）
_INTENTS: frozenset[str] = frozenset(INTENT_DEFINITIONS)


# ── 意图 → 承接技能（intent 是"想干什么"，skill 是"用什么能力承接"）──────
# None 表示当前暂无对应能力 → 走纯对话（由下游反问式兜底，见 chat 退化路径）。
_INTENT_TO_SKILL: dict[str, Optional[str]] = {
    "research": "deep-research",   # 未上市变体由 resolve_skill 二次判定 → private-company
    "compare": "deep-research",    # 暂挂：对比能力未单独立项，先复用研究框架
    "screen": "quick-screen",
    "recommend": None,             # 暂挂：推荐能力未单独立项（筛选 ≠ 推荐，不误挂 quick-screen）
    "knowledge": "knowledge_qa",
    "calculate": None,             # 暂挂：暂无计算工具
    "portfolio": "holdings-track",
    "review": "trade-review",
    "chitchat": None,
}

# 技能 → 意图（反向映射，供显式指定 skill 时补 intent 标签）
_SKILL_TO_INTENT: dict[str, str] = {
    "deep-research": "research",
    "private-company": "research",
    "quick-screen": "screen",
    "knowledge_qa": "knowledge",
    "holdings-track": "portfolio",
    "trade-review": "review",
}


def resolve_skill(intent: str, user_message: str = "") -> Optional[str]:
    """把意图解析为可执行的 skill id（intent → skill 的薄层）。

    - research 若带"未上市/一级市场"信号 → 切 private-company，否则 deep-research；
    - 其余意图直接查 _INTENT_TO_SKILL；
    - 返回 None 表示当前无能力承接（走纯对话）。
    """
    intent = (intent or "").strip().lower()
    if intent == "research" and _PRIVATE_COMPANY_PATTERN.search(user_message or ""):
        return "private-company"
    return _INTENT_TO_SKILL.get(intent)


# ── 关键词规则（按命中优先级排序，先匹配先用；每条：(正则, intent, 置信度)）────
# 定位：LLM 失败/低置信时的零成本兜底。只放高精度信号，不做穷举——
# "研究某公司"这类需要实体识别的场景，交给 LLM 语义识别主力承担。
_RULES: list[tuple[re.Pattern, str, float]] = [
    # 对比（多标的）——放最前，避免被"研究"抢走
    (re.compile(r"(对比|比较|pk|vs\.?)\s*.{0,12}(更|哪个|谁|区别|优劣)", re.IGNORECASE),
     "compare", 0.85),
    (re.compile(r".{1,8}\s*(和|与|跟|vs\.?)\s*.{1,8}\s*(哪个|谁|对比|比较|区别)", re.IGNORECASE),
     "compare", 0.7),

    # 筛选
    (re.compile(r"(筛选|选股|过滤|找|挑).{0,8}(股票|标的|公司|个股)", re.IGNORECASE),
     "screen", 0.85),
    (re.compile(r"(低估值|高股息|破净|低市盈率|高roe).{0,6}(的|有哪些|股票|标的)", re.IGNORECASE),
     "screen", 0.75),

    # 推荐
    (re.compile(r"(推荐|买什么|买哪只|买哪支|买哪家|有什么好).{0,8}(股票|标的|基金|票|股)", re.IGNORECASE),
     "recommend", 0.8),

    # 计算
    (re.compile(r"(算|计算|换算|等于多少).{0,6}(收益率|收益|回报|复利|估值|多少钱)", re.IGNORECASE),
     "calculate", 0.8),

    # 持仓
    (re.compile(r"(我的|当前)?\s*(持仓|组合|仓位|账户)(怎么样|情况|状态|如何|是多少)?", re.IGNORECASE),
     "portfolio", 0.85),
    (re.compile(r"(加仓|减仓|清仓|调仓|补仓)", re.IGNORECASE),
     "portfolio", 0.75),

    # 复盘
    (re.compile(r"(复盘|回顾|总结).{0,10}(交易|买卖|操作|投资)", re.IGNORECASE),
     "review", 0.85),
    (re.compile(r"(上次|最近).{0,4}(买|卖|操作).{0,6}(对不对|怎么样|好不好)", re.IGNORECASE),
     "review", 0.7),

    # 研究（有标的）：研究动词 + 行业后缀 / 代码 / ticker
    (re.compile(r"(深度|详细|认真)?\s*(研究|分析|调研|解读|评估|看看|看下).{0,12}"
                r"(标的|股票|公司|股份|集团|银行|证券|保险|稀土|医药|能源|科技|半导体|"
                r"白酒|食品|饮料|汽车|新能源|光伏|锂电|互联网|软件|[0-9]{6}|[A-Z]{2,5})",
                re.IGNORECASE),
     "research", 0.85),
    # 研究（买卖意图）："XX值得买吗 / 该不该买 / 能不能买" → 对标的的研究判断
    (re.compile(r"(值得买|该不该买|能不能买|值不值得|该买吗|可以买吗|能买吗|要不要买)", re.IGNORECASE),
     "research", 0.75),
    (re.compile(r"(基本面|财报|营收|净利润|毛利率|估值水平|六关|四大师|镜子测试)", re.IGNORECASE),
     "research", 0.8),
    # 研究（研究动词兜底）："分析一下X" 这类无明确后缀但强研究信号（X 交给 LLM/实体解析）
    (re.compile(r"(分析|研究|调研|解读|评估|看看|看下)一下?", re.IGNORECASE),
     "research", 0.6),

    # 知识（概念问答，放研究之后，避免"护城河/安全边际"被研究抢走）
    (re.compile(r"(什么是|什么意思|怎么(?:理解|看|用)|如何(?:理解|看)|"
                r"\b(?:roi|roe|pe|pb|dcf|roic)\b|复利|现金流折现|护城河|安全边际|"
                r"内在价值|自由现金流)",
                re.IGNORECASE),
     "knowledge", 0.8),
]

# 研究类标的属性：未上市/一级市场 → 用 private-company 技能承接
_PRIVATE_COMPANY_PATTERN = re.compile(
    r"((?:未|没|尚未|还未)\s*上市|拟上市|一级市场|pre-?ipo|创业公司|初创|startup|融资轮|天使轮|风投)",
    re.IGNORECASE,
)


# LLM 语义识别置信度阈值：低于则降级到关键词
_LLM_CONFIDENCE_THRESHOLD = 0.6


LLM_ROUTER_PROMPT = """你是投研助手的意图分类器。判断用户这句话的"意图"（想干什么），只输出 JSON，不要输出其他文字。

意图定义（intent 字段取括号内的值）：
- research：对某个具体标的做深度研究/分析（财报、估值、护城河、买卖判断等）
  （如"分析一下腾讯"、"茅台值得买吗"、"北方稀土的基本面怎么样"）
- compare：对两个及以上标的做对比、比较、选择
  （如"茅台和五粮液哪个更值得买"、"腾讯阿里对比"）
- screen：按量化条件筛选候选标的，不针对某个具体标的
  （如"筛选低估值高股息的股票"、"市盈率低于10的银行股有哪些"）
- recommend：让系统推荐/选股，未指定标的也无明确条件
  （如"给我推荐几只股票"、"最近买什么好"）
- knowledge：投资知识/概念/方法论问答，不针对具体公司做深度计算
  （如"什么是ROE"、"DCF怎么理解"、"怎么看护城河"）
- calculate：明确的数字计算诉求
  （如"帮我算下今年收益率"、"100万复利10%十年后多少"）
- portfolio：用户自己的持仓/组合/仓位管理
  （如"我的持仓怎么样"、"帮我看看仓位"、"加仓/减仓"）
- review：回顾/复盘历史交易
  （如"复盘我上次的操作"、"回顾一下我的买卖"）
- chitchat：闲聊、问候、无关话题或其他无法归类
  （如"你好"、"谢谢"、"今天天气"）

判断要点：
1. 先看用户是否提到"具体标的"（某只股票/公司/基金）——这是 research/compare 与其它意图的关键分界。
2. 多标的对比 → compare；单标的深度分析 → research。
3. 无标的时按任务动词归类：筛选→screen，推荐→recommend，问概念→knowledge，
   算数字→calculate，我的持仓→portfolio，复盘→review。
4. 实在无法归类 → chitchat。

输出格式：
{{"intent": "<意图>", "confidence": 0.0-1.0, "reasoning": "<一句话说明>"}}"""


def _normalize_explicit(value: str) -> tuple[str, Optional[str]]:
    """显式指定的值可能是 skill id 或 intent 值；统一成 (intent, skill)。

    兼容前端传 skill id（deep-research）与意图值（research）两种写法。
    """
    v = (value or "").strip()
    if not v:
        return "", None
    if v in _SKILL_TO_INTENT:            # 是 skill id
        return _SKILL_TO_INTENT[v], v
    if v in _INTENT_TO_SKILL:            # 是 intent 值
        return v, _INTENT_TO_SKILL[v]
    return "", v                         # 未知：透传为 skill（保持兼容）


def route(user_message: str, explicit_skill: Optional[str] = None) -> RouteResult:
    """
    同步路由（零成本兜底）：显式指定优先，然后关键词匹配，最后默认闲聊。

    Args:
        user_message: 用户原始输入
        explicit_skill: 用户/API 显式指定的 skill（优先级最高）

    Returns:
        RouteResult: 含 intent 与 skill（skill 可能为 None 表示纯对话）
    """
    # 1. 显式指定优先
    if explicit_skill:
        intent, skill = _normalize_explicit(explicit_skill)
        logger.debug(f"路由：显式指定 skill={skill} intent={intent}")
        return RouteResult(intent=intent, skill=skill, matched_by="explicit", confidence=1.0)

    # 2. 关键词匹配（按意图）
    for pattern, intent, conf in _RULES:
        if pattern.search(user_message):
            skill = resolve_skill(intent, user_message)
            logger.debug(f"路由：关键词命中 intent={intent} skill={skill} (conf={conf})")
            return RouteResult(intent=intent, skill=skill, matched_by="keyword", confidence=conf)

    # 3. 兜底：闲聊
    logger.debug("路由：未命中，默认闲聊")
    return RouteResult(intent="chitchat", skill=None, matched_by="default", confidence=0.0)


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

    # 2. LLM 语义识别（分类意图）
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
        intent = str(data.get("intent", "")).strip().lower()
        confidence = float(data.get("confidence", 0.0) or 0.0)

        if intent in _INTENTS and confidence >= _LLM_CONFIDENCE_THRESHOLD:
            skill = resolve_skill(intent, user_message)
            logger.debug(f"路由：LLM 命中 intent={intent} skill={skill} (conf={confidence:.2f})")
            return RouteResult(
                intent=intent, skill=skill, matched_by="llm", confidence=confidence
            )

        logger.debug(
            f"路由：LLM 结果未采用 intent={intent!r} conf={confidence:.2f}，降级关键词"
        )
    except Exception as e:
        logger.warning(f"路由：LLM 语义识别失败，降级关键词: {e}")

    # 3. 关键词兜底
    return route(user_message)


def available_intents() -> list[str]:
    """返回路由器认识的意图枚举值。"""
    return sorted(_INTENTS)


def available_skills() -> list[str]:
    """返回路由器可解析出的所有 skill id（供 UI 展示 / 健康检查）。"""
    return sorted({s for s in _INTENT_TO_SKILL.values() if s} | {"private-company"})
