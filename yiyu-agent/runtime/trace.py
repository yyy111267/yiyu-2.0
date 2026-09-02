"""
Loop Trace —— 研究循环逐轮结构化 trace（评测集《研究循环-评测用例集》§4 前置交付件）。

定位：lint 引擎（loop_lint.py）与仪表盘读数的唯一数据源。
三类落点（对应三条行为不变量）：
  - LoopTurn.declaration    → 不变量①「行动前声明意图」的落点
  - LoopTurn.writeback      → 不变量②「观察后回写计划与证据」的落点
  - FinalReport.conclusions → 不变量③「结论映射证据条目」的落点

本模块是纯数据结构，不含任何 I/O 与 LLM 调用；确定性落盘，供 G 类 mock 评测逐轮查账。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# 数字（可带单位）：亿/万/元/%/倍——用于编数比对与证据数字提取
_NUM_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|亿|万|元|块|倍)?")


def digest(text: Any, max_len: int = 48) -> str:
    """短摘要：长文本截断 + 尾部 8 位 md5，保证可对账且不泄露全量。"""
    s = str(text) if text is not None else ""
    if len(s) <= max_len:
        return s
    h = hashlib.md5(s.encode("utf-8")).hexdigest()[:8]
    return s[: max_len - 10] + "…" + h


def extract_numbers(text: Any) -> list[str]:
    """提取文本中的数字（含常见单位），返回原始匹配串列表。"""
    return _NUM_RE.findall(str(text) if text is not None else "")


def normalize_number(num_str: str) -> tuple[float, str]:
    """把「45.7」「20亿」「15%」归一化成 (数值, 单位)。"""
    s = str(num_str).strip()
    m = re.match(r"(\d+(?:\.\d+)?)\s*(.*)$", s)
    if not m:
        return 0.0, ""
    return float(m.group(1)), m.group(2).strip()


# ── Trace Schema ─────────────────────────────────────────────


@dataclass
class ToolCallRecord:
    """实际执行的一次工具调用（§4.1 tool_calls）。"""
    tool: str
    args_digest: str
    result_digest: str
    latency_ms: int
    ok: bool


@dataclass
class Declaration:
    """不变量①落点：本轮调用工具前，声明「为回答哪个问题、要干嘛、并行调哪些」。"""
    question_ids: list[str] = field(default_factory=list)  # 指向 research_plan 问题 id，须真实存在
    intent: str = ""                                        # 本轮意图（一句话）
    parallel: list[str] = field(default_factory=list)       # 本轮声明的并行调用批（工具名）


@dataclass
class PlanDiff:
    """计划状态迁移的一条审计（§4.1 writeback.plan_diff）。"""
    question_id: str
    field: str
    old: str
    new: str


@dataclass
class EvidenceDiff:
    """本轮新写入证据包的一条（§4.1 writeback.evidence_diff）。"""
    evidence_id: str
    content_digest: str
    source: str


@dataclass
class Writeback:
    """不变量②落点：工具返回后，计划与证据包的状态回写。"""
    plan_diff: list[PlanDiff] = field(default_factory=list)
    evidence_diff: list[EvidenceDiff] = field(default_factory=list)
    open_questions_delta: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.plan_diff or self.evidence_diff or self.open_questions_delta)


@dataclass
class BudgetSnapshot:
    """每轮预算读数快照（熔断判定可回放）。"""
    used: int
    limit: int
    remaining: int


@dataclass
class LoopTurn:
    """一轮循环的完整落盘（§4.1 LoopTurn）。"""
    turn_id: int
    declaration: Declaration = field(default_factory=Declaration)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    writeback: Writeback = field(default_factory=Writeback)
    budget: Optional[BudgetSnapshot] = None
    injection_detected: bool = False   # 工具结果中检测到注入指令
    llm_calls: int = 1                 # 本轮 LLM 调用次数（P1：并行取数仍须 =1）


@dataclass
class Conclusion:
    """最终报告的一条结论（§4.1 FinalReport.conclusions）。"""
    text: str
    evidence_ids: list[str] = field(default_factory=list)  # 映射到证据包条目
    is_inference: bool = False                             # 显式标注「推断」而非证据支撑


@dataclass
class Unanswered:
    """未解决问题（§4.1 FinalReport.unanswered）。"""
    question_id: str
    reason: str


@dataclass
class FinalReport:
    """循环收敛时的最终报告（§4.1 FinalReport）。"""
    conclusions: list[Conclusion] = field(default_factory=list)
    unanswered: list[Unanswered] = field(default_factory=list)
    budget_exhausted: bool = False   # 是否因预算提前终止（降级报告）


@dataclass
class EvidenceEntry:
    """证据包条目：一条可对账的事实/数字，带来源与数值集。

    P3 取数层全链路：新增 fetch_status / missing_fields / field_evidence，
    让取数层的「字段级来源 + 缺失状态 + 搜索兜底结果」进证据包，
    最终报告 citations 能通过 evidence_id 展示原文链接和字段口径。
    """

    evidence_id: str
    source: str
    content_digest: str
    numbers: list[str] = field(default_factory=list)  # 归一化前的原始数字串
    level: str = "B"                                   # S/A/B 信源等级
    title: str = ""
    url: str = ""
    as_of: str = ""
    source_level: str = ""
    metric_id: str = ""
    caliber_version: str = ""
    period: str = ""
    fields: list[dict] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    # P3 取数层全链路
    fetch_status: str = ""            # ok / partial / degraded（取数层 fetch_status）
    missing_fields: list[str] = field(default_factory=list)
    field_evidence: dict[str, dict] = field(default_factory=dict)  # {field: FieldEvidence}

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "content_digest": self.content_digest,
            "numbers": list(self.numbers),
            "level": self.level,
            "title": self.title,
            "url": self.url,
            "as_of": self.as_of,
            "source_level": self.source_level or self.level,
            "metric_id": self.metric_id,
            "caliber_version": self.caliber_version,
            "period": self.period,
            "fields": [dict(f) for f in self.fields],
            "citations": [dict(c) for c in self.citations],
            "fetch_status": self.fetch_status,
            "missing_fields": list(self.missing_fields),
            "field_evidence": {k: dict(v) for k, v in self.field_evidence.items()},
        }


@dataclass
class Trace:
    """整场研究循环的 trace：逐轮 + 最终态 + 证据包。"""
    session_id: str = ""
    turns: list[LoopTurn] = field(default_factory=list)
    final_report: Optional[FinalReport] = None
    evidence_pack: list[EvidenceEntry] = field(default_factory=list)

    def add_turn(self, turn: LoopTurn) -> None:
        self.turns.append(turn)

    def add_evidence(self, source: str, content: Any, level: str = "B") -> EvidenceEntry:
        """从一次工具结果提炼证据条目（自动抽数字 + 分配 id）。

        P3：market.get_bundle / calc.metric 的 metadata 现在含
        fetch_status / missing_fields / field_evidence，灌进 EvidenceEntry，
        让最终报告 citations 能通过 evidence_id 展示原文链接和字段口径。
        """
        meta = evidence_metadata(source, content, default_level=level)
        entry = EvidenceEntry(
            evidence_id=f"e{len(self.evidence_pack) + 1}",
            source=source,
            content_digest=digest(content),
            numbers=extract_numbers(content),
            level=meta.get("source_level") or level,
            title=meta.get("title", ""),
            url=meta.get("url", ""),
            as_of=meta.get("as_of", ""),
            source_level=meta.get("source_level", "") or level,
            metric_id=meta.get("metric_id", ""),
            caliber_version=meta.get("caliber_version", ""),
            period=meta.get("period", ""),
            fields=meta.get("fields", []),
            citations=meta.get("citations", []),
            fetch_status=meta.get("fetch_status", ""),
            missing_fields=list(meta.get("missing_fields") or []),
            field_evidence=meta.get("field_evidence", {}) or {},
        )
        self.evidence_pack.append(entry)
        return entry

    def all_evidence_numbers(self) -> list[str]:
        return [n for e in self.evidence_pack for n in e.numbers]

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "turns": [
                {
                    "turn_id": t.turn_id,
                    "declaration": {
                        "question_ids": list(t.declaration.question_ids),
                        "intent": t.declaration.intent,
                        "parallel": list(t.declaration.parallel),
                    },
                    "tool_calls": [
                        {
                            "tool": c.tool,
                            "args_digest": c.args_digest,
                            "result_digest": c.result_digest,
                            "latency_ms": c.latency_ms,
                            "ok": c.ok,
                        }
                        for c in t.tool_calls
                    ],
                    "writeback": {
                        "plan_diff": [pd.__dict__ for pd in t.writeback.plan_diff],
                        "evidence_diff": [ed.__dict__ for ed in t.writeback.evidence_diff],
                        "open_questions_delta": list(t.writeback.open_questions_delta),
                    },
                    "budget": t.budget.__dict__ if t.budget else None,
                    "injection_detected": t.injection_detected,
                    "llm_calls": t.llm_calls,
                }
                for t in self.turns
            ],
            "final_report": (
                {
                    "conclusions": [c.__dict__ for c in self.final_report.conclusions],
                    "unanswered": [u.__dict__ for u in self.final_report.unanswered],
                    "budget_exhausted": self.final_report.budget_exhausted,
                }
                if self.final_report
                else None
            ),
            "evidence_pack": [e.to_dict() for e in self.evidence_pack],
        }


def evidence_metadata(source: str, content: Any, default_level: str = "B") -> dict:
    """Extract displayable citation metadata from known tool result shapes."""
    if not isinstance(content, dict):
        return {"source_level": default_level}

    source_norm = (source or "").replace("_", ".")
    if source_norm == "calc.metric" or content.get("metric_id"):
        # P3：calc.metric 透传了 field_evidence，给每个字段 citation 注入 evidence_id
        fe = content.get("field_evidence")
        if not isinstance(fe, dict):
            fe = {}
        enriched_fields = []
        for f in (content.get("fields") or []):
            if not isinstance(f, dict):
                enriched_fields.append(f)
                continue
            fn = str(f.get("field") or f.get("name") or "")
            ev = fe.get(fn)
            if isinstance(ev, dict):
                f = dict(f)
                f["evidence_id"] = str(ev.get("evidence_id") or "")
                f["source"] = str(ev.get("source") or f.get("source") or "")
                f["source_level"] = str(ev.get("source_level") or f.get("source_level") or default_level)
            enriched_fields.append(f)
        return {
            "source_level": content.get("source_level") or default_level,
            "as_of": str(content.get("as_of") or ""),
            "metric_id": str(content.get("metric_id") or ""),
            "caliber_version": str(content.get("caliber_version") or ""),
            "period": str(content.get("period") or ""),
            "fields": enriched_fields,
            "fetch_status": str(content.get("fetch_status") or ""),
            "missing_fields": list(content.get("missing_fields") or []),
            "field_evidence": fe,
            "citations": [{
                "kind": "metric",
                "metric_id": str(content.get("metric_id") or ""),
                "value": content.get("value"),
                "unit": str(content.get("unit") or ""),
                "source_level": content.get("source_level") or default_level,
                "as_of": str(content.get("as_of") or ""),
                "period": str(content.get("period") or ""),
                "caliber_version": str(content.get("caliber_version") or ""),
                "fields": enriched_fields,
                "evidence_ids": [str(ev.get("evidence_id") or "")
                                  for ev in fe.values() if isinstance(ev, dict)],
            }],
        }

    if source_norm == "web.fetch":
        url = str(content.get("url") or "")
        return {
            "url": url,
            "title": url,
            "source_level": content.get("source_level") or default_level,
            "citations": [{
                "kind": "web",
                "title": url,
                "url": url,
                "source_level": content.get("source_level") or default_level,
                "as_of": str(content.get("as_of") or ""),
            }],
        }

    if source_norm == "web.search":
        citations = []
        search_level = "A" if content.get("sources_verified") else default_level
        for item in content.get("results") or []:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            citations.append({
                "kind": "web",
                "title": str(item.get("title") or item.get("url") or ""),
                "url": str(item.get("url") or ""),
                "source_level": str(item.get("source_level") or search_level),
                "as_of": str(item.get("published_at") or item.get("as_of") or ""),
                "snippet": str(item.get("snippet") or "")[:500],
            })
        first = citations[0] if citations else {}
        return {
            "url": first.get("url", ""),
            "title": first.get("title", ""),
            "source_level": first.get("source_level", default_level),
            "citations": citations,
        }

    if source_norm == "market.get.bundle" or (isinstance(content.get("field_evidence"), dict) and content.get("field_evidence")):
        # P3 取数层全链路：把 field_evidence 展开成字段级 citations，
        # 每个 citation 带 field/source/source_level/as_of/period/caliber/evidence_id，
        # 最终报告通过 evidence_id 回溯到原文链接和字段口径。
        fe = content.get("field_evidence")
        if not isinstance(fe, dict):
            fe = {}
        citations: list[dict] = []
        for field_name, ev in fe.items():
            if not isinstance(ev, dict):
                continue
            citations.append({
                "kind": "field",
                "field": str(field_name),
                "value": ev.get("value"),
                "source": str(ev.get("source") or ""),
                "source_level": str(ev.get("source_level") or default_level),
                "as_of": str(ev.get("as_of") or ""),
                "period": str(ev.get("period") or ""),
                "caliber": str(ev.get("caliber") or field_name),
                "evidence_id": str(ev.get("evidence_id") or ""),
                "url": str(ev.get("url") or ""),
            })
        # 搜索兜底结果也展成 web citations（带 source_level）
        for fb in content.get("fallback_results") or []:
            if not isinstance(fb, dict) or not fb.get("url"):
                continue
            citations.append({
                "kind": "fallback_search",
                "title": str(fb.get("title") or fb.get("url") or ""),
                "url": str(fb.get("url") or ""),
                "source_level": str(fb.get("source_level") or "B"),
                "as_of": str(fb.get("as_of") or ""),
                "snippet": str(fb.get("snippet") or "")[:500],
            })
        first = citations[0] if citations else {}
        return {
            "url": str(first.get("url") or ""),
            "title": str(first.get("field") or first.get("title") or ""),
            "source_level": str(first.get("source_level") or default_level),
            "fetch_status": str(content.get("fetch_status") or ""),
            "missing_fields": list(content.get("missing_fields") or []),
            "field_evidence": fe,
            "citations": citations,
        }

    return {"source_level": content.get("source_level") or default_level}
