"""环节级评测公共层 —— 加载用例 → 执行 → 断言 → 汇总。

设计约定（详见 README.md）：
1. cases.yaml 是稳定验收标准（按新 PRD 目标行为写），不随实现摇摆；
2. run.py 的 execute() 调用环节真实实现；现状与 PRD 的字段差异在
   execute 内做临时映射（标注 TODO），该环节改造完成后删除映射；
3. 断言只做确定性比对；LLM 随机输出断言结构化字段，不断言自由文本全文；
4. execute 抛 NotImplementedError → 该用例 SKIP（环节待建，评测集已就位）；
5. execute 抛其他异常 → ERROR（与 FAIL 区分：环境问题 vs 行为错误）。

用法（各环节 run.py 末尾）：
    asyncio.run(run_stage(__file__, execute))

可选参数：--skip-live 跳过 live: true 的用例。
退出码：0 全过 / 1 有失败或错误 / 2 全跳过。
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

_MISSING = object()


# ── 用例加载 ─────────────────────────────────────────────

def load_cases(cases_file: Path) -> dict:
    """加载并校验 cases.yaml 的顶层结构。"""
    if not cases_file.exists():
        sys.exit(f"用例文件不存在: {cases_file}")
    data = yaml.safe_load(cases_file.read_text(encoding="utf-8")) or {}
    for key in ("stage", "name", "target", "status", "cases"):
        if key not in data:
            sys.exit(f"{cases_file} 缺少顶层字段: {key}")
    if not isinstance(data["cases"], list) or not data["cases"]:
        sys.exit(f"{cases_file} 的 cases 必须是非空列表")
    for i, case in enumerate(data["cases"]):
        for key in ("id", "name", "input", "expect"):
            if key not in case:
                sys.exit(f"{cases_file} 第 {i + 1} 条用例缺少字段: {key}")
        if not ((case.get("expect") or {}).get("assertions")):
            sys.exit(f"{cases_file} 用例 {case['id']} 的 expect.assertions 为空")
    return data


# ── 断言引擎 ─────────────────────────────────────────────

def get_path(data: Any, path: str) -> Any:
    """点号路径取值，支持数组下标：'entity.symbol'、'value.1.1'。

    路径不存在返回 _MISSING（与显式 None 区分）。
    """
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return _MISSING
        elif isinstance(cur, dict):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _fmt(v: Any) -> str:
    if v is _MISSING:
        return "<路径不存在>"
    if isinstance(v, str) and len(v) > 60:
        return repr(v[:60] + "…")
    return repr(v)


def check_assertion(result: Any, assertion: dict) -> tuple[bool, str]:
    """执行单条断言，返回 (是否通过, 失败说明)。"""
    path = assertion.get("path", "")
    op = assertion.get("op", "eq")
    expected = assertion.get("value")
    actual = get_path(result, path) if path else result
    ok = False
    where = f"path '{path or '<根>'}'"

    try:
        if op == "eq":
            ok = actual == expected
        elif op == "ne":
            ok = actual != expected
        elif op == "in":
            ok = isinstance(expected, (list, tuple, set)) and actual in expected
        elif op == "not_in":
            ok = isinstance(expected, (list, tuple, set)) and actual not in expected
        elif op == "contains":
            ok = expected in actual if actual is not _MISSING else False
        elif op == "not_contains":
            ok = expected not in actual if actual is not _MISSING else False
        elif op in ("gt", "ge", "lt", "le"):
            if actual is _MISSING or actual is None:
                ok = False
            else:
                ok = {"gt": actual > expected, "ge": actual >= expected,
                      "lt": actual < expected, "le": actual <= expected}[op]
        elif op == "approx":
            tol = assertion.get("tol", 0.01)
            ok = actual is not _MISSING and actual is not None \
                and abs(float(actual) - float(expected)) <= tol
        elif op == "regex":
            ok = actual is not _MISSING and bool(re.search(expected, str(actual)))
        elif op == "exists":
            ok = actual is not _MISSING and actual is not None
        elif op == "not_exists":
            ok = actual is _MISSING or actual is None
        elif op == "not_empty":
            ok = actual is not _MISSING and actual is not None \
                and len(actual) > 0
        elif op == "len_ge":
            ok = actual is not _MISSING and actual is not None \
                and len(actual) >= expected
        elif op == "len_le":
            ok = actual is not _MISSING and actual is not None \
                and len(actual) <= expected
        else:
            return False, f"{where} 未知 op: {op!r}"
    except TypeError:
        ok = False

    if op == "approx":
        detail = f"{where} 期望 approx {expected!r}（tol={assertion.get('tol', 0.01)}），实际 {_fmt(actual)}"
    else:
        detail = f"{where} 期望 {op} {_fmt(expected)}，实际 {_fmt(actual)}"
    return ok, detail


# ── 运行与汇总 ───────────────────────────────────────────

async def run_stage(run_py_path: str | Path, execute: Callable[[dict], Any]) -> None:
    """加载同目录 cases.yaml，逐条执行 + 断言，stdout 输出报告并设置退出码。"""
    run_py = Path(run_py_path).resolve()
    meta = load_cases(run_py.parent / "cases.yaml")
    cases = meta["cases"]
    skip_live = "--skip-live" in sys.argv

    print(f"━━━ 环节评测：{meta['stage']} · {meta['name']} ━━━")
    live_n = sum(1 for c in cases if c.get("live"))
    print(f"用例 {len(cases)} 条（live {live_n} / offline {len(cases) - live_n}）"
          f"｜被测: {meta['target']}")
    if meta.get("status") == "pending":
        print("⚠️ 环节状态: pending —— 目标实现待建，评测集先就位（用例即接口契约）")
    if skip_live:
        print("模式: --skip-live（跳过 live 用例）")
    print()

    passed: list[str] = []
    failed: list[str] = []
    errors: list[str] = []
    skipped: list[str] = []

    for case in cases:
        cid, name = case["id"], case.get("name", "")
        tag = " [live]" if case.get("live") else ""

        if skip_live and case.get("live"):
            print(f"  ⏭️  {cid} {name}{tag}（--skip-live）")
            skipped.append(cid)
            continue

        try:
            result = execute(case.get("input", {}))
            if inspect.isawaitable(result):
                result = await result
        except NotImplementedError as e:
            print(f"  ⏭️  {cid} {name}{tag}（环节待建: {e}）")
            skipped.append(cid)
            continue
        except Exception as e:  # noqa: BLE001 - 环境问题与行为错误分开计
            print(f"  ⚠️  {cid} {name}{tag} ERROR: {type(e).__name__}: {e}")
            errors.append(cid)
            continue

        if result is None:
            print(f"  ⚠️  {cid} {name}{tag} ERROR: 环节无输出（execute 返回 None）")
            errors.append(cid)
            continue

        assertions = (case.get("expect") or {}).get("assertions", [])
        fails = []
        for i, a in enumerate(assertions, 1):
            ok, msg = check_assertion(result, a)
            if not ok:
                fails.append(f"[断言{i}] {msg}")

        if fails:
            print(f"  ❌ {cid} {name}{tag}")
            for f in fails:
                print(f"       · {f}")
            failed.append(cid)
        else:
            print(f"  ✅ {cid} {name}{tag}")
            passed.append(cid)

    # ── 汇总 ──
    judged = len(passed) + len(failed) + len(errors)
    print()
    print("━━━ 汇总 ━━━")
    rate = f"（通过率 {len(passed) / judged:.0%}）" if judged else ""
    print(f"✅ 通过 {len(passed)}   ❌ 失败 {len(failed)}   "
          f"⚠️ 错误 {len(errors)}   ⏭️ 跳过 {len(skipped)}   {rate}")
    if failed:
        print(f"失败: {', '.join(failed)}")
    if errors:
        print(f"错误: {', '.join(errors)}")
    if not failed and not errors and skipped == len(cases):
        print("（全部跳过：环节待建或 --skip-live）")

    sys.exit(0 if not failed and not errors and judged else (2 if not judged else 1))
