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

MAX_PARALLEL_STEPS = 4


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
    """执行一轮计划；同优先级受控并行，每个组件共享一份总预算。"""
    unresolved = set(plan.fields_to_fetch)
    found: dict[str, FetchedField] = {}
    attempts: list[FetchAttempt] = []
    semaphore = asyncio.Semaphore(MAX_PARALLEL_STEPS)
    loop = asyncio.get_running_loop()
    component_steps: dict[str, list[FetchStep]] = {}
    for step in plan.steps:
        component_steps.setdefault(step.component, []).append(step)
    component_limits = {
        component: plan.budgets.get(
            component, min(item.budget_seconds for item in steps)
        )
        for component, steps in component_steps.items()
    }
    deadlines: dict[str, float] = {}

    async def run_step(step: FetchStep, wanted: tuple[str, ...]):
        pairs = [(field, source) for field, source in zip(step.fields, step.source_fields)
                 if field in wanted]
        active_step = FetchStep(**{
            **asdict(step), "fields": tuple(field for field, _ in pairs),
            "source_fields": tuple(source for _, source in pairs),
        })
        deadlines.setdefault(step.component, loop.time() + component_limits[step.component])
        remaining = min(step.budget_seconds, deadlines[step.component] - loop.time())

        async def invoke():
            async with semaphore:
                value = route_runner(active_step, plan.symbol)
                return await value if inspect.isawaitable(value) else value

        try:
            if remaining <= 0:
                raise TimeoutError(f"{step.component} total budget exhausted")
            return active_step, await asyncio.wait_for(invoke(), timeout=remaining), None
        except asyncio.TimeoutError:
            return active_step, None, TimeoutError(
                f"{step.component} total budget exhausted"
            )
        except Exception as exc:  # Provider 异常必须转成可降级结果
            return active_step, None, exc

    steps = sorted(plan.steps, key=lambda item: (item.attempt, item.order))
    for attempt_number in sorted({step.attempt for step in steps}):
        wave: list[tuple[FetchStep, tuple[str, ...]]] = []
        for step in steps:
            if step.attempt != attempt_number:
                continue
            wanted = tuple(name for name in step.fields if name in unresolved)
            if wanted:
                wave.append((step, wanted))
        if not wave:
            continue
        results = await asyncio.gather(*(run_step(step, wanted) for step, wanted in wave))
        for active_step, returned, error in sorted(results, key=lambda item: item[0].order):
            wanted = active_step.fields
            accepted: list[str] = []
            if error is None:
                if not isinstance(returned, Mapping):
                    error = TypeError("provider result must be a field mapping")
                else:
                    for field_name in wanted:
                        if field_name not in returned:
                            continue
                        normalized = _normalize_result(field_name, returned[field_name], active_step)
                        if normalized is not None:
                            found[field_name] = normalized
                            unresolved.discard(field_name)
                            accepted.append(field_name)
            if error is not None:
                status = "failed"
                reason = f"{type(error).__name__}: {error}"
            else:
                status = "success" if len(accepted) == len(wanted) else "partial" if accepted else "empty"
                reason = "" if status == "success" else "返回为空、类型错误或报告期不匹配"
            attempts.append(FetchAttempt(
                order=active_step.order, attempt=active_step.attempt,
                provider=active_step.provider, upstream=active_step.upstream,
                request=active_step.request, requested_fields=wanted,
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
