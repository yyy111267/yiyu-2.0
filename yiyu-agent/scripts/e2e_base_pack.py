"""端到端 base_pack 验证：真实 westock 数据 → 冻结公式 → 档位。

用法：cd yiyu-agent && python3 scripts/e2e_base_pack.py <symbol>
只测数据链路（westock provider 直连，绕过 bundle 缓存层），不调 LLM。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toolkit.calc.base_pack import _compute_pack, _map_fields, _render_prompt_block  # noqa: E402


async def main() -> None:
    from core.config import settings
    from toolkit.market.market import MarketBundle
    from toolkit.market.market_router import westock_code
    from toolkit.market.sources.market_providers import WeStockProvider

    symbol = sys.argv[1] if len(sys.argv) > 1 else "600519.SH"
    print(f"=== 端到端验证: {symbol} ===")

    ws = WeStockProvider(bin_path=settings.westock_bin, timeout=10.0, years=5)
    code = westock_code(symbol)
    f = await ws.fundamentals(code)
    if not f or not f.years:
        print("取数失败，无法继续")
        return

    print(f"数据源: {f.source} | 期数: {len(f.years)}")
    print(f"最新期字段({len(f.years[0])}个): {sorted(f.years[0].keys())}")
    print()

    # 组装 bundle（只填 fundamentals，base_pack 的 _map_fields 只用它）
    bundle = MarketBundle(symbol=symbol, status=None, fundamentals=f)  # type: ignore[arg-type]
    fields = _map_fields(bundle)

    # 分类结果：用规则快筛（L1 名称表），不调 LLM
    from toolkit.entity.classify import match_name_hints
    group_hint = match_name_hints(f.years[0].get("SecuCode") or symbol)
    if not group_hint:
        # 手工指定已知示例：600519→G1a
        group = "G1a" if symbol.startswith("600519") else "G1a"
    else:
        group = group_hint
    print(f"group: {group}")

    classification = {
        "symbol": symbol, "group": group, "stage": "profitable",
        "confidence": 0.9, "by": "test", "needs_review": False,
        "is_conglomerate": False, "sotp_tier": 0, "reasoning": "test",
    }
    result = _compute_pack(symbol, group, classification, fields)
    print()
    print(_render_prompt_block(result))


if __name__ == "__main__":
    asyncio.run(main())
