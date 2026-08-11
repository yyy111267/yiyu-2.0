"""
delivery.submit_conclusion —— 硬规则安全门（最高优先级）

所有"提交结论"类写操作都必须先过本工具校验。校验失败的文本**不得**返回给用户。
本模块同时以两种形态提供：
  - `SubmitConclusionTool(Tool)`：供 LLM 通过 tool calling 调用（纳入 registry/executor）。
  - `validate_conclusion(...)`：供 runtime/loop.py 在 emit FINAL_ANSWER 前直接调用。

硬规则（代码级，不可被 prompt 绕过）：
  R1  G5 / C 级（或 first_principles 模式）禁止出现"买入"类结论。
  R2  六关存在 FAIL，或镜子测试不足 5 句时，禁止"买入"类结论。
  R3  结论必须包含「AI 置信度 / 投资确定性」区分声明。
  R4  禁止给出具体买卖价 / 目标价（正则拦截）。
  R5  data 降级 / C 级时，结论须含「一手验证」指引。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from toolkit.base import Tool, ToolResult, ToolSchema, WriteTool
from toolkit.permission import PermissionLevel, PermissionResult


# ── 硬规则元数据（供 evaluation/gate_bypass.py 做攻击回归）─────────────
HARD_RULES = {
    "R1_no_buy_for_g5_c": "G5/C/第一性原理 模式禁止买入结论",
    "R2_no_buy_on_fail_or_short_mirror": "存在 FAIL 或镜子<5 句时禁止买入结论",
    "R3_must_disclaim_confidence": "必须区分 AI 置信度 vs 投资确定性",
    "R4_no_price_target": "禁止具体买卖价 / 目标价",
    "R5_need_primary_verification": "降级/C 级须有一手验证指引",
}

# 买入类判定词（verdict 或结论文本中的强买入信号）
_BUY_VERDICTS = {"买入", "强烈买入", "建仓", "重仓买入"}
_BUY_TEXT_PATTERN = re.compile(r"(强烈)?(建议|推荐)?(买入|建仓|满仓|加仓买入)", re.IGNORECASE)
# 否定语境：如「而非可买入」「不构成买入」「不建议买入」「非买入」「暂不建仓」等。
# 这类表述是"不买"，不是买入信号，须先从结论文本中剔除再判断 is_buy。
_NEG_BUY_PATTERN = re.compile(
    r"(而非|并非|不是|不代表|不构成|不建议|不推荐|暂不|避免|勿|禁止|拒绝)"
    r"[^。；;，,]{0,6}?(买入|建仓|满仓|加仓买入)",
    re.IGNORECASE,
)

# 目标价 / 买卖点位（如 "目标价 ￥120"、"跌破 30 元买入"）
_PRICE_PATTERN = re.compile(
    r"(目标价|止盈|止损|买入价|卖出价|建仓位|支撑位|压力位|跌破|涨破|突破|站上|回踩)"
    r"[^。]{0,12}?[¥￥\$]?\s*\d+(\.\d+)?\s*(元|块|刀|usd| RMB)?",
    re.IGNORECASE,
)
# 价位+动作组合（如 "30元买入"、"120 元卖出"）
_PRICE_ACTION_PATTERN = re.compile(
    r"[¥￥\$]?\s*\d+(\.\d+)?\s*(元|块|刀|usd)?\s*(买入|卖出|建仓|清仓)",
    re.IGNORECASE,
)

_DISCLAIMER_PATTERN = re.compile(r"(AI\s*置信度|投资确定性|模型.*确定|置信度.*投资)", re.IGNORECASE)
_VERIFY_PATTERN = re.compile(r"(一手验证|需.*验证|待验证|验证节点|去.*查)", re.IGNORECASE)


@dataclass
class ValidationResult:
    passed: bool
    violated_rules: list[str] = field(default_factory=list)  # 触发的硬规则 id
    reasons: list[str] = field(default_factory=list)         # 人类可读说明

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "violated_rules": self.violated_rules,
            "reasons": self.reasons,
        }


def validate_conclusion(
    conclusion: str,
    tier: str = "",
    info_richness: str = "",
    research_mode: str = "",
    checklist_json: Optional[str] = None,
    mirror_json: Optional[str] = None,
    verdict: Optional[str] = None,
    data_status: str = "ok",
) -> ValidationResult:
    """
    校验结论是否违反硬规则。

    Args:
        conclusion: 结论文本（或 JSON 字符串）。
        tier: 行业分层 G1–G6。
        info_richness: 信息丰富度 A/B/C。
        research_mode: full / g5 / first_principles。
        checklist_json: 六关 JSON 字符串（可选，用于 R2）。
        mirror_json: 镜子测试 JSON 字符串（可选，用于 R2）。
        verdict: 显式判定词（可选，优先于文本推断）。
        data_status: ok / partial / degraded / unknown。
    """
    violated: list[str] = []
    reasons: list[str] = []

    verdict_norm = (verdict or "").strip()
    # 剔除否定语境（「而非可买入」「不构成买入」等不算买入信号）后再匹配买入词
    conclusion_clean = _NEG_BUY_PATTERN.sub("", conclusion)
    is_buy = verdict_norm in _BUY_VERDICTS or bool(_BUY_TEXT_PATTERN.search(conclusion_clean))
    is_g5_c = (tier.upper() == "G5" or info_richness.upper() == "C"
               or research_mode in ("g5", "first_principles"))

    # R1：G5/C/第一性原理 模式禁止买入
    if is_buy and is_g5_c:
        violated.append("R1_no_buy_for_g5_c")
        reasons.append("标的为 G5/C 或第一性原理模式，结论禁止为「买入」类。")

    # R2：存在 FAIL 或镜子不足 5 句时禁止买入
    fail_present = False
    mirror_count = None
    if checklist_json:
        try:
            data = json.loads(checklist_json) if isinstance(checklist_json, str) else checklist_json
            gates = data.get("checklist", [])
            fail_present = any(str(g.get("result", "")).lower() == "fail" for g in gates)
        except (json.JSONDecodeError, AttributeError):
            pass
    if mirror_json:
        try:
            m = json.loads(mirror_json) if isinstance(mirror_json, str) else mirror_json
            mirror_count = len(m.get("mirror_test", []))
        except (json.JSONDecodeError, AttributeError):
            pass
    if is_buy and (fail_present or (mirror_count is not None and mirror_count != 5)):
        violated.append("R2_no_buy_on_fail_or_short_mirror")
        if fail_present:
            reasons.append("六关存在 FAIL，不得给买入结论。")
        if mirror_count is not None and mirror_count != 5:
            reasons.append(f"镜子测试须恰好 5 句，当前 {mirror_count} 句。")

    # R3：必须区分 AI 置信度 vs 投资确定性
    if not _DISCLAIMER_PATTERN.search(conclusion):
        violated.append("R3_must_disclaim_confidence")
        reasons.append("结论须声明「AI 置信度」与「投资确定性」的区别。")

    # R4：禁止具体买卖价 / 目标价
    if _PRICE_PATTERN.search(conclusion) or _PRICE_ACTION_PATTERN.search(conclusion):
        violated.append("R4_no_price_target")
        reasons.append("结论禁止出现具体目标价 / 买卖点位。")

    # R5：降级 / C 级须有一手验证指引
    degraded = data_status in ("degraded", "unknown", "partial") or info_richness.upper() == "C"
    if degraded and not _VERIFY_PATTERN.search(conclusion):
        violated.append("R5_need_primary_verification")
        reasons.append("数据降级或 C 级时，结论须包含一手验证指引。")

    return ValidationResult(
        passed=len(violated) == 0,
        violated_rules=violated,
        reasons=reasons,
    )


class SubmitConclusionTool(WriteTool):
    """硬规则安全门：提交结论前的最后一道代码级校验。"""

    schema = ToolSchema(
        name="delivery.submit_conclusion",
        description=(
            "提交最终研究结论前的硬性合规校验。传入结论文本与研究元数据，"
            "校验通过才允许返回用户；触发硬规则时返回失败原因。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "conclusion": {"type": "string", "description": "结论文本（或 JSON 字符串）"},
                "tier": {"type": "string", "description": "行业分层 G1–G6，可空"},
                "info_richness": {"type": "string", "description": "信息丰富度 A/B/C，可空"},
                "research_mode": {"type": "string", "description": "full/g5/first_principles，可空"},
                "checklist_json": {"type": "string", "description": "六关 JSON，可选"},
                "mirror_json": {"type": "string", "description": "镜子测试 JSON，可选"},
                "verdict": {"type": "string", "description": "显式判定词，可选"},
                "data_status": {"type": "string", "description": "ok/partial/degraded/unknown，默认 ok"},
            },
            "required": ["conclusion"],
        },
        read_only=False,
    )

    async def execute(
        self,
        conclusion: str,
        tier: str = "",
        info_richness: str = "",
        research_mode: str = "",
        checklist_json: Optional[str] = None,
        mirror_json: Optional[str] = None,
        verdict: Optional[str] = None,
        data_status: str = "ok",
        **kwargs: Any,
    ) -> dict:
        result = validate_conclusion(
            conclusion=conclusion,
            tier=tier,
            info_richness=info_richness,
            research_mode=research_mode,
            checklist_json=checklist_json,
            mirror_json=mirror_json,
            verdict=verdict,
            data_status=data_status,
        )
        if result.passed:
            return {"ok": True, "passed": True, "message": "硬规则校验通过，结论可返回。"}
        return {
            "ok": False,
            "passed": False,
            "violated_rules": result.violated_rules,
            "reasons": result.reasons,
            "message": "结论违反硬规则，已拦截，请修正后重试。",
        }

    # 工具执行后由 executor 统一封装为 ToolResult；这里提供同步校验入口供 loop 直接调用
    def check(self, **kwargs) -> ValidationResult:
        return validate_conclusion(**kwargs)


# 与 permission.py 的 SENSITIVE_OPERATIONS 对齐：该操作强制走本硬规则
def permission_hook(operation: str) -> Optional[PermissionResult]:
    if operation == "delivery.submit_conclusion":
        return PermissionResult(
            allowed=True,
            level=PermissionLevel.CONFIRM,
            reason="将通过硬规则校验（toolkit.delivery.submit_conclusion）",
        )
    return None
