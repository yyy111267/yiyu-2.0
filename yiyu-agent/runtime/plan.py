"""研究计划（PRD 5.3 环节④）—— 把"菜谱"换成"任务清单"。

设计依据：《环节④研究计划生成·评测用例集》

字段生命周期（PRD 11 字段 + 分阶段填充，消解"简化 vs 全校验"之争）：
- 出生字段（LLM 填，生成时一次校验，失败重试代价=整张计划）：
  question / priority / dimension（id 由代码分配，LLM 不产 id）
- 激活字段（LLM 填，问题转入 in_progress 时逐题校验，失败只重试该题）：
  falsification / completion_rule / required_evidence
  —— 没有"停止规则"的问题不许开工：completion_rule 是"已回答"的客观判据，
  required_evidence 是行为不变量③"结论映射证据条目"的依据。
- 代码字段（代码自动填，LLM 永不产出）：id / status / 审计时间戳 / reason

清单不是菜谱：无步骤号、无强制序列字段；任一 pending 的 P0 都可作为起点。
活的清单：状态机（pending → in_progress → answered / unanswerable 终态）、
动态新增（附 reason）、降级（附 reason；P0 不许降空）。
dimension_coverage 从 questions[].dimension 推导，绝不独立维护。
允许答不了：unanswerable 是合法终态，但必须附原因——否则它是逃避必答题的后门。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 枚举与常量 ────────────────────────────────────────────

# 母框架 7 维（PRD 母框架；F-R1 四类底线 growth/profitability/valuation/risk 为其子集）
DIMENSIONS = ("growth", "profitability", "moat", "financial_health",
              "valuation", "risk", "management")
PRIORITIES = ("P0", "P1", "P2")
STATUSES = ("pending", "in_progress", "answered", "unanswerable")
TERMINAL_STATUSES = ("answered", "unanswerable")

# 数据需求类型（轻量启动器模式：preloop 不取重数据，loop 按需调用）
DATA_REQUIREMENTS = ("light_evidence", "market_data_required", "calc_required", "memory_verify")
# light_evidence     : 白名单搜索即可回答（preloop 已覆盖）
# market_data_required: 必须正式 loop 调行情/财报（market.get_bundle）
# calc_required       : 必须正式 loop 调指标计算（calc.metric(s) / calc.run_code）
# memory_verify       : 来自认知库，需在研究中验证

# P0 分层纪律：少而准（"不回答就没法做买卖判断"），全是重点等于没有重点
P0_MAX = 8
P0_MIN = 1

# required_evidence 信源等级（S=官方一手 A=权威二手 B=媒体/研报）
EVIDENCE_LEVELS = ("S", "A", "B")

# 菜谱残留检测（步骤号/强制序列字段）
_RECIPE_FIELDS = ("step", "steps", "order", "sequence", "seq", "step_no", "sort")
# completion_rule 循环定义检测（"该问题被回答时即视为已回答"类自引用空话）
_CIRCULAR_RULE_RE = re.compile(r"(已回答|被回答|视为已回答|完成即|回答.{0,6}即)")
# unanswerable 的空话 reason（六个字以内或套话）
_EMPTY_REASON_RE = re.compile(r"^\s*(答不了|无法回答|不知道|没有数据|n/?a)\s*$", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class PlanValidationError(ValueError):
    """计划校验失败（拦截信息必须说明根因，供重试与人工归因）。"""


# ── 数据结构 ──────────────────────────────────────────────

@dataclass
class PlanQuestion:
    """研究问题（PRD 11 字段：出生 4 + 激活 3 + 代码 4）。"""

    id: str
    priority: str                     # 出生：P0/P1/P2
    question: str                     # 出生：问题文本
    dimension: str                    # 出生：7 维枚举
    status: str = "pending"           # 代码：状态机
    # ── 激活字段（转入 in_progress 时必须填写且合法；出生时保持为空）──
    falsification: str = ""           # 证伪条件：什么情况下该问题的核心假设被推翻
    completion_rule: str = ""         # 停止规则：什么信号出现即视为"已回答"
    required_evidence: list[dict] = field(default_factory=list)
                                      # [{type, level(S/A/B), description}]
    # ── 代码字段 ──
    unit_id: str = ""                 # 所属业务单元（split 粒度时挂单元）
    reason: str = ""                  # 新增/降级/答不了的原因
    source: str = "llm"               # llm / adapter / cognition / dynamic / fallback
    memory_id: str = ""                # cognition_atoms.id，产出引用与审计用
    memory_mode: str = ""              # verify / constraint
    data_requirement: str = ""         # 数据需求类型：light_evidence / market_data_required / calc_required / memory_verify
    created_at: str = ""
    updated_at: str = ""
    audit_log: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "priority": self.priority, "question": self.question,
            "dimension": self.dimension, "status": self.status,
            "falsification": self.falsification,
            "completion_rule": self.completion_rule,
            "required_evidence": [dict(e) for e in self.required_evidence],
            "unit_id": self.unit_id, "reason": self.reason, "source": self.source,
            "memory_id": self.memory_id, "memory_mode": self.memory_mode,
            "data_requirement": self.data_requirement,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "audit_log": [dict(a) for a in self.audit_log],
        }


@dataclass
class ResearchPlan:
    """研究计划：P0/P1/P2 问题清单 + 状态机（不是线性步骤菜谱）。"""

    user_goal: str = ""
    facts_version: str = ""
    units: list[str] = field(default_factory=list)
    questions: list[PlanQuestion] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    created_at: str = ""

    # ── 派生（只读，从不落库）────────────────────────────

    @property
    def p0_questions(self) -> list[PlanQuestion]:
        return [q for q in self.questions if q.priority == "P0"]

    @property
    def dimension_coverage(self) -> list[str]:
        """从 questions[].dimension 推导——冗余数组独立维护是一致性 bug 之源。"""
        seen: dict[str, int] = {}
        for q in self.questions:
            seen[q.dimension] = seen.get(q.dimension, 0) + 1
        return sorted(seen)

    @property
    def is_converged(self) -> bool:
        """收敛条件：所有 P0 已回答，或明确答不了（unanswerable 且带 reason）。"""
        p0 = self.p0_questions
        if not p0:
            return False
        return all(
            q.status == "answered"
            or (q.status == "unanswerable" and bool((q.reason or "").strip()))
            for q in p0
        )

    # ── 状态机 ──────────────────────────────────────────

    def get(self, question_id: str) -> PlanQuestion:
        for q in self.questions:
            if q.id == question_id:
                return q
        raise PlanValidationError(f"问题不存在: {question_id}")

    def advance(self, question_id: str, to_status: str, *,
                activation: Optional[dict] = None,
                reason: str = "") -> PlanQuestion:
        """状态迁移：pending → in_progress（须激活）→ answered / unanswerable(须 reason)。

        非法迁移全部拒绝且不污染原状态：跳步、倒退（answered 不可回 pending）、
        非法状态值。
        """
        q = self.get(question_id)
        if to_status not in STATUSES:
            raise PlanValidationError(
                f"非法状态值: {to_status!r}（合法: {', '.join(STATUSES)}）")
        if q.status in TERMINAL_STATUSES:
            raise PlanValidationError(
                f"{q.id} 已处于终态 {q.status}，不可再迁移（防计划倒退）")
        if q.status == "pending":
            if to_status != "in_progress":
                raise PlanValidationError(
                    f"{q.id} 不能从 pending 直接迁移到 {to_status}（须经 in_progress 激活）")
            act = activation or {}
            validate_activation(q, act)
            q.falsification = act.get("falsification", "").strip()
            q.completion_rule = act.get("completion_rule", "").strip()
            q.required_evidence = _norm_evidence(act.get("required_evidence"))
            self._log(q, "advance", q.status, to_status, "")
            q.status = to_status
            q.updated_at = _now_iso()
            return q
        if q.status == "in_progress":
            if to_status == "answered":
                self._log(q, "advance", q.status, to_status, "")
                q.status = to_status
                q.updated_at = _now_iso()
                return q
            if to_status == "unanswerable":
                r = (reason or "").strip()
                if not r:
                    raise PlanValidationError(
                        f"{q.id} 标记「确实答不了」必须附原因——否则它是逃避必答题的后门")
                if _EMPTY_REASON_RE.match(r) or len(r) < 6:
                    self.warnings.append(
                        f"{q.id} 的 unanswerable 原因疑似空话（{r!r}），建议写明具体障碍")
                self._log(q, "advance", q.status, to_status, r)
                q.reason = r
                q.status = to_status
                q.updated_at = _now_iso()
                return q
            raise PlanValidationError(
                f"{q.id} 不能从 in_progress 迁移到 {to_status}")
        raise PlanValidationError(f"{q.id} 状态异常: {q.status!r}")

    # ── 动态更新 ────────────────────────────────────────

    def add_question(self, question: str, priority: str, dimension: str, *,
                     reason: str = "", source: str = "dynamic",
                     unit_id: str = "", memory_id: str = "",
                     memory_mode: str = "") -> PlanQuestion:
        """动态新增问题：必须附 reason（谁在何时为什么加的）。"""
        if not (reason or "").strip():
            raise PlanValidationError("动态新增问题必须附 reason（说明触发原因）")
        if priority not in PRIORITIES:
            raise PlanValidationError(f"非法优先级: {priority!r}")
        if dimension not in DIMENSIONS:
            raise PlanValidationError(f"非法维度: {dimension!r}")
        q = PlanQuestion(
            id=self._next_id(), priority=priority,
            question=(question or "").strip(), dimension=dimension,
            unit_id=unit_id, reason=reason.strip(), source=source,
            memory_id=memory_id, memory_mode=memory_mode,
            created_at=_now_iso(), updated_at=_now_iso())
        self._log(q, "add", "", q.status, reason.strip())
        self.questions.append(q)
        if len(self.p0_questions) > P0_MAX:
            self._converge_p0()
        return q

    def downgrade(self, question_id: str, reason: str, *,
                  to: str = "P1") -> PlanQuestion:
        """降级（P0→P1/P2 等）：留痕；不允许把必答题全降没了。"""
        q = self.get(question_id)
        if not (reason or "").strip():
            raise PlanValidationError("降级必须附 reason（说明降级依据）")
        if to not in PRIORITIES:
            raise PlanValidationError(f"非法优先级: {to!r}")
        if q.priority == "P0" and to != "P0" and len(self.p0_questions) <= P0_MIN:
            raise PlanValidationError(
                "不允许把 P0 全部降级——没有必答题的计划等于没有计划")
        old = q.priority
        q.priority = to
        self._log(q, "downgrade", old, to, reason.strip())
        q.reason = reason.strip()
        q.updated_at = _now_iso()
        return q

    # ── 序列化 ──────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "user_goal": self.user_goal,
            "facts_version": self.facts_version,
            "units": list(self.units),
            "created_at": self.created_at,
            "warnings": list(self.warnings),
            "questions": [q.to_dict() for q in self.questions],
            # 冗余字段按推导输出（评测 F-05：coverage 与明细永远一致）
            "dimension_coverage": self.dimension_coverage,
            "p0_count": len(self.p0_questions),
            "is_converged": self.is_converged,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ResearchPlan":
        """从（可信的）字典重建计划——内部供评测与循环回放使用。"""
        plan = cls(
            user_goal=str(payload.get("user_goal", "")),
            facts_version=str(payload.get("facts_version", "")),
            units=list(payload.get("units") or []),
            created_at=payload.get("created_at") or _now_iso(),
            warnings=list(payload.get("warnings") or []),
        )
        for i, item in enumerate(payload.get("questions") or []):
            q = PlanQuestion(
                id=str(item.get("id") or f"q{i + 1}"),
                priority=item.get("priority", ""),
                question=str(item.get("question", "")),
                dimension=item.get("dimension", ""),
                status=item.get("status", "pending"),
                falsification=str(item.get("falsification", "")),
                completion_rule=str(item.get("completion_rule", "")),
                required_evidence=list(item.get("required_evidence") or []),
                unit_id=str(item.get("unit_id", "")),
                reason=str(item.get("reason", "")),
                source=str(item.get("source", "llm")),
                memory_id=str(item.get("memory_id", "")),
                memory_mode=str(item.get("memory_mode", "")),
                data_requirement=str(item.get("data_requirement", "")),
                created_at=str(item.get("created_at", "")),
                updated_at=str(item.get("updated_at", "")),
                audit_log=list(item.get("audit_log") or []),
            )
            plan.questions.append(q)
        return plan

    # ── 内部 ────────────────────────────────────────────

    def _next_id(self) -> str:
        used = {q.id for q in self.questions}
        n = len(self.questions) + 1
        while f"q{n}" in used:
            n += 1
        return f"q{n}"

    def _log(self, q: PlanQuestion, action: str, frm: str, to: str, reason: str) -> None:
        q.audit_log.append({"ts": _now_iso(), "action": action,
                            "from": frm, "to": to, "reason": reason})

    def _converge_p0(self) -> None:
        """P0 超上限自动收敛：多余的降 P1 并给出收敛建议（F-D3）。"""
        p0s = self.p0_questions
        overflow = p0s[P0_MAX:]
        for q in overflow:
            q.priority = "P1"
            self._log(q, "downgrade", "P0", "P1", "P0 超上限自动收敛")
            q.updated_at = _now_iso()
        if overflow:
            names = "、".join(q.id for q in overflow)
            self.warnings.append(
                f"P0 数量超上限（{len(p0s)} > {P0_MAX}），已自动收敛：{names} 降为 P1"
                f"（全是重点等于没有重点）")


# ── 校验器 ────────────────────────────────────────────────

def _norm_evidence(items: Any) -> list[dict]:
    """required_evidence 归一化：[{type, level, description}]。"""
    if not isinstance(items, (list, tuple)) or not items:
        return []
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise PlanValidationError(
                f"required_evidence 第 {i + 1} 项须为 {{type, level, description}} 对象")
        out.append({"type": str(it.get("type", "")).strip(),
                    "level": str(it.get("level", "B")).strip().upper(),
                    "description": str(it.get("description", "")).strip()})
    return out


def _coerce_dict(v: Any) -> dict:
    """把可能为字符串（LLM 把嵌套对象序列化成字符串）的参数归一化为 dict。

    - 已是 dict → 原样返回；
    - 是 str → 尝试 json.loads；失败（或仍非 dict）返回空 dict；
    - 其余 → 空 dict。
    """
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s:
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
    return {}


def validate_activation(question: PlanQuestion, activation: dict) -> None:
    """激活字段校验（F-D5）：进入 in_progress 的门票，逐题校验、失败只重试该题。

    - falsification 非空；
    - completion_rule 非空且非循环定义（自引用空话 = 没有停止规则）；
    - required_evidence 非空且每项 level ∈ S/A/B；
    - 拒绝信息必须指明具体非法字段。
    """
    if not isinstance(activation, dict):
        activation = _coerce_dict(activation)
    falsification = str(activation.get("falsification", "") or "").strip()
    rule = str(activation.get("completion_rule", "") or "").strip()
    evidence = activation.get("required_evidence")

    if not falsification:
        raise PlanValidationError("激活失败 [falsification]：证伪条件不能为空")
    if not rule:
        raise PlanValidationError("激活失败 [completion_rule]：停止规则不能为空")
    if _CIRCULAR_RULE_RE.search(rule):
        raise PlanValidationError(
            f"激活失败 [completion_rule]：循环定义（{rule[:30]}…）——"
            "停止规则必须引用外部证据信号，不能自引用问题的回答状态")
    if not isinstance(evidence, (list, tuple)) or not evidence:
        raise PlanValidationError(
            "激活失败 [required_evidence]：证据清单不能为空")
    for i, it in enumerate(evidence):
        if not isinstance(it, dict):
            raise PlanValidationError(
                f"激活失败 [required_evidence]：第 {i + 1} 项须为对象")
        level = str(it.get("level", "")).strip().upper()
        if level not in EVIDENCE_LEVELS:
            raise PlanValidationError(
                f"激活失败 [required_evidence]：第 {i + 1} 项等级 {level!r} 非法"
                f"（合法: {', '.join(EVIDENCE_LEVELS)}）")


def validate_plan_payload(payload: dict) -> ResearchPlan:
    """LLM 输出 → 合法 ResearchPlan（出生校验；激活字段此时允许为空）。

    拦截：出生字段缺失/非法、菜谱残留字段、id 重复、P0 为空、
    unanswerable 缺 reason、payload 携带的 dimension_coverage 与明细不一致。
    P0 超上限：自动收敛（降 P1 + 警告 + 收敛建议），不静默通过。
    """
    if not isinstance(payload, dict):
        raise PlanValidationError("计划 payload 须为对象")
    questions_raw = payload.get("questions")
    if not isinstance(questions_raw, list) or not questions_raw:
        raise PlanValidationError("questions 为空——空计划不许进循环")

    # 菜谱残留检测（顶层与问题级）
    recipe_hits = [k for k in payload if str(k).lower() in _RECIPE_FIELDS]
    if recipe_hits:
        raise PlanValidationError(
            f"检测到菜谱残留字段: {recipe_hits}——计划是任务清单，不锁执行顺序")

    plan = ResearchPlan(
        user_goal=str(payload.get("user_goal", "")),
        facts_version=str(payload.get("facts_version", "")),
        units=list(payload.get("units") or []),
        created_at=_now_iso(),
    )

    seen_ids: set[str] = set()
    for i, item in enumerate(questions_raw):
        if not isinstance(item, dict):
            raise PlanValidationError(f"第 {i + 1} 条问题须为对象")
        q_recipe = [k for k in item if str(k).lower() in _RECIPE_FIELDS]
        if q_recipe:
            raise PlanValidationError(
                f"第 {i + 1} 条问题含菜谱残留字段: {q_recipe}（不锁顺序，任一 pending 的 P0 都可作为起点）")
        qid = str(item.get("id") or f"q{i + 1}")
        if qid in seen_ids:
            raise PlanValidationError(f"问题 id 重复: {qid}")
        seen_ids.add(qid)
        priority = str(item.get("priority", "") or "").strip()
        dimension = str(item.get("dimension", "") or "").strip()
        text = str(item.get("question", "") or "").strip()
        if not text:
            raise PlanValidationError(f"问题 {qid} 的 question 为空")
        if priority not in PRIORITIES:
            raise PlanValidationError(
                f"问题 {qid} 优先级非法: {priority!r}（合法: {', '.join(PRIORITIES)}）")
        if dimension not in DIMENSIONS:
            raise PlanValidationError(
                f"问题 {qid} 维度非法: {dimension!r}（合法: {', '.join(DIMENSIONS)}）")
        status = str(item.get("status", "pending") or "pending")
        reason = str(item.get("reason", "") or "").strip()
        if status not in STATUSES:
            raise PlanValidationError(
                f"问题 {qid} 状态非法: {status!r}")
        if status == "unanswerable" and not reason:
            raise PlanValidationError(
                f"问题 {qid} 标记 unanswerable 但未附原因——答不了必须说明为什么")
        data_requirement = str(item.get("data_requirement", "") or "").strip()
        if data_requirement and data_requirement not in DATA_REQUIREMENTS:
            raise PlanValidationError(
                f"问题 {qid} 数据需求类型非法: {data_requirement!r}"
                f"（合法: {', '.join(DATA_REQUIREMENTS)}）")
        plan.questions.append(PlanQuestion(
            id=qid, priority=priority, question=text, dimension=dimension,
            status=status, reason=reason,
            unit_id=str(item.get("unit_id", "") or ""),
            source=str(item.get("source", "llm") or "llm"),
            data_requirement=data_requirement,
            created_at=_now_iso(), updated_at=_now_iso(),
        ))

    # P0 为空：拒绝（没有必答题的计划等于没有计划）
    if not plan.p0_questions:
        raise PlanValidationError(
            "P0 为空——没有必答题的计划等于没有计划（重试或回落 Adapter 问题）")

    # P0 超上限：自动收敛 + 收敛建议（F-D3）
    if len(plan.p0_questions) > P0_MAX:
        plan._converge_p0()

    # 冗余 coverage 一致性（F-05）：带了就以明细为准并警告
    given = payload.get("dimension_coverage")
    if isinstance(given, list) and sorted(set(given)) != plan.dimension_coverage:
        plan.warnings.append(
            f"payload 携带的 dimension_coverage 与明细不一致，已以明细推导为准: "
            f"{plan.dimension_coverage}")

    return plan


# ── LLM 生成 ──────────────────────────────────────────────

PLAN_PROMPT_TEMPLATE = """你是投研研究计划生成器。基于用户目标、公司事实包、研究粒度与手册重点，生成一份「问题清单」式研究计划（不是步骤清单）。

核心纪律：
1. 分层：P0=不回答就没法做买卖判断的问题（3~8 条，少而准）；P1=重要但可后置；P2=锦上添花。
2. 四类底线：P0 合起来至少各含 1 条 增长(growth)、盈利(profitability)、估值(valuation)、风险(risk)。
3. 吃上游：{granularity_note}
4. 听用户：user_goal 决定侧重——估值判断与风险排查的问题清单应明显不同。
5. 听认知：用户既有认知转为验证型问题注入（证伪式措辞：「是否」「多大程度」）。
6. 反共识：事实包中的反共识信号必须变成验证问题（P0/P1，不许沉底 P2）。
7. 不编造：问题前提只能来自事实包/手册；事实包没有的数据不许作为前提（改为「是否有X」的验证式，不引用不存在的数字）。
8. 整体研究（whole）的多业务公司：必须含 ≥1 条业务协同验证问题（如「A 业务与 B 业务的协同是否可量化」）——整体论要拿出整体的证据。
9. 反共识问题的 priority 必须为 P0 或 P1，不许沉底 P2。
10. 与 user_goal 无关的背景信息（如风险排查 goal 下的分红记录）最多放入 P2，不得进入 P0/P1。
11. 严禁引用事实包之外的具体数字——不写「30% 增速」「90% 毛利率」，用「当前水平」「历史区间」等相对表述。
12. 数据需求标记（轻量启动器模式）：每个问题必须标注 data_requirement 字段，取值如下：
    - light_evidence：白名单搜索即可回答（preloop 已覆盖的事实类问题）
    - market_data_required：必须正式 loop 调行情/财报工具（market.get_bundle）才能回答
    - calc_required：必须正式 loop 调指标计算工具（calc.metric / calc.metrics / calc.run_code）才能回答
    - memory_verify：来自用户认知库，需在研究中验证

手册重点问题（必须吸收）：{adapter_questions}
用户既有认知（转为验证问题）：{cognitions}

事实包摘要：{facts_summary}
用户目标：{goal}

只输出 JSON：
{{"questions": [{{"question": "...", "priority": "P0|P1|P2", "dimension": "growth|profitability|moat|financial_health|valuation|risk|management", "data_requirement": "light_evidence|market_data_required|calc_required|memory_verify"}}]}}"""

# fallback 底线问题（生成失败时兜底；四类覆盖保证 F-R1 底线）
_FALLBACK_CORE = [
    ("未来三年的增长主要靠什么驱动，能否持续", "growth"),
    ("盈利能力（毛利率/ROIC）处于什么水平，利润是否真金白银", "profitability"),
    ("当前估值隐含了怎样的增长与回报假设", "valuation"),
    ("最可能破坏投资逻辑的风险是什么", "risk"),
]

_DIMENSION_HINTS = (
    ("监管", "合规", "牌照", "政策", "风险", "竞争", "替代", "risk"),
    ("增长", "增速", "空间", "渗透", "驱动", "growth"),
    ("估值", "定价", "隐含", "折现", "valuation"),
    ("网络效应", "生态", "护城河", "壁垒", "转换成本", "moat"),
    ("盈利", "毛利", "ROIC", "利润", "现金", "profitability"),
)


def _guess_dimension(text: str) -> str:
    for *kws, dim in _DIMENSION_HINTS:
        if any(k in text for k in kws):
            return dim
    return "risk"


def _llm_client():
    from core.config import settings
    from core.llm import LLMClient
    return LLMClient(settings)


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1) if not s[i:i + 2].isspace()}


def _enforce_anti_consensus(plan: ResearchPlan, signal: str) -> None:
    """反共识纪律的结构兜底：信号对应的问题不许沉底 P2（冷信息必须被验证）。

    prompt 纪律对 LLM 是软约束（温度波动下偶发沉底）；重要纪律用结构保证：
    与反共识信号文本强匹配（bigram 重合 ≥3）且沉在 P2 的问题强制升 P1，
    审计留痕。评测 F-R6 的验收标准。
    """
    if not signal:
        return
    sig_grams = _bigrams(signal)
    for q in plan.questions:
        if q.priority == "P2" and len(sig_grams & _bigrams(q.question)) >= 3:
            q.priority = "P1"
            plan._log(q, "upgrade", "P2", "P1", "反共识信号问题不得沉底 P2")
            q.updated_at = _now_iso()
            plan.warnings.append(
                f"{q.id} 与反共识信号强相关，已从 P2 强制升至 P1"
                f"（冷信息必须变成验证问题）")


def _fallback_plan(goal: str, adapter_questions: list[str],
                   user_cognitions: list[dict] | None) -> ResearchPlan:
    """生成失败回落：手册问题 + 四类底线 + 认知验证问题，保证必答骨架。"""
    plan = ResearchPlan(user_goal=goal, facts_version="fallback",
                        created_at=_now_iso(),
                        warnings=["LLM 生成失败，回落 Adapter 推荐问题 + 四类底线"])
    for q in (adapter_questions or [])[:4]:
        plan.questions.append(PlanQuestion(
            id=plan._next_id(), priority="P0", question=q,
            dimension=_guess_dimension(q), source="adapter",
            created_at=_now_iso(), updated_at=_now_iso()))
    for text, dim in _FALLBACK_CORE:
        plan.questions.append(PlanQuestion(
            id=plan._next_id(), priority="P0", question=text,
            dimension=dim, source="fallback",
            created_at=_now_iso(), updated_at=_now_iso()))
    for cog in (user_cognitions or []):
        stmt = str(cog.get("statement", "")).strip()
        if stmt:
            plan.questions.append(PlanQuestion(
                id=plan._next_id(), priority="P1",
                question=f"验证用户认知：「{stmt}」是否成立（证伪式核查）",
                dimension=_guess_dimension(stmt), source="cognition",
                created_at=_now_iso(), updated_at=_now_iso()))
    return plan


def _guard_absent_premise(plan: "ResearchPlan", facts: dict | None) -> None:
    """数字溯源守卫（SAFE15）：数据缺口不得写成带数字的事实前提。

    facts 的 data_gaps 明确标注某主题为缺口（如「海外收入」）时，
    LLM 生成的问题若既命中该缺口主题、又带具体数字（如「海外收入 200 亿元
    贡献多大」），即判定为把缺口编造成了事实 → 拦截整张计划重试。
    缺口只能变成开放式问题（「海外收入趋势如何」），不能带数字当前提。

    判定口径与 delivery R6 / loop_lint 编数判定一致：只看带单位的
    关键数字（_iter_key_numbers），「近 3 年」这类无单位数字不参与。
    """
    if not facts:
        return
    gaps = facts.get("data_gaps") or facts.get("absent_topics") or []
    if not gaps:
        return
    from toolkit.delivery.submit_conclusion import _iter_key_numbers

    for q in plan.questions:
        text = str(q.question or "")
        hit_gap = any(str(gap) in text for gap in gaps)
        if not hit_gap:
            continue
        has_number = next(_iter_key_numbers(text), None) is not None
        if has_number:
            raise PlanValidationError(
                f"问题 {q.id} 把数据缺口写成带数字的事实前提"
                f"（缺口主题命中 + 关键数字出现）：{text[:40]}…"
                f"——缺口只能作为开放式研究问题，请去掉虚构数字")


def _facts_summary(facts: dict | None) -> str:
    if not facts:
        return "（无事实包）"
    parts = []
    if facts.get("one_line_business"):
        parts.append(f"主营业务: {facts['one_line_business']}")
    segs = facts.get("segments") or []
    if segs:
        seg_text = "、".join(
            f"{s.get('name')}({s.get('revenue_share', '?')})" for s in segs)
        parts.append(f"业务构成: {seg_text}")
    oq = facts.get("open_questions") or []
    if oq:
        parts.append(f"事实包遗留问题: {'；'.join(map(str, oq))}")
    ac = facts.get("anti_consensus_signal")
    if ac:
        parts.append(f"反共识信号（必须变成验证问题）: {ac}")
    gaps = facts.get("data_gaps") or facts.get("absent_topics") or []
    if gaps:
        parts.append(f"数据缺口（不得作为问题前提）: {'；'.join(map(str, gaps))}")
    return "；".join(parts) or "（无有效字段）"


def _plan_update_schema() -> dict:
    """plan.update 工具的 function calling schema（研究循环内驱动状态迁移的唯一出口）。"""
    return {
        "name": "plan.update",
        "description": (
            "更新研究计划：推进问题状态（advance）、动态新增问题（add_question）、"
            "降级问题（downgrade）。所有研究判断与证据回写都必须通过本工具落到计划与证据包，"
            "禁止在思考里自说自话地「认为已完成」而不改计划状态。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "本轮对研究计划的一批状态更新（可一次提交多条）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string",
                                   "enum": ["advance", "add_question", "downgrade"]},
                            "question_id": {"type": "string",
                                            "description": "advance/downgrade 的目标问题 id"},
                            "to": {"type": "string",
                                   "enum": ["in_progress", "answered", "unanswerable"]},
                            "activation": {"type": "object",
                                           "description": "进入 in_progress 的激活字段（falsification/completion_rule/required_evidence）"},
                            "reason": {"type": "string",
                                       "description": "新增/降级/答不了 的原因（必填）"},
                            "question": {"type": "string",
                                         "description": "add_question 的问题文本"},
                            "priority": {"type": "string", "enum": ["P0", "P1", "P2"]},
                            "dimension": {"type": "string", "enum": list(DIMENSIONS)},
                        },
                        "required": ["op"],
                    },
                }
            },
            "required": ["operations"],
        },
    }


def apply_plan_update(plan: "ResearchPlan", operations: list[dict]) -> list[dict]:
    """把 plan.update 的 operations 确定性应用到 ResearchPlan，逐条返回结果。

    每条结果：{op, ok, question_id, field, old, new, error}。
    - advance：field=status，old/new 为状态值；unanswerable 附 reason。
    - add_question：field=question，new=新增 id，error 为空时 question_id=新 id。
    - downgrade：field=priority，old/new 为优先级。
    非法操作单独失败（error 说明根因），不影响同批其他操作；不污染计划原状态。
    """
    results: list[dict] = []
    for op in (operations or []):
        kind = (op or {}).get("op", "")
        qid = str((op or {}).get("question_id", "") or "")
        target = next((q for q in plan.questions if q.id == qid), None)
        before = target.status if target is not None else ""
        base = {"op": kind, "ok": False, "question_id": qid,
                "field": "", "old": "", "new": "", "error": ""}
        try:
            if kind == "advance":
                before_priority = target.priority if target else ""
                activation = _coerce_dict(op.get("activation"))
                plan.advance(qid, op.get("to"),
                             activation=activation,
                             reason=op.get("reason", ""))
                after = next(q for q in plan.questions if q.id == qid)
                base.update(ok=True, field="status", old=before, new=after.status)
                if op.get("to") == "unanswerable":
                    base["reason"] = after.reason
                elif op.get("to") == "in_progress":
                    base["old"] = f"{before}({before_priority})"
                    base["new"] = f"{after.status}(activated)"
            elif kind == "add_question":
                added = plan.add_question(
                    str(op.get("question", "")), str(op.get("priority", "P1")),
                    str(op.get("dimension", "risk")),
                    reason=op.get("reason", ""), source="dynamic",
                    unit_id=str(op.get("unit_id", "")))
                base.update(ok=True, question_id=added.id, field="question",
                            old="", new=added.id)
            elif kind == "downgrade":
                plan.downgrade(qid, op.get("reason", ""), to=op.get("to", "P1"))
                after = next(q for q in plan.questions if q.id == qid)
                base.update(ok=True, field="priority",
                            old=str(op.get("from", target.priority if target else "")),
                            new=after.priority)
            else:
                base["error"] = f"未知操作: {kind!r}"
        except PlanValidationError as e:
            base["error"] = str(e)
        results.append(base)
    return results


async def generate_research_plan(
    *,
    goal: str,
    entity: Optional[dict] = None,
    facts: Optional[dict] = None,
    adapter_questions: Optional[list[str]] = None,
    user_cognitions: Optional[list[dict]] = None,
    granularity: Optional[dict] = None,
    llm_client: Any = None,
    force_fail: bool = False,
    llm_timeout_seconds: float = 45,
) -> ResearchPlan:
    """生成研究计划：LLM（出生字段）→ 校验 → 失败重试一次 → 仍失败回落 fallback。

    llm_client 不可用 / 生成失败 / force_fail 时走 fallback（确定性，供测试）。
    """
    adapter_questions = list(adapter_questions or [])
    user_cognitions = list(user_cognitions or [])
    signal = str((facts or {}).get("anti_consensus_signal", "") or "")
    mode = (granularity or {}).get("mode", "whole")
    granularity_note = ("split 粒度：每个业务单元至少 1 条问题（问题须点名业务线）。"
                        if mode == "split" else "whole 粒度：整体研究，但主要业务板块的增长与盈利仍须被问题触及。")

    if not force_fail:
        prompt = PLAN_PROMPT_TEMPLATE.format(
            granularity_note=granularity_note,
            adapter_questions="；".join(adapter_questions) or "（无）",
            cognitions="；".join(str(c.get("statement", "")) for c in user_cognitions) or "（无）",
            facts_summary=_facts_summary(facts), goal=goal)
        for attempt in (1, 2):  # 失败重试一次（代价=整张计划）
            try:
                client = llm_client or _llm_client()
                data = await client.chat_json(
                    system="你是资深买方研究员，只输出 JSON。",
                    user=prompt, temperature=0.1, timeout=llm_timeout_seconds)
                plan = validate_plan_payload({
                    "user_goal": goal,
                    "facts_version": str((facts or {}).get("facts_version", "v1")),
                    "units": [u.get("id", "") for u in (granularity or {}).get("units", [])]
                    if mode == "split" else [],
                    "questions": data.get("questions") or [],
                })
                _guard_absent_premise(plan, facts)
                _enforce_anti_consensus(plan, signal)
                _infer_data_requirements(plan)  # 轻量启动器：自动推断未标记的数据需求类型
                return plan
            except PlanValidationError as e:
                logger.warning("计划校验失败（第 %d 次）: %s", attempt, e)
                if attempt == 2:
                    break
            except Exception as e:  # noqa: BLE001 - LLM 不可用/输出解析失败 → fallback
                logger.warning("计划生成失败（第 %d 次，回落 fallback）: %s", attempt, e)

    fb = _fallback_plan(goal, adapter_questions, user_cognitions)
    _enforce_anti_consensus(fb, signal)
    _infer_data_requirements(fb)  # fallback 也推断
    return fb


# ── 数据需求自动推断（轻量启动器后处理）─────────────────────

_FINANCIAL_KEYWORDS = (
    "营收", "收入", "利润", "毛利率", "净利率", "ROIC", "ROE",
    "现金流", "EBIT", "EBITDA", "资产负债率", "估值", "PE", "PB",
    "PS", "EV/EBITDA", "DCF", "市盈率", "市净率", "财务", "盈利能力",
    "资产回报", "资本回报", "费用率", "研发投入", "capex", "CapEx",
    "revenue", "profit", "margin", "cash flow", "earnings", "valuation",
)

_CALC_KEYWORDS = (
    "ROIC", "ROE", "现金质量", "UE", "unit economics",
    "可支撑月数", "burn rate", "烧钱速度", "单位经济模型",
)


def _infer_data_requirements(plan: ResearchPlan) -> None:
    """为未标记 data_requirement 的问题自动推断数据需求类型。

    推断规则（基于 dimension + question 关键词）：
      - 含财务/估值关键词 → market_data_required
      - 含指标计算关键词 → calc_required
      - source=cognition → memory_verify
      - 其余 → light_evidence
    """
    for q in plan.questions:
        if q.data_requirement:  # LLM 已标记则跳过
            continue
        text = f"{q.question} {q.dimension}".lower()
        if any(kw in text for kw in _CALC_KEYWORDS):
            q.data_requirement = "calc_required"
        elif any(kw in text for kw in _FINANCIAL_KEYWORDS):
            q.data_requirement = "market_data_required"
        elif q.source == "cognition":
            q.data_requirement = "memory_verify"
        else:
            q.data_requirement = "light_evidence"
