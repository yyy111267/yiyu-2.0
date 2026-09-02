"""Run blocking market provider calls in killable child processes.

Some free-data SDKs such as akshare/yfinance expose blocking APIs. Wrapping them
with asyncio.to_thread makes the coroutine cancellable, but it does not stop the
underlying thread. This helper runs the blocking call in a short-lived process so
timeouts and cancellations can actually tear down the work.
"""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import queue
import traceback
from typing import Any, Callable


def _worker(fn: Callable[..., Any], args: tuple, kwargs: dict, out_q) -> None:
    try:
        out_q.put(("ok", fn(*args, **kwargs)))
    except BaseException as exc:  # noqa: BLE001 - preserve provider failure semantics
        out_q.put(("err", type(exc).__name__, str(exc), traceback.format_exc()))


async def run_sync_in_process(
    fn: Callable[..., Any],
    *args: Any,
    timeout: float | None = None,
    **kwargs: Any,
) -> Any:
    """Run a blocking function in a child process and return its result.

    On timeout or coroutine cancellation the child process is terminated. The
    function must be picklable under multiprocessing spawn semantics.
    """
    ctx = mp.get_context("spawn")
    out_q = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_worker, args=(fn, args, kwargs, out_q), daemon=True)
    proc.start()
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    try:
        while proc.is_alive():
            if deadline is not None and loop.time() >= deadline:
                _stop_process(proc)
                raise TimeoutError(f"process call timed out after {timeout}s")
            await asyncio.sleep(0.05)
        proc.join(timeout=0)
        try:
            msg = out_q.get_nowait()
        except queue.Empty:
            if proc.exitcode == 0:
                return None
            raise RuntimeError(f"process exited without result (exit={proc.exitcode})")
        if msg[0] == "ok":
            return msg[1]
        _, exc_type, exc_msg, _tb = msg
        raise RuntimeError(f"{exc_type}: {exc_msg}")
    except asyncio.CancelledError:
        _stop_process(proc)
        raise
    finally:
        try:
            out_q.close()
        except Exception:  # noqa: BLE001
            pass


def _stop_process(proc) -> None:
    if not proc.is_alive():
        return
    proc.terminate()
    proc.join(timeout=0.5)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=0.5)
