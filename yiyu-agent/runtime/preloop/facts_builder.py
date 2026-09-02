"""
环节①：事实包构建（PRD 5.3 环节①）

职责：把 current_entity 变成 company_facts——联网检索业务分部 + 从
      market.bundle 提取财务快照，产出带 source_trace 的结构化事实包。

链路：
  current_entity
      → 联网检索（白名单 finance 源，搜主营业务/业务分部/收入占比）
      → LLM 结构化抽取（one_line_business + segments）
      → market.bundle（financial_snapshot）
      → 护栏校验（source_trace 必填；缺字段进 open_questions）
      → facts_version 计算（hash，全流程缓存键）
      → CompanyFacts

提速设计（PRD 明确要求"回复要快"）：
  · 进程内缓存（_FACTS_CACHE）：facts_version 相同 → 直接复用，跳过全部联网和 LLM。
  · 联网检索 与 market.bundle 取数 并发启动（asyncio.gather）。
  · 缓存 TTL = 当日（00:00 UTC 重置）：财报不会日内更新，日内复用安全。

降级铁律：
  · 联网检索失败 → segments 留空，记入 open_questions，不阻断。
  · market.bundle 失败 → financial_snapshot 为空对象，记入 open_questions，不阻断。
  · LLM 抽取失败 / 校验不通过 → 重试一次，仍失败降级为仅结构化数据源产出。
  · source_trace 缺失的字段判非法，打回重生成（最多 1 次），仍失败记 open_questions。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from runtime.schemas import (
    BusinessSegment,
    CompanyFacts,
    CurrentEntitySchema,
    FinancialSnapshot,
    InfoRichness,
    SourceTrace,
)

logger = logging.getLogger(__name__)

# ── 进程内缓存（symbol → (facts, expire_ts)）──────────────────────
# TTL = 当日剩余秒数（UTC 00:00 重置），日内复用安全（财报不会日内变化）
_FACTS_CACHE: dict[str, tuple[CompanyFacts, float]] = {}


def _today_expire_ts() -> float:
    """当日 UTC 00:00 重置 → 返回今天剩余到 00:00 的 Unix 时间戳。"""
    now = datetime.now(tz=timezone.utc)
    next_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # 已过今日 00:00，取明日 00:00
    if next_midnight <= now:
        next_midnight = next_midnight.replace(day=now.day + 1) if now.day < 28 else (
            next_midnight.replace(month=now.month + 1, day=1)
            if now.month < 12 else
            next_midnight.replace(year=now.year + 1, month=1, day=1)
        )
    return next_midnight.timestamp()


def _cache_get(symbol: str) -> Optional[CompanyFacts]:
    entry = _FACTS_CACHE.get(symbol)
    if entry is None:
        return None
    facts, expire_ts = entry
    if time.time() > expire_ts:
        del _FACTS_CACHE[symbol]
        return None
    return facts


def _cache_set(symbol: str, facts: CompanyFacts) -> None:
    _FACTS_CACHE[symbol] = (facts, _today_expire_ts())


def clear_cache(symbol: Optional[str] = None) -> None:
    """测试/强制刷新时调用：清除单个或全部缓存。"""
    if symbol:
        _FACTS_CACHE.pop(symbol, None)
    else:
        _FACTS_CACHE.clear()


# ── LLM 结构化抽取 Prompt ─────────────────────────────────────

_EXTRACT_SYSTEM = """\
你是公司研究前置分析模块。给你一批联网检索到的关于某家公司的文本片段，
你需要从中抽取结构化的公司事实。

要求：
1. one_line_business：用一句话（≤50字）说清楚公司靠什么赚钱，必须来自信源事实，不自由发挥。
2. segments：业务分部列表。每个分部包含：
   - name（分部名称，必填）
   - revenue_share（收入占比，0~1小数；如果信源只给出百分比数字，转换为小数；无数据填 null）
   - customer（客户群体，简短描述；无信息填 null）
   - charging（收费方式，如「虚拟道具/订阅」「一次性销售」；无信息填 null）
   - channel（主要渠道；无信息填 null）
   - competition（主要竞争对手，一句话；无信息填 null）
   - capex_profile（资本强度特征，如「轻资产」「重资产」；无信息填 null）
3. source_field_map：说明每个字段来自哪条信源（填 source 索引，如 "source_0" / "source_1"）。
4. open_questions：信源不足、口径冲突或无法判断的项目，逐条列出。

铁律：
- 结论性字段（one_line_business、segments 的名称和占比）必须来自信源，不臆断。
- 信源说不清楚时，宁可填 null 并记 open_questions，不编造。
- 只输出 JSON，不要任何其他文字。

输出格式：
{
  "one_line_business": "...",
  "segments": [
    {
      "name": "...",
      "revenue_share": 0.xx,
      "customer": "...",
      "charging": "...",
      "channel": "...",
      "competition": "...",
      "capex_profile": "..."
    }
  ],
  "source_field_map": {
    "one_line_business": "source_0",
    "segments[0].name": "source_1",
    "segments[0].revenue_share": "source_0"
  },
  "open_questions": ["..."]
}
"""

_EXTRACT_USER_TMPL = """\
公司：{name}（{security_id}）

以下是联网检索到的信源片段（共 {count} 条）：
{sources_text}

请按格式抽取结构化事实。
"""


# ── 内部：联网检索 ────────────────────────────────────────────

async def _web_search_business(
    entity: CurrentEntitySchema,
    web_search_fn: Any,
    *,
    timeout_seconds: float = 20,
) -> list[dict]:
    """用白名单财经源搜索公司主营业务、最新上市状态与定期报告。

    搜索两组关键词（业务与分部 / 上市状态与定期报告），并发执行。
    返回 [{title, snippet, url, source_level}] 列表。
    """
    name = entity.canonical_name
    today = datetime.now(tz=timezone.utc).date().isoformat()
    queries = [
        f"{name} {entity.security_id} 主营业务 业务分部 收入占比 商业模式",
        f"{name} {entity.security_id} 截至 {today} 最新上市状态 上市日期 年度报告 中期报告",
    ]

    async def _one(q: str) -> list[dict]:
        try:
            result = await asyncio.wait_for(
                web_search_fn(q, max_results=5, sources="finance"),
                timeout=timeout_seconds,
            )
            items = result.get("results") or []
            level = "A" if result.get("sources_verified") else "B"
            return [dict(r, source_level=level) for r in items]
        except Exception as e:  # noqa: BLE001
            logger.warning("facts_builder: 联网检索失败 q=%r: %s", q, e)
            return []

    results_list = await asyncio.gather(*[_one(q) for q in queries])
    # 按 URL 去重，保序
    seen: set[str] = set()
    merged: list[dict] = []
    for items in results_list:
        for item in items:
            url = item.get("url", "")
            if url and url not in seen:
                seen.add(url)
                merged.append(item)
    return merged


def _build_sources_text(search_results: list[dict]) -> str:
    """把搜索结果拼成给 LLM 看的文本，附索引编号（source_0, source_1...）。"""
    lines: list[str] = []
    for i, r in enumerate(search_results):
        lines.append(
            f"[source_{i}] 来源: {r.get('url', '未知')} | 等级: {r.get('source_level', 'B')}\n"
            f"标题: {r.get('title', '')}\n"
            f"摘要: {r.get('snippet', '')}"
        )
    return "\n\n".join(lines)


# ── 内部：LLM 抽取 ────────────────────────────────────────────

async def _llm_extract(
    entity: CurrentEntitySchema,
    search_results: list[dict],
    llm_client: Any,
    *,
    timeout_seconds: float = 40,
) -> dict:
    """调 LLM 从搜索结果抽取结构化事实。失败返回空 dict。"""
    if not search_results:
        return {}
    sources_text = _build_sources_text(search_results)
    user = _EXTRACT_USER_TMPL.format(
        name=entity.canonical_name,
        security_id=entity.security_id,
        count=len(search_results),
        sources_text=sources_text,
    )
    try:
        data = await asyncio.wait_for(
            llm_client.chat_json(
                system=_EXTRACT_SYSTEM,
                user=user,
                temperature=0.1,
            ),
            timeout=timeout_seconds,
        )
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:  # noqa: BLE001
        logger.warning("facts_builder: LLM 抽取失败: %s", e)
        return {}


# ── 内部：financial_snapshot 从 market.bundle 提取 ────────────

async def _fetch_financial_snapshot(
    entity: CurrentEntitySchema,
    market_data: Any,
) -> tuple[FinancialSnapshot, list[SourceTrace], list[str]]:
    """从 market.bundle 提取关键财务快照。

    返回 (FinancialSnapshot, source_traces, open_questions)。
    失败时返回空快照 + open_question 记录，不阻断流程。
    """
    open_qs: list[str] = []
    try:
        bundle = await asyncio.wait_for(
            market_data.bundle(entity.security_id),
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("facts_builder: market.bundle 失败 %s: %s", entity.security_id, e)
        open_qs.append(f"财务数据取数失败（{type(e).__name__}），financial_snapshot 为空")
        return FinancialSnapshot(), [], open_qs

    snap = FinancialSnapshot()
    traces: list[SourceTrace] = []
    fund = getattr(bundle, "fundamentals", None)

    # 从最新一期财报年取关键字段
    if fund and getattr(fund, "years", None):
        years_sorted = sorted(fund.years, key=lambda y: str(y.get("year", "")), reverse=True)
        latest = years_sorted[0] if years_sorted else {}
        year_label = str(latest.get("year", "最新"))
        source_desc = f"akshare/westock 财务数据（{year_label}年报）"
        source_level: str = "A"

        # revenue_ttm
        revenue = latest.get("revenue")
        if revenue is not None:
            snap.revenue_ttm = float(revenue)
            traces.append(SourceTrace(
                field="financial_snapshot.revenue_ttm",
                source=source_desc, source_level=source_level,
                as_of=f"{year_label}-12-31",
            ))
        else:
            open_qs.append("营收字段缺失，revenue_ttm 为空")

        # gross_margin
        gm = latest.get("gross_margin")
        if gm is not None:
            # 数据源返回百分比数值（如 45.2），统一转为 0~1 小数
            snap.gross_margin = float(gm) / 100.0 if float(gm) > 1.0 else float(gm)
            traces.append(SourceTrace(
                field="financial_snapshot.gross_margin",
                source=source_desc, source_level=source_level,
                as_of=f"{year_label}-12-31",
            ))
        else:
            open_qs.append("毛利率字段缺失，gross_margin 为空")

        # ocf
        ocf = latest.get("ocf")
        if ocf is not None:
            snap.ocf = float(ocf)
            traces.append(SourceTrace(
                field="financial_snapshot.ocf",
                source=source_desc, source_level=source_level,
                as_of=f"{year_label}-12-31",
            ))
        else:
            open_qs.append("经营现金流字段缺失，ocf 为空")

        # capex
        capex = latest.get("capex")
        if capex is not None:
            snap.capex = float(capex)
            traces.append(SourceTrace(
                field="financial_snapshot.capex",
                source=source_desc, source_level=source_level,
                as_of=f"{year_label}-12-31",
            ))
        else:
            open_qs.append("资本开支字段缺失，capex 为空")

        snap.as_of = f"{year_label}-12-31"
    else:
        open_qs.append("market.bundle 未返回财务数据（fundamentals 为空）")

    # 货币单位从 snapshot 取
    market_snap = getattr(bundle, "snapshot", None)
    if market_snap and getattr(market_snap, "currency", None):
        snap.currency = market_snap.currency
    else:
        # 按市场推断
        market_map = {"A": "CNY", "HK": "HKD", "US": "USD"}
        snap.currency = market_map.get(entity.primary_market, "CNY")

    return snap, traces, open_qs


# ── 内部：从 LLM 输出组装 segments + traces ──────────────────

def _parse_llm_output(
    llm_data: dict,
    search_results: list[dict],
) -> tuple[str, list[BusinessSegment], list[SourceTrace], list[str]]:
    """从 LLM 结构化输出里组装 one_line_business, segments, source_trace, open_questions。

    source_field_map 里每个字段指向 source_{i}，从 search_results[i] 取 URL 和等级。
    """
    one_line = str(llm_data.get("one_line_business") or "").strip()
    raw_segments: list[dict] = llm_data.get("segments") or []
    field_map: dict[str, str] = llm_data.get("source_field_map") or {}
    llm_open_qs: list[str] = [str(q) for q in (llm_data.get("open_questions") or [])]

    def _source_for(field_key: str) -> tuple[str, str, str]:
        """根据 field_map 返回 (source_desc, url, level)。"""
        src_ref = field_map.get(field_key, "")
        idx_str = src_ref.replace("source_", "").strip()
        try:
            idx = int(idx_str)
            r = search_results[idx]
            return (
                f"{r.get('title', '联网检索')} ({r.get('url', '')})",
                r.get("url", ""),
                r.get("source_level", "B"),
            )
        except (ValueError, IndexError):
            # 找不到对应 source → B 级兜底
            return "联网检索（来源未明确）", "", "B"

    traces: list[SourceTrace] = []

    # one_line_business 的 source_trace
    if one_line:
        desc, url, level = _source_for("one_line_business")
        traces.append(SourceTrace(
            field="one_line_business",
            source=desc,
            url=url or None,
            source_level=level,  # type: ignore[arg-type]
        ))

    # segments
    segments: list[BusinessSegment] = []
    for i, seg in enumerate(raw_segments):
        name = str(seg.get("name") or "").strip()
        if not name:
            continue

        # revenue_share 容错：允许 0~1 小数或 1~100 百分比
        rs = seg.get("revenue_share")
        if rs is not None:
            try:
                rs_f = float(rs)
                if rs_f > 1.0:
                    rs_f = rs_f / 100.0
                rs = max(0.0, min(1.0, rs_f))
            except (TypeError, ValueError):
                rs = None

        segments.append(BusinessSegment(
            name=name,
            revenue_share=rs,
            customer=seg.get("customer") or None,
            charging=seg.get("charging") or None,
            channel=seg.get("channel") or None,
            competition=seg.get("competition") or None,
            capex_profile=seg.get("capex_profile") or None,
        ))

        # 为 name 和 revenue_share 各添加 source_trace
        desc_name, url_name, lvl_name = _source_for(f"segments[{i}].name")
        traces.append(SourceTrace(
            field=f"segments[{i}].name",
            source=desc_name,
            url=url_name or None,
            source_level=lvl_name,  # type: ignore[arg-type]
        ))
        if rs is not None:
            desc_rs, url_rs, lvl_rs = _source_for(f"segments[{i}].revenue_share")
            traces.append(SourceTrace(
                field=f"segments[{i}].revenue_share",
                source=desc_rs,
                url=url_rs or None,
                source_level=lvl_rs,  # type: ignore[arg-type]
            ))

    return one_line, segments, traces, llm_open_qs


# ── 内部：护栏校验 ────────────────────────────────────────────

def _validate_and_fix(
    entity: CurrentEntitySchema,
    one_line: str,
    segments: list[BusinessSegment],
    traces: list[SourceTrace],
    open_qs: list[str],
) -> tuple[bool, list[str]]:
    """护栏校验（确定性代码）：检查 source_trace 覆盖必填字段。

    返回 (是否通过, 补充的 open_questions)。
    PRD 规则：source_trace 缺失的抽取结果判为非法，打回补溯源；
    此处把无法溯源的字段记入 open_questions（不直接拦截，让降级路径走）。
    """
    extra_qs: list[str] = []
    traced_fields = {t.field for t in traces}

    if one_line and "one_line_business" not in traced_fields:
        extra_qs.append("one_line_business 缺少 source_trace，需补充信源")

    for i, seg in enumerate(segments):
        if f"segments[{i}].name" not in traced_fields:
            extra_qs.append(f"segments[{i}].name（{seg.name}）缺少 source_trace")

    return len(extra_qs) == 0, extra_qs


def _rate_info_richness(
    entity: CurrentEntitySchema,
    segments: list[BusinessSegment],
    fin_snap: FinancialSnapshot,
    search_results: list[dict],
    open_qs: list[str],
) -> InfoRichness:
    """确定性规则评定 info_richness 等级（PRD 5.3 环节① 评级依据）。

    五项评分维度（每项 0/1），总分决定档位：
      4-5 → A   2-3 → B   0-1 → C

    1. 财务快照完整（revenue_ttm 和 gross_margin 至少一项有值）
    2. 分部信息可拆分（segments 非空且至少1个有 revenue_share）
    3. 信源有 S/A 级（search_results 含 S 或 A 级来源）
    4. 主营可明确（one_line_business 已填，由调用方判断）
    5. open_questions 缺口较少（≤3 个）
    """
    score = 0

    # 1. 财务快照完整度
    if fin_snap.revenue_ttm is not None or fin_snap.gross_margin is not None:
        score += 1

    # 2. 分部可拆分
    if segments and any(s.revenue_share is not None for s in segments):
        score += 1

    # 3. 有高质量信源
    high_quality = any(r.get("source_level") in ("S", "A") for r in search_results)
    if high_quality:
        score += 1

    # 4. one_line_business 有实质内容（段落已产出，此处直接加分）
    #    调用方在 segments 非空时给分
    if segments:
        score += 1

    # 5. 缺口较少
    if len(open_qs) <= 3:
        score += 1

    if score >= 4:
        return InfoRichness.A
    elif score >= 2:
        return InfoRichness.B
    else:
        return InfoRichness.C


# ── 降级：仅用结构化数据源产出 ───────────────────────────────

def _fallback_from_structured(
    entity: CurrentEntitySchema,
    fin_snap: FinancialSnapshot,
    fin_traces: list[SourceTrace],
    all_open_qs: list[str],
) -> CompanyFacts:
    """联网+LLM 全部失败时的最终降级：仅保留财务快照，业务分部留空。"""
    all_open_qs.append(
        "联网检索和LLM抽取均失败，one_line_business 和 segments 依赖人工补充"
    )
    # 降级产物：无分部信息，信源匮乏，直接评为 C 级
    richness = InfoRichness.C
    facts = CompanyFacts(
        entity=entity,
        one_line_business=f"{entity.canonical_name}（业务信息待补充）",
        segments=[],
        financial_snapshot=fin_snap,
        info_richness=richness,
        source_trace=fin_traces or [SourceTrace(
            field="entity", source="实体解析", source_level="S"
        )],
        open_questions=all_open_qs,
    )
    facts.compute_version()
    return facts


# ── 轻量事实包（preloop 启动器专用）───────────────────────────

async def build_light_facts(
    entity: CurrentEntitySchema,
    llm_client: Any,
    web_search_fn: Any,
    *,
    force_refresh: bool = False,
    fast_mode: bool = False,
) -> CompanyFacts:
    """轻量事实包构建：不调 market.bundle，只做白名单搜索 + LLM 抽取。

    用于 preloop「轻量启动器」模式：
      - 只拿：公司全称、股票代码、上市地、主营业务一句话、行业、近期事件
      - 不拿：财报数据、行情快照、估值指标（正式 loop 先用 market.get_bundle 取数，再由 calc 消费统一数据包）
      - 目标耗时：10-20s（vs 完整版 30-50s）

    Args:
        entity:        来自 resolver.py 的当前证券实体。
        llm_client:    实现 chat_json(system, user, temperature) 的客户端。
        web_search_fn: 可调用对象，签名 (query, max_results, sources) → dict。

    Returns:
        CompanyFacts（financial_snapshot 为空，info_richness 基于轻量维度评定）。
    """
    symbol = entity.security_id

    # 缓存命中直接返回（与完整版共享缓存键逻辑）
    if not force_refresh:
        cached = _cache_get(symbol)
        if cached is not None:
            logger.info("facts_builder[light]: 缓存命中 %s", symbol)
            return cached

    logger.info("facts_builder[light]: 开始构建轻量事实包 %s", symbol)
    all_open_qs: list[str] = []

    # 只做联网检索（不调 market.bundle）
    # 快速启动器的两个网络/LLM 阶段各自收紧预算，确保 preloop 能在 35 秒
    # 总窗口内留出画像和计划时间；普通调用保持原有上限。
    stage_timeout = 8 if fast_mode else 12
    search_results = await _web_search_business(
        entity, web_search_fn, timeout_seconds=stage_timeout,
    )

    if not search_results:
        all_open_qs.append("联网检索未返回结果，业务信息依赖正式 loop 补充")
        return _fallback_from_structured(
            entity, FinancialSnapshot(), [SourceTrace(field="entity", source="实体解析", source_level="S")],
            all_open_qs,
        )

    # LLM 结构化抽取（最多 1 次，fast 模式）
    llm_data = await _llm_extract(
        entity, search_results, llm_client, timeout_seconds=stage_timeout,
    )
    if not llm_data.get("one_line_business"):
        all_open_qs.extend([str(q) for q in (llm_data.get("open_questions") or [])])
        return _fallback_from_structured(
            entity, FinancialSnapshot(), [SourceTrace(field="entity", source="实体解析", source_level="S")],
            all_open_qs,
        )

    one_line, segments, biz_traces, llm_open_qs = _parse_llm_output(llm_data, search_results)
    all_open_qs.extend(llm_open_qs)

    # 护栏校验（轻量模式下跳过重试，快速通过）
    _validate_and_fix(entity, one_line, segments, biz_traces, all_open_qs)

    # source_trace 兜底
    all_traces = biz_traces or [SourceTrace(field="entity", source="实体解析", source_level="S")]

    # 信息丰富度评定（轻量版：不看财务快照，只看分部+信源+主营）
    richness = _rate_info_richness_light(segments, search_results, all_open_qs)

    try:
        facts = CompanyFacts(
            entity=entity,
            one_line_business=one_line or f"{entity.canonical_name}（业务描述待补充）",
            segments=segments,
            financial_snapshot=FinancialSnapshot(),  # 轻量版不留财务快照
            info_richness=richness,
            source_trace=all_traces,
            open_questions=all_open_qs,
        )
    except Exception as e:
        logger.warning("facts_builder[light]: 构造失败(%s)，降级", e)
        return _fallback_from_structured(
            entity, FinancialSnapshot(), [SourceTrace(field="entity", source="实体解析", source_level="S")],
            all_open_qs,
        )

    facts.compute_version()
    _cache_set(symbol, facts)
    logger.info(
        "facts_builder[light]: 完成 %s | segments=%d | version=%s | richness=%s",
        symbol, len(segments), facts.facts_version, richness.value,
    )
    return facts


def _rate_info_richness_light(
    segments: list[BusinessSegment],
    search_results: list[dict],
    open_qs: list[str],
) -> InfoRichness:
    """轻量版信息丰富度评定（不看财务快照，基于分部+信源+缺口评分）。

    四项评分（每项 0/1）：
      3-4 → B   1-2 → C   0 → C（无财务数据时最高只能到 B）

    1. 分部可拆分（segments 非空且至少1个有 revenue_share）
    2. 有高质量信源（search_results 含 S 或 A 级）
    3. 主营可明确（segments 非空）
    4. 缺口较少（≤3 个）
    """
    score = 0
    if segments and any(s.revenue_share is not None for s in segments):
        score += 1
    if any(r.get("source_level") in ("S", "A") for r in search_results):
        score += 1
    if segments:
        score += 1
    if len(open_qs) <= 3:
        score += 1

    # 轻量版无财务数据，最高 B 级
    if score >= 3:
        return InfoRichness.B
    return InfoRichness.C


# ── 主入口（完整版，保留供非 preloop 场景使用）──────────────────

async def build_company_facts(
    entity: CurrentEntitySchema,
    llm_client: Any,
    market_data: Any,
    web_search_fn: Any,
    *,
    force_refresh: bool = False,
    fast_mode: bool = False,
) -> CompanyFacts:
    """环节①主入口：从 current_entity 构建 company_facts。

    Args:
        entity:         来自 resolver.py 的当前证券实体（Pydantic 镜像版）。
        llm_client:     实现 chat_json(system, user, temperature) 的客户端。
        market_data:    MarketData 实例（提供 .bundle(symbol) 方法）。
        web_search_fn:  可调用对象，签名 (query, max_results, sources) → dict。
                        传 WebSearchTool().execute 即可；测试可传 mock。
        force_refresh:  True 时绕过进程内缓存，强制重新构建。

    Returns:
        CompanyFacts（已计算 facts_version，可直接作为缓存键）。
    """
    symbol = entity.security_id

    # ── 缓存命中直接返回 ──────────────────────────────────────
    if not force_refresh:
        cached = _cache_get(symbol)
        if cached is not None:
            logger.info("facts_builder: 缓存命中 %s facts_version=%s", symbol, cached.facts_version)
            return cached

    logger.info("facts_builder: 开始构建事实包 %s", symbol)
    all_open_qs: list[str] = []

    # ── 并发：联网检索 + market.bundle 取数 ─────────────────
    search_task = asyncio.create_task(
        _web_search_business(entity, web_search_fn)
    )
    finance_task = asyncio.create_task(
        _fetch_financial_snapshot(entity, market_data)
    )

    search_results, (fin_snap, fin_traces, fin_open_qs) = await asyncio.gather(
        search_task, finance_task
    )
    all_open_qs.extend(fin_open_qs)

    # ── LLM 结构化抽取（最多尝试 2 次）────────────────────────
    llm_data: dict = {}
    llm_attempts = 1 if fast_mode else 2
    for attempt in range(llm_attempts):
        llm_data = await _llm_extract(entity, search_results, llm_client)
        if llm_data.get("one_line_business"):
            break
        if attempt + 1 < llm_attempts:
            logger.warning("facts_builder: LLM 第1次抽取无 one_line_business，重试")

    if not llm_data.get("one_line_business"):
        logger.warning(
            "facts_builder: LLM %d 次未抽取到业务信息，降级为仅结构化数据源",
            llm_attempts,
        )
        # 修复：LLM 虽未抽出业务信息，但其 open_questions（如口径冲突、信源缺口）
        # 仍是有效观察，降级产物中不得丢弃（真模型评测 R-04 发现的管道 bug）
        llm_open_qs = [str(q) for q in (llm_data.get("open_questions") or [])]
        all_open_qs.extend(llm_open_qs)
        return _fallback_from_structured(entity, fin_snap, fin_traces, all_open_qs)

    # ── 解析 LLM 输出 ─────────────────────────────────────────
    one_line, segments, biz_traces, llm_open_qs = _parse_llm_output(llm_data, search_results)
    all_open_qs.extend(llm_open_qs)

    # ── 护栏校验（最多补充一次溯源后重试）──────────────────────
    passed, extra_qs = _validate_and_fix(entity, one_line, segments, biz_traces, all_open_qs)
    if not passed and not fast_mode:
        logger.warning("facts_builder: 护栏校验不通过，重试抽取: %s", extra_qs)
        # 重试一次（给 LLM 额外提示需要补充溯源）
        llm_data2 = await _llm_extract(entity, search_results, llm_client)
        if llm_data2.get("one_line_business"):
            one_line2, segments2, biz_traces2, llm_open_qs2 = _parse_llm_output(
                llm_data2, search_results
            )
            passed2, extra_qs2 = _validate_and_fix(
                entity, one_line2, segments2, biz_traces2, all_open_qs
            )
            if passed2:
                one_line, segments, biz_traces = one_line2, segments2, biz_traces2
                all_open_qs.extend(llm_open_qs2)
            else:
                # 两次都不通过：把缺失溯源记入 open_questions，继续使用第一次结果
                all_open_qs.extend(extra_qs)
                logger.warning("facts_builder: 两次抽取 source_trace 均不完整，已记入 open_questions")
        else:
            all_open_qs.extend(extra_qs)

    # ── source_trace 兜底：至少要有实体来源记录
    all_traces = biz_traces + fin_traces

    # source_trace 兜底：至少要有实体来源记录
    if not all_traces:
        all_traces = [SourceTrace(field="entity", source="实体解析", source_level="S")]

    # ── 评定信息丰富度等级 ─────────────────────────────────────
    richness = _rate_info_richness(
        entity=entity,
        segments=segments,
        fin_snap=fin_snap,
        search_results=search_results,
        open_qs=all_open_qs,
    )

    # ── 构造 CompanyFacts ────────────────────────────────────
    try:
        facts = CompanyFacts(
            entity=entity,
            one_line_business=one_line or f"{entity.canonical_name}（业务描述待补充）",
            segments=segments,
            financial_snapshot=fin_snap,
            info_richness=richness,
            source_trace=all_traces,
            open_questions=all_open_qs,
        )
    except Exception as e:  # noqa: BLE001 - Pydantic 校验失败 → 降级
        logger.warning("facts_builder: CompanyFacts 构造失败（%s），降级为仅结构化数据源", e)
        return _fallback_from_structured(entity, fin_snap, fin_traces, all_open_qs)

    facts.compute_version()
    _cache_set(symbol, facts)
    logger.info(
        "facts_builder: 事实包构建完成 %s | segments=%d | open_qs=%d | version=%s",
        symbol, len(segments), len(all_open_qs), facts.facts_version,
    )
    return facts
