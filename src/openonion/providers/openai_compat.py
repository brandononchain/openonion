"""Adapter for any OpenAI-compatible /chat/completions endpoint.

Covers OpenAI, Ollama, vLLM, LM Studio, llama.cpp server, OpenRouter, Together,
Groq, and most self-hosted gateways — set OPENONION_BASE_URL accordingly.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ._http import post_json
from .base import Message, ModelResponse, ProviderError, ToolCall, Usage


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            out.append({
                "role": "assistant",
                "content": m.content or None,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                    for tc in m.tool_calls
                ],
            })
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    except json.JSONDecodeError:
        # Surface malformed arguments to the tool layer, which reports a
        # validation error back to the model so it can correct itself.
        return {"_unparseable_arguments": str(raw)[:2000]}


class OpenAICompatProvider:
    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str = "", max_tokens: int = 4096,
                 timeout: float = 180.0) -> None:
        if not model:
            raise ProviderError("OPENONION_MODEL is required for the openai-compatible provider")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=timeout)

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "max_tokens": self.max_tokens,
        }
        if tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
                for t in tools
            ]
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = await post_json(self._client, f"{self.base_url}/chat/completions", body, headers)

        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"unexpected response shape: {str(data)[:300]}") from exc

        calls = [
            ToolCall(id=tc.get("id") or f"call_{i}", name=tc["function"]["name"],
                     arguments=parse_arguments(tc["function"].get("arguments")))
            for i, tc in enumerate(msg.get("tool_calls") or [])
        ]
        usage = data.get("usage") or {}
        return ModelResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            usage=Usage(usage.get("prompt_tokens", 0) or 0, usage.get("completion_tokens", 0) or 0),
            stop_reason=choice.get("finish_reason"),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
