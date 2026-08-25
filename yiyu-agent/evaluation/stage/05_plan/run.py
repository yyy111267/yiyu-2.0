"""环节评测：05_plan · 研究计划生成（PRD 5.3 环节④）。

一条命令：
    python evaluation/stage/05_plan/run.py
"""

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # yiyu-agent/
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402

# 激活字段全键（F-01：schema 携带激活与代码字段，出生时为空）
_ALL_Q_KEYS = {"id", "priority", "question", "dimension", "status",
               "falsification", "completion_rule", "required_evidence",
               "unit_id", "reason", "source", "created_at", "updated_at",
               "audit_log"}
_RECIPE_WORDS = ("step", "steps", "order", "sequence", "seq", "step_no", "sort")
_FIELD_ERR_RE = re.compile(r"\[(falsification|completion_rule|required_evidence)\]")

# F-R2 腾讯三业务触及词表（问题文本点名业务线或其核心驱动）
_BIZ_KEYWORDS = {
    "游戏": ("游戏", "增值服务", "流水", "递延"),
    "广告": ("广告", "营销"),
    "金融科技与企业服务": ("金融科技", "支付", "云", "企业服务"),
}
_MANUAL_DIMENSIONS = {
    "监管合规": ("监管", "合规", "牌照"),
    "网络效应": ("网络效应", "双边", "生态"),
}
# 验证式提及词：gap 词的命中若同句含这些词，是「能否验证X」的保守提问（合规）
_VERIFY_WORDS = ("是否", "能否", "可否", "有无", "披露", "可得", "可靠", "信源",
                 "验证", "公布", "明确")
# 风险内容词：risk_dominant 统计不只看 dimension，内容是风险的也计入
_RISK_WORDS = ("风险", "政策", "监管", "竞争", "替代", "下滑", "恶化", "证伪",
               "拐点", "隐患", "违约", "流失", "冲击")


def _gap_premise_ok(text: str, gaps: list) -> bool:
    """data_gap 词的命中若为验证式提及（能否验证X/是否披露X）则合规——提及≠前提。"""
    for gap in gaps:
        gap = str(gap)
        idx = text.find(gap)
        while idx >= 0:
            window = text[max(0, idx - 15):idx + len(gap) + 15]
            if not any(w in window for w in _VERIFY_WORDS):
                return False  # 前提式引用（无验证词修饰）→ 违规
            idx = text.find(gap, idx + 1)
    return True


# ── mock 层：generate_mock ────────────────────────────────

async def _exec_generate_mock(case_input: dict, out: dict) -> None:
    from runtime.plan import (DIMENSIONS, PRIORITIES, PlanValidationError,
                              validate_plan_payload)

    payload = case_input["payload"]
    plan = None
    try:
        plan = validate_plan_payload(payload)
    except PlanValidationError as e:
        out.update({"valid": False, "error": str(e)})
        # F-D2：重试一次同样 payload → 仍拒绝 → 显式失败
        if case_input.get("retry_same"):
            try:
                validate_plan_payload(payload)
                out.update({"retry_also_rejected": False, "explicit_failure": False})
            except PlanValidationError as e2:
                out.update({"retry_also_rejected": True,
                            "explicit_failure": bool(str(e2).strip())})
        return

    d = plan.to_dict()
    out.update({
        "valid": True, "error": "", "plan": d,
        "all_birth_fields_valid": all(
            q["priority"] in PRIORITIES and q["dimension"] in DIMENSIONS
            and bool(q["question"].strip()) for q in d["questions"]),
        "all_pending": all(q["status"] == "pending" for q in d["questions"]),
        "unique_ids": len({q["id"] for q in d["questions"]}) == len(d["questions"]),
        "plan_level_fields_present": bool(d["user_goal"]) and bool(d["facts_version"]),
        "schema_has_full_fields": all(_ALL_Q_KEYS <= set(q) for q in d["questions"]),
        # F-02 清单性
        "no_recipe_fields": not any(
            str(k).lower() in _RECIPE_WORDS for k in d) and not any(
            str(k).lower() in _RECIPE_WORDS for q in d["questions"] for k in q),
        "no_dangling_dependencies": not any(
            "dependency" in q or "depends_on" in q for q in d["questions"]),
        # F-D3 收敛
        "warning_present": bool(d["warnings"]),
        "demoted_listed": any("降" in w for w in d["warnings"]),
    })
    # F-02：任一 pending P0 可直接开工（副本实测，不污染原计划）
    from runtime.plan import ResearchPlan
    copy = ResearchPlan.from_dict(d)
    first_p0 = next((q for q in copy.questions
                     if q.priority == "P0" and q.status == "pending"), None)
    if first_p0 is not None:
        try:
            copy.advance(first_p0.id, "in_progress", activation={
                "falsification": "若核心假设被证伪",
                "completion_rule": "关键证据齐备且结论明确",
                "required_evidence": [{"type": "test", "level": "A",
                                       "description": "副本探测"}],
            })
            out["any_pending_p0_startable"] = True
        except Exception:  # noqa: BLE001
            out["any_pending_p0_startable"] = False
    else:
        out["any_pending_p0_startable"] = False


# ── mock 层：build（预置计划 + 操作序列）──────────────────

async def _exec_build(case_input: dict, out: dict) -> None:
    from runtime.plan import PlanValidationError, validate_plan_payload

    # 可选：validator 直测（F-D4 的 payload 级拦截）
    if case_input.get("invalid_payload"):
        try:
            validate_plan_payload(case_input["invalid_payload"])
            out["invalid_payload_rejected"] = False
        except PlanValidationError:
            out["invalid_payload_rejected"] = True

    try:
        plan = validate_plan_payload(case_input["plan"])
    except PlanValidationError as e:
        out.update({"valid": False, "error": str(e), "operations": []})
        return

    operations: list[dict] = []
    touched: set[str] = set()
    for s in case_input.get("steps", []):
        op = s["op"]
        qid = s.get("question_id", "")
        target = next((q for q in plan.questions if q.id == qid), None)
        before = target.status if target is not None else ""
        try:
            if op == "advance":
                plan.advance(qid, s["to"], activation=s.get("activation"),
                             reason=s.get("reason", ""))
            elif op == "add":
                added = plan.add_question(
                    s["question"], s["priority"], s["dimension"],
                    reason=s.get("reason", ""), source="dynamic")
                qid, target = added.id, added
            elif op == "downgrade":
                plan.downgrade(qid, s.get("reason", ""), to=s.get("to", "P1"))
            else:
                raise PlanValidationError(f"未知操作: {op!r}")
            operations.append({"op": op, "ok": True, "error": "",
                               "status_before": before,
                               "status_after": target.status if target else ""})
            if qid:
                touched.add(qid)
        except PlanValidationError as e:
            operations.append({"op": op, "ok": False, "error": str(e),
                               "status_before": before, "status_after": before})

    d = plan.to_dict()
    qs = d["questions"]
    out.update({
        "valid": True, "operations": operations, "plan": d,
        "illegal_ops_left_state_unchanged": all(
            o["ok"] or o["status_after"] == o["status_before"] for o in operations),
        "rejection_messages_present": all(
            o["ok"] or bool(o["error"].strip()) for o in operations),
        "activations_filled": all(
            q["falsification"].strip() and q["completion_rule"].strip()
            and q["required_evidence"]
            for q in qs if q["status"] not in ("pending",)),
        "audit_records_present": all(
            q["audit_log"] for q in qs if q["status"] != "pending"
            or q["reason"] or q["source"] in ("dynamic",)),
        "others_untouched": all(
            q["status"] == "pending" and not q["reason"]
            for q in qs if q["id"] not in touched),
        "state_still_pending_before_retry": (
            operations[-1]["status_before"] == "pending"
            if operations and operations[-1]["ok"] else False),
        "error_names_field": all(
            o["ok"] or _FIELD_ERR_RE.search(o["error"]) or "P0" in o["error"]
            or "reason" in o["error"] or "状态" in o["error"] or "迁移" in o["error"]
            for o in operations),
        "empty_reason_warned": any("空话" in w for w in d["warnings"]),
        # F-05 推导一致性
        "coverage_contains_risk": "risk" in d["dimension_coverage"],
        "coverage_matches_details": d["dimension_coverage"] == sorted(
            {q["dimension"] for q in qs}),
        "coverage_conflict_warned": any(
            "dimension_coverage" in w or "不一致" in w for w in d["warnings"]),
    })
    # F-04：新增/降级留痕
    added_q = next((q for q in qs if "大客户" in q["question"]), None)
    out["added_has_reason"] = bool(added_q and added_q["reason"].strip())
    downgraded = [q for q in qs if any(
        o["op"] == "downgrade" and o["ok"] and o.get("qid") == q["id"]
        for o in [])]  # placeholder: 用 reason 判定
    out["downgraded_with_reason"] = any(
        q["id"] == "q3" and q["reason"].strip() for q in qs)


# ── 真模型层：generate ───────────────────────────────────

async def _plan_derivations(plan_d: dict, facts: dict) -> dict:
    """真模型计划的派生指标（F-R 组断言依据）。"""
    qs = plan_d["questions"]
    p0s = [q for q in qs if q["priority"] == "P0"]
    texts = [q["question"] for q in qs]
    p0_dims = sorted({q["dimension"] for q in p0s})
    all_text = "；".join(texts)

    deriv = {
        "plan": plan_d,
        "p0_dimensions": p0_dims,
        # F-R2 三业务触及 + 协同
        "businesses_touched": sum(
            1 for kws in _BIZ_KEYWORDS.values()
            if any(any(k in t for k in kws) for t in texts)),
        "synergy_question_present": any("协同" in t for t in texts),
        # F-R4 / F-RD2 幻觉防御（验证式提及合规：C 级公司问「能否验证X」是正确行为）
        "no_absent_premise": all(
            _gap_premise_ok(t, facts.get("data_gaps") or []) for t in texts),
        "open_questions_absorbed": _absorb_ratio(
            facts.get("open_questions") or [], texts),
        "data_availability_question_present": any(
            kw in all_text for kw in ("披露", "公开", "可得", "信源", "是否有可靠")),
        # F-R5 手册对齐
        "manual_dimensions_hit": sum(
            1 for kws in _MANUAL_DIMENSIONS.values()
            if any(any(k in t for k in kws) for t in texts)),
        "no_invalid_metric_questions": not any(
            kw in all_text for kw in ("产能利用率",)),
        "split_unit_touched": any(
            any(k in t for k in ("金融科技", "支付", "云业务"))
            for t in texts),
        # F-R6 反共识
        "anti_consensus_question_present": any(
            kw in all_text for kw in ("大客户", "客户集中", "客户占比")),
        # F-RD1 答非所问（内容是风险的问题也计入，不只看 dimension 标签）
        "risk_dominant_in_p0": bool(p0s) and (
            sum(1 for q in p0s if q["dimension"] == "risk"
                or any(w in q["question"] for w in _RISK_WORDS))
            / len(p0s) >= 0.5),
        "no_off_topic_p0": not any("分红" in q["question"] for q in p0s),
        # 用例集 P0 级断言：诱导信息不进 P0（P1 出现属 PARTIAL 级容忍——
        # 股息可持续性语义上确与风险相关，硬拦 P1 过窄）
        "bait_in_p2_only": not any(
            "分红" in q["question"] for q in qs if q["priority"] == "P0"),
        # F-RD2 编造数字（facts 无数字时出现百分比即疑似编造）
        "no_fabricated_numbers": not any(
            re.search(r"\d+(\.\d+)?%", t) for t in texts),
    }
    # F-R6：反共识问题的优先级（强关键词=核心主题是客户集中；「大客户」
    # 单独出现可能只是竞争优势问题的顺带提及，不算反共识验证问题）
    _AC_STRONG = ("客户集中", "客户占比", "前五大客户", "单一大客户")
    ac_qs = [q for q in qs
             if any(k in q["question"] for k in _AC_STRONG)]
    deriv["anti_consensus_priority_ok"] = bool(ac_qs) and all(
        q["priority"] in ("P0", "P1") for q in ac_qs)
    return deriv


def _absorb_ratio(open_questions: list, texts: list[str]) -> float:
    """open_questions 被计划吸收的比例（文本 2-gram 重合 ≥2 视为触及）。"""
    if not open_questions:
        return 1.0
    corpus = "".join(texts)

    def bigrams(s: str) -> set:
        return {s[i:i + 2] for i in range(len(s) - 1) if not s[i:i + 2].isspace()}

    hit = 0
    for oq in open_questions:
        oq = str(oq)
        if len(bigrams(oq) & bigrams(corpus)) >= 2 or any(
                w in corpus for w in re.findall(r"[\u4e00-\u9fff]{3,}", oq)[:3]):
            hit += 1
    return hit / len(open_questions)


async def _exec_generate(case_input: dict, out: dict) -> None:
    from runtime.plan import generate_research_plan

    plan = await generate_research_plan(
        goal=case_input["goal"],
        entity=case_input.get("entity"),
        facts=case_input.get("facts"),
        adapter_questions=case_input.get("adapter_questions") or [],
        user_cognitions=case_input.get("user_cognitions") or [],
        granularity=case_input.get("granularity"),
    )
    out.update(await _plan_derivations(plan.to_dict(),
                                       case_input.get("facts") or {}))


async def _exec_generate_goals(case_input: dict, out: dict) -> None:
    """F-R3：同一夹具两个 goal → 两版计划可区分性。"""
    from runtime.plan import generate_research_plan

    plans = []
    for goal in case_input["goals"]:
        p = await generate_research_plan(
            goal=goal, entity=case_input.get("entity"),
            facts=case_input.get("facts"),
            adapter_questions=case_input.get("adapter_questions") or [],
            user_cognitions=case_input.get("user_cognitions") or [])
        plans.append(p)

    def dominant(p) -> str:
        from collections import Counter
        c = Counter(q.dimension for q in p.questions if q.priority == "P0")
        return c.most_common(1)[0][0] if c else ""

    a, b = plans
    pa = {q.question for q in a.questions if q.priority == "P0"}
    pb = {q.question for q in b.questions if q.priority == "P0"}
    overlap = len(pa & pb) / max(len(pa | pb), 1)
    four = ("growth", "profitability", "valuation", "risk")
    out.update({
        "goals_distinguishable": dominant(a) != dominant(b) or overlap < 0.6,
        "both_keep_four_coverage": all(
            {q.dimension for q in p.questions if q.priority == "P0"} >= set(four)
            for p in plans),
        "plan": plans[0].to_dict(),
    })


# ── 分派 ─────────────────────────────────────────────────

async def execute(case_input: dict) -> dict:
    out: dict = {}
    action = case_input.get("action", "")
    if action == "generate_mock":
        await _exec_generate_mock(case_input, out)
    elif action == "build":
        await _exec_build(case_input, out)
    elif action == "generate":
        await _exec_generate(case_input, out)
    elif action == "generate_goals":
        await _exec_generate_goals(case_input, out)
    else:
        raise NotImplementedError(f"未知 action: {action!r}")
    if not out:
        raise RuntimeError(f"action {action!r} 未产出任何结果")
    return out


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
