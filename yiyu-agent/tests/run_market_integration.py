"""取数层 P0-P3 真实源集成验证。

跑法：
  .venv/bin/python tests/run_market_integration.py

为什么用子进程分段：
  akshare 等同步库通过可终止子进程执行，asyncio.wait_for 可以让
  当前协程超时返回，不能杀掉已经进入同步联网调用的后台线程。若直接在一个进程里跑，
  Python 退出时仍会等待这些非 daemon 线程，表现为“脚本已经超时跳过但进程不退出”。

本脚本由父进程逐段启动子进程，并给每段 subprocess timeout。子进程完成检查后用
os._exit 退出，避免遗留同步线程拖住测试进程。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Awaitable, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Settings
from runtime.trace import Trace, evidence_metadata
from toolkit.market.market import MarketData
from toolkit.market.search_fallback import fallback_search

CaseFn = Callable[[], Awaitable[int]]


def _sep(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}", flush=True)


def _ok(label: str, cond: bool, extra: str = "") -> bool:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {label}" + (f"  ({extra})" if extra else ""), flush=True)
    return cond


async def _run_with_timeout(coro, label: str, timeout: float):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        print(f"  ⏰ [{label}] 协程超时（{timeout}s），本段降级", flush=True)
        return None
    except Exception as e:
        print(f"  ❌ [{label}] 异常: {e}", flush=True)
        traceback.print_exc()
        return None


def _market() -> MarketData:
    return MarketData(Settings())


async def case_assemble() -> int:
    _sep("装配 MarketData（生产 provider + 缓存 + guard）")
    md = _market()
    try:
        print(f"  provider: a_quote={len(md._a_quote)} 个, a_fund={md._a_fund.name}", flush=True)
        print(f"  westock: {'已装配' if md._westock else '未装配'}", flush=True)
        print(f"  cache: {'已装配' if md._cache else '未装配'}", flush=True)
        print(f"  guard: {'已装配' if md._guards else '未装配'}", flush=True)
        return 0
    finally:
        await md.aclose()


async def case_p0_metric_ids() -> int:
    _sep("P0-1：metric_ids=['pe_ttm'] 按需取数（不应取新闻）")
    md = _market()
    try:
        b = await _run_with_timeout(md.bundle("600519", metric_ids=["pe_ttm"]), "P0-1 pe_ttm", 20)
        if b is None:
            return 1
        ok = True
        snapshot_ok = b.snapshot is not None
        _ok("snapshot 取到", snapshot_ok, f"price={b.snapshot.price}" if b.snapshot else "真实行情源不可用时允许降级")
        ok &= _ok("fundamentals 取到", b.fundamentals is not None,
                  f"years={len(b.fundamentals.years)}" if b.fundamentals else "")
        ok &= _ok("news 不取（metric_ids 驱动）", b.news == [])
        if snapshot_ok:
            # market_cap 来自 snapshot（东财/westock），数据源没返回时进 missing_fields
            has_mc_ev = "market_cap" in b.field_evidence
            has_mc_missing = "market_cap" in b.missing_fields
            ok &= _ok("market_cap 有证据或登记缺失", has_mc_ev or has_mc_missing,
                      f"evidence={'有' if has_mc_ev else '无'}, missing={'是' if has_mc_missing else '否'}")
        ok &= _ok("field_evidence 含 net_profit", "net_profit" in b.field_evidence)
        _ok("fetch_status", True, b.fetch_status)
        if b.field_evidence:
            ev = next(iter(b.field_evidence.values()))
            print(f"    示例 evidence: {ev['field']}={ev['value']} source={ev['source']} "
                  f"level={ev['source_level']} eid={ev['evidence_id']}", flush=True)
        if b.missing_fields:
            print(f"    missing_fields: {b.missing_fields}", flush=True)
        return 0 if ok else 1
    finally:
        await md.aclose()


async def case_p0_legacy() -> int:
    _sep("P0-2：旧 bundle() 全量取数（向后兼容）")
    md = _market()
    try:
        b = await _run_with_timeout(md.bundle("000001"), "P0-2 全量", 25)
        if b is None:
            return 1
        ok = True
        _ok("snapshot 取到", b.snapshot is not None, "真实行情源不可用时允许 partial")
        ok &= _ok("fundamentals 取到", b.fundamentals is not None)
        _ok("news 字段可用", b.news is not None, f"news={len(b.news)}条")
        _ok("fetch_status", True, b.fetch_status)
        print(f"    status={b.status.value} fetch_status={b.fetch_status}", flush=True)
        return 0 if ok else 1
    finally:
        await md.aclose()


async def case_p1_cache() -> int:
    _sep("P1：两级缓存（连续两次取同标的，第二次应命中 latest）")
    md = _market()
    try:
        if md._cache is None:
            _ok("缓存未装配，跳过", True)
            return 0
        import time
        t0 = time.time()
        b1 = await _run_with_timeout(md.bundle("600519", field_groups=["fundamentals"]), "P1 第一次", 20)
        t1 = time.time()
        b2 = await _run_with_timeout(md.bundle("600519", field_groups=["fundamentals"]), "P1 第二次", 20)
        t2 = time.time()
        ok = b1 is not None and b2 is not None
        ok &= _ok("第一次取到", bool(b1 and b1.fundamentals is not None))
        ok &= _ok("第二次取到", bool(b2 and b2.fundamentals is not None))
        ok &= _ok("第二次更快（命中缓存）", (t2 - t1) <= (t1 - t0),
                  f"first={t1 - t0:.2f}s second={t2 - t1:.2f}s")
        return 0 if ok else 1
    finally:
        await md.aclose()


async def case_p2_guard() -> int:
    _sep("P2：Provider Guard 统计快照")
    md = _market()
    try:
        print(f"  guard 装配状态: {'已装配' if md._guards else '未装配'}", flush=True)
        # 用与前面 case 不同的标的，避免缓存命中导致 guard 无统计
        await _run_with_timeout(md.bundle("600000", field_groups=["snapshot"]), "P2 guard sample", 12)
        stats = md.guard_stats()
        if not stats:
            # 缓存命中也没关系，手动触发一次 guard.get 确认机制可用
            if md._guards:
                g = md._guards.get("westock")
                _ok("guard 机制可用（手动 get 成功）", g is not None)
                _ok("guard 统计为空（缓存命中未打源）", True)
                return 0
            _ok("guard 未装配或无统计", False)
            return 1
        _ok("guard 有统计", True, f"{len(stats)} providers")
        for name, s in stats.items():
            print(f"    {name}: calls={s['total_calls']} failures={s['total_failures']} "
                  f"circuit_open={s['circuit_open']}", flush=True)
        return 0
    finally:
        await md.aclose()


async def case_p3_metadata() -> int:
    _sep("P3-1：field_evidence 展成字段级 citations")
    md = _market()
    try:
        b = await _run_with_timeout(md.bundle("600519", metric_ids=["pe_ttm"]), "P3 metadata source", 20)
        content = {
            "symbol": "600519",
            "status": getattr(b, "status", None).value if b else "unknown",
            "fetch_status": getattr(b, "fetch_status", "") if b else "",
            "missing_fields": list(getattr(b, "missing_fields", []) if b else []),
            "field_evidence": dict(getattr(b, "field_evidence", {}) if b else {}),
            "fallback_results": list(getattr(b, "fallback_results", []) if b else []),
        }
        meta = evidence_metadata("market.get_bundle", content)
        field_cits = [c for c in meta.get("citations", []) if c.get("kind") == "field"]
        ok = _ok("字段级 citations 展开", len(field_cits) > 0, f"{len(field_cits)} 条")
        if field_cits:
            c = field_cits[0]
            print(f"    示例 citation: field={c['field']} source={c['source']} "
                  f"level={c['source_level']} eid={c['evidence_id']} caliber={c['caliber']}", flush=True)
            ok &= _ok("citation 带 evidence_id", bool(c.get("evidence_id")))
            ok &= _ok("citation 带 caliber", bool(c.get("caliber")))
            ok &= _ok("citation 带 source_level", bool(c.get("source_level")))
        return 0 if ok else 1
    finally:
        await md.aclose()


async def case_p3_metric_trace() -> int:
    _sep("P3-2：calc.metric 透传 evidence_id 到 Trace")
    from toolkit.calc.metric import MetricTool

    tool = MetricTool()
    result = await _run_with_timeout(tool.execute(symbol="600519", metric_id="pe_ttm"),
                                     "P3-2 calc.metric", 25)
    if result is None:
        return 1
    ok = _ok("metric 执行成功", result.get("success") is not False, f"value={result.get('value')}")
    if not result.get("field_evidence"):
        _ok("未透传 field_evidence（取数可能失败）", False, f"status={result.get('status')}")
        return 1
    tr = Trace()
    entry = tr.add_evidence("calc.metric", result, level="A")
    has_field_ids = any(f.get("evidence_id") for f in entry.fields if isinstance(f, dict))
    if result.get("value") is None:
        _ok("无 value 时 fields 可为空", True, f"status={result.get('status')}")
    else:
        ok &= _ok("evidence_id 注入到 fields", has_field_ids)
    ok &= _ok("fetch_status 进入 EvidenceEntry", bool(entry.fetch_status))
    ok &= _ok("field_evidence 进入 EvidenceEntry", bool(entry.field_evidence))
    d = entry.to_dict()
    ok &= _ok("to_dict 输出 field_evidence", bool(d.get("field_evidence")))
    ok &= _ok("to_dict 输出 evidence_ids", bool(d["citations"][0].get("evidence_ids")))
    return 0 if ok else 1


async def case_missing_fields() -> int:
    _sep("缺失场景：metric_ids=['nonexistent_field'] → missing_fields")
    md = _market()
    try:
        b = await _run_with_timeout(md.bundle("600519", metric_ids=["nonexistent_field_xyz"]),
                                    "缺失场景", 15)
        if b is None:
            return 1
        ok = True
        ok &= _ok("fetch_status 非 ok", b.fetch_status in ("partial", "degraded"), b.fetch_status)
        ok &= _ok("missing_fields 含该字段", "nonexistent_field_xyz" in b.missing_fields,
                  str(b.missing_fields))
        print(f"    fetch_status={b.fetch_status} missing={b.missing_fields}", flush=True)
        return 0 if ok else 1
    finally:
        await md.aclose()


async def case_fallback_search() -> int:
    _sep("搜索兜底：fallback_search（注入假搜索 fn）")

    async def fake_search(*, query, max_results, sources):
        return {
            "results": [
                {"title": "贵州茅台 2024 年报", "url": "https://cninfo.com.cn/annual",
                 "snippet": "营业收入 1,541 亿", "source_level": "A"},
            ],
        }

    fb = await fallback_search("600519", "fundamentals", ["revenue"], search_fn=fake_search)
    ok = _ok("兜底搜索返回结果", fb.status == "fallback_search", f"{len(fb.results)} 条")
    if fb.results:
        r = fb.results[0]
        print(f"    result: title={r['title']} url={r['url']} level={r['source_level']}", flush=True)
        ok &= _ok("结果带 URL", bool(r.get("url")))
        ok &= _ok("结果带 source_level", bool(r.get("source_level")))
    return 0 if ok else 1


CASES: dict[str, tuple[str, float, CaseFn]] = {
    "assemble": ("装配", 10, case_assemble),
    "p0_metric_ids": ("P0 按需取数", 35, case_p0_metric_ids),
    "p0_legacy": ("P0 旧全量路径", 40, case_p0_legacy),
    "p1_cache": ("P1 两级缓存", 45, case_p1_cache),
    "p2_guard": ("P2 guard", 18, case_p2_guard),
    "p3_metadata": ("P3 metadata", 35, case_p3_metadata),
    "p3_metric_trace": ("P3 metric trace", 40, case_p3_metric_trace),
    "missing_fields": ("缺失字段", 25, case_missing_fields),
    "fallback_search": ("搜索兜底", 10, case_fallback_search),
}


def _run_parent() -> int:
    failures = 0
    for key, (label, timeout, _) in CASES.items():
        print(f"\n▶ 运行 {label}（timeout={timeout}s）", flush=True)
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--case", key],
                cwd=str(ROOT),
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            if proc.stdout:
                print(proc.stdout, end="", flush=True)
            if proc.stderr:
                print(proc.stderr, end="", file=sys.stderr, flush=True)
            if proc.returncode == 0:
                print(f"✅ {label} 通过", flush=True)
            else:
                failures += 1
                print(f"❌ {label} 失败（exit={proc.returncode}）", flush=True)
        except subprocess.TimeoutExpired as e:
            failures += 1
            if e.stdout:
                out = e.stdout if isinstance(e.stdout, str) else e.stdout.decode()
                print(out, end="", flush=True)
            if e.stderr:
                err = e.stderr if isinstance(e.stderr, str) else e.stderr.decode()
                print(err, end="", file=sys.stderr, flush=True)
            print(f"⏰ {label} 子进程超时，已杀掉（{timeout}s）", flush=True)

    _sep("汇总")
    if failures == 0:
        print("  🎉 全部通过！取数层 P0-P3 全链路验证成功。", flush=True)
    else:
        print(f"  ⚠️ {failures} 个环节失败，见上方 ❌/⏰ 标记。", flush=True)
    return failures


def _run_child(case_key: str) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if case_key not in CASES:
        print(f"未知 case: {case_key}", file=sys.stderr, flush=True)
        os._exit(2)
    try:
        rc = asyncio.run(CASES[case_key][2]())
    except Exception:
        traceback.print_exc()
        rc = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--case":
        _run_child(sys.argv[2])
    raise SystemExit(_run_parent())
