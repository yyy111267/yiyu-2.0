"""base_pack 离线冒烟验证：用真实茅台财务数据喂冻结函数，验证"冻结公式→档位→prompt block"链路。

用法：cd yiyu-agent && python3 scripts/smoke_base_pack.py
不联网、不调 LLM，只验证 _compute_pack + _render_prompt_block。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toolkit.calc.base_pack import _compute_pack, _render_prompt_block  # noqa: E402

# ── 茅台 2023/2024 真实财务数据（单位：亿元，序列按时间升序）────
# 模拟未来数据源扩展后 market.bundle 能提供的完整字段
FIELDS = {
    # 单值（2024 最新）
    "ebit": 1200.0,
    "total_debt": 0.0,
    "total_equity": 2600.0,
    "cash": 1000.0,
    "total_assets": 2900.0,
    "current_liabilities": 600.0,
    "gross_margin": 91.4,
    "net_profit": 862.3,
    "capital_expenditure": 57.0,
    "ebitda": 1250.0,
    "weighted_average_shares_diluted": 12.56,
    "stock_based_compensation": 0.0,
    "tax_rate": 0.25,
    "depreciation_amortization": 80.0,
    "working_capital_change": -50.0,
    "cogs": 150.0,
    "accounts_receivable": 1.5,
    "accounts_payable": 40.0,
    "selling_expense": 60.0,
    "current_price": 1500.0,             # 2024 年末股价（假设）
    "discount_rate": 0.08,               # WACC（LLM 假设）
    "normalized_earnings_ps": 68.7,      # 正常化每股盈利（LLM 提供）
    "pe": 32.0,                          # 当前 PE
    # 序列（2023 → 2024）
    "revenue": [1505.6, 1741.4],
    "sales_volume": [4.2, 4.6],          # 万吨
    "contract_liability": [141.3, 62.2],  # 2024 主动去库，合同负债大幅下降
    "inventory": [470.0, 500.0],
    "operating_cash_flow": [665.9, 924.7],
    "weighted_average_shares_diluted__series": [12.56, 12.56],
    "pe__series": [30.0, 32.0],
}

CLASSIFICATION = {
    "symbol": "600519.SH", "group": "G1a", "stage": "profitable",
    "confidence": 0.95, "by": "llm", "needs_review": False,
    "is_conglomerate": False, "sotp_tier": 0,
    "reasoning": "主营白酒，靠品牌收溢价，单一主业",
}


def main() -> None:
    result = _compute_pack("600519.SH", "G1a", CLASSIFICATION, FIELDS)
    print("=" * 70)
    print(f"符号: {result['symbol']} | group: {result['group']}")
    print(f"缺字段: {result['missing_fields'] or '无'}")
    print("=" * 70)
    print(_render_prompt_block(result))


if __name__ == "__main__":
    main()
