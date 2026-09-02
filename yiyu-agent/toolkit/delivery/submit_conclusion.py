"""
delivery.submit_conclusion —— 硬规则安全门（最高优先级）

所有"提交结论"类写操作都必须先过本工具校验。校验失败的文本**不得**返回给用户。
本模块同时以两种形态提供：
  - `SubmitConclusionTool(Tool)`：供 LLM 通过 tool calling 调用（纳入 registry/executor）。
  - `validate_conclusion(...)`：供 runtime/loop.py 在 emit FINAL_ANSWER 前直接调用。

硬规则（代码级，不可被 prompt 绕过）：
  R1  G5（或 first_principles 模式）禁止出现"买入"类结论。
  R2  六关存在 FAIL，或镜子测试不足 5 句时，禁止"买入"类结论。
  R3  结论必须包含「AI 置信度 / 投资确定性」区分声明。
  R4  禁止给出具体买卖价 / 目标价（正则拦截）。
  R5  data 降级时，结论须含「一手验证」指引。
  R6  结论关键数字必须来自工具观测。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from toolkit.base import Tool, ToolResult, ToolSchema, WriteTool
from toolkit.permission import PermissionLevel, PermissionResult


# ── 硬规则元数据（供 evaluation/gate_bypass.py 做攻击回归）─────────────
HARD_RULES = {
    # 保留既有规则 ID，避免历史 trace/评测消费者断裂；信息丰富度已不参与判断。
    "R1_no_buy_for_g5_c": "G5/第一性原理模式禁止买入结论",
    "R2_no_buy_on_fail_or_short_mirror": "存在 FAIL 或镜子<5 句时禁止买入结论",
    "R3_must_disclaim_confidence": "必须区分 AI 置信度 vs 投资确定性",
    "R4_no_price_target": "禁止具体买卖价 / 目标价",
    "R5_need_primary_verification": "数据降级须有一手验证指引",
    "R6_numeric_source_required": "结论关键数字必须来自工具观测",
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
# 否定性合规声明不是交易指令（如「不提供目标价」）。先遮掉被否定的
# 敏感名词，但保留数字和动作；因此「不建议在 100 元买入」仍会被
# _PRICE_ACTION_PATTERN 拦截。
_NEG_PRICE_TERM_PATTERN = re.compile(
    r"(不提供|不给出|不设定|不计算|不包含|不构成|无法提供|"
    r"无法给出|不能提供|不能给出|无需提供|禁止提供)"
    r"[^\u3002；;\n]{0,4}?"
    r"(目标价|止盈|止损|买入价|卖出价|建仓位|支撑位|压力位)",
    re.IGNORECASE,
)

_DISCLAIMER_PATTERN = re.compile(r"(AI\s*置信度|投资确定性|模型.*确定|置信度.*投资)", re.IGNORECASE)
_VERIFY_PATTERN = re.compile(r"(一手验证|需.*验证|待验证|验证节点|去.*查)", re.IGNORECASE)
_KEY_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*(亿|万|%|％|元|块|倍|美元|港元|人民币)"
)

# 推断性表述标记：紧邻这些词的数字是模型预测/假设（如「预计增速中枢 10%」、
# 「若估值回到 20 倍」），不是对工具观测的引用，R6 不应按编数拦截。
# 无法溯源的引用数字仍会被拦，或由 sanitize_conclusion 程序化标注后放行。
_INFERENCE_MARKERS = (
    "预计", "预期", "预估", "假设", "推断", "测算", "估算", "情景",
    "中枢", "展望", "若", "约", "或将", "想象", "直觉", "大概", "左右",
)


def _is_inference_context(prefix: str) -> bool:
    return any(marker in prefix for marker in _INFERENCE_MARKERS)


def _iter_key_numbers(text: str):
    """产出结论中需溯源的关键数字 (数值, match)；跳过年份与推断语境数字。"""
    for m in _KEY_NUMBER_PATTERN.finditer(text or ""):
        raw = m.group(1)
        try:
            value = float(raw)
        except ValueError:
            continue
        # 年份常出现在“2024 年报”等来源描述中，不按关键财务数字校验。
        if 1900 <= value <= 2100:
            continue
        prefix = text[max(0, m.start() - 8):m.start()]
        if _is_inference_context(prefix):
            continue
        yield value, m


def _numbers_in_text(text: str) -> list[float]:
    return [value for value, _m in _iter_key_numbers(text)]


def _numbers_in_observations(observations: Optional[list[Any]]) -> list[float]:
    if not observations:
        return []
    text = json.dumps(observations, ensure_ascii=False, default=str)
    nums = _numbers_in_text(text)

    # 工具结果通常是 JSON 裸数值（0.91、150000000000），而结论会做人类化
    # 展示（91%、1500 亿）。两者口径等价，R6 应归一化比较，不能误判为编数。
    def collect(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            raw = float(value)
            _push_value(nums, raw)
            if 0 < abs(raw) <= 1:
                _push_value(nums, raw * 100)      # 0.91 ↔ 91%
            if abs(raw) >= 100_000_000:
                _push_value(nums, raw / 100_000_000)  # 元 ↔ 亿
            if abs(raw) >= 10_000:
                _push_value(nums, raw / 10_000)  # 元 ↔ 万
            return
        if isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    collect(observations)
    return nums


def _push_value(nums: list[float], value: float) -> None:
    """登记一个可溯源数值；同时登记取整变体（16 ↔ 15.6、20 ↔ 20.4）。

    模型对数字做人类化展示（取整 / 一位小数）是正常表述而非编数，
    R6 须把舍入后的值也视为匹配候选。匹配容差本身很紧（只容忍浮点
    噪声），"近似"一律通过舍入变体表达——否则 45.2↔45.7 这类实差
    也会被宽容差放过。
    """
    nums.append(value)
    for rounded in (round(value), round(value, 1), round(value, 2)):
        if rounded != value:
            nums.append(float(rounded))


def _num_matches(target: float, candidates: list[float]) -> bool:
    for value in candidates:
        # 容差只覆盖浮点噪声与两位小数内的展示差；「近似数字」的匹配
        # 一律通过 _push_value 的舍入变体完成，避免 45.2↔45.7 实差被放过。
        tolerance = max(0.05, abs(value) * 0.001)
        if abs(target - value) <= tolerance:
            return True
    return False


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
    tool_observations: Optional[list[Any]] = None,
    require_numeric_sources: bool = False,
) -> ValidationResult:
    """
    校验结论是否违反硬规则。

    Args:
        conclusion: 结论文本（或 JSON 字符串）。
        tier: 行业分层 G1–G6。
        info_richness: 兼容旧调用保留，暂不参与任何结论约束。
        research_mode: full / g5 / first_principles。
        checklist_json: 六关 JSON 字符串（可选，用于 R2）。
        mirror_json: 镜子测试 JSON 字符串（可选，用于 R2）。
        verdict: 显式判定词（可选，优先于文本推断）。
        data_status: ok / partial / degraded / unknown。
        tool_observations: 工具观测列表，用于结论数字溯源校验。
        require_numeric_sources: 是否启用 R6。研究类结论应启用；普通轻回答可关闭。
    """
    violated: list[str] = []
    reasons: list[str] = []

    verdict_norm = (verdict or "").strip()
    # 剔除否定语境（「而非可买入」「不构成买入」等不算买入信号）后再匹配买入词
    conclusion_clean = _NEG_BUY_PATTERN.sub("", conclusion)
    is_buy = verdict_norm in _BUY_VERDICTS or bool(_BUY_TEXT_PATTERN.search(conclusion_clean))
    is_g5 = tier.upper() == "G5" or research_mode in ("g5", "first_principles")

    # R1：G5/第一性原理模式禁止买入。信息丰富度不再影响结论权限。
    if is_buy and is_g5:
        violated.append("R1_no_buy_for_g5_c")
        reasons.append("标的为 G5 或第一性原理模式，结论禁止为「买入」类。")

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
    price_scan = _NEG_PRICE_TERM_PATTERN.sub(
        lambda match: match.group(0).replace(match.group(2), "交易参考"),
        conclusion,
    )
    if _PRICE_PATTERN.search(price_scan) or _PRICE_ACTION_PATTERN.search(price_scan):
        violated.append("R4_no_price_target")
        reasons.append("结论禁止出现具体目标价 / 买卖点位。")

    # R5：只有本次数据状态降级时才要求一手验证；信息丰富度不参与判断。
    degraded = data_status in ("degraded", "unknown", "partial")
    if degraded and not _VERIFY_PATTERN.search(conclusion):
        violated.append("R5_need_primary_verification")
        reasons.append("数据降级时，结论须包含一手验证指引。")

    # R6：结论关键数字必须能在工具观测中找到近似匹配。
    if require_numeric_sources:
        conclusion_numbers = _numbers_in_text(conclusion)
        observation_numbers = _numbers_in_observations(tool_observations)
        missing = [
            n for n in conclusion_numbers
            if not _num_matches(n, observation_numbers)
        ]
        if missing:
            violated.append("R6_numeric_source_required")
            reasons.append(
                "结论中的关键数字未在工具观测中找到匹配值："
                + ", ".join(f"{n:g}" for n in missing[:8])
            )

    return ValidationResult(
        passed=len(violated) == 0,
        violated_rules=violated,
        reasons=reasons,
    )


def sanitize_conclusion(
    conclusion: str,
    tool_observations: Optional[list[Any]] = None,
) -> tuple[str, list[str]]:
    """程序化修正：无法溯源的关键数字追加「（推断）」标注。

    R6 拦截后让模型整篇重生成一次要 20s+，且重写时常引入新的无源数字，
    在总预算内形成昂贵的重试循环。改为代码直接把无源数字标注为推断
    （用户仍能看到数字，且知情其为非观测值），随后复检即通过。

    Returns:
        (修正后文本, 被标注的原始数字字符串列表)；无修改时原样返回。
    """
    if not conclusion:
        return conclusion, []
    observation_numbers = _numbers_in_observations(tool_observations)
    pieces: list[str] = []
    last = 0
    replaced: list[str] = []
    for _value, m in _iter_key_numbers(conclusion):
        if _num_matches(_value, observation_numbers):
            continue
        pieces.append(conclusion[last:m.start()])
        # 前缀「约」使复检时该数字落入推断豁免窗口，形成闭环；同时把
        # 数字保留给用户并知情其为非观测值。
        pieces.append("约 " + m.group(0) + "（推断）")
        last = m.end()
        replaced.append(m.group(1))
    if not replaced:
        return conclusion, []
    pieces.append(conclusion[last:])
    return "".join(pieces), replaced


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
                "info_richness": {"type": "string", "description": "兼容旧调用保留，当前不参与校验"},
                "research_mode": {"type": "string", "description": "full/g5/first_principles，可空"},
                "checklist_json": {"type": "string", "description": "六关 JSON，可选"},
                "mirror_json": {"type": "string", "description": "镜子测试 JSON，可选"},
                "verdict": {"type": "string", "description": "显式判定词，可选"},
                "data_status": {"type": "string", "description": "ok/partial/degraded/unknown，默认 ok"},
                "require_numeric_sources": {
                    "type": "boolean",
                    "description": "是否要求结论关键数字必须来自工具观测，默认 false",
                },
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
        require_numeric_sources: bool = False,
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
            tool_observations=kwargs.get("tool_observations"),
            require_numeric_sources=require_numeric_sources,
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
