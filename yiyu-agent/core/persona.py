"""用户画像（合并原 tiering/style/rewriter/glossary）。功能4。

NOVICE（1-2年小白）：AI 全程通俗语言，术语先白话解释再用。
"""

from dataclasses import dataclass
from enum import Enum


class Tier(str, Enum):
    NOVICE = "novice"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


@dataclass
class TierPolicy:
    tier: Tier
    allow_jargon: bool
    require_glossary_inline: bool
    use_analogy: bool
    show_raw_numbers: bool


# 术语白话对照（可扩充）
GLOSSARY = {
    "护城河": "别人抢不走的独门优势",
    "安全边际": "买得比它真实价值便宜多少",
    "ROE": "公司用自己的钱赚钱的效率",
    "自由现金流": "公司真正能拿到手、能自由花的钱",
}


def classify(invest_years: float) -> Tier:
    if invest_years <= 2:
        return Tier.NOVICE
    if invest_years <= 5:
        return Tier.INTERMEDIATE
    return Tier.ADVANCED


class Persona:
    def __init__(self, llm, prompt_loader) -> None:
        self.llm = llm
        self.prompts = prompt_loader

    async def rewrite(self, draft: str, persona: dict) -> str:
        """按分层改写语气；只改表达，不改结论/数字/风险提示。"""
        raise NotImplementedError
