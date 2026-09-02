from __future__ import annotations

import asyncio
import time

import pytest

from toolkit.market.process_runner import run_sync_in_process


def _add(a: int, b: int) -> int:
    return a + b


def _slow() -> str:
    time.sleep(5)
    return "done"


def test_run_sync_in_process_returns_value() -> None:
    assert asyncio.run(run_sync_in_process(_add, 2, 3, timeout=2)) == 5


def test_run_sync_in_process_times_out() -> None:
    with pytest.raises(TimeoutError):
        asyncio.run(run_sync_in_process(_slow, timeout=0.2))
