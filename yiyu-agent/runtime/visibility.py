"""
SSE 对外进度摘要层（信息可见性收口）。

原则（见 docs/sse-contract.md「对外可见性红线」）：
- 用户端不收到原始 thought、工具名、调用参数、路由规则、置信度等内部细节
- 内部事件（thought/tool_call/tool_result/debug/spawn_agent/agent_done/start）
  统一归并为 `progress` 事件，只带 stage / status / sources 白名单字段
- 错误只下发固定安全文案；原始异常仅进服务端日志
- 最终交付（answer_delta / final_answer / ask_confirmation / warning / complete）透传，
  但 metadata 中的内部调试键会被剔除

用法（api/routes/chat.py 下发前统一过一层）：
    public = to_public_event(evt)
    if public is not None:
        yield {"event": "message", "data": public.to_json()}

对外 stage 枚举（前端按此映射固定文案，最多三阶段）：
- understand_question  理解研究问题（受理 / 路由 / 前处理）
- evidence_check       收集并核验信息（循环取数 / 工具调用）
- synthesizing         整理分析结论（答案组织）
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Optional

from runtime.events import AgentEvent, EventType

logger = logging.getLogger(__name__)

# 对外阶段枚举
STAGE_UNDERSTAND = "understand_question"
STAGE_EVIDENCE = "evidence_check"
STAGE_SYNTHESIZE = "synthesizing"

# 透传事件（最终交付类）——metadata 仍需剔除内部键
_PASSTHROUGH: set[EventType] = {
    EventType.ANSWER_DELTA,
    EventType.FINAL_ANSWER,
    EventType.ASK_CONFIRMATION,
    EventType.WARNING,
    EventType.COMPLETE,
    EventType.NEED_INPUT,
}

# 内部事件 → 归并为 progress；调试用，SSE 不下发
_MERGED_INTO_PROGRESS: set[EventType] = {
    EventType.START,
    EventType.THOUGHT,
    EventType.DEBUG,
    EventType.TOOL_CALL,
    EventType.SPAWN_AGENT,
    EventType.AGENT_DONE,
}

# 研究计划：白名单字段（question/status），其余内部结构不下发
_PLAN_STATUS_ZH = {
    "pending": "待分析",
    "in_progress": "分析中",
    "done": "已完成",
    "answered": "已回答",
    "unanswerable": "无法回答",
}

# ERROR 事件的对外固定安全文案（原始异常只在服务端日志里）
_PUBLIC_ERROR_TEXT = "研究过程出现异常，已停止。可稍后重试，或把问题缩小为一个维度。"

# metadata 中不允许下发的内部键
_INTERNAL_META_KEYS = {
    "error", "reason", "skill", "matched_by", "confidence",
    "tool_name", "tool", "arguments", "args", "result",
    "step", "phase", "debug", "trace", "stack", "exception",
}


# 内部工具名 → 用户可理解的来源类型。
# 严禁把 market_get_bundle 这类内部实现名下发到界面（用户不认可它作为来源）。
_SOURCE_TYPE_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("calc.", "指标计算"),
    ("web.", "新闻与研报"),
    ("cognition.", "个人认知库"),
    ("company.", "公司画像"),
    ("entity.", "公司画像"),
)
_TYPE_REPORT = "财报"
_TYPE_QUOTE = "行情数据"
_TYPE_DEFAULT = "公开资料"


def _source_type(source: str, period: str) -> str:
    """把内部工具名映射为用户可核对的来源类型。"""
    s = str(source or "").replace("_", ".").lower()
    for prefix, label in _SOURCE_TYPE_BY_PREFIX:
        if s.startswith(prefix):
            return label
    if s.startswith("market."):
        # 带报告期的来自财报，否则是实时行情快照
        return _TYPE_REPORT if period else _TYPE_QUOTE
    return _TYPE_DEFAULT


def _public_citations(citations) -> list[dict]:
    """最终报告来源清单：只保留用户可核对的字段。

    剔除 source（内部工具名）/ content_digest / numbers 等内部结构；
    source 统一映射为「财报 / 行情数据 / 新闻与研报 / 指标计算…」，
    让界面上的每一条来源都是用户真正能去核对的东西。
    """
    if not isinstance(citations, list):
        return []
    out: list[dict] = []
    for item in citations:
        if not isinstance(item, dict):
            continue
        period = str(item.get("period") or "")
        fields: list[str] = []
        for f in (item.get("fields") or []):
            if isinstance(f, dict):
                name = str(f.get("field") or f.get("name") or "")
                if name:
                    fields.append(name)
            elif isinstance(f, str) and f.strip():
                fields.append(f.strip())
        out.append({
            "evidence_id": str(item.get("evidence_id") or ""),
            "title": str(item.get("title") or ""),
            "type": _source_type(item.get("source"), period),
            "url": str(item.get("url") or ""),
            "as_of": str(item.get("as_of") or ""),
            "period": period,
            "metric_id": str(item.get("metric_id") or ""),
            "caliber_version": str(item.get("caliber_version") or ""),
            "fields": fields[:12],
            "level": str(item.get("level") or item.get("source_level") or "B"),
        })
    return out


def _public_error() -> AgentEvent:
    """错误事件对外形态：固定安全文案，不带任何内部错误码/模块名。"""
    return AgentEvent(
        type=EventType.ERROR,
        content=_PUBLIC_ERROR_TEXT,
        metadata={"stage": "done", "status": "failed"},
    )


def _sanitize_metadata(meta: dict) -> dict:
    """剔除 metadata 中的内部调试键（浅层）。"""
    if not meta:
        return {}
    return {k: v for k, v in meta.items() if k not in _INTERNAL_META_KEYS}


# 研究框架主题桶：内部 P0 问题 → 用户可理解的研究主题。
# 后台可能有 8 / 20 / 50 个问题，用户只看到归纳后的研究主题，不显示
# 「待分析」等任务态——那是内部任务管理器视角，不是用户要的研究框架。
_FOCUS_THEMES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    # 注意：不要放"增长"这类跨主题的泛词，否则会把估值/风险类问题吞进第一桶
    ("生意质量", ("生意", "商业", "模式", "需求", "赚钱", "盈利模式", "收入", "主营", "客户", "放量"),
     "这门生意长期靠什么赚钱，核心需求是否稳定"),
    ("竞争优势", ("护城河", "竞争", "品牌", "定价权", "渠道", "壁垒", "份额", "对手", "优势"),
     "品牌、渠道与定价权能否持续，优势有没有削弱"),
    ("盈利质量", ("利润", "毛利", "净利", "ROIC", "ROE", "现金流", "资本回报", "财务", "成本", "费用", "周转"),
     "高利润、高资本回报和现金流能否持续"),
    ("估值与回报", ("估值", "PE", "PB", "市值", "价格", "预期", "安全边际", "回报", "折价", "溢价", "股息"),
     "当前价格隐含了怎样的增长预期"),
    ("主要风险", ("风险", "不确定", "威胁", "证伪", "恶化", "下滑", "隐患", "政策", "监管", "减持"),
     "哪些变化最可能推翻长期投资逻辑"),
)


def _focus_items(questions: list[dict]) -> list[dict]:
    """把内部 P0 问题归纳成用户可理解的研究框架主题。

    Returns:
        [{"title": "生意质量", "summary": "..."}]；按 _FOCUS_THEMES 固定顺序，
        只输出命中的主题。未归类的问题归入"其他关键问题"，不丢信息。
    """
    buckets: dict[str, int] = {}
    for q in questions:
        text = str(q.get("question") or q.get("q") or "")
        if not text:
            continue
        theme = ""
        for name, keywords, _summary in _FOCUS_THEMES:
            if any(k in text for k in keywords):
                theme = name
                break
        buckets[theme] = buckets.get(theme, 0) + 1
    items = [
        {"title": name, "summary": summary}
        for name, _kw, summary in _FOCUS_THEMES
        if buckets.get(name)
    ]
    if buckets.get(""):
        items.append({"title": "其他关键问题", "summary": "研究中需要补充验证的专项问题"})
    return items


def _plan_public_payload(plan) -> dict:
    """研究计划只对外暴露分析重点。

    focus_items：归纳后的研究框架主题（用户视角，前端主渲染）；
    p0_questions：内部问题原文 + 中文状态，仅供 trace 与旧消费者，前端不展示状态。
    """
    questions = []
    for q in getattr(plan, "p0_questions", []) or []:
        questions.append({
            "question": str(getattr(q, "question", "") or ""),
            "status": _PLAN_STATUS_ZH.get(getattr(q, "status", ""), "待分析"),
        })
    return {"p0_questions": questions, "focus_items": _focus_items(questions)}


def _plan_from_content(content: str) -> dict:
    """PLAN 事件的 content 是完整计划 JSON；只提取白名单字段。

    兼容 loop 内部 fallback 计划（未通过入参传递）的场景。
    解析失败返回空列表（前端不渲染面板）。
    """
    try:
        data = json.loads(content) if content else {}
    except (json.JSONDecodeError, TypeError):
        return {"p0_questions": [], "focus_items": []}
    raw = data.get("p0_questions") or []
    questions = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        questions.append({
            "question": str(q.get("question") or q.get("q") or ""),
            "status": _PLAN_STATUS_ZH.get(str(q.get("status") or ""), "待分析"),
        })
    return {"p0_questions": questions, "focus_items": _focus_items(questions)}


def to_public_event(event: AgentEvent, plan=None) -> Optional[AgentEvent]:
    """把内部 AgentEvent 转换为对外 SSE 事件。

    Args:
        event: loop / preloop 产出的原始事件
        plan: 可选，当前研究计划（PLAN 事件过滤时提取白名单字段）

    Returns:
        对外事件；None 表示该事件不应下发（前端不收到）。
    """
    et = event.type

    # 1) 内部过程事件 → progress 摘要（不携带任何原文/工具名/参数）
    if et in _MERGED_INTO_PROGRESS or et == EventType.TOOL_RESULT:
        meta = {"stage": STAGE_EVIDENCE, "status": "running"}
        # 来源白名单：仅 tool_result 可能带 sources / references（供信息来源区）
        for key in ("sources", "references"):
            val = (event.metadata or {}).get(key)
            if isinstance(val, list) and val:
                meta[key] = val
        return AgentEvent(type=EventType.PROGRESS, content="", metadata=meta)

    # 2) 研究计划 → 只下发分析重点（优先入参 plan，兜底解析事件 content）
    if et == EventType.PLAN:
        meta = {"stage": STAGE_UNDERSTAND, "status": "done"}
        payload = _plan_public_payload(plan) if plan is not None else _plan_from_content(event.content)
        if not payload["p0_questions"]:
            payload = _plan_from_content(event.content)
        meta.update(payload)
        return AgentEvent(type=EventType.PLAN, content="", metadata=meta)

    # 3) 错误 → 固定安全文案（原始内容进日志）
    if et == EventType.ERROR:
        logger.warning(
            "[visibility] 内部错误(仅日志): %s | meta=%s",
            event.content[:300],
            {k: (event.metadata or {}).get(k) for k in ("reason",)},
        )
        return _public_error()

    # 4) 最终交付类 → 透传并净化 metadata
    if et in _PASSTHROUGH:
        clean = copy.deepcopy(event)
        meta = _sanitize_metadata(clean.metadata)
        # 来源清单单独净化：内部工具名 → 用户可理解的来源类型
        if "citations" in meta:
            meta["citations"] = _public_citations(meta["citations"])
        clean.metadata = meta
        return clean

    # 5) 其余未知类型一律不下发
    return None
