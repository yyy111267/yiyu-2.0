"""生成 A 股 名称↔代码 预置清单 `toolkit/entity/data/known_a_share.json`。

数据源：新浪行情中心「沪深 A 股」节点（免 token，稳定，含沪深北全部 A 股）。
用途：出厂打包预置清单，配合 resolver.py 的降级链
      SQLite 缓存 → akshare 联网刷新 → 预置 JSON → 空，
      保证「首次使用 + 断网」也能识别绝大部分 A 股。

用法：python3 scripts/build_a_share_preset.py [--out path/to/known_a_share.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

_SINA = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         "Market_Center.getHQNodeData")
_REFERER = "https://vip.stock.finance.sina.com.cn/"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_NUM = 100  # 新浪单页上限


def _clean_name(name: str) -> str:
    """名称归一化：NFKC（全角→半角）+ 去空白（新浪简称含空格/全角字符）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name or "")).strip()


def _build_url(page: int) -> str:
    return (_SINA + f"?page={page}&num={_NUM}&sort=symbol&asc=1"
            f"&node=hs_a&symbol=&_s_r_a=init")


def _fetch_page(page: int) -> list[dict]:
    req = urllib.request.Request(
        _build_url(page),
        headers={"User-Agent": _UA, "Referer": _REFERER},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else []


def fetch_all() -> list[dict]:
    """分页拉取全 A 股 [{code, name}]。返回空说明网络异常。"""
    items: list[dict] = []
    page = 1
    while True:
        data = None
        for attempt in range(3):
            try:
                data = _fetch_page(page)
                break
            except Exception as e:  # noqa: BLE001 - 网络抖动退避重试
                if attempt == 2:
                    print(f"  [warn] 第 {page} 页拉取失败（保留已得 {len(items)} 条）: {e}",
                          file=sys.stderr)
                    return items
                time.sleep(1 + attempt * 1.5)
        if not data:
            break
        for x in data:
            code = str(x.get("code", "")).strip()
            name = _clean_name(str(x.get("name", "")))
            if re.fullmatch(r"\d{6}", code) and name:
                items.append({"code": code, "name": name})
        if len(data) < _NUM:
            break
        page += 1
        time.sleep(0.3)  # 防限流
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 A 股预置清单")
    default_out = Path(__file__).resolve().parents[1] / "toolkit/entity/data/known_a_share.json"
    parser.add_argument("--out", type=str, default=str(default_out))
    args = parser.parse_args()

    out_path = Path(args.out)
    print("拉取沪深北全 A 股列表（新浪）...")
    items = fetch_all()

    if not items:
        print("未拉取到任何 A 股数据，请检查网络后重试。", file=sys.stderr)
        return 1

    # 按 code 去重（防御）后排序
    by_code = {it["code"]: it for it in items}
    items = sorted(by_code.values(), key=lambda x: x["code"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"生成完成：{out_path}（共 {len(items)} 家 A 股）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
