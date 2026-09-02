from __future__ import annotations

import asyncio
from types import SimpleNamespace

from runtime.loop import AgentLoop
from toolkit.calc.metric import MetricsTool
from toolkit.executor import ToolCall
from toolkit.market import tools as market_tools
from toolkit.market.market import DataStatus, Fundamentals, MarketBundle, Snapshot
from toolkit.market.tools import MarketBundleTool
from toolkit.market.research_data import (
    clear_research_bundles,
    get_research_bundle,
    reset_research_data_scope,
    set_research_data_scope,
    store_research_bundle,
)


def _bundle(*, price=None, years=None) -> MarketBundle:
    return MarketBundle(
        symbol="600519",
        status=DataStatus.OK,
        snapshot=Snapshot(symbol="600519", source="test", price=price),
        fundamentals=Fundamentals(
            symbol="600519", source="test", years=years or [],
        ),
    )


def test_research_bundle_is_versioned_and_incrementally_merged() -> None:
    clear_research_bundles()
    first = store_research_bundle(_bundle(price=100.0, years=[{"year": "2025", "revenue": 10.0}]))
    second = store_research_bundle(_bundle(years=[{"year": "2025", "net_profit": 4.0}]))

    pack_id, bundle = get_research_bundle("600519.SH") or (None, None)

    assert first == "600519.SH:v1"
    assert second == pack_id == "600519.SH:v2"
    assert bundle.snapshot.price == 100.0
    assert bundle.fundamentals.years == [{"year": "2025", "revenue": 10.0, "net_profit": 4.0}]


def test_research_bundle_is_isolated_per_task_scope() -> None:
    clear_research_bundles()
    first_scope = set_research_data_scope("task-1")
    try:
        store_research_bundle(_bundle(price=100.0))
    finally:
        reset_research_data_scope(first_scope)

    second_scope = set_research_data_scope("task-2")
    try:
        assert get_research_bundle("600519") is None
    finally:
        reset_research_data_scope(second_scope)


def test_market_fetches_once_and_calc_reuses_the_same_bundle(monkeypatch) -> None:
    clear_research_bundles()
    calls: list[str] = []
    bundle = _bundle(
        price=100.0,
        years=[{"year": "2025", "revenue": 10.0, "net_profit": 2.0}],
    )

    class MarketData:
        async def bundle(self, symbol, **kwargs):
            calls.append(symbol)
            return bundle

    monkeypatch.setattr(market_tools, "_get_market_data", lambda: MarketData())

    market_result = asyncio.run(MarketBundleTool().execute("600519"))
    calc_result = asyncio.run(MetricsTool().execute("600519.SH", ["pe_ttm"]))

    assert calls == ["600519"]
    assert calc_result["data_pack_id"] == market_result["data_pack_id"]


def test_loop_starts_unrelated_work_in_parallel_but_calc_waits_for_market() -> None:
    order: list[str] = []
    market_done = asyncio.Event()

    class Executor:
        async def execute(self, call):
            order.append(f"start:{call.name}")
            if call.name == "market.get_bundle":
                await asyncio.sleep(0.01)
                market_done.set()
                order.append("done:market.get_bundle")
            elif call.name == "calc.metrics":
                assert market_done.is_set()
            return SimpleNamespace(success=True, data={"tool": call.name}, error=None)

        async def execute_batch(self, calls):
            return [await self.execute(call) for call in calls]

    calls = [
        ToolCall("market.get_bundle", {"symbol": "600519"}, "market"),
        ToolCall("calc.metrics", {"symbol": "600519", "metric_ids": ["pe_ttm"]}, "calc"),
        ToolCall("cognition.recall", {"query": "茅台"}, "memory"),
    ]
    loop = SimpleNamespace(executor=Executor())

    results = asyncio.run(AgentLoop._execute_data_batch(loop, calls))

    assert [result.data["tool"] for result in results] == [call.name for call in calls]
    assert order.index("start:cognition.recall") < order.index("done:market.get_bundle")
    assert order.index("done:market.get_bundle") < order.index("start:calc.metrics")
