"""研究主链路的统一行情数据包。

``market.get_bundle`` 是唯一写入者；计算工具只读取，不自行实例化
``MarketData`` 或访问外部 provider。同一标的后续补数时合并为新版本，
因此“统一取数”不等于数据包不可扩展。
"""

from __future__ import annotations

from collections import OrderedDict
from contextvars import ContextVar, Token
from dataclasses import fields, replace
from threading import RLock

from toolkit.market.market import DataStatus, Fundamentals, MarketBundle, NewsItem
from toolkit.market.market_router import (
    classify_symbol,
    normalize_hk_symbol,
    normalize_us_symbol,
    split_a_symbol,
)


_lock = RLock()
_scope: ContextVar[str] = ContextVar("research_data_scope", default="default")
# ponytail: 进程内最多保留 256 个任务×标的数据包；多实例部署时升级为带 TTL 的共享存储。
_MAX_BUNDLES = 256
_bundles: OrderedDict[tuple[str, str], tuple[int, MarketBundle]] = OrderedDict()


def set_research_data_scope(scope: str) -> Token:
    """为当前异步研究任务隔离数据包，子任务会继承该 scope。"""
    return _scope.set(str(scope or "default"))


def reset_research_data_scope(token: Token) -> None:
    _scope.reset(token)


def canonical_symbol(symbol: str) -> str:
    """把同一证券的常见写法归一为稳定的数据包键。"""
    market = classify_symbol(symbol)
    if market == "A":
        code, exchange = split_a_symbol(symbol)
        return f"{code}.{exchange}"
    if market == "HK":
        return normalize_hk_symbol(symbol)
    return normalize_us_symbol(symbol)


def _merge_record(old, new):
    if old is None:
        return new
    if new is None:
        return old
    values = {
        item.name: (
            getattr(new, item.name)
            if getattr(new, item.name) is not None
            else getattr(old, item.name)
        )
        for item in fields(old)
    }
    return replace(old, **values)


def _merge_fundamentals(old: Fundamentals | None, new: Fundamentals | None) -> Fundamentals | None:
    if old is None:
        return new
    if new is None:
        return old
    by_period = {str(row.get("year", "")): dict(row) for row in old.years}
    for row in new.years:
        period = str(row.get("year", ""))
        current = by_period.setdefault(period, {})
        current.update({key: value for key, value in row.items() if value is not None})
    return Fundamentals(
        symbol=new.symbol or old.symbol,
        source=new.source or old.source,
        years=[by_period[key] for key in sorted(by_period, reverse=True)],
        asof=new.asof or old.asof,
    )


def _merge_news(old: list[NewsItem], new: list[NewsItem]) -> list[NewsItem]:
    merged: list[NewsItem] = []
    seen: set[tuple[str, str]] = set()
    for item in [*new, *old]:
        key = (item.url or "", item.title)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def _merge_bundle(old: MarketBundle, new: MarketBundle) -> MarketBundle:
    snapshot = _merge_record(old.snapshot, new.snapshot)
    fundamentals = _merge_fundamentals(old.fundamentals, new.fundamentals)
    if snapshot is not None and fundamentals is not None:
        status = DataStatus.OK
    elif snapshot is not None or fundamentals is not None:
        status = DataStatus.PARTIAL
    else:
        status = DataStatus.DEGRADED
    evidence = {**old.field_evidence, **new.field_evidence}
    missing = sorted((set(old.missing_fields) | set(new.missing_fields)) - set(evidence))
    return MarketBundle(
        symbol=new.symbol or old.symbol,
        status=status,
        snapshot=snapshot,
        fundamentals=fundamentals,
        news=_merge_news(old.news, new.news),
        errors=list(dict.fromkeys([*old.errors, *new.errors])),
        field_evidence=evidence,
        missing_fields=missing,
        fetch_status="ok" if status is DataStatus.OK and not missing else status.value,
        fallback_results=[*old.fallback_results, *new.fallback_results],
    )


def store_research_bundle(bundle: MarketBundle) -> str:
    """写入或增量合并行情数据，返回 ``<symbol>:v<N>``。"""
    key = canonical_symbol(bundle.symbol)
    scoped_key = (_scope.get(), key)
    with _lock:
        version, old = _bundles.get(scoped_key, (0, None))
        merged = _merge_bundle(old, bundle) if old is not None else bundle
        version += 1
        _bundles[scoped_key] = (version, merged)
        _bundles.move_to_end(scoped_key)
        while len(_bundles) > _MAX_BUNDLES:
            _bundles.popitem(last=False)
    return f"{key}:v{version}"


def get_research_bundle(symbol: str) -> tuple[str, MarketBundle] | None:
    """读取同一标的的最新数据包版本。"""
    key = canonical_symbol(symbol)
    with _lock:
        entry = _bundles.get((_scope.get(), key))
        if entry is not None:
            _bundles.move_to_end((_scope.get(), key))
    if entry is None:
        return None
    version, bundle = entry
    return f"{key}:v{version}", bundle


def clear_research_bundles() -> None:
    """测试与会话清理使用；生产取数不应调用。"""
    with _lock:
        _bundles.clear()
