"""
研报结论「用户侧质量」打分引擎（100 分制加减分）。

评什么：AI 跑完交付的**最终研报结论**，用户读起来好不好——不管数字对错，
只看阅读体验（回答质量 + 新手友好度）。

怎么打分：十项指标，每项满分 10 分，总分 100 分。纯规则实现（离线、零成本），
数结构、查词表、算密度，不需要调 LLM。

指标清单：
    质量（回答质量）：
        Q1 结构清晰    —— 有没有小标题 / 分点列表
        Q2 结论先行    —— 开头有没有先亮判断
        Q3 通顺不重复  —— 有没有车轱辘套话反复出现
        Q4 长短合适    —— 字数在合理区间（不是一句话，也不是论文）
        Q5 无空话      —— 信息密度够不够（有没有"众所周知"这类废话）
    新手友好度：
        F1 术语有解释  —— 说 ROIC 时有没有跟一句"就是投入资本回报率"
        F2 有大白话    —— 有没有"打个比方"式的转述
        F3 不吓人      —— 有没有"仅供参考、不是买卖建议"的风险提示
        F4 告诉下一步  —— 新手看完知道该关注什么 / 做什么
        F5 不堆黑话    —— 术语密度是不是太高（满屏黑话）

用法：
    from evaluation.e2e.scripts.quality import QualityJudge
    report = QualityJudge().check(conclusion_text)
    report.total            # 总分 0-100
    report.grade           # A/B/C/D/F
    [ (it.category, it.name, it.score, it.reason) for it in report.items ]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ══════════════════════════════════════════════════════════════════
# 词表（可随业务调整）
# ══════════════════════════════════════════════════════════════════

# 常见财务/估值术语（用于 F1 术语解释、F5 术语密度）
_TERM_PATTERN = re.compile(
    r"投入资本回报率|ROIC|税后营业利润|EBITDA|息税折旧摊销前利润|"
    r"经营现金流|OCF|自由现金流|净资产收益率|ROE|ROA|总资产收益率|"
    r"毛利率|净利率|资产负债率|市盈率|PE|市净率|PB|股息率|"
    r"合同负债|应收账款|存货周转|护城河|贴现率|DCF|复合增长率|CAGR|"
    r"流动资产|流动负债|商誉|股权质押|货币资金|有息负债"
)

# 术语旁的"解释"标记词（F1 判定"这个词有没有被解释"）
_EXPLAIN_MARKERS = [
    "即", "也就是", "也就是", "意思是", "意思是说", "可以理解为", "指的是",
    "说白了", "简单说", "简单来说", "通俗地讲", "相当于", "就是", "意为",
]

# 大白话转述标记（F2）
_VERNACULAR_MARKERS = [
    "打个比方", "换句话说", "简单来说", "通俗地讲", "说白了",
    "就像", "相当于", "你可以理解为", "可以把它理解成", "举个",
]

# 风险提示 / 免责（F3：不吓人 = 有边界感，不承诺收益）
_RISK_STRONG_MARKERS = [
    "仅供参考", "不构成投资建议", "不作为买卖依据", "风险提示",
    "市场有风险", "投资需谨慎", "本文不构成", "非投资建议", "自行判断",
]
_RISK_WEAK_MARKERS = ["风险", "谨慎", "波动", "不确定性"]

# 行动指引（F4：新手读完知道下一步）
_ACTION_STRONG_MARKERS = [
    "建议您", "可以关注", "建议关注", "下一步", "可留意", "建议投资者",
    "持续跟踪", "可以观察", "建议进一步", "值得关注", "留意", "建议结合",
]
_ACTION_WEAK_MARKERS = ["建议", "关注", "跟踪"]

# 结论先行：开头亮判断的词（Q2）
_CONCLUSION_FIRST_MARKERS = [
    "综合判断", "总体来看", "总体而言", "我们认为", "我的判断", "结论是",
    "综合来看", "更看好", "倾向于", "大概率", "基本面", "整体看", "总体看",
]

# 车轱辘套话（Q3：反复出现 = 重复啰嗦）
_REPETITIVE_MARKERS = [
    "综合来看", "总体而言", "值得注意的是", "我们需要", "可以说",
    "从这个角度看", "在某种程度上", "与此同时", "需要注意的是",
]

# 空话 / 废话词（Q5：信息密度低）
_FILLER_MARKERS = [
    "综上所述", "众所周知", "总而言之", "不言而喻", "毋庸置疑",
    "显而易见", "意义重大", "有目共睹", "众所周知的是",
]

# 字数区间（Q4）
_LEN_GOOD = (400, 2000)     # 理想
_LEN_OK = ((200, 400), (2000, 3000))   # 可接受
_LEN_POOR = ((100, 200), (3000, 4000)) # 偏短/偏长


# ══════════════════════════════════════════════════════════════════
# 结果结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class QualityItem:
    """单项指标得分。"""
    key: str          # Q1 / Q2 / ... / F5
    name: str         # 指标名
    category: str     # 质量 / 新手友好
    score: float      # 0-10
    reason: str       # 给分的理由（可读）

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "score": self.score,
            "reason": self.reason,
        }


@dataclass
class QualityReport:
    """一次结论打分的完整结果。"""
    total: float = 0.0
    items: list[QualityItem] = field(default_factory=list)

    @property
    def grade(self) -> str:
        return grade_of(self.total)

    def to_dict(self) -> dict:
        return {
            "total": round(self.total, 1),
            "grade": self.grade,
            "items": [it.to_dict() for it in self.items],
        }


def grade_of(score: float) -> str:
    """100 分制评级：A≥90 / B≥80 / C≥70 / D≥60 / F<60。"""
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


# ══════════════════════════════════════════════════════════════════
# 各指标评分函数（每条满分 10 分）
# ══════════════════════════════════════════════════════════════════

def _strip_ws(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def score_structure(text: str) -> QualityItem:
    """Q1 结构清晰：小标题 / 分点列表。"""
    headings = len(re.findall(r"^#{1,6}\s+\S", text, re.M))
    ordered = len(re.findall(r"^\s*\d{1,2}[.、．]\s*\S", text, re.M))
    bullets = len(re.findall(r"^\s*[-*•]\s*\S", text, re.M))
    bold_labels = len(re.findall(r"\*\*[^*\n]{2,24}[:：][^*\n]*\*\*", text))
    cn_heads = len(re.findall(r"^[一二三四五六七八九十]+、", text, re.M))
    total = headings + ordered + bullets + bold_labels + cn_heads

    if total >= 3:
        score, reason = 10.0, f"检测到 {total} 处结构标记（小标题/分点），层次清晰"
    elif total >= 1:
        score, reason = 6.0, f"仅有 {total} 处结构标记，建议多用小标题分节"
    else:
        score, reason = 2.0, "全文无小标题、无分点，读起来是一大块文字"
    return QualityItem("Q1", "结构清晰", "质量", score, reason)


def score_conclusion_first(text: str) -> QualityItem:
    """Q2 结论先行：开头是否先亮判断。"""
    head = text[:300]
    head_far = text[:600]
    hits = [m for m in _CONCLUSION_FIRST_MARKERS if m in head]
    if hits:
        return QualityItem("Q2", "结论先行", "质量", 10.0,
                           f"开头即出现判断词：{hits[0]}，读者能快速抓住观点")
    hits_far = [m for m in _CONCLUSION_FIRST_MARKERS if m in head_far]
    if hits_far:
        return QualityItem("Q2", "结论先行", "质量", 6.0,
                           f"前 600 字内才出现判断词（{hits_far[0]}），建议结论更靠前")
    return QualityItem("Q2", "结论先行", "质量", 2.0,
                       "前 600 字未见明确判断/结论词，容易让读者抓不住重点")


def score_no_repetition(text: str) -> QualityItem:
    """Q3 通顺不重复：车轱辘套话是否反复出现。"""
    violations: list[str] = []
    for m in _REPETITIVE_MARKERS:
        cnt = text.count(m)
        if cnt >= 3:
            violations.append(f"{m}×{cnt}")
    if not violations:
        return QualityItem("Q3", "通顺不重复", "质量", 10.0,
                           "未发现反复出现的套话，行文干净")
    score = max(4.0, 10.0 - 3.0 * len(violations))
    return QualityItem("Q3", "通顺不重复", "质量", score,
                       f"套话反复出现：{', '.join(violations)}，读起来啰嗦")


def score_length(text: str) -> QualityItem:
    """Q4 长短合适：字数区间。"""
    n = len(_strip_ws(text))
    if _LEN_GOOD[0] <= n <= _LEN_GOOD[1]:
        score, note = 10.0, "篇幅适中"
    elif any(lo <= n <= hi for lo, hi in _LEN_OK):
        score, note = 7.0, "篇幅略偏短/偏长，但可接受"
    elif any(lo <= n <= hi for lo, hi in _LEN_POOR):
        score, note = 4.0, "篇幅明显偏短/偏长"
    else:
        score, note = 1.0, "篇幅严重失衡（一句话或长篇大论）"
    return QualityItem("Q4", "长短合适", "质量", score,
                       f"正文约 {n} 字（不含空白），{note}")


def score_no_filler(text: str) -> QualityItem:
    """Q5 无空话：信息密度。"""
    hits = [m for m in _FILLER_MARKERS if m in text]
    if not hits:
        return QualityItem("Q5", "无空话", "质量", 10.0, "无空话套话，信息密度高")
    score = max(0.0, 10.0 - 2.0 * len(hits))
    return QualityItem("Q5", "无空话", "质量", score,
                       f"出现空话词：{', '.join(hits)}，稀释了信息量")


def _term_explain_ratio(text: str) -> tuple[list[str], list[str]]:
    """返回 (未解释术语, 已解释术语)。

    按"术语类型"去重判定：同一个术语在文中只要出现过一次带解释，
    就算"这个词被解释过了"（后续再次出现不必每次重复解释）。
    """
    seen: dict[str, bool] = {}
    for m in _TERM_PATTERN.finditer(text):
        term = m.group()
        start, end = m.span()
        window = text[max(0, start - 40): min(len(text), end + 60)]
        # 两种"解释"形态都算：① 附近有解释词（即/就是/意思是…）；
        # ② 术语后紧跟括号注释，如 "ROE（净资产收益率）"。
        after = text[end: min(len(text), end + 40)]
        parenthetical = bool(re.search(r"（[^（）]{1,40}）", after))
        if parenthetical or any(mk in window for mk in _EXPLAIN_MARKERS):
            seen[term] = True
        else:
            seen.setdefault(term, False)
    unexplained = [t for t, ok in seen.items() if not ok]
    explained = [t for t, ok in seen.items() if ok]
    return unexplained, explained


def score_term_explained(text: str) -> QualityItem:
    """F1 术语有解释：用了术语就要配一句人话。"""
    unexplained, explained = _term_explain_ratio(text)
    total_terms = len(unexplained) + len(explained)
    if total_terms == 0:
        return QualityItem("F1", "术语有解释", "新手友好", 5.0,
                           "全文未出现术语（无需解释），给中性分")
    ratio = len(explained) / total_terms
    if ratio >= 0.8:
        score, note = 10.0, "术语基本都配了通俗解释"
    elif ratio >= 0.5:
        score, note = 6.0, "部分术语未解释"
    else:
        score, note = 2.0, "多数术语没有解释"
    uniq_un = sorted(set(unexplained))
    reason = f"共 {total_terms} 个术语，{note}"
    if uniq_un:
        reason += f"；未解释：{'、'.join(uniq_un[:6])}" + (" 等" if len(uniq_un) > 6 else "")
    return QualityItem("F1", "术语有解释", "新手友好", score, reason)


def score_vernacular(text: str) -> QualityItem:
    """F2 有大白话：有没有通俗化转述。"""
    hits = [m for m in _VERNACULAR_MARKERS if m in text]
    if hits:
        return QualityItem("F2", "有大白话", "新手友好", 10.0,
                           f"有通俗化转述（{hits[0]}），新手易理解")
    return QualityItem("F2", "有大白话", "新手友好", 3.0,
                       "全文都是书面语，缺少'打个比方'式的转述，新手容易读不进去")


def score_risk_notice(text: str) -> QualityItem:
    """F3 不吓人：有风险提示/免责，不把话说死。"""
    strong = [m for m in _RISK_STRONG_MARKERS if m in text]
    if strong:
        return QualityItem("F3", "不吓人", "新手友好", 10.0,
                           f"有明确风险提示（{strong[0]}），读者知道是参考不是保证")
    weak = [m for m in _RISK_WEAK_MARKERS if m in text]
    if weak:
        return QualityItem("F3", "不吓人", "新手友好", 5.0,
                           "仅提到风险相关词，建议加一句'仅供参考、不构成投资建议'")
    return QualityItem("F3", "不吓人", "新手友好", 0.0,
                       "无任何风险提示，可能让读者误以为稳赚，需要补免责声明")


def score_next_step(text: str) -> QualityItem:
    """F4 告诉下一步：读者知道该关注什么。"""
    strong = [m for m in _ACTION_STRONG_MARKERS if m in text]
    if strong:
        return QualityItem("F4", "告诉下一步", "新手友好", 10.0,
                           f"有行动指引（{strong[0]}），读者知道下一步做什么")
    weak = [m for m in _ACTION_WEAK_MARKERS if m in text]
    if weak:
        return QualityItem("F4", "告诉下一步", "新手友好", 5.0,
                           "提到'建议/关注'但较笼统，可给出更具体的行动指引")
    return QualityItem("F4", "告诉下一步", "新手友好", 0.0,
                       "读完不知道下一步该关注什么，建议补充行动指引")


def score_term_density(text: str) -> QualityItem:
    """F5 不堆黑话：术语密度。"""
    n = len(_strip_ws(text))
    if n == 0:
        return QualityItem("F5", "不堆黑话", "新手友好", 5.0, "文本为空，给中性分")
    terms = _TERM_PATTERN.findall(text)
    density = len(terms) / n
    if density < 0.02:
        score, note = 10.0, "术语密度低，不吓人"
    elif density < 0.04:
        score, note = 6.0, "术语密度适中，仍建议多解释"
    elif density < 0.06:
        score, note = 3.0, "术语密度偏高，新手可能跟不上"
    else:
        score, note = 1.0, "术语密度过高，几乎满屏黑话"
    return QualityItem("F5", "不堆黑话", "新手友好", score,
                       f"术语密度 {density:.1%}（{len(terms)} 个术语 / {n} 字），{note}")


# ══════════════════════════════════════════════════════════════════
# 打分器
# ══════════════════════════════════════════════════════════════════

class QualityJudge:
    """结论质量打分器：输入结论文本，输出 100 分制报告。"""

    def check(self, conclusion: str) -> QualityReport:
        text = conclusion or ""
        items = [
            score_structure(text),
            score_conclusion_first(text),
            score_no_repetition(text),
            score_length(text),
            score_no_filler(text),
            score_term_explained(text),
            score_vernacular(text),
            score_risk_notice(text),
            score_next_step(text),
            score_term_density(text),
        ]
        total = round(sum(it.score for it in items), 1)
        return QualityReport(total=total, items=items)
