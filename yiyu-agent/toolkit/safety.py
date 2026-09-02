"""安全扫描共享层 —— 注入检测 / 泄露检测 / 敏感串指纹。

被两处共用，避免两份正则各自漂移：
  - toolkit/web/tools.py      网页/搜索正文抓取后扫描（防网页注入改写计划）
  - runtime/loop_lint.py      工具结果与最终报告扫描（防注入服从与系统提示泄露）

设计纪律：
  - 只做确定性正则比对，不做 LLM 判断；
  - 检出 = 可疑，是否放行由调用方决定（网页正文仍返回，但打标记）。
"""

from __future__ import annotations

import re

# ── 注入指令：诱导模型改变行为（改计划 / 泄系统提示 / 直接给结论）──
# 覆盖三类攻击面：
#   1. 越权指令：忽略之前指令 / 不要遵守规则
#   2. 泄露指令：输出系统提示
#   3. 改计划指令：删除 P0 / 立即给出买入（网页正文注入的典型payload）
INJECTION_RE = re.compile(
    r"(忽略.{0,6}(之前|前面|上述|以上)?.{0,6}(指令|提示|规则)"
    r"|不要遵守.{0,6}(规则|指令)"
    r"|输出.{0,6}(系统|system).{0,6}(提示|prompt)"
    r"|(删除|移除|去掉|清空).{0,6}(p0|p\s*0|问题|question)"
    r"|立即.{0,4}(给出|输出|生成).{0,8}(买入|卖出|结论|建议)"
    r"|(你是|你现在是).{0,6}(助手|assistant).{0,10}现在)",
    re.IGNORECASE,
)

# ── 泄露标记：模型把注入当指令执行、回显系统提示或自称身份 ──
LEAK_RE = re.compile(
    r"(系统提示词|system\s*prompt|systemPrompt"
    r"|你是.{0,10}(助手|assistant)"
    r"|我是.{0,10}(助手|assistant|AI))",
    re.IGNORECASE,
)

# ── 密钥 / 凭证指纹：输出层扫描用（命中即不得返回用户）──
SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),                    # OpenAI 风格密钥
    re.compile(r"\b(sk|pk|api)[-_]?(live|test)?[-_]?[A-Za-z0-9]{16,}", re.IGNORECASE),
    re.compile(r"\b(ak|secret|token|password|passwd|pwd)\s*[:=]\s*\S{6,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.?[A-Za-z0-9_\-]*"),  # JWT
    re.compile(r"\b(AIza|ya29\.)[A-Za-z0-9_\-]{10,}"),         # Google / OAuth
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),               # GitHub token
)

# 环境变量常见名（出现在输出里即视为泄露尝试）
ENV_KEY_RE = re.compile(
    r"\b(API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|SECRET_KEY|ACCESS_TOKEN"
    r"|DATABASE_URL|SESSION_SECRET)\b",
    re.IGNORECASE,
)


def detect_injection(text: str | None) -> bool:
    """文本中是否含注入指令。"""
    return bool(text) and bool(INJECTION_RE.search(str(text)))


def detect_leak(text: str | None) -> bool:
    """文本中是否含系统提示泄露 / 身份泄露标记。"""
    return bool(text) and bool(LEAK_RE.search(str(text)))


def scan_secrets(text: str | None) -> list[str]:
    """扫描文本中的密钥类敏感串，返回命中原文列表（去重保序）。

    用于输出层闸（SAFE21/22）：命中即不得返回用户，避免模型照抄 .env 内容。
    """
    if not text:
        return []
    hits: list[str] = []
    for pattern in SECRET_PATTERNS:
        for m in pattern.finditer(str(text)):
            raw = m.group(0)
            if raw not in hits:
                hits.append(raw)
    return hits


def scan_env_keys(text: str | None) -> list[str]:
    """扫描环境变量名（出现在输出里说明模型在读环境）。"""
    if not text:
        return []
    return list(dict.fromkeys(m.group(0) for m in ENV_KEY_RE.finditer(str(text))))


def redact_secrets(text: str | None, placeholder: str = "[REDACTED]") -> str:
    """把文本中的密钥类敏感串替换为占位符（用于 trace 落盘脱敏）。"""
    if not text:
        return text or ""
    out = str(text)
    for pattern in SECRET_PATTERNS:
        out = pattern.sub(placeholder, out)
    return out
