"""最小无工具流式探针；失败时打印安全的状态码与响应正文（不打印密钥）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import settings  # noqa: E402
from core.llm import LLMClient  # noqa: E402


async def main() -> int:
    client = LLMClient(settings)

    async def emit(piece: str) -> None:
        print(piece, end="", flush=True)

    try:
        result = await client.chat_with_tools_stream(
            "你是连通性检查助手。", "只回复 OK。", [],
            on_content_delta=emit, timeout=15,
        )
    except httpx.HTTPStatusError as exc:
        print(f"\nHTTP {exc.response.status_code}: {exc.response.text[:2000]}")
        return 1
    print(f"\nOK: tool_calls={bool(result.get('tool_calls'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
