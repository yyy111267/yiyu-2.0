"""第三层：为本轮请求生成最小、可降级的字段级取数计划。

Source Mapping 回答“一个字段有哪些已验证来源”；本模块结合本轮需求、当前
Research Data、缓存新鲜度、市场和 Provider 健康状态，回答“这一次实际还要调
哪些请求、先后顺序是什么”。它只制定计划，不执行网络请求、不写缓存、不计算指标。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from toolkit.calc.metric_service import _catalog_by_id
from toolkit.market.field_registry import FIELD_COMPONENTS, FIELD_SPECS, canonical_field, canonical_fields, field_parts
from toolkit.market.market_router import classify_symbol
from toolkit.market.source_mapping import SourceSpec, mapping_dict, mappings_for


COMPONENT_BUDGETS = {"snapshot": 3.0, "fundamentals": 15.0, "news": 5.0, "announcements": 5.0}
MAX_FAILURES_PER_COMPONENT = 3
DEFAULT_FRESHNESS_SECONDS = {"snapshot": 300, "fundamentals": 86400}
_UNAVAILABLE = {"down", "disabled", "unavailable", "circuit_open", "rate_limited"}
_DEGRADED = {"degraded", "unstable", "half_open"}


@dataclass(frozen=True)
class FieldAvailability:
    """Planner 看到的一条已有数据；可来自会话 Data Pack 或持久缓存。"""

    field: str
    value: Any
    source: str = ""
    period: str = "current"
    fetched_at: str = ""
    status: str = "ok"
    fresh: bool | None = None
    location: str = "data_pack"


@dataclass(frozen=True)
class FetchStep:
    """一个可合并为一次真实调用的请求批次。"""

    order: int
    attempt: int
    provider: str
    upstream: str
    method: str
    request: str
    fields: tuple[str, ...]
    source_fields: tuple[str, ...]
    component: str
    budget_seconds: float
    conditional: bool
    health: str
    freshness: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FetchPlan:
    """一轮规划结果；旧字段保留给现有执行器，新增字段表达动态决策。"""

    components: list[str]
    required_fields: list[str]
    missing_field_groups: list[str]
    budgets: dict[str, float] = field(default_factory=dict)
    max_failures: int = MAX_FAILURES_PER_COMPONENT
    field_sources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    source_mapping: dict[str, tuple[dict, ...]] = field(default_factory=dict)
    symbol: str = ""
    market: str = ""
    data_pack_id: str = ""
    requested_metrics: list[str] = field(default_factory=list)
    reusable_metrics: list[str] = field(default_factory=list)
    metrics_to_compute: list[str] = field(default_factory=list)
    reusable_fields: list[str] = field(default_factory=list)
    stale_fields: list[str] = field(default_factory=list)
    fields_to_fetch: list[str] = field(default_factory=list)
    unsupported_fields: list[str] = field(default_factory=list)
    unregistered_fields: list[str] = field(default_factory=list)
    provider_blocked_fields: list[str] = field(default_factory=list)
    steps: list[FetchStep] = field(default_factory=list)
    web_search_candidates: list[str] = field(default_factory=list)
    status: str = "ready"

    def budget_for(self, component: str) -> float:
        return self.budgets.get(component, COMPONENT_BUDGETS.get(component, 5.0))

    def to_dict(self) -> dict:
        return asdict(self)


def expand_required_fields(metric_ids: list[str] | None) -> list[str]:
    """把指标展开为基础字段；未知指标名按裸字段交给 Registry 判定。"""
    if not metric_ids:
        return []
    catalog = _catalog_by_id()
    out: list[str] = []
    for metric_id in metric_ids:
        name = str(metric_id).strip()
        definition = catalog.get(name)
        out.extend(definition.get("required_fields") or [] if definition else [name])
    return canonical_fields(out)


def _fields_to_components(fields: list[str]) -> list[str]:
    result: list[str] = []
    for name in fields:
        base, _ = field_parts(name)
        component = FIELD_COMPONENTS.get(base, "fundamentals")
        if component not in result:
            result.append(component)
    return result


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_existing(name: str, raw: Any, *, location: str) -> FieldAvailability:
    if isinstance(raw, FieldAvailability):
        return raw
    if isinstance(raw, Mapping) and "value" in raw:
        return FieldAvailability(
            field=canonical_field(str(raw.get("field") or name)), value=raw.get("value"),
            source=str(raw.get("source") or ""), period=str(raw.get("period") or "current"),
            fetched_at=str(raw.get("fetched_at") or raw.get("as_of") or ""),
            status=str(raw.get("status") or "ok"), fresh=raw.get("fresh"),
            location=str(raw.get("location") or location),
        )
    return FieldAvailability(field=canonical_field(name), value=raw, fresh=True, location=location)


def _bundle_fields(bundle: Any) -> dict[str, FieldAvailability]:
    if bundle is None:
        return {}
    raw_fields = getattr(bundle, "fields", None)
    if raw_fields is None and isinstance(bundle, Mapping):
        raw_fields = bundle.get("fields", bundle)
    if not isinstance(raw_fields, Mapping):
        return {}
    return {canonical_field(str(name)): _normalize_existing(str(name), value, location="data_pack")
            for name, value in raw_fields.items()}


def _reusable_metrics(bundle: Any, metric_ids: list[str], data_pack_id: str) -> list[str]:
    """只复用明确绑定当前 Data Pack 版本的计算结果，防止底层字段更新后沿用旧指标。"""
    raw = getattr(bundle, "derived_metrics", None)
    if raw is None and isinstance(bundle, Mapping):
        raw = bundle.get("derived_metrics")
    if not isinstance(raw, Mapping):
        return []
    reusable: list[str] = []
    for metric_id in metric_ids:
        result = raw.get(metric_id)
        if not isinstance(result, Mapping) or result.get("value") is None or result.get("status", "ok") != "ok":
            continue
        if result.get("fresh") is True or (data_pack_id and result.get("data_pack_id") == data_pack_id):
            reusable.append(metric_id)
    return reusable


def _period_matches(requested: str | None, available: FieldAvailability) -> bool:
    if requested is None:
        return True
    period = available.period.replace("-", "")
    return period == requested or (len(period) == 4 and requested == f"{period}1231")


def _is_fresh(field: str, available: FieldAvailability, *, now: datetime,
              freshness_seconds: Mapping[str, int]) -> bool:
    base, requested_period = field_parts(field)
    if available.value is None or available.status != "ok" or not _period_matches(requested_period, available):
        return False
    if available.source == "entity":
        return True
    if available.fresh is not None:
        return available.fresh
    if requested_period is not None:
        return True
    fetched = _parse_time(available.fetched_at)
    if fetched is None:
        return False
    component = FIELD_COMPONENTS.get(base, "fundamentals")
    return (now - fetched).total_seconds() <= freshness_seconds.get(component, 0)


def _health_for(spec: SourceSpec, states: Mapping[str, Any]) -> str:
    raw = states.get(spec.audit_key, states.get(spec.provider, states.get(spec.upstream, "available")))
    if isinstance(raw, Mapping):
        if raw.get("circuit_open"):
            return "circuit_open"
        if raw.get("enabled") is False:
            return "disabled"
        if raw.get("allowed") is False:
            return str(raw.get("reason") or "unavailable").split(":", 1)[0]
        raw = raw.get("status", "available")
    return str(raw or "available").lower()


def _rank_routes(routes: tuple[SourceSpec, ...], states: Mapping[str, Any]) -> list[tuple[SourceSpec, str]]:
    eligible = [(route, _health_for(route, states)) for route in routes]
    eligible = [item for item in eligible if item[1] not in _UNAVAILABLE]
    return sorted(eligible, key=lambda item: (item[1] in _DEGRADED, item[0].priority))


def _build_steps(chains: Mapping[str, list[tuple[SourceSpec, str]]]) -> list[FetchStep]:
    groups: dict[tuple, dict[str, Any]] = {}
    for field_name, chain in chains.items():
        for attempt, (route, health) in enumerate(chain, 1):
            key = (attempt, route.provider, route.upstream, route.method, route.request,
                   route.component, route.freshness, health)
            group = groups.setdefault(key, {"route": route, "fields": [], "source_fields": []})
            group["fields"].append(field_name)
            group["source_fields"].append(route.source_field)

    steps: list[FetchStep] = []
    for order, (key, group) in enumerate(sorted(groups.items(), key=lambda item: item[0][0]), 1):
        attempt, *_, health = key
        route: SourceSpec = group["route"]
        steps.append(FetchStep(
            order=order, attempt=attempt, provider=route.provider, upstream=route.upstream,
            method=route.method, request=route.request,
            fields=tuple(dict.fromkeys(group["fields"])),
            source_fields=tuple(dict.fromkeys(group["source_fields"])),
            component=route.component,
            budget_seconds=COMPONENT_BUDGETS.get(route.component, 5.0),
            conditional=attempt > 1, health=health, freshness=route.freshness,
            reason=("本轮缺失，首选可用来源" if attempt == 1
                    else "仅在前序来源失败、缺值或报告期不匹配时执行"),
        ))
    return steps


def plan_fetch(
    *, symbol: str | None = None, market: str | None = None,
    metric_ids: list[str] | None = None, requested_fields: list[str] | None = None,
    field_groups: list[str] | None = None, days: int | None = None,
    current_data: Any = None, cache_fields: Mapping[str, Any] | None = None,
    provider_status: Mapping[str, Any] | None = None,
    freshness_seconds: Mapping[str, int] | None = None, now: datetime | None = None,
    use_research_data: bool = True,
) -> FetchPlan:
    """结合本轮上下文生成计划；缓存层只把字段状态传入，本模块不访问 SQLite。"""
    del days
    provider_status = provider_status or {}
    freshness = {**DEFAULT_FRESHNESS_SECONDS, **(freshness_seconds or {})}
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    resolved_market = market or (classify_symbol(symbol) if symbol else None)
    data_pack_id = ""
    if current_data is None and symbol and use_research_data:
        from toolkit.market.research_data import get_research_bundle
        stored = get_research_bundle(symbol)
        if stored is not None:
            data_pack_id, current_data = stored
    if not data_pack_id and isinstance(current_data, Mapping):
        data_pack_id = str(current_data.get("data_pack_id") or "")

    existing = _bundle_fields(current_data)
    cached: dict[str, FieldAvailability] = {}
    for name, raw in (cache_fields or {}).items():
        canonical = canonical_field(str(name))
        cached[canonical] = _normalize_existing(canonical, raw, location="cache")

    requested_metrics = [str(item).strip() for item in (metric_ids or []) if str(item).strip()]
    reusable_metrics = _reusable_metrics(current_data, requested_metrics, data_pack_id)
    metrics_to_compute = [item for item in requested_metrics if item not in reusable_metrics]
    required_fields = (expand_required_fields(metrics_to_compute) if metric_ids else
                       canonical_fields(requested_fields) if requested_fields else [])
    explicit_groups = [str(group).strip() for group in (field_groups or []) if str(group).strip()]
    components = (["snapshot", "fundamentals", "news"]
                  if not metric_ids and not requested_fields and not field_groups
                  else _fields_to_components(required_fields))
    for group in explicit_groups:
        if group not in components:
            components.append(group)

    reusable: list[str] = []
    stale: list[str] = []
    fields_to_fetch: list[str] = []
    unsupported: list[str] = []
    unregistered: list[str] = []
    provider_blocked: list[str] = []
    chains: dict[str, list[tuple[SourceSpec, str]]] = {}
    for requested in required_fields:
        base, _ = field_parts(requested)
        candidates = [item for item in (
            existing.get(requested) or existing.get(base),
            cached.get(requested) or cached.get(base),
        ) if item is not None]
        if any(_is_fresh(requested, item, now=now, freshness_seconds=freshness)
               for item in candidates):
            reusable.append(requested)
            continue
        if candidates:
            stale.append(requested)
        if base not in FIELD_SPECS:
            unregistered.append(requested)
            continue
        routes = mappings_for(requested, market=resolved_market)
        if not routes:
            unsupported.append(requested)
            continue
        ranked = _rank_routes(routes, provider_status)
        if not ranked:
            provider_blocked.append(requested)
            continue
        fields_to_fetch.append(requested)
        chains[requested] = ranked

    steps = _build_steps(chains)
    unresolved = [*unsupported, *unregistered, *provider_blocked]
    if requested_metrics and not metrics_to_compute:
        status = "satisfied"
    elif required_fields and not fields_to_fetch and not unresolved:
        status = "satisfied"
    elif steps and unresolved:
        status = "partial"
    elif steps:
        status = "ready"
    elif unresolved:
        status = "blocked"
    else:
        status = "ready"

    if required_fields:
        components = _fields_to_components(fields_to_fetch)
        for group in explicit_groups:
            if group in {"news", "announcements"} and group not in components:
                components.append(group)

    return FetchPlan(
        symbol=str(symbol or ""), market=str(resolved_market or ""), data_pack_id=data_pack_id,
        requested_metrics=requested_metrics, reusable_metrics=reusable_metrics,
        metrics_to_compute=metrics_to_compute,
        components=components, required_fields=required_fields,
        missing_field_groups=[group for group in explicit_groups if group not in components],
        budgets={component: COMPONENT_BUDGETS.get(component, 5.0) for component in components},
        field_sources={name: tuple(dict.fromkeys(route.provider for route, _ in chains.get(name, [])))
                       for name in required_fields},
        source_mapping=mapping_dict(required_fields, market=resolved_market),
        reusable_fields=reusable, stale_fields=stale, fields_to_fetch=fields_to_fetch,
        unsupported_fields=unsupported, unregistered_fields=unregistered,
        provider_blocked_fields=provider_blocked, steps=steps,
        web_search_candidates=list(dict.fromkeys([*fields_to_fetch, *unresolved])), status=status,
    )


def should_fetch_news(plan: FetchPlan) -> bool:
    return "news" in plan.components or "announcements" in plan.components
