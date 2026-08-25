"""临时测试脚本：跑一次「从价值投资的角度分析北方稀土」，打印完整过程 + 最终结论。"""
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import settings
from core.llm import LLMClient
from runtime.assembler import PromptAssembler
from runtime.loop import AgentLoop
from runtime.router import route_async
from toolkit.executor import ToolExecutor
from toolkit import register_all
from agents.base import AgentSpawner


async def main():
    register_all()
    llm = LLMClient(settings)
    assembler = PromptAssembler(prompts_dir=str(PROJECT_ROOT / "prompts"))
    executor = ToolExecutor()
    spawner = AgentSpawner()
    loop = AgentLoop(
        llm_client=llm,
        assembler=assembler,
        tool_executor=executor,
        agent_spawner=spawner,
    )

    msg = "从价值投资的角度分析北方稀土"
    routed = await route_async(llm_client=llm, user_message=msg)
    print("=" * 60)
    print(f"[路由] skill={routed.skill} matched_by={routed.matched_by} confidence={routed.confidence}")
    print("=" * 60)

    final_answer = None
    async for evt in loop.run(user_message=msg, session_id="test-beifang-xitu", skill_name=routed.skill):
        t = evt.type.value if hasattr(evt.type, "value") else str(evt.type)
        content = evt.content or ""
        meta = evt.metadata or {}

        if t == "start":
            print("\n[START]", content)
        elif t == "plan":
            print("\n[PLAN]")
            print(content[:1200])
        elif t == "thought":
            print("\n[THOUGHT]", content[:1000])
        elif t == "tool_call":
            args = json.dumps(meta.get("arguments", {}), ensure_ascii=False)
            print(f"\n[TOOL_CALL] {meta.get('tool_name')} args={args[:300]}")
        elif t == "tool_result":
            ok = meta.get("success")
            print(f"[TOOL_RESULT {'OK' if ok else 'FAIL'}] {meta.get('tool_name')} → {content[:400]}")
        elif t == "warning":
            print(f"\n[WARNING] {content}")
        elif t == "error":
            print(f"\n[ERROR] {content}")
        elif t == "final_answer":
            final_answer = content
        elif t == "complete":
            print("\n[COMPLETE]", json.dumps(meta, ensure_ascii=False))

    print("\n" + "=" * 60)
    print("最终结论 FINAL_ANSWER：")
    print("=" * 60)
    if final_answer is None:
        print("!!! 未拿到 FINAL_ANSWER（可能被拦截或异常）")
    else:
        print(final_answer)


if __name__ == "__main__":
    asyncio.run(main())
