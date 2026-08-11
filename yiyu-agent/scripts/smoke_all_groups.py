"""全行业 base_pack 离线验证：对 8 个组各跑一遍，确认配置加载 + 公式调用 + 档位判定。

用法：cd yiyu-agent && python3 scripts/smoke_all_groups.py
不联网、不调 LLM。每个组用模拟完整字段（代表该行业典型公司）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toolkit.calc.base_pack import _compute_pack, _render_prompt_block  # noqa: E402

# ── 每组给一组模拟"完整字段"（模拟未来数据源扩展后能提供的）────
GROUP_FIXTURES: dict[str, tuple[str, dict]] = {
    "G1a": ("贵州茅台 600519.SH", {
        "ebit": 1200.0, "tax_rate": 0.25, "total_debt": 0.0, "total_equity": 2600.0,
        "cash": 1000.0, "total_assets": 2900.0, "current_liabilities": 600.0,
        "gross_margin": 91.4, "net_profit": 862.3, "capital_expenditure": 57.0,
        "ebitda": 1250.0, "weighted_average_shares_diluted": 12.56,
        "stock_based_compensation": 0.0, "depreciation_amortization": 80.0,
        "working_capital_change": -50.0, "cogs": 150.0, "accounts_receivable": 1.5,
        "accounts_payable": 40.0, "selling_expense": 60.0,
        "revenue": [1505.6, 1741.4], "sales_volume": [4.2, 4.6],
        "contract_liability": [141.3, 62.2], "inventory": [470.0, 500.0],
        "operating_cash_flow": [665.9, 924.7], "current_price": 1500.0,
        "discount_rate": 0.08, "normalized_earnings_ps": 68.7, "pe": 32.0,
        "weighted_average_shares_diluted__series": [12.56, 12.56],
    }),
    "G1b": ("长江电力 600900.SH", {
        "ebit": 350.0, "tax_rate": 0.25, "total_debt": 2500.0, "total_equity": 1800.0,
        "cash": 100.0, "ebitda": 520.0, "net_profit": 280.0,
        "operating_cash_flow": 480.0, "capital_expenditure": 180.0,
        "interest_expense": 70.0, "total_dividends_paid": 200.0,
        "maintenance_capex": 120.0, "regulated_asset_base": 5000.0,
        "allowed_roe": 8.0, "actual_output": 1000.0, "capacity": 1100.0,
        "depreciation": 120.0, "dividend_yield": 3.5, "treasury_10y": 2.3,
        "revenue": [600.0, 650.0], "current_price": 25.0,
    }),
    "G2a": ("招商银行 600036.SH", {
        "net_interest_income": 2100.0, "average_interest_earning_assets": 90000.0,
        "interest_earning_assets": [88000.0, 91000.0],
        "non_performing_loans": 600.0, "total_loans": 60000.0,
        "overdue_90d_loans": 500.0, "special_mention_loans": 1200.0,
        "loan_loss_provisions": 2200.0, "loan_impairment_charge": 500.0,
        "average_total_loans": 59000.0, "core_tier1_capital_ratio": 12.0,
        "operating_expenses": 1300.0, "operating_income": 3400.0,
        "net_income": 1400.0, "average_total_assets": 110000.0,
        "average_total_equity": 10000.0, "demand_deposits": 42000.0,
        "total_deposits": 80000.0, "pe": 6.5,
        "pe__series": [7.0, 6.5, 6.0, 6.5, 7.0, 6.8, 6.2, 6.5, 6.0, 6.5],
    }),
    "G2b": ("中国平安 601318.SH", {
        "new_business_value": [350.0, 380.0],
        "first_year_premium_annualized": 1500.0, "embedded_value": 14000.0,
        "embedded_value_opening": 13500.0, "ev_operating_variance_amount": 100.0,
        "contractual_service_margin": 8000.0, "total_investment_yield": 4.5,
        "actuarial_investment_assumption": 4.8, "persistency_rate_13m": 92.0,
        "loss_ratio": 55.0, "expense_ratio": 38.0,
        "claims_incurred": 1500.0, "earned_premium": 2800.0,
        "underwriting_expenses": 1000.0, "net_income": 900.0, "pe": 8.0,
        "pe__series": [9.0, 8.5, 8.0, 8.5, 9.0, 8.8, 8.2, 8.5, 8.0, 8.5],
    }),
    "G3": ("中国神华 601088.SH", {
        "net_profit": [400.0, 500.0, 600.0, 450.0, 380.0, 520.0, 480.0, 460.0, 500.0, 470.0],
        "current_price": 35.0, "normalized_earnings_ps": 2.5,
        "total_debt": 800.0, "cash": 1200.0, "trough_ebitda": 400.0,
        "actual_output": 95.0, "capacity": 100.0,
        "ebit": 600.0, "tax_rate": 0.25, "total_equity": 3500.0,
        "roic__series": [15.0, 16.0, 17.0, 14.0, 13.0, 16.0, 15.5, 15.0, 16.0, 15.5],
    }),
    "G4": ("腾讯控股 0700.HK", {
        "stock_based_compensation": 300.0, "revenue": [6000.0, 6600.0],
        "weighted_average_shares_diluted": [96.0, 95.0],
        "operating_cash_flow": 2600.0, "net_profit": 1800.0,
        "expense_growth": 8.0, "ebit": 2200.0, "tax_rate": 0.2,
        "total_debt": 4000.0, "total_equity": 8000.0, "cash": 3500.0,
        "ebitda": 2600.0, "current_price": 380.0,
        "revenue_growth": 10.0,
    }),
    "G6": ("立讯精密 002475.SZ", {
        "actual_output": 85.0, "capacity": 100.0,
        "revenue": [2300.0, 2600.0], "fixed_assets": 800.0,
        "depreciation_amortization": 120.0, "capital_expenditure": 180.0,
        "ebit": 260.0, "tax_rate": 0.15, "total_debt": 600.0,
        "total_equity": 900.0, "cash": 300.0,
        "operating_cash_flow": 350.0, "net_profit": 180.0,
        "ebitda": 340.0, "interest_expense": 20.0,
    }),
}

# G5 是 overlay，单独验证（特殊结构，这里确认降级路径不崩）
GROUP_FIXTURES["G5"] = ("未盈利 SaaS（模拟）", {
    "revenue": [10.0, 18.0, 30.0], "net_profit": [-8.0, -12.0, -9.0],
    "cash_and_equivalents": 50.0, "monthly_operating_cash_burn": 3.0,
    "total_market_cap": 300.0, "total_shares": [1.0, 1.2, 1.4],
    "operating_cash_flow": -30.0, "total_debt": 0.0, "ebitda": -9.0,
})


def main() -> None:
    total_ok = 0
    total_nc = 0
    failures: list[str] = []
    for group, (name, fields) in GROUP_FIXTURES.items():
        print("=" * 70)
        print(f"[{group}] {name}")
        try:
            result = _compute_pack(f"TEST-{group}", group, {
                "symbol": f"TEST-{group}", "group": group, "stage": "profitable",
                "confidence": 0.9, "by": "test", "needs_review": False,
                "is_conglomerate": False, "sotp_tier": 0, "reasoning": "test",
            }, fields)
            ok = sum(1 for m in result["metrics"] if m["status"] == "OK")
            nc = sum(1 for m in result["metrics"] if m["status"] != "OK")
            total_ok += ok
            total_nc += nc
            print(f"  已算 {ok} 项 / 缺失 {nc} 项")
            # 只打印 OK 的指标名+档位，NC 的打印原因前 40 字
            for m in result["metrics"]:
                if m["status"] == "OK":
                    print(f"    ✓ {m['name']}: {m['value']} [{m['band'] or '无档位'}]")
                else:
                    reason = (m["reason"] or "")[:40]
                    print(f"    ✗ {m['name']}: {reason}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"{group}: {e}")
            print(f"  ❌ 执行异常: {e}")
    print("=" * 70)
    print(f"汇总: OK {total_ok} 项 / NC {total_nc} 项 / 异常组: {failures or '无'}")


if __name__ == "__main__":
    main()
