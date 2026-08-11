"""LLM 能力层（合并原 client/vision/embedding/prompt_loader/guardrails）。

支持 DeepSeek（产品默认，OpenAI 兼容）/ OpenAI 兼容端点切换。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError


# OpenAI 兼容接口的默认 base（DeepSeek / OpenAI 共用同一套 /chat/completions 协议）
_PROVIDER_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
}


class PromptLoader:
    """从 prompts/ 目录加载 md 提示词，支持简单变量替换。"""

    def __init__(self, base_dir: str = "prompts") -> None:
        self.base = Path(base_dir)

    def load(self, rel_path: str, **vars) -> str:
        text = (self.base / rel_path).read_text(encoding="utf-8")
        for k, v in vars.items():
            text = text.replace("{{ " + k + " }}", str(v))
        return text


class LLMClient:
    """OpenAI 兼容的 LLM 客户端（DeepSeek 默认）。"""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model or "deepseek-chat"
        self.provider = (settings.llm_provider or "deepseek").lower()
        self.base_url = settings.llm_base_url or _PROVIDER_BASE_URLS.get(
            self.provider, "https://api.deepseek.com"
        )

    def _ensure_key(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "缺少 LLM API Key。请在 .env 中设置 IC_LLM_API_KEY（DeepSeek 控制台获取）。"
            )

    async def chat(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.7,
        timeout: float = 120.0,
    ) -> str:
        self._ensure_key()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        return data["choices"][0]["message"]["content"]

    async def chat_json(self, system: str, user: str, **kw) -> Any:
        text = await self.chat(system, user, json_mode=True, **kw)
        return _extract_json(text)

    async def chat_with_tools(
        self,
        system: str,
        user: str,
        tools: list[dict],
        *,
        temperature: float = 0.7,
        timeout: float = 120.0,
    ) -> dict:
        """带 function calling 的对话。

        Args:
            tools: OpenAI 兼容的 tool schema 列表，形如
                [{"type": "function", "function": {"name", "description", "parameters"}}]

        Returns:
            dict: {
                "content": str | None,           # 模型文本回复（无 tool call 时）
                "tool_calls": list[dict] | None,  # [{name, arguments(dict), id}]
                "tokens_used": int,               # token 用量
            }
        """
        self._ensure_key()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
            "tools": tools,
            "tool_choice": "auto",
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        message = data["choices"][0]["message"]
        raw_tool_calls = message.get("tool_calls") or []

        parsed_calls: list[dict] = []
        for tc in raw_tool_calls:
            fn = tc.get("function", {})
            args_raw = fn.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except json.JSONDecodeError:
                args = {"_raw": args_raw}
            parsed_calls.append({
                "name": fn.get("name", ""),
                "arguments": args,
                "id": tc.get("id", ""),
            })

        usage = data.get("usage", {})
        return {
            "content": message.get("content"),
            "tool_calls": parsed_calls if parsed_calls else None,
            "tokens_used": usage.get("total_tokens", 0),
        }

    async def chat_structured(
        self,
        system: str,
        user: str,
        schema: "Type[_M]",
        *,
        temperature: float = 0.7,
        timeout: float = 120.0,
        retries: int = 2,
    ) -> "_M":
        """调用 LLM 并把返回校验成给定的 Pydantic 模型。

        校验失败（JSON 解析 / 字段不符）时自动重试，重试会在 user 末尾追加
        纠正提示。全部失败后抛出 ValueError（含最后一次错误），不会静默兜底。
        """
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                data = await self.chat_json(
                    system, user, temperature=temperature, timeout=timeout
                )
                return schema.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
                user = (
                    user
                    + "\n\n[纠正] 上一次输出不是合法 JSON 或结构/类型不符合要求，"
                    "请严格按要求的字段名与类型返回纯 JSON。"
                )
        raise ValueError(f"结构化输出校验失败（已重试 {retries} 次）：{last_err}")


_M = TypeVar("_M", bound=BaseModel)


def _extract_json(text: str) -> Any:
    """从模型返回中提取 JSON（兼容 ```json 代码块包裹）。"""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```", 2)
        if len(parts) >= 2:
            text = parts[1]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
            text = text.strip().rstrip("`").strip()
    return json.loads(text)
