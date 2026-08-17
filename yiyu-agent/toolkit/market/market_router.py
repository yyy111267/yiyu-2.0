"""标的符号路由与规范化：决定一个 symbol 走哪条数据源链路。

市场分工（Phase 0 拍板）：
- A股  → 东财/新浪实时行情 + akshare 财报新闻 + 巨潮公告
- 港股 → 东财实时行情优先（yfinance 兜底），代码规范化为 00700.HK 形式（统一 5 位）
- 美股 → 东财实时行情优先（yfinance 兜底），字母 ticker 原样
"""

from typing import Literal

Market = Literal["A", "HK", "US"]


def classify_symbol(symbol: str) -> Market:
    """按符号形态判断市场。

    - 600519 / 600519.SH / 000001.SZ / 920001.BJ → A股
    - 0700.HK / 700 / 00700                       → 港股
    - AAPL / BRK.B                                 → 美股
    """
    s = symbol.strip().upper()
    if not s:
        raise ValueError("symbol 不能为空")
    if s.endswith((".SH", ".SS", ".SZ", ".BJ")):
        return "A"
    if s.endswith(".HK"):
        return "HK"
    if s.isdigit():
        if len(s) == 6:
            return "A"
        if len(s) <= 5:  # 港股 1~5 位数字代码
            return "HK"
    return "US"


def split_a_symbol(symbol: str) -> tuple[str, str]:
    """A股符号 → (6 位代码, 交易所 SH/SZ/BJ)。

    无后缀时按号段推断：920/4/8 开头为北交所，60/68/9 开头为上交所，其余深交所。
    """
    s = symbol.strip().upper()
    if "." in s:
        code, ex = s.split(".", 1)
        return code, {"SS": "SH"}.get(ex, ex)
    if s.startswith(("920", "4", "8")):
        return s, "BJ"
    if s.startswith(("60", "68", "9")):
        return s, "SH"
    return s, "SZ"


def normalize_hk_symbol(symbol: str) -> str:
    """港股代码 → 标准格式（700 / 0700.HK / 00700.HK → 00700.HK，统一 5 位）。

    与 westock_code（zfill(5)）、东财 secid（zfill(5)）、known_hk_us.json（5 位）一致，
    避免 9988 vs 09988 产生两个 symbol 导致去重/缓存失效。
    """
    s = symbol.strip().upper()
    if s.endswith(".HK"):
        s = s[:-3]
    return f"{s.zfill(5)}.HK" if s.isdigit() else f"{s}.HK"


def normalize_us_symbol(symbol: str) -> str:
    """美股代码 → 大写原样（BRK.B 等保留）。"""
    return symbol.strip().upper()


def westock_code(symbol: str) -> str:
    """用户 symbol → westock CLI 代码：

    - A股：sh/sz/bj + 6位（600519 → sh600519）
    - 港股：hk + 5位（700 / 0700.HK → hk00700）
    - 美股：us + ticker（AAPL → usAAPL）
    """
    market = classify_symbol(symbol)
    if market == "A":
        code, ex = split_a_symbol(symbol)
        return f"{ex.lower()}{code}"
    if market == "HK":
        return f"hk{symbol.upper().replace('.HK', '').zfill(5)}"
    return f"us{symbol.upper().replace('.US', '').replace('.O', '').replace('.N', '')}"
