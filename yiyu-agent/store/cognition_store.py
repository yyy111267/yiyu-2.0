"""认知陪练双路检索 —— 默认投资框架 vs 用户个人认知。

双路回答的核心：
  默认框架检索 + 用户认知检索 → 一致/冲突/盲区 → 供 LLM 组织双路输出。

默认框架库：内置经典投资认知（四大师 + 常见偏误），不可被用户数据覆盖。
用户认知库：从 CognitionRepo 检索该用户已生效的认知原子。
"""

from __future__ import annotations

import asyncio
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


# ── 认知提取（PRD 6.3 环节①）LLM 结构化抽取 prompt ─────────────
_EXTRACT_CANDIDATES_SYSTEM = """你是投资认知提取器：从「用户原话」与「研究结论」中识别可沉淀的投资认知，整理成结构化候选条目。

判定标准——四个条件必须同时满足（缺一即不是认知）：
1. 可复用：换成另一家公司仍然适用；单期财报数字、单一公司当期事实不是认知；
2. 有结构：包含判断逻辑或因果机制，不是孤立结论；
3. 有边界：能说清适用于什么、不适用于什么；
4. 可证伪：能说清什么情况下这条应当被推翻。

识别信号（优先从用户原话中找）：
- 用户纠正判断（如「高毛利不等于护城河」）；用户补充研究维度（如「还得看跨周期稳定性」）；
- 普遍化表述（「这类公司」「凡是……都要」）；用户给出因果解释（「……会让利润失真，所以……」）；
- 显式指令（「记住」「以后都按这个来」）；研究收敛后的用户点评。

输出 JSON（只输出 JSON，不要多余文字）：
{"candidates": [{
  "type": "cognition",
  "statement": "一句话核心主张，不超过40字，不得包含公司名",
  "content": "适用条件：…\\n不适用于：…\\n判断逻辑：…\\n证伪条件：…",
  "category": "护城河/估值/成长/管理层/风险/财务质量/认知偏误 之一",
  "subject_scope": "general / industry / company（可跨行业用 general；特定行业用 industry；仅当前标的用 company）",
  "scope": "subject_scope=industry 时填写行业名，否则留空",
  "is_hard_constraint": false,
  "source_quote": "产生该认知的原话（用户原话或结论原句）"
}]}

纪律：输出偏好与情绪不是投资认知，不要输出；用户没说的数字不补；
statement 去掉公司名（抽象为一类公司）、保留因果机制；
没有认知时输出 {"candidates": []}；最多 3 条，宁缺毋滥。"""


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
        user_views = self.repo.search(query, user_id=user_id, top_k=top_k * 2, mode="hybrid") \
            if user_id else []
        user_views = [item for item in user_views
                      if self._is_active(item) and item.get("type", "cognition") == "cognition"]
        user_views.sort(key=lambda item: (item.get("owner") != "user", -float(item.get("_score", 0))))
        user_views = user_views[:top_k]
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

    # ── PRD 6.2 记忆闭环 ────────────────────────────────────

    @staticmethod
    def _is_active(item: dict) -> bool:
        """兼容旧库的 confirmed，新写入统一使用 active。"""
        return item.get("status") in ("active", "confirmed")

    def prepare_research_memory(self, *, user_id: str, symbol: str = "",
                                query: str = "", directory_threshold: int = 100) -> dict:
        """研究启动前的确定性记忆准备。

        少于阈值时注入通用认知与同公司判断的摘要目录；达到阈值使用混合检索。
        行业认知先随读取结果返回，事实包就绪后由 attach_industry_memory 精确并入；
        公司作用域判断会被单独输出，供计划层强制转为验证题。
        """
        all_items = [a for a in self.repo.list_atoms(user_id, limit=500) if self._is_active(a)]
        preferences = [a for a in all_items if a.get("type") == "preference"]
        cognitions = [a for a in all_items if a.get("type", "cognition") == "cognition"]
        if len(cognitions) >= directory_threshold and query:
            candidates = self.repo.search(query, user_id=user_id, top_k=12, mode="hybrid")
            selected = [a for a in candidates if self._is_active(a) and a.get("type") == "cognition"
                        and a.get("subject_scope", "general") != "industry"]
        else:
            selected = [a for a in cognitions if a.get("subject_scope", "general") != "industry"]

        # 同标的判断永远可见；通用底线也始终进入收尾校验。
        company_assertions = [a for a in cognitions if a.get("subject_scope") == "company"
                              and a.get("symbol") == symbol]
        industry_candidates = [a for a in cognitions if a.get("subject_scope") == "industry"]
        hard_constraints = [a for a in cognitions if a.get("is_hard_constraint")
                            and (a.get("subject_scope") != "company" or a.get("symbol") == symbol)]
        selected = [a for a in selected
                    if a.get("subject_scope", "general") != "company" or a.get("symbol") == symbol]
        selected.sort(key=lambda item: (item.get("owner") != "user", item.get("updated_at") or ""), reverse=False)
        selected = selected[:8]
        directory = [
            {"id": a["id"], "statement": a["statement"], "category": a.get("category", ""),
             "subject_scope": a.get("subject_scope", "general"), "scope": a.get("scope", ""),
             "symbol": a.get("symbol", ""),
             "is_hard_constraint": bool(a.get("is_hard_constraint")), "owner": a.get("owner", "user")}
            for a in selected
        ]
        return {
            "preferences": preferences,
            "cognition_directory": directory,
            "selected_cognitions": selected,
            "company_assertions": company_assertions,
            "industry_candidates": industry_candidates,
            "hard_constraints": hard_constraints,
            "used_retrieval": len(cognitions) >= directory_threshold,
        }

    @staticmethod
    def attach_industry_memory(memory: dict, industry: str) -> dict:
        """把匹配行业的认知并入已并行读取的记忆结果，不再访问数据库。"""
        needle = _normalize_scope(industry)
        if not needle:
            return memory
        matches = [item for item in memory.get("industry_candidates", [])
                   if _scope_matches(item.get("scope", ""), needle)]
        selected = list(memory.get("selected_cognitions", []))
        match_ids = {item.get("id") for item in matches}
        selected = (matches + [item for item in selected if item.get("id") not in match_ids])[:8]
        memory["selected_cognitions"] = selected
        memory["industry_assertions"] = matches
        memory["cognition_directory"] = [
            {"id": item["id"], "statement": item["statement"],
             "category": item.get("category", ""), "subject_scope": item.get("subject_scope", "general"),
             "scope": item.get("scope", ""), "symbol": item.get("symbol", ""),
             "is_hard_constraint": bool(item.get("is_hard_constraint")),
             "owner": item.get("owner", "user")}
            for item in selected
        ]
        return memory

    def cognitions_to_questions(self, assertions: list[dict]) -> list[dict]:
        """将同标的已确认判断转成待验证问题；不把它们当作既成结论。"""
        questions: list[dict] = []
        for item in assertions:
            statement = str(item.get("statement", "")).strip()
            if not statement:
                continue
            questions.append({
                "memory_id": item["id"],
                "question": f"验证既有判断「{statement}」在本次研究中是否仍成立，并寻找反证。",
                "dimension": _guess_dimension(item.get("category", "") or statement),
                "priority": "P0" if item.get("is_hard_constraint") else "P1",
                "memory_mode": "constraint" if item.get("is_hard_constraint") else "verify",
            })
        return questions

    def extract_preferences(self, text: str, *, user_id: str,
                            source_task_id: str = "") -> list[dict]:
        """静默提取明确表达的偏好；只接受带第一人称偏好信号的句子。

        这是无 LLM 时的保守兜底，避免将一般研究提问误写为长期偏好。后续的
        结构化 LLM extractor 只需替换本方法，写入合并规则不变。
        """
        import re
        items: list[dict] = []
        for sentence in re.split(r"[。！？\n]", text):
            sentence = sentence.strip()
            if not (4 <= len(sentence) <= 80):
                continue
            if not re.search(r"(我.{0,6}(偏好|希望|只看|不看|关注)|请.{0,8}(简短|详细|图表|风险))", sentence):
                continue
            items.append({
                "user_id": user_id, "statement": sentence, "type": "preference",
                "status": "active", "source": "auto_preference", "owner": "user",
                "subject_scope": "general", "source_task_id": source_task_id,
                "is_hard_constraint": False,
            })
        return items[:3]

    def merge_preferences(self, preferences: list[dict], *, user_id: str) -> list[str]:
        """静默合并：精确重复不新增；用户手动保存的条目不被自动提取覆盖。"""
        existing = self.repo.list_atoms(user_id, type="preference", limit=500)
        existing_by_statement = {str(item.get("statement", "")).strip(): item for item in existing}
        ids: list[str] = []
        for pref in preferences:
            statement = str(pref.get("statement", "")).strip()
            if not statement or statement in existing_by_statement:
                continue
            saved = self.repo.upsert_atom(
                user_id=user_id, statement=statement, type="preference", status="active",
                source="auto_preference", owner="user", subject_scope="general",
                source_task_id=pref.get("source_task_id", ""), is_hard_constraint=False,
            )
            ids.append(saved["id"])
        return ids

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
                      symbol: str = "", source_task_id: str = "") -> list[dict]:
        """正则兜底抽取（LLM 缺失/失败时的降级路径）。

        关键词 + 评价性词双命中切句，产出候选确认卡；绝不在这里写库。
        主路径见 extract_candidates（LLM 四条件结构化抽取）。
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
                    "status": "pending_confirmation", "confidence": "low",
                    "basis": f"来自 {symbol} 研究" if symbol else "来自研究结论",
                    "type": "cognition", "content": "",
                    "subject_scope": "company" if symbol else "general", "symbol": symbol,
                    "verification_status": "needs_recheck" if symbol else "",
                    "source_task_id": source_task_id, "owner": "agent",
                })
        return atoms[:5]  # 最多 5 条候选

    async def extract_candidates(
        self, research_summary: str, *, user_id: str, symbol: str = "",
        source_task_id: str = "", user_message: str = "",
        company_name: str = "", llm_client=None,
    ) -> list[dict]:
        """LLM 结构化抽取候选认知（PRD 6.3 认知提取环节①）。

        输入覆盖六类识别信号：用户本轮原话（纠正/补充/普遍化/因果/显式指令）
        + 研究结论；按「可复用/有结构/有边界/可证伪」四条件判定，并把原话
        整理成结构化条目（statement ≤40 字不含公司名 + content 边界与证伪）。

        降级规则：
        - llm_client 为 None → 直接走 extract_atoms 正则兜底；
        - LLM 调用失败 / 输出形状不合法 → 降级正则兜底（候选仍需用户确认）；
        - LLM 成功返回空列表 → 信任「无认知」判定，不再降级（防止把单期
          事实数字句误抽为认知）。
        """
        if llm_client is None:
            return self.extract_atoms(
                research_summary, user_id=user_id, symbol=symbol,
                source_task_id=source_task_id,
            )
        user_prompt = _build_extraction_prompt(
            research_summary, user_message=user_message,
            company_name=company_name, symbol=symbol,
        )
        try:
            raw = await asyncio.wait_for(
                llm_client.chat_json(
                    system=_EXTRACT_CANDIDATES_SYSTEM, user=user_prompt,
                    temperature=0.1,
                ),
                # 候选卡在 FINAL_ANSWER 之后同步执行，直接决定 SSE 连接的
                # 尾部占用。正常 GLM flash 3-5s 返回；10s 未回即降级正则，
                # 不为非关键路径多挂连接（120s SLA 内答案已交付，这里只
                # 影响连接关闭时间）。
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 — 抽取失败不得影响已交付的研究结论
            logger.warning("候选认知 LLM 提取失败，降级正则兜底: %s", exc)
            return self.extract_atoms(
                research_summary, user_id=user_id, symbol=symbol,
                source_task_id=source_task_id,
            )
        items = raw.get("candidates") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            logger.warning("候选认知 LLM 输出形状不合法（%s），降级正则兜底", type(raw).__name__)
            return self.extract_atoms(
                research_summary, user_id=user_id, symbol=symbol,
                source_task_id=source_task_id,
            )
        return _parse_llm_candidates(
            items, user_id=user_id, symbol=symbol,
            source_task_id=source_task_id, company_name=company_name,
        )

    def confirm_cards(self, cards: list[dict], *, user_id: str,
                      source_task_id: str = "") -> list[dict]:
        """写入经用户确认的卡片；拒绝项不落库。

        card.action: confirm / edit / reject。编辑卡片可携带 edited 字段；当用户
        改写 AI 候选时，保留 agent 原判断与 user 改写两条记录，检索时 owner=user 优先。
        """
        results: list[dict] = []
        for card in cards:
            action = str(card.get("action", "")).lower()
            if action == "reject":
                results.append({"card_id": card.get("card_id", ""), "saved": False, "status": "rejected"})
                continue
            if action not in ("confirm", "edit"):
                results.append({"card_id": card.get("card_id", ""), "saved": False, "error": "action 必须为 confirm/edit/reject"})
                continue
            original = dict(card.get("candidate") or {})
            edited = dict(card.get("edited") or {}) if action == "edit" else {}
            payload = {**original, **edited}
            validation_error = _validate_memory_payload(payload)
            if validation_error:
                results.append({"card_id": card.get("card_id", ""), "saved": False, "error": validation_error})
                continue
            if action == "edit" and original.get("statement") and payload["statement"] != original["statement"]:
                agent_saved = self.repo.upsert_atom(
                    user_id=user_id, statement=original["statement"], category=original.get("category", ""),
                    status="active", source="research_extract", type="cognition",
                    content=original.get("content", ""), subject_scope=original.get("subject_scope", "general"),
                    symbol=original.get("symbol", ""), source_task_id=source_task_id or original.get("source_task_id", ""),
                    verification_status=original.get("verification_status", ""), owner="agent",
                )
                payload["variant_of"] = agent_saved["id"]
            saved = self.repo.upsert_atom(
                user_id=user_id, statement=payload["statement"], category=payload.get("category", ""),
                scope=payload.get("scope", ""), basis=payload.get("basis", ""),
                confidence=payload.get("confidence", "medium"), status="active",
                source="user_confirmed", type=payload.get("type", "cognition"), content=payload.get("content", ""),
                is_hard_constraint=bool(payload.get("is_hard_constraint")),
                subject_scope=payload.get("subject_scope", "general"), symbol=payload.get("symbol", ""),
                verification_status=payload.get("verification_status", ""),
                source_task_id=source_task_id or payload.get("source_task_id", ""),
                source_message_ids=payload.get("source_message_ids") or [], owner="user",
                variant_of=payload.get("variant_of", ""),
            )
            results.append({"card_id": card.get("card_id", ""), "saved": True, "id": saved["id"], "status": "active"})
        return results

    def save_extracted(self, atoms: list[dict]) -> list[str]:
        """兼容旧调用：明确拒绝自动沉淀，防止 AI 产物直接污染认知库。"""
        logger.warning("save_extracted 已废弃：候选认知必须经 confirm_cards 用户确认")
        return []


def _guess_dimension(text: str) -> str:
    mapping = [("护城河", "moat"), ("估值", "valuation"), ("成长", "growth"),
               ("增长", "growth"), ("管理", "management"), ("资本配置", "management"),
               ("负债", "financial_health"), ("现金流", "financial_health"), ("风险", "risk")]
    for keyword, dimension in mapping:
        if keyword in text:
            return dimension
    return "risk"


def _validate_memory_payload(payload: dict) -> str:
    """确认入库前的结构护栏，避免认知库退化成可搜索笔记。"""
    statement = str(payload.get("statement", "")).strip()
    kind = payload.get("type", "cognition")
    subject_scope = payload.get("subject_scope", "general")
    if not statement:
        return "statement 不能为空"
    if len(statement) > 40:
        return "statement 须为不超过 40 字的核心主张；请把详细论证放入 content"
    if kind not in ("cognition", "preference"):
        return "type 仅支持 cognition 或 preference"
    if subject_scope not in ("general", "industry", "company"):
        return "subject_scope 仅支持 general、industry 或 company"
    if subject_scope == "industry" and not str(payload.get("scope", "")).strip():
        return "行业作用域认知必须提供 scope（行业名）"
    if subject_scope == "company" and not payload.get("symbol"):
        return "公司作用域认知必须提供 symbol"
    if kind == "preference" and payload.get("is_hard_constraint"):
        return "偏好不能设为投资底线"
    content = str(payload.get("content", ""))
    if kind == "cognition" and subject_scope in ("general", "industry"):
        if "适用条件" not in content or "证伪条件" not in content:
            return "通用认知的 content 必须包含适用条件与证伪条件"
    return ""


def _normalize_scope(value: str) -> str:
    return "".join(str(value or "").lower().split()).replace("行业", "")


def _scope_matches(scope: str, normalized_industry: str) -> bool:
    candidate = _normalize_scope(scope)
    return bool(candidate and (candidate in normalized_industry or normalized_industry in candidate))


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


def _build_extraction_prompt(research_summary: str, *, user_message: str,
                             company_name: str, symbol: str) -> str:
    """抽取输入：用户原话 + 研究结论 + 标的信息（六类信号的载体）。"""
    if company_name and symbol:
        target = f"{company_name}（{symbol}）"
    else:
        target = company_name or symbol or "无（通用研究）"
    parts = [
        "【用户本轮原话】",
        (user_message or "").strip() or "（无）",
        "",
        "【研究结论】",
        (research_summary or "").strip()[:2000],
        "",
        "【当前标的】",
        target,
    ]
    return "\n".join(parts)


def _parse_llm_candidates(items: list, *, user_id: str, symbol: str,
                          source_task_id: str, company_name: str) -> list[dict]:
    """把 LLM 结构化输出转成候选卡；护栏与 confirm_cards 校验保持一致。

    - type=preference → 丢弃（偏好走静默通道，不进确认卡）；
    - statement 非 4~40 字 / 含公司名 → 拦截（公司身份由 symbol 承载）；
    - company 作用域但无 symbol → 拦截（无标的可挂的判断无法在同标的研究中复核）；
    - general 作用域但 content 缺「适用条件 / 证伪条件」→ 拦截（认知必须有边界与可证伪性）。
    """
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "cognition")) != "cognition":
            continue  # 输出偏好走静默通道
        statement = str(item.get("statement", "")).strip()
        if not (4 <= len(statement) <= 40):
            continue  # 核心主张必须 ≤40 字（PRD 6.3 认知条目结构）
        if any(name and name in statement for name in (company_name, symbol)):
            continue  # 核心主张不得含公司名；公司身份由 symbol 承载
        content = str(item.get("content", "")).strip()
        scope = str(item.get("subject_scope", "general"))
        if scope not in ("general", "industry", "company"):
            scope = "general"
        if scope == "company":
            if not symbol:
                continue  # 公司作用域判断无标的可挂 → 丢弃
        elif "适用条件" not in content or "证伪条件" not in content:
            continue  # 通用认知必须自带边界与证伪条件
        out.append({
            "user_id": user_id, "statement": statement,
            "category": str(item.get("category", "")).strip() or _guess_category(statement),
            "source": "research_extract", "status": "pending_confirmation",
            "confidence": "low",
            "basis": f"来自 {symbol} 研究" if symbol else "来自研究结论",
            "type": "cognition", "content": content,
            "is_hard_constraint": bool(item.get("is_hard_constraint")),
            "subject_scope": scope, "scope": str(item.get("scope", "")).strip(),
            "symbol": symbol if scope == "company" else "",
            "verification_status": "needs_recheck" if scope == "company" else "",
            "source_task_id": source_task_id, "owner": "agent",
            "source_quote": str(item.get("source_quote", "")).strip(),
        })
    return out[:5]
