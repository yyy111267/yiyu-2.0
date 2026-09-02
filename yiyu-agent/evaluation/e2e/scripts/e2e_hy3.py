"""真模型 e2e 验证（带超时 + 实时进度），拆两条独立路径。

避免 preloop 真模型慢拖累 loop 验证，分两条跑：

  MODE=loop      preloop 用 mock 四件套（秒级，不调真 LLM）+ loop 用真 LLM + mock 行情
                 → 专验"loop 取数工具链是否接通"：真 LLM 是否调 market_get_bundle、
                   是否推进 plan、收敛闸门是否生效、lint 是否全过。
                 （默认模式）

  MODE=preloop   preloop 全环节用真 LLM（验证结构化输出质量，可能慢）
                 → 专验 facts_builder/granularity/profiler 在真模型下的输出合规性。

  USE_REAL_MARKET=1   行情/搜索走真实联网（更慢、可能限流），默认 mock。

用法：
  E2E_MODE=loop    .venv/bin/python evaluation/e2e/scripts/e2e_hy3.py
  E2E_MODE=preloop .venv/bin/python evaluation/e2e/scripts/e2e_hy3.py
"""
import asyncio
import importlib.util as _ilu
import os
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, ".")

from core.config import settings
from core.llm import LLMClient
from runtime.loop import AgentLoop, LoopConfig
from runtime.assembler import PromptAssembler
from runtime.loop_lint import lint_trace
from toolkit.executor import ToolExecutor
from toolkit.market.tools import MarketBundleTool
from toolkit.entity.resolver import Entity
from toolkit.entity.tools import EntityResolveTool
from toolkit.registry import TOOL_REGISTRY
from toolkit import register_all

E2E_MODE = os.environ.get("E2E_MODE", "loop")  # "loop" | "preloop"
USE_REAL_MARKET = os.environ.get("USE_REAL_MARKET") == "1"
# 内部研究预算（AgentLoop.timeout_seconds）：到点由内部产出降级报告。
OVERALL_TIMEOUT = int(os.environ.get("E2E_OVERALL_TIMEOUT", "120"))
LLM_TIMEOUT = float(os.environ.get("E2E_LLM_TIMEOUT", "30"))
# 收尾综合轮的单次模型时限：不配置则沿用 LLM_TIMEOUT（由 LoopConfig 决定）。
# 需要给正文生成更长窗口时显式设置，例如 E2E_FINALIZE_TIMEOUT=45。
FINALIZE_TIMEOUT = (
    float(os.environ["E2E_FINALIZE_TIMEOUT"])
    if os.environ.get("E2E_FINALIZE_TIMEOUT")
    else None
)
# 外层 watchdog：必须大于内部预算，否则内部来不及输出降级报告就被外层
# 取消，表现为「用户已取消」而看不到终态。余量需覆盖收尾轮最长窗口。
WATCHDOG_MARGIN = float(os.environ.get("E2E_WATCHDOG_MARGIN", "30"))
# 日志不截断（默认 0 = 完整输出）；设为正整数则按字符数截断。
LOG_TRUNCATE = int(os.environ.get("E2E_LOG_TRUNCATE", "0"))


def _clip(text, limit: int | None = None) -> str:
    """按 LOG_TRUNCATE 裁剪日志片段；0 表示不截断。"""
    s = str(text).replace("\n", " ")
    n = LOG_TRUNCATE or 0
    if n > 0 and len(s) > n:
        return s[:n] + f"…(共{len(s)}字)"
    return s


class MockMarketBundleTool(MarketBundleTool):
    """mock 行情回放：固定返回茅台数据包，不触真实行情源。"""

    async def execute(self, symbol: str = "", **kwargs):
        return {
            "symbol": symbol or "600519.SH",
            "snapshot": {"last_price": 1680.0, "pct_change": 1.2, "turnover": 3.1e9},
            "fundamentals": {
                "revenue_ttm": 1.5e11, "net_profit_ttm": 7.5e10,
                "gross_margin": 0.91, "revenue_growth": 0.16, "roe": 0.34,
            },
            "news": [{"title": "茅台批价企稳", "url": "https://e.com/m1", "summary": "渠道反馈"}],
        }


class MockEntityResolveTool(EntityResolveTool):
    """离线 e2e 固定实体；USE_REAL_MARKET=0 时禁止快照验证偷跑真实网络。"""

    async def execute(self, query: str, **kwargs):
        return {
            "resolved": True,
            "needs_disambiguation": False,
            "entity": {
                "symbol": "600519.SH", "name": "贵州茅台", "market": "A",
                "currency": "CNY", "source": "e2e_mock", "confidence": 1.0,
                "alias_type": "fullname", "scope_note": "",
                "entity_source": "explicit", "resolved_at": "",
            },
            "candidates": [], "raw_input": query,
            "message": "e2e mock 已锁定贵州茅台",
        }


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def _load_preloop_mocks():
    mock_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mock_tools"))
    sys.path.insert(0, mock_dir)
    spec = _ilu.spec_from_file_location("preloop_mocks", os.path.join(mock_dir, "preloop_mocks.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _build_mock_preloop_inputs():
    mod = _load_preloop_mocks()
    md = mod._make_market_data([{"year": 2025, "revenue": 1.5e11, "net_profit": 7.5e10}])
    web = mod._make_web_search([{"title": "茅台", "url": "https://e.com"}])
    return md, web


async def _build_real_preloop_inputs():
    from toolkit.market.market import MarketData
    from toolkit.web.tools import WebSearchTool
    return MarketData(settings), WebSearchTool().execute


async def _run_loop_with_preloop(llm, r):
    """loop 阶段：真 hy3 + 真实 ToolExecutor（行情默认 mock 替换）。"""
    register_all()
    if not USE_REAL_MARKET:
        TOOL_REGISTRY["entity.resolve"] = MockEntityResolveTool()
        TOOL_REGISTRY["market.get_bundle"] = MockMarketBundleTool()
        # loop 阶段也用 mock 联网，避免真实 30s 超时拖慢评测
        from toolkit.web.tools import WebSearchTool
        mock_web = WebSearchTool()
        mock_web.execute = _load_preloop_mocks()._make_web_search(
            [{"title": "茅台批价企稳", "url": "https://e.com/m1"},
             {"title": "白酒消费税改革", "url": "https://e.com/m2"}])
        TOOL_REGISTRY["web.search"] = mock_web
    executor = ToolExecutor()
    assembler = PromptAssembler()

    log("=== [2] loop.run (真 LLM, %s行情) ===" % ("真实" if USE_REAL_MARKET else "mock"))
    loop = AgentLoop(llm_client=llm, assembler=assembler, tool_executor=executor,
                     agent_spawner=None, config=LoopConfig(
                         max_steps=14,
                         max_tool_calls=48,
                         timeout_seconds=OVERALL_TIMEOUT,
                         llm_timeout_seconds=LLM_TIMEOUT,
                         finalize_timeout_seconds=FINALIZE_TIMEOUT,
                         max_consecutive_llm_failures=2,
                     ))

    t0 = time.time()

    async def _heartbeat():
        while True:
            await asyncio.sleep(20)
            log(f"  … 心跳：loop 已运行 {time.time() - t0:.0f}s")

    hb = asyncio.create_task(_heartbeat())
    streamed_chars = 0
    loop_context = dict(r.initial_context)
    loop_context["latency_mode"] = True
    # 外层 watchdog 必须晚于内部预算，否则内部来不及收口（含降级报告）
    # 就被取消，只能看到「已取消」而不见终态。
    watchdog_deadline = OVERALL_TIMEOUT + WATCHDOG_MARGIN

    async def _consume():
        nonlocal streamed_chars
        async for evt in loop.run(
            user_message="请研究贵州茅台是否值得买入，按研究计划逐题取证后收工",
            session_id="e2e_hy3_001",
            skill_name="deep-research",
            research_plan=r.plan,
            initial_context=loop_context,
        ):
            et = evt.type.value
            if et == "tool_call":
                name = evt.metadata.get("tool_name")
                args = evt.metadata.get("arguments", {})
                log(f"  • TOOL_CALL  {name}  {_clip(args, 120)}")
            elif et == "tool_result":
                log(f"    └ TOOL_RESULT ok={evt.metadata.get('success')} "
                    f"{_clip(evt.content, 200)}")
            elif et == "thought":
                log(f"  · THOUGHT {_clip(evt.content, 240)}")
            elif et == "answer_delta":
                previous = streamed_chars
                streamed_chars += len(str(evt.content))
                if previous == 0 or previous // 120 != streamed_chars // 120:
                    preview = str(evt.content).replace("\n", " ")[:80]
                    log(f"  ↳ STREAM chars={streamed_chars} {preview}")
            elif et == "final_answer":
                log(f"  ★ FINAL_ANSWER {_clip(evt.content, 400)}")
            elif et == "warning":
                log(f"  ⚠ WARNING {evt.content}")
            elif et == "error":
                log(f"  ✗ ERROR {evt.content}")
            elif et == "complete":
                log(f"  ○ COMPLETE steps={evt.metadata.get('total_steps')} "
                    f"tokens={evt.metadata.get('total_tokens')} "
                    f"duration={evt.metadata.get('duration_sec')}s")

    try:
        await asyncio.wait_for(_consume(), timeout=watchdog_deadline)
    except asyncio.TimeoutError:
        log(f"  ✗ WATCHDOG 外层超时 {watchdog_deadline:.0f}s（内部预算 "
            f"{OVERALL_TIMEOUT}s 未收口），本次判定 FAIL")
    except asyncio.CancelledError:
        log("  ⨯ CANCELLED 任务被外部取消（非内部超时）；"
            "若此时内部已完成，属外层与内部收尾竞争")
        raise
    finally:
        hb.cancel()
    log("loop 用时 %.1fs" % (time.time() - t0))

    trace = loop.trace
    converged = bool(trace and trace.final_report and trace.final_report.conclusions)
    log("=== [2] 结果 ===")
    log("converged     :", converged)
    log("turns         :", len(trace.turns) if trace else 0)
    log("final_report  :", bool(trace and trace.final_report))
    log("budget_exhausted:", bool(trace and getattr(trace, "budget_exhausted", False)))
    log("streamed_chars  :", streamed_chars)

    log("=== [3] loop_lint ===")
    findings = []
    if trace is not None and trace.turns:
        findings = lint_trace(trace, plan=r.plan)
        if findings:
            for f in findings:
                log("  ❌", f.rule, f.turn_id, f.message)
        else:
            log("  ✅ 无 lint 违规 (10 条规则全过)")
    else:
        log("  ❌ trace 为空或无 turn，不能判定 lint 全过")
    return bool(converged and trace and trace.turns and not findings)


async def main():
    llm = LLMClient(settings)
    endpoint = urlparse(llm.base_url)
    log(
        "LLM 配置:",
        f"provider={llm.provider}",
        f"model={llm.model}",
        f"endpoint={endpoint.scheme}://{endpoint.netloc}{endpoint.path}",
        f"key={'已配置' if bool(llm.api_key) else '缺失'}",
    )
    entity = Entity(symbol="600519.SH", name="贵州茅台", market="A", currency="CNY",
                    source="builtin", confidence=0.95)

    if E2E_MODE == "loop":
        # ── 路径 A：preloop 用 mock LLM 跑（秒级，不调真 hy3），
        #         得到合法四件套；loop 用真 LLM 验工具链 ──
        log("=== 模式: loop (preloop=mock LLM, loop=真 LLM) ===")
        from runtime.preloop import run_preloop
        mod = _load_preloop_mocks()
        mock_llm = mod._make_llm_client([
            {"one_line_business": "高端白酒生产与销售", "segments": [{"name": "白酒", "revenue_share": 98.0}],
             "open_questions": [], "sources": [{"title": "年报", "url": "https://e.com/a"}]},
            {"mode": "whole", "units": [{"id": "u_whole", "scope": "整体公司", "reason": "单一主营整体研究",
             "differential_dimensions": []}], "confidence": 0.9, "source": "年报", "open_questions": []},
            {"development_stage": {"value": "成熟", "evidence": "多年", "confidence": 0.8},
             "charging": {"value": "一次性销售", "evidence": "产品", "confidence": 0.7},
             "capital_intensity": {"value": "轻资产", "evidence": "固资低", "confidence": 0.7},
             "cycle": {"value": "弱周期", "evidence": "稳定", "confidence": 0.6},
             "value_chain": {"value": "整机", "evidence": "终端", "confidence": 0.6},
             "selected_adapter": "consumer_brand", "selection_reason": "命中"},
            {"questions": [{"question": "增长驱动", "priority": "P0", "dimension": "growth"},
             {"question": "毛利率", "priority": "P0", "dimension": "profitability"},
             {"question": "估值假设", "priority": "P0", "dimension": "valuation"},
             {"question": "政策风险", "priority": "P0", "dimension": "risk"}]},
        ])
        md, web_fn = await _build_mock_preloop_inputs()
        r = await run_preloop("贵州茅台是否值得买入", llm_client=mock_llm, market_data=md,
                              web_search_fn=web_fn,
                              entity=Entity(symbol="600519.SH", name="贵州茅台", market="A",
                                            currency="CNY", source="builtin", confidence=0.95),
                              force_refresh=True)
        log("preloop(mock) 就绪: P0=%d info_richness=%s" % (
            len(r.plan.p0_questions), r.initial_context["info_richness"]))
        return await _run_loop_with_preloop(llm, r)

    elif E2E_MODE == "preloop":
        # ── 路径 B：preloop 全环节真 LLM ──
        log("=== 模式: preloop (全环节真 LLM, %s) ===" % ("真实行情" if USE_REAL_MARKET else "mock 行情"))
        from runtime.preloop import run_preloop
        if USE_REAL_MARKET:
            md, web_fn = _build_real_preloop_inputs()
        else:
            md, web_fn = await _build_mock_preloop_inputs()

        t0 = time.time()
        r = await run_preloop(
            "贵州茅台是否值得买入", llm_client=llm, market_data=md,
            web_search_fn=web_fn, entity=entity, force_refresh=True,
        )
        log("preloop 用时 %.1fs" % (time.time() - t0))
        log("entity        :", r.entity.security_id)
        log("info_richness :", r.facts.info_richness.value)
        log("facts_version :", r.facts.facts_version)
        log("granularity   :", r.granularity.mode.value, [u.id for u in r.granularity.units])
        log("adapters      :", [p.selected_adapter for p in r.profiles])
        log("P0 count      :", len(r.plan.p0_questions))
        for q in r.plan.p0_questions:
            log("   -", q.id, q.priority, q.dimension, "|", q.question[:28])
        log("initial_ctx   :", list(r.initial_context.keys()))
        # loop 阶段一并跑，验证完整链路
        return await _run_loop_with_preloop(llm, r)

    else:
        log("未知 E2E_MODE=%s，请用 loop / preloop" % E2E_MODE)
        return False


if __name__ == "__main__":
    # 最外层兜底 = 内部预算 + 余量。必须与内部 120s 错开：否则内部刚要输出
    # 降级报告就被这里取消，表现为「已取消」而非明确的 FAIL。
    outer_deadline = OVERALL_TIMEOUT + WATCHDOG_MARGIN
    try:
        ok = asyncio.run(asyncio.wait_for(main(), outer_deadline))
    except asyncio.TimeoutError:
        log(f"✗ FAIL 整体超时（>{outer_deadline:.0f}s，内部预算 {OVERALL_TIMEOUT}s）"
            f"，已中止。当前模式={E2E_MODE}；"
            f"可用 E2E_OVERALL_TIMEOUT / E2E_WATCHDOG_MARGIN 调整评测时间预算。")
        sys.exit(1)
    except asyncio.CancelledError:
        log("⨯ CANCELLED 最外层任务被取消（外部中断，非评测失败）")
        sys.exit(130)
    if ok:
        log("✅ PASS 全部断言通过")
        sys.exit(0)
    log("❌ FAIL 断言未通过（见上方 lint / 结果输出）")
    sys.exit(1)
