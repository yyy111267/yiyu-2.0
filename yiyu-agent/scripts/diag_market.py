"""诊断 market.bundle 为什么慢/卡：逐步打印每步耗时，整体超时保护。

用法：cd yiyu-agent && python3 scripts/diag_market.py <symbol>
不修改任何业务代码，只用于定位卡点。
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def timed(label, coro, timeout):
    t0 = time.time()
    try:
        r = await asyncio.wait_for(coro, timeout=timeout)
        print(f"[{label}] OK {time.time()-t0:.1f}s")
        return r
    except asyncio.TimeoutError:
        print(f"[{label}] 超时 {timeout}s (取消，继续)")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[{label}] 异常 {time.time()-t0:.1f}s: {type(e).__name__}: {str(e)[:120]}")
        return None


async def main():
    from core.config import settings
    from toolkit.market.market import MarketData
    from toolkit.market.market_router import westock_code

    symbol = sys.argv[1] if len(sys.argv) > 1 else "600519.SH"
    print(f"=== 诊断 {symbol} ===")
    print(f"westock_code → {westock_code(symbol)}")
    print(f"market_timeout_seconds = {settings.market_timeout_seconds}")
    print(f"westock_enabled = {settings.westock_enabled}")

    md = MarketData(settings)

    # 1) 单独测 westock snapshot（3 个子进程）
    from toolkit.market.sources.market_providers import WeStockProvider
    ws = md._westock
    if ws is None:
        print("[westock] 未启用（westock_enabled=False 或 bin 缺失）")
        ws = WeStockProvider(bin_path=settings.westock_bin, timeout=10.0, years=5)

    code = westock_code(symbol)
    await timed("westock.snapshot", ws.snapshot(code), 45)
    await timed("westock.fundamentals", ws.fundamentals(code), 45)

    # 2) 测 bundle 全流程（但只等 90s，超时就放弃）
    print("\n--- 测试 bundle 全流程（上限 90s）---")
    b = await timed("bundle", md.bundle(symbol), 90)
    if b:
        print("bundle status:", b.status)
        if b.fundamentals:
            f = b.fundamentals
            print("source:", f.source, "| years:", len(f.years))
            y = f.years[0]
            print("YEAR0 keys:", sorted(y.keys()))
            for k in ("revenue", "ebit", "contract_liability", "cash",
                      "inventory", "total_debt", "roic", "cogs"):
                print(f"  {k}: {y.get(k)}")
        if b.errors:
            print("errors:", b.errors[:8])
    await md.aclose()
    print("=== 诊断结束 ===")


if __name__ == "__main__":
    asyncio.run(main())
