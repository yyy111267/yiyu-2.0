"""LLM 能力层（合并原 client/vision/embedding/prompt_loader/guardrails）。

支持 DeepSeek / 混元3（腾讯 MaaS TokenHub）/ OpenAI 兼容端点切换。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

_usage_sink: ContextVar[Callable[[dict], None] | None] = ContextVar(
    "llm_usage_sink", default=None,
)
_usage_stage: ContextVar[str] = ContextVar("llm_usage_stage", default="unknown")


@contextmanager
def capture_llm_usage(sink: Callable[[dict], None]):
    """为当前请求绑定 usage 账本；ContextVar 保证并发会话不串账。"""
    token = _usage_sink.set(sink)
    try:
        yield
    finally:
        _usage_sink.reset(token)


@contextmanager
def llm_usage_stage(stage: str):
    """标记当前模型调用所属阶段（routing/preloop/research/synthesis）。"""
    token = _usage_stage.set(stage)
    try:
        yield
    finally:
        _usage_stage.reset(token)


def _record_usage(usage: dict, *, system: str, user: str, tools: list[dict] | None) -> None:
    sink = _usage_sink.get()
    if sink is None:
        return
    try:
        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(usage.get("completion_tokens", 0) or 0)
        cached_tokens = int(
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            or usage.get("prompt_cache_hit_tokens", 0) or 0
        )
        sink({
            "stage": _usage_stage.get(),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": int(usage.get("total_tokens", 0) or 0)
            or input_tokens + output_tokens,
            "cached_input_tokens": cached_tokens,
            "chars": {
                "system": len(system or ""),
                "user": len(user or ""),
                "tool_schemas": len(json.dumps(tools or [], ensure_ascii=False)),
            },
        })
    except Exception as exc:  # 埋点失败不影响主链路
        logger.warning("LLM usage 埋点失败: %s", exc)


# OpenAI 兼容接口的默认 base（共用同一套 /chat/completions 协议）
# 注意：base_url 末尾需带 /v1，代码会拼成 {base_url}/chat/completions
_PROVIDER_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
    "hunyuan": "https://tokenhub.tencentmaas.com/v1",
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
        self.thinking_enabled = bool(getattr(settings, "llm_thinking_enabled", False))
        self.reasoning_effort = str(getattr(settings, "llm_reasoning_effort", "low"))
        self.max_tokens = int(getattr(settings, "llm_max_tokens", 2048))
        self.base_url = (settings.llm_base_url or _PROVIDER_BASE_URLS.get(
            self.provider, "https://api.deepseek.com"
        )).rstrip("/")

    def _ensure_key(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "缺少 LLM API Key。请在 .env 中设置 IC_LLM_API_KEY（DeepSeek 控制台获取）。"
            )

    async def _chat_message(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.7,
        timeout: float = 120.0,
    ) -> dict:
        """底层请求，返回完整的 message dict（含 content / reasoning_content）。"""
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
            "max_tokens": self.max_tokens,
        }
        if self.model.lower().startswith("glm-"):
            payload["thinking"] = {
                "type": "enabled" if self.thinking_enabled else "disabled",
            }
            if self.thinking_enabled:
                payload["reasoning_effort"] = self.reasoning_effort
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

        _record_usage(data.get("usage") or {}, system=system, user=user, tools=None)
        return data["choices"][0]["message"]

    async def chat(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.7,
        timeout: float = 120.0,
    ) -> str:
        message = await self._chat_message(
            system, user, json_mode=json_mode, temperature=temperature, timeout=timeout
        )
        return message.get("content") or ""

    async def chat_with_usage(self, system: str, user: str, **kw) -> dict:
        """普通对话的带 usage 版本，供 synthesis 精确计费使用。"""
        self._ensure_key()
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": user}
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": kw.get("temperature", 0.7),
            "stream": False,
            "max_tokens": self.max_tokens,
        }
        if self.model.lower().startswith("glm-"):
            payload["thinking"] = {
                "type": "enabled" if self.thinking_enabled else "disabled",
            }
            if self.thinking_enabled:
                payload["reasoning_effort"] = self.reasoning_effort
        timeout = kw.get("timeout", 120.0)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        usage = data.get("usage") or {}
        _record_usage(usage, system=system, user=user, tools=None)
        return {
            "content": data["choices"][0]["message"].get("content") or "",
            "tokens_used": int(usage.get("total_tokens", 0) or 0),
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        }

    async def chat_json(self, system: str, user: str, **kw) -> Any:
        message = await self._chat_message(system, user, json_mode=True, **kw)
        # hy3 等推理模型可能把答案放 reasoning_content、content 留空；做兜底
        text = message.get("content")
        if not text:
            rc = message.get("reasoning_content")
            if rc:
                logger.warning(
                    "chat_json: content 为空，从 reasoning_content 回退提取 JSON"
                )
                text = rc
            else:
                logger.warning(
                    "chat_json: content 与 reasoning_content 均为空，raw=%s", message
                )
                return None
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
                "tokens_used": int,               # token 用量（total）
                "prompt_tokens": int,             # 输入 token（usage 缺失时为 0）
                "completion_tokens": int,         # 输出 token（usage 缺失时为 0）
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
            "max_tokens": self.max_tokens,
            "tools": tools,
            "tool_choice": "auto",
        }
        if self.model.lower().startswith("glm-"):
            payload["thinking"] = {
                "type": "enabled" if self.thinking_enabled else "disabled",
            }
            if self.thinking_enabled:
                payload["reasoning_effort"] = self.reasoning_effort

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

        _record_usage(data.get("usage") or {}, system=system, user=user, tools=tools)

        message = data["choices"][0]["message"]
        raw_tool_calls = message.get("tool_calls") or []

        # 部分 OpenAI 兼容模型（当前 hy3 就有此情况）会把本应放在
        # message.tool_calls 里的调用以 JSON 文本放进 content。如果不在能力层
        # 归一化，loop 会把这段 JSON 当成最终研究结论，导致跳过工具取数。
        if not raw_tool_calls and message.get("content"):
            fallback_calls = _extract_text_tool_calls(message["content"], tools)
            if fallback_calls:
                logger.warning("chat_with_tools: 从 content JSON 恢复 %d 个工具调用", len(fallback_calls))
                return {
                    "content": None,
                    "tool_calls": fallback_calls,
                    "tokens_used": data.get("usage", {}).get("total_tokens", 0),
                    "prompt_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                    "completion_tokens": data.get("usage", {}).get("completion_tokens", 0),
                }

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
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        }

    async def chat_with_tools_stream(
        self,
        system: str,
        user: str,
        tools: list[dict],
        *,
        on_content_delta: Callable[[str], Awaitable[None]],
        temperature: float = 0.7,
        timeout: float = 120.0,
    ) -> dict:
        """流式 function calling；边收 content delta，边拼装完整工具调用。

        最终仍返回与 ``chat_with_tools`` 相同的归一化结构，因此 loop 的硬规则、
        工具执行和 trace 逻辑无需分叉。``reasoning_content`` 不向客户端透传。
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
            "stream": True,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.model.lower().startswith("glm-"):
            payload["thinking"] = {
                "type": "enabled" if self.thinking_enabled else "disabled",
            }
            if self.thinking_enabled:
                payload["reasoning_effort"] = self.reasoning_effort
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        content_parts: list[str] = []
        calls_by_index: dict[int, dict[str, str]] = {}
        tokens_used = 0
        prompt_tokens = 0
        completion_tokens = 0

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions",
                json=payload, headers=headers,
            ) as resp:
                if resp.is_error:
                    await resp.aread()
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    usage = event.get("usage") or {}
                    tokens_used = usage.get("total_tokens", tokens_used) or tokens_used
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens) or prompt_tokens
                    completion_tokens = (
                        usage.get("completion_tokens", completion_tokens)
                        or completion_tokens
                    )
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        content_parts.append(piece)
                        await on_content_delta(piece)
                    for call in delta.get("tool_calls") or []:
                        index = int(call.get("index", 0))
                        slot = calls_by_index.setdefault(
                            index, {"id": "", "name": "", "arguments": ""},
                        )
                        if call.get("id"):
                            slot["id"] = call["id"]
                        fn = call.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""

        content = "".join(content_parts) or None
        parsed_calls: list[dict] = []
        for index in sorted(calls_by_index):
            raw_call = calls_by_index[index]
            args_raw = raw_call["arguments"] or "{}"
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                args = {"_raw": args_raw}
            parsed_calls.append({
                "name": raw_call["name"],
                "arguments": args,
                "id": raw_call["id"] or f"stream_call_{index}",
            })

        if not parsed_calls and content:
            parsed_calls = _extract_text_tool_calls(content, tools)
            if parsed_calls:
                content = None
        _record_usage({
            "total_tokens": tokens_used,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }, system=system, user=user, tools=tools)
        return {
            "content": content,
            "tool_calls": parsed_calls or None,
            "tokens_used": tokens_used,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
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


def _extract_text_tool_calls(content: str, tools: list[dict]) -> list[dict]:
    """从模型的普通文本 JSON 中恢复工具调用。

    只接受本轮已下发的工具名，避免把普通 JSON 回答误当成工具调用。
    兼容 ``{"name": ..., "arguments": ...}`` 和 ``{"tool_calls": [...]}``。
    """
    try:
        payload = _extract_json(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []

    raw_calls: list[Any]
    if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
        raw_calls = payload["tool_calls"]
    elif isinstance(payload, dict) and payload.get("name"):
        raw_calls = [payload]
    else:
        return []

    offered = {
        str(item.get("function", {}).get("name", ""))
        for item in tools
        if isinstance(item, dict)
    }
    offered_norm = {name.replace("_", "."): name for name in offered if name}
    parsed: list[dict] = []
    for index, call in enumerate(raw_calls):
        if not isinstance(call, dict):
            return []
        function = call.get("function") if isinstance(call.get("function"), dict) else call
        name = str(function.get("name", "")).strip()
        canonical = name.replace("_", ".")
        if not name or canonical not in offered_norm:
            return []
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return []
        if not isinstance(arguments, dict):
            return []
        parsed.append({
            "name": name,
            "arguments": arguments,
            "id": str(call.get("id") or f"text_call_{index}"),
        })
    return parsed
