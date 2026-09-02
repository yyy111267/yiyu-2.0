"""最小 LLM 连通性探针：只发一次极短请求，用于在 e2e 前区分配置/计费/网络问题。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import settings
from core.llm import LLMClient


async def main() -> int:
    client = LLMClient(settings)
    endpoint = urlparse(client.base_url)
    print(
        "LLM:",
        f"provider={client.provider}",
        f"model={client.model}",
        f"endpoint={endpoint.scheme}://{endpoint.netloc}{endpoint.path}",
        f"key={'已配置' if bool(client.api_key) else '缺失'}",
        flush=True,
    )
    try:
        answer = await asyncio.wait_for(
            client.chat(
                system="这是连通性检查。",
                user="只回复 OK。",
                temperature=0.0,
                timeout=20.0,
            ),
            timeout=25.0,
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 402:
            print(
                "FAIL: 402 Payment Required — TokenHub 额度/套餐/计费授权不可用。",
                flush=True,
            )
        elif status == 401:
            print("FAIL: 401 Unauthorized — API Key 错误或过期。", flush=True)
        elif status == 403:
            print("FAIL: 403 Forbidden — 凭证没有该模型权限。", flush=True)
        else:
            print(f"FAIL: HTTP {status} — {exc}", flush=True)
        return 1
    except (TimeoutError, httpx.TimeoutException):
        print("FAIL: 请求超时 — 检查 DNS、代理、防火墙和 LLM 端点。", flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 需要给出可操作的失败信息
        print(f"FAIL: {type(exc).__name__}: {exc}", flush=True)
        return 1

    print(f"OK: LLM 已连通，返回={answer[:80]!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
