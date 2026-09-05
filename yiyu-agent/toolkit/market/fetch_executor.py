"""字段级取数执行器。

它只负责消费 FetchPlan：按 attempt 顺序执行，成功字段立即停止
降级，并把每次失败理由留在结果中。具体接口调用由 route_runner 完成。
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import inspect
import math
from typing import Any, Awaitable, Callable, Mapping

from toolkit.market.fetch_planner import FetchPlan, FetchStep
from toolkit.market.field_registry import FIELD_SPECS, field_parts


@dataclass(frozen=True)
class FetchedField:
    field: str
    value: Any
    period: str
    provider: str
    upstream: str
    source_field: str
    fetched_at: str = ""
    unit: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FetchAttempt:
    order: int
    attempt: int
    provider: str
    upstream: str
    request: str
    requested_fields: tuple[str, ...]
    returned_fields: tuple[str, ...]
    status: str
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FetchExecution:
    status: str
    fields: dict[str, FetchedField] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    unregistered_fields: list[str] = field(default_factory=list)
    unsupported_fields: list[str] = field(default_factory=list)
    blocked_fields: list[str] = field(default_factory=list)
    attempts: list[FetchAttempt] = field(default_factory=list)
    web_search_candidates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["fields"] = {name: value.to_dict() for name, value in self.fields.items()}
        return result


RouteRunner = Callable[
    [FetchStep, str],
    Mapping[str, FetchedField | Mapping[str, Any] | Any]
    | Awaitable[Mapping[str, FetchedField | Mapping[str, Any] | Any]],
]


def _valid_value(field_name: str, value: Any) -> bool:
    if value is None:
        return False
    base, _ = field_parts(field_name)
    spec = FIELD_SPECS.get(base)
    if spec is None:
        return False
    if spec.type == "float":
        if isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return False
    return bool(str(value).strip())


def _normalize_result(
    field_name: str, raw: FetchedField | Mapping[str, Any] | Any, step: FetchStep
) -> FetchedField | None:
    if isinstance(raw, FetchedField):
        result = raw
    elif isinstance(raw, Mapping):
        result = FetchedField(
            field=field_name,
            value=raw.get("value"),
            period=str(raw.get("period") or "current"),
            provider=str(raw.get("provider") or step.provider),
            upstream=str(raw.get("upstream") or step.upstream),
            source_field=str(raw.get("source_field") or field_name),
            fetched_at=str(raw.get("fetched_at") or ""),
            unit=str(raw.get("unit") or ""),
        )
    else:
        result = FetchedField(
            field=field_name, value=raw, period="current", provider=step.provider,
            upstream=step.upstream, source_field=field_name,
        )
    base, requested_period = field_parts(field_name)
    actual_period = result.period.replace("-", "")
    if requested_period and actual_period != requested_period:
        return None
    if not _valid_value(base, result.value):
        return None
    return result


async def execute_fetch_plan(plan: FetchPlan, route_runner: RouteRunner) -> FetchExecution:
    """执行一轮计划。同一字段一旦成功，后续条件步骤不再请求它。"""
    unresolved = set(plan.fields_to_fetch)
    found: dict[str, FetchedField] = {}
    attempts: list[FetchAttempt] = []

    for step in sorted(plan.steps, key=lambda item: item.order):
        wanted = tuple(name for name in step.fields if name in unresolved)
        if not wanted:
            continue
        source_by_field = dict(zip(step.fields, step.source_fields))
        active_step = FetchStep(**{
            **asdict(step), "fields": wanted,
            "source_fields": tuple(source_by_field[name] for name in wanted),
        })
        try:
            returned = route_runner(active_step, plan.symbol)
            if inspect.isawaitable(returned):
                returned = await asyncio.wait_for(returned, timeout=step.budget_seconds)
            if not isinstance(returned, Mapping):
                raise TypeError("provider result must be a field mapping")
            accepted: list[str] = []
            for field_name in wanted:
                if field_name not in returned:
                    continue
                normalized = _normalize_result(field_name, returned[field_name], step)
                if normalized is not None:
                    found[field_name] = normalized
                    unresolved.discard(field_name)
                    accepted.append(field_name)
            status = "success" if len(accepted) == len(wanted) else "partial" if accepted else "empty"
            reason = "" if status == "success" else "返回为空、类型错误或报告期不匹配"
        except Exception as exc:  # Provider 异常必须转成可降级结果
            accepted = []
            status = "failed"
            reason = f"{type(exc).__name__}: {exc}"
        attempts.append(FetchAttempt(
            order=step.order, attempt=step.attempt, provider=step.provider,
            upstream=step.upstream, request=step.request, requested_fields=wanted,
            returned_fields=tuple(accepted), status=status, reason=reason,
        ))

    missing = [name for name in plan.fields_to_fetch if name in unresolved]
    unresolved_all = list(dict.fromkeys([
        *missing, *plan.unregistered_fields, *plan.unsupported_fields,
        *plan.provider_blocked_fields,
    ]))
    if not unresolved_all:
        status = "ok"
    elif found or plan.reusable_fields:
        status = "partial"
    elif plan.unregistered_fields and not (missing or plan.unsupported_fields or plan.provider_blocked_fields):
        status = "unregistered"
    elif plan.unsupported_fields and not (missing or plan.provider_blocked_fields):
        status = "not_supported"
    else:
        status = "request_failed"
    return FetchExecution(
        status=status, fields=found, missing_fields=missing,
        unregistered_fields=list(plan.unregistered_fields),
        unsupported_fields=list(plan.unsupported_fields),
        blocked_fields=list(plan.provider_blocked_fields), attempts=attempts,
        web_search_candidates=unresolved_all,
    )
