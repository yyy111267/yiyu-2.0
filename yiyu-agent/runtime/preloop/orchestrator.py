"""preloop 轻量启动器 —— 把「一家公司 + 研究意图」快速加工成研究循环可消费的四件套。

设计目标：preloop 是「快速识别标的、准备上下文、生成计划」的轻量启动器，
不是「提前把所有数据都取完」的重研究环节。

新链路：
  用户问题
    → 轻量实体解析（本地表优先，不依赖 LLM）
    → 并行：白名单基础搜索 / 认知库检索 / 信息丰富度初判 / 行业初判
    → 生成研究计划（标记哪些问题需要正式 loop 重取数）
    → 进入正式 loop（loop 再按需调行情/财报/指标/估值工具）

四件套交付（PRD 5.3 → 5.4）：
  company_facts（轻量事实包）→ granularity_decision（粒度）→
  per-unit company_profile + selected_adapter（画像/Adapter）→ research_plan（计划，带数据需求标记）

模块边界：只做编排与字段适配，不做投资判断；每个环节失败各自有降级。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from toolkit.entity.resolver import Entity, resolve_entity
from toolkit.entity.mention import extract_candidates
from runtime.schemas import (
    CompanyFacts,
    CurrentEntitySchema,
    GranularityDecision,
    UnitProfile,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ADAPTER_CATALOG_PATH = _PROJECT_ROOT / "bus_router" / "adapters_catalog.yaml"


# ── 字段适配 ──────────────────────────────────────────────

def entity_to_current_schema(entity: Entity) -> CurrentEntitySchema:
    """resolver 的 Entity（dataclass）→ 事实包所需的 CurrentEntitySchema（Pydantic 镜像）。

    字段映射（PRD 5.2 六字段对齐）：
      symbol→security_id, name→canonical_name, market→primary_market；
      exchange 从 symbol 后缀推断；alias_type / entity_source 原样透传（缺省填默认）。
    """
    symbol = entity.symbol or ""
    if "." in symbol:
        exchange = symbol.rsplit(".", 1)[1]
        code = symbol.split(".", 1)[0]
    else:
        exchange = ""
        code = symbol
    kwargs: dict[str, Any] = {
        "canonical_name": entity.name or entity.symbol,
        "security_id": symbol,
        "code": code,
        "exchange": exchange,
        "primary_market": entity.market or "A",
        "entity_source": entity.entity_source or "explicit",
        "alias_type": entity.alias_type or "fullname",
        "scope_note": entity.scope_note or None,
    }
    if entity.resolved_at:
        kwargs["resolved_at"] = entity.resolved_at  # 缺省走模型默认时间戳
    return CurrentEntitySchema(**kwargs)


def should_load_cognition(user_id: str | None) -> bool:
    """匿名会话（user_id 为空）不得注入任何用户的私有认知。

    抽成纯函数而非内联判断：这是安全闸（SAFE28），必须可被评测直接断言，
    否则「匿名会话会不会读到别人认知」只能靠人工review，无法回归。
    """
    return bool(user_id and str(user_id).strip())


def facts_to_plan_input(facts: CompanyFacts) -> dict:
    """CompanyFacts（Pydantic）→ generate_research_plan 期望的 facts dict。

    注意：facts_builder 目前产出 anti_consensus_triggered（bool）而非
    anti_consensus_signal（str）、也没有 data_gaps 字段——这两项留空，
    待 facts_builder 补齐后再对接（不阻塞四件套串联）。
    """
    return {
        "facts_version": facts.facts_version,
        "one_line_business": facts.one_line_business,
        "segments": [
            {"name": s.name, "revenue_share": s.revenue_share}
            for s in facts.segments
        ],
        "open_questions": list(facts.open_questions),
        "anti_consensus_signal": "",
        "data_gaps": [],
    }


def _load_priority_questions(adapter_id: str) -> list[str]:
    """从 adapters_catalog.yaml 读某 Adapter 的 priority_questions（推荐研究问题）。"""
    try:
        import yaml
        raw = yaml.safe_load(_ADAPTER_CATALOG_PATH.read_text(encoding="utf-8")) or {}
        for adapter in raw.get("adapters", []):
            if adapter.get("adapter_id") == adapter_id:
                return [str(q) for q in adapter.get("priority_questions", [])]
    except Exception as e:  # noqa: BLE001 - 目录缺失不阻塞主流程
        logger.warning("orchestrator: 读 Adapter 目录失败 %s: %s", adapter_id, e)
    return []


def collect_adapter_questions(profiles: list[UnitProfile]) -> list[str]:
    """从各单元的 selected_adapter 汇总推荐问题（供计划生成吸收）。"""
    seen: set[str] = set()
    questions: list[str] = []
    for p in profiles:
        if p.adapter_fallback:
            continue
        for q in _load_priority_questions(p.selected_adapter):
            if q not in seen:
                seen.add(q)
                questions.append(q)
        # 备选 Adapter 的推荐问题也纳入（split 模式多分部场景）
        if p.secondary_adapter:
            for q in _load_priority_questions(p.secondary_adapter):
                if q not in seen:
                    seen.add(q)
                    questions.append(q)
    return questions


def build_initial_context(
    facts: CompanyFacts,
    granularity: GranularityDecision,
    profiles: list[UnitProfile],
    memory_context: Optional[dict] = None,
    *,
    user_goal: str = "",
) -> dict:
    """把四件套关键字段压进 loop 的 initial_context。

    - facts_version / granularity：全流程缓存基准 + split 模式点名业务线；
    - selected_adapters：研究方法 Adapter（供后续 prompt 注入全文/摘要）。
    """
    from runtime.research_recipe import build_research_recipe

    context = {
        "facts_version": facts.facts_version,
        "company_facts": facts,
        "granularity": granularity,
        "selected_adapters": [p.selected_adapter for p in profiles],
        "profiles": profiles,
        # preloop 已完成实体锁定和画像/Adapter 识别。loop 应直接进入取数研究，
        # 不能再花两轮 LLM 重做 entity.resolve / company.classify。
        "skill_phase": 2,
        "current_entity": facts.entity.model_dump(),
        "business_group": next(
            (p.selected_adapter for p in profiles if not p.adapter_fallback),
            profiles[0].selected_adapter if profiles else "",
        ),
        "business_group_fallback": bool(
            profiles and all(p.adapter_fallback for p in profiles)
        ),
        # 研究配方与 ResearchPlan 互补：Plan 防漏问题，Recipe 防漏字段、
        # 计算和资料。二者都由上游确定性数据生成，不依赖模型临场记忆。
        "research_recipe": build_research_recipe(profiles, user_goal=user_goal),
    }
    if memory_context:
        # 偏好只作系统背景；认知目录用于按需展开与引用；底线供收尾闸门使用。
        context.update({
            "memory_preferences": memory_context.get("preferences", []),
            "cognition_directory": memory_context.get("cognition_directory", []),
            "hard_constraints": memory_context.get("hard_constraints", []),
            "memory_used_retrieval": memory_context.get("used_retrieval", False),
        })
    return context


# ── 编排结果 ──────────────────────────────────────────────

@dataclass
class PreloopOutput:
    """四件套 + loop 注入参数的一次编排结果（实现层）。

    注意：与 schemas.PreloopResult（PRD 4 章契约层，Pydantic）区分——本类的
    plan 是 runtime.plan.ResearchPlan（dataclass，带状态机），直接喂给 loop.run。
    """
    entity: CurrentEntitySchema
    facts: CompanyFacts
    granularity: GranularityDecision
    profiles: list[UnitProfile]
    plan: Any                                   # runtime.plan.ResearchPlan（dataclass）
    initial_context: dict = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)  # 降级/跳过环节的说明

    def to_dict(self) -> dict:
        return {
            "entity": self.entity.model_dump(),
            "facts_version": self.facts.facts_version,
            "granularity_mode": self.granularity.mode.value,
            "units": [u.id for u in self.granularity.units],
            "adapters": [p.selected_adapter for p in self.profiles],
            "p0_count": len(self.plan.p0_questions),
            "skipped": list(self.skipped),
        }


# ── 主编排 ────────────────────────────────────────────────

async def run_preloop(
    user_message: str,
    *,
    llm_client: Any,
    market_data: Any,
    web_search_fn: Any,
    candidate: Optional[str] = None,
    entity: Optional[Entity] = None,
    previous_entity: Optional[Entity] = None,
    force_refresh: bool = False,
    user_id: str = "",
    fast_mode: bool = False,
) -> PreloopOutput:
    """轻量启动器：快速识别标的 → 并行准备上下文 → 生成研究计划。

    新链路（vs 旧版串行重研究）：
      1. 实体解析（本地表优先，不依赖 LLM）
      2. 并行启动：
         - build_light_facts（白名单搜索，不调 market.bundle / calc.metric）
         - prepare_research_memory（认知库检索，同步 SQLite 用 asyncio.to_thread 包裹）
         - 事实完整性记录（缺失项进入 open_questions，不形成 A/B/C 约束）
      3. 粒度决策 + 画像/Adapter 选型（可并行）
      4. 计划生成（显式标记数据需求类型：light_evidence / market_data_required /
         calc_required / memory_verify）

    Args:
        user_message:   用户研究目标（兼作计划生成的 goal）。
        candidate:      标的指称（来自路由 entity_candidates）；entity 为空时从 message/candidate 解析。
        entity:         已解析的实体（跳过 resolve_entity，供上游/测试注入）。
        previous_entity: 上轮 current_entity（继承判定用）。
        fast_mode:      True 时跳过粒度 LLM 判断、计划强制失败降级。

    Returns:
        PreloopOutput：四件套 + initial_context（可直接喂给 loop.run）。
    """
    from runtime.preloop.facts_builder import build_light_facts, build_company_facts
    from runtime.preloop.granularity import decide_granularity
    from runtime.preloop.profiler import build_unit_profiles
    from runtime.plan import generate_research_plan

    # ══════════════════════════════════════════════
    # 第 1 步：实体解析（必须先完成，后续步骤依赖 entity）
    # ══════════════════════════════════════════════
    if entity is None:
        raw = candidate or (extract_candidates(user_message)[0] if extract_candidates(user_message) else "")
        if not raw:
            raise ValueError("无法从输入解析标的：preloop 需要明确的公司指称")
        resolution = await asyncio.wait_for(
            resolve_entity(raw, previous_entity=previous_entity, market_data=market_data),
            timeout=5 if fast_mode else 10,
        )
        if not resolution.resolved or resolution.entity is None:
            raise ValueError(f"实体解析未收敛: {resolution.message}")
        entity = resolution.entity
    entity_schema = entity_to_current_schema(entity)
    logger.info("preloop[light]: 实体解析完成 %s (%s)", entity_schema.canonical_name, entity_schema.security_id)

    # ══════════════════════════════════════════════
    # 第 2 步：并行启动轻量准备（核心提速点）
    # ══════════════════════════════════════════════

    # 在并发区前完成同步 import / 配置解析。否则首次 import 的开销会发生在
    # event loop 上，表面上用了 gather，实际却可能把 to_thread 的启动推迟到
    # light_facts 之后，退化为串行。
    memory_store = None
    if should_load_cognition(user_id):
        try:
            from store.cognition_store import CognitionStore
            from core.config import get_config
            db_url = get_config().database_url.replace("+aiosqlite", "")
            memory_store = CognitionStore(db_url)
        except Exception as exc:
            logger.warning("preloop[light]: 认知库初始化失败，按空记忆继续: %s", exc)

    async def _prepare_memory() -> dict:
        """认知库检索（同步 SQLite 用 asyncio.to_thread 包裹避免阻塞）。"""
        ctx: dict = {}
        if memory_store is not None:
            try:
                ctx = await asyncio.to_thread(
                    memory_store.prepare_research_memory,
                    user_id=user_id, symbol=entity_schema.security_id, query=user_message,
                )
            except Exception as exc:
                logger.warning("preloop[light]: 认知库读取失败，按空记忆继续: %s", exc)
        return ctx

    async def _build_facts() -> Any:
        """轻量事实包（不调 market.bundle，只做白名单搜索）。"""
        return await build_light_facts(
            entity_schema, llm_client, web_search_fn,
            force_refresh=force_refresh, fast_mode=fast_mode,
        )

    # 并行启动：轻量事实包 + 认知库检索
    logger.info("preloop[light]: 并行启动 light_facts + memory_retrieval ...")
    # 先让同步 SQLite 读取进入工作线程，再启动联网/LLM 事实包；这避免冷启动
    # 时线程池创建被轻量事实包的同步准备阶段遮住，确保两支确实并行。
    memory_task = asyncio.create_task(_prepare_memory())
    await asyncio.sleep(0)
    facts_task = asyncio.create_task(_build_facts())
    facts, memory_context = await asyncio.gather(facts_task, memory_task)
    if memory_store is not None and memory_context:
        segments_by_share = sorted(
            list(getattr(facts, "segments", []) or []),
            key=lambda item: getattr(item, "revenue_share", None) or 0,
            reverse=True,
        )
        industry_hint = getattr(segments_by_share[0], "name", "") if segments_by_share else ""
        memory_context = memory_store.attach_industry_memory(memory_context, industry_hint)
    logger.info(
        "preloop[light]: 并行完成 | facts_version=%s | memory=%d items",
        facts.facts_version, len(memory_context.get("selected_cognitions", [])),
    )

    # ══════════════════════════════════════════════
    # 第 3 步：粒度决策 + 画像/Adapter（可并行）
    # ══════════════════════════════════════════════
    segments = list(getattr(facts, "segments", []) or [])
    dominant_share = max(
        (float(getattr(item, "revenue_share", 0) or 0) for item in segments),
        default=0,
    )

    if fast_mode and (len(segments) <= 1 or dominant_share >= 80):
        from runtime.preloop.granularity import _make_whole_fallback
        granularity = _make_whole_fallback(
            facts, reason="低延迟快速路径：单主营或主业收入占比>=80%",
        )
    else:
        granularity = await decide_granularity(facts, llm_client, force_refresh=force_refresh)

    profiles = await build_unit_profiles(facts, granularity, llm_client, force_refresh=force_refresh, fast_mode=fast_mode)

    # ══════════════════════════════════════════════
    # 第 4 步：研究计划（显式标记数据需求类型）
    # ══════════════════════════════════════════════
    plan = await generate_research_plan(
        goal=user_message,
        entity=entity_schema.model_dump(),
        facts=facts_to_plan_input(facts),
        adapter_questions=collect_adapter_questions(profiles),
        user_cognitions=memory_context.get("selected_cognitions", []),
        granularity={
            "mode": granularity.mode.value,
            "units": [{"id": u.id, "name": u.scope} for u in granularity.units],
        },
        llm_client=llm_client,
        force_fail=fast_mode,
        llm_timeout_seconds=12,
    )

    # 公司作用域的历史判断必须变成待验证问题
    if memory_context and memory_context.get("company_assertions"):
        try:
            from store.cognition_store import CognitionStore
            from core.config import get_config
            db_url = get_config().database_url.replace("+aiosqlite", "")
            store = CognitionStore(db_url)
            for item in store.cognitions_to_questions(memory_context["company_assertions"]):
                if any(q.memory_id == item["memory_id"] for q in plan.questions):
                    continue
                plan.add_question(
                    item["question"], item["priority"], item["dimension"],
                    reason="已确认的同标的历史判断必须在本次研究中验证",
                    source="cognition", memory_id=item["memory_id"], memory_mode=item["memory_mode"],
                )
        except Exception as exc:
            logger.warning("preloop[light]: 历史判断转问题失败: %s", exc)

    initial_context = build_initial_context(
        facts, granularity, profiles, memory_context, user_goal=user_message,
    )
    initial_context["memory_symbol"] = entity_schema.security_id
    initial_context["memory_company"] = entity_schema.canonical_name

    logger.info(
        "preloop[light]: 完成 | entity=%s | p0=%d | skipped=%s",
        entity_schema.security_id, len(plan.p0_questions),
        getattr(plan, "skipped_phases", ""),
    )
    return PreloopOutput(
        entity=entity_schema,
        facts=facts,
        granularity=granularity,
        profiles=profiles,
        plan=plan,
        initial_context=initial_context,
    )
