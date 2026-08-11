"""认知陪练双路检索 —— 默认投资框架 vs 用户个人认知。

双路回答的核心：
  默认框架检索 + 用户认知检索 → 一致/冲突/盲区 → 供 LLM 组织双路输出。

默认框架库：内置经典投资认知（四大师 + 常见偏误），不可被用户数据覆盖。
用户认知库：从 CognitionRepo 检索该用户的认知原子。
"""

from __future__ import annotations

import logging
from typing import Optional

from store.repos.cognition_repo import CognitionRepo

logger = logging.getLogger(__name__)


# ── 默认投资框架库（内置，版本化，不可被用户覆盖）──────────────
DEFAULT_FRAMEWORK: list[dict] = [
    {
        "id": "df_moat_1", "category": "护城河",
        "statement": "高毛利率本身不等于护城河；需区分结构性优势与周期性高毛利。",
        "scope": "所有企业", "confidence": "high",
        "counter_example": "周期品在高点毛利高但不可持续；缺乏转换成本的消费品毛利易被侵蚀。",
    },
    {
        "id": "df_moat_2", "category": "护城河",
        "statement": "护城河需用 ROIC 趋势与竞争对手对比验证，而非单一时点高 ROE。",
        "scope": "所有企业", "confidence": "high",
        "counter_example": "高杠杆推高的 ROE 不等于真实竞争优势。",
    },
    {
        "id": "df_valuation_1", "category": "估值",
        "statement": "好公司不等于好价格；安全边际要求买入价显著低于内在价值。",
        "scope": "所有企业", "confidence": "high",
        "counter_example": "优质成长股长期高估，买入仍可能多年不赚钱。",
    },
    {
        "id": "df_valuation_2", "category": "估值",
        "statement": "PE 低可能反映价值低估，也可能反映衰退预期；须结合业务质量判断。",
        "scope": "所有企业", "confidence": "high",
        "counter_example": "周期股低 PE 常是行业顶部信号。",
    },
    {
        "id": "df_growth_1", "category": "成长",
        "statement": "增长必须由投入资本回报率支撑；靠堆资本的低回报增长会毁灭价值。",
        "scope": "成长型企业", "confidence": "high",
        "counter_example": "持续融资扩张但 ROIC < WACC 的企业，增长越快价值越低。",
    },
    {
        "id": "df_management_1", "category": "管理层",
        "statement": "管理层资本配置能力比经营能力更稀缺；关注分红/回购/再投资/并购的长期回报。",
        "scope": "所有企业", "confidence": "medium",
        "counter_example": "经营优秀但乱并购的管理层同样会毁灭价值。",
    },
    {
        "id": "df_risk_1", "category": "风险",
        "statement": "最大风险往往是你没想到的，而不是已经列出来的；须做反向思考与证伪。",
        "scope": "所有企业", "confidence": "high",
        "counter_example": "所有列出的风险都成立，企业仍可能因未预见因素暴雷。",
    },
    {
        "id": "df_bias_1", "category": "认知偏误",
        "statement": "确认偏误：人会下意识寻找支持已有观点的证据；须主动找反证。",
        "scope": "所有决策", "confidence": "high",
        "counter_example": "只看利好不看利空的研究结论必然失真。",
    },
    {
        "id": "df_bias_2", "category": "认知偏误",
        "statement": "近因效应：近期表现权重过高，忽视长期均值回归。",
        "scope": "所有决策", "confidence": "high",
        "counter_example": "用最近 1 年高增长外推 10 年，常严重高估。",
    },
]


class CognitionStore:
    """双路认知检索：默认框架 + 用户认知 → 差异分析。

    用法：
        store = CognitionStore()
        result = store.recall("茅台护城河", user_id="u1")
        # result = {default_views, user_views, agreement, conflict, blind_spots}
    """

    def __init__(self, db_url: str = "sqlite:///yiyu_agent.db"):
        self.repo = CognitionRepo(db_url)

    def recall(self, query: str, *, user_id: str, top_k: int = 5) -> dict:
        """双路检索 + 差异分析，供 LLM 组织双路回答。"""
        # 路径 A：默认投资框架（关键词匹配内置库）
        default_views = self._search_default(query, top_k)
        # 路径 B：用户个人认知（混合检索）
        user_views = self.repo.search(query, user_id=user_id, top_k=top_k, mode="hybrid") \
            if user_id else []
        # 差异分析
        agreement, conflict, blind_spots = self._diff(default_views, user_views)
        return {
            "query": query,
            "default_framework": default_views,
            "user_cognition": user_views,
            "agreement": agreement,
            "conflict": conflict,
            "blind_spots": blind_spots,
        }

    def _search_default(self, query: str, top_k: int) -> list[dict]:
        """默认框架库检索：关键词命中 + category 匹配。"""
        if not query:
            return DEFAULT_FRAMEWORK[:top_k]
        scored: list[tuple[float, dict]] = []
        q = query.lower()
        for item in DEFAULT_FRAMEWORK:
            hay = " ".join([item.get("statement", ""), item.get("category", ""),
                            item.get("scope", "")]).lower()
            kw_hits = sum(1 for kw in _tokenize(query) if kw in hay)
            cats = ["护城河", "估值", "成长", "管理层", "风险", "偏误"]
            cat_bonus = 0.5 if any(cat in query and cat in hay for cat in cats) else 0
            score = kw_hits * 0.3 + cat_bonus
            scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:top_k]]

    def _diff(self, default_views: list[dict],
              user_views: list[dict]) -> tuple[list, list, list]:
        """对比默认框架与用户认知，输出一致/冲突/盲区。"""
        agreement: list[dict] = []
        conflict: list[dict] = []
        blind_spots: list[dict] = []

        # 用户认知与默认框架的语义对比（基于 category + 关键词重叠）
        used_default: set[str] = set()
        for uv in user_views:
            u_cat = uv.get("category", "")
            u_stmt = uv.get("statement", "")
            matched = False
            for dv in default_views:
                if dv["id"] in used_default:
                    continue
                d_cat = dv.get("category", "")
                # 同类目 + 关键词重叠 → 对比
                if u_cat and d_cat and (u_cat in d_cat or d_cat in u_cat):
                    used_default.add(dv["id"])
                    # 简化判断：含否定词视为冲突
                    neg = any(w in u_stmt for w in ["不", "错", "假", "反", "过度", "盲目"])
                    if neg:
                        conflict.append({
                            "user_view": uv["statement"],
                            "framework_view": dv["statement"],
                            "category": u_cat,
                            "note": "用户表述含否定/质疑，可能存在认知冲突",
                        })
                    else:
                        agreement.append({
                            "user_view": uv["statement"],
                            "framework_view": dv["statement"],
                            "category": u_cat,
                        })
                    matched = True
                    break
            if not matched:
                # 用户认知未命中默认框架——可能是个人化观点
                agreement.append({
                    "user_view": u_stmt,
                    "framework_view": None,
                    "category": u_cat,
                    "note": "用户个人化认知，默认框架无直接对应",
                })

        # 盲区：默认框架有但用户认知库里没有的相关条目
        for dv in default_views:
            if dv["id"] not in used_default:
                blind_spots.append({
                    "framework_view": dv["statement"],
                    "category": dv.get("category", ""),
                    "note": "默认框架强调但用户认知库未体现",
                })

        return agreement, conflict, blind_spots

    def extract_atoms(self, research_summary: str, *, user_id: str,
                      symbol: str = "") -> list[dict]:
        """从研究结论文本中抽取候选认知原子（草稿，status=candidate）。

        简化实现：按句切分，提取含判断性词汇的句子作为候选。
        生产级应改用 LLM 结构化抽取。
        """
        import re
        sentences = re.split(r"[。！？\n]", research_summary)
        cues = ["护城河", "估值", "安全边际", "成长", "管理层", "风险", "ROIC",
                "毛利", "现金流", "竞争优势", "定价权", "集中度", "周期", "PE", "资本配置"]
        # 判断性/评价性表达（比严格判断词更宽松，适配研报自然语言）
        evaluative = ["是", "等于", "意味着", "说明", "表明", "因为", "所以",
                      "强", "弱", "好", "差", "优秀", "高", "低", "可持续",
                      "可接受", "限制", "突出", "稳健", "恶化", "改善", "构成"]
        atoms: list[dict] = []
        for s in sentences:
            s = s.strip()
            if len(s) < 8 or len(s) > 80:
                continue
            if any(c in s for c in cues) and any(w in s for w in evaluative):
                atoms.append({
                    "user_id": user_id, "statement": s,
                    "category": _guess_category(s), "source": "research_extract",
                    "status": "candidate", "confidence": "low",
                    "basis": f"来自 {symbol} 研究" if symbol else "来自研究结论",
                })
        return atoms[:5]  # 最多 5 条候选

    def save_extracted(self, atoms: list[dict]) -> list[str]:
        """保存抽取的候选认知原子，返回 atom_id 列表。"""
        ids: list[str] = []
        for a in atoms:
            r = self.repo.upsert_atom(
                user_id=a["user_id"], statement=a["statement"],
                category=a.get("category", ""), scope=a.get("scope", ""),
                basis=a.get("basis", ""), confidence=a.get("confidence", "low"),
                status=a.get("status", "candidate"), source=a.get("source", "research_extract"),
            )
            ids.append(r["id"])
        return ids


def _tokenize(query: str) -> list[str]:
    """简易分词：中文按字、英文按词。"""
    import re
    tokens = re.findall(r"[a-zA-Z]{2,}|[\u4e00-\u9fff]", query)
    return tokens


def _guess_category(text: str) -> str:
    for kw, cat in [("护城河", "护城河"), ("毛利", "护城河"), ("竞争优势", "护城河"),
                    ("估值", "估值"), ("PE", "估值"), ("安全边际", "估值"),
                    ("成长", "成长"), ("增长", "成长"),
                    ("管理层", "管理层"), ("资本配置", "管理层"),
                    ("风险", "风险"), ("周期", "风险")]:
        if kw in text:
            return cat
    return ""
