"""Adapter for any OpenAI-compatible /chat/completions endpoint.

Covers OpenAI, Ollama, vLLM, LM Studio, llama.cpp server, OpenRouter, Together,
Groq, and most self-hosted gateways — set OPENONION_BASE_URL accordingly.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ._http import StreamUnsupported, post_json, stream_sse
from .base import Message, ModelResponse, ProviderError, TextSink, ToolCall, Usage


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
        self._can_stream = True
        self._usage_opt = True  # some servers reject stream_options; drop it if so

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]],
                       on_text: TextSink = None) -> ModelResponse:
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
        url = f"{self.base_url}/chat/completions"
        if on_text and self._can_stream:
            for _ in range(2):
                try:
                    return await self._stream(url, body, headers, on_text)
                except StreamUnsupported:
                    if self._usage_opt:
                        self._usage_opt = False
                        continue
                    self._can_stream = False  # this server can't stream; stop asking
                    break
        data = await post_json(self._client, url, body, headers)
        return self._parse(data)

    async def _stream(self, url: str, body: dict[str, Any], headers: dict[str, str],
                      on_text: Any) -> ModelResponse:
        body = {**body, "stream": True}
        if self._usage_opt:
            body["stream_options"] = {"include_usage": True}
        text: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finish = None
        async for chunk in stream_sse(self._client, url, body, headers):
            if "__whole__" in chunk:
                resp = self._parse(chunk["__whole__"])
                if resp.text:
                    await on_text(resp.text)
                return resp
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices") or []:
                finish = choice.get("finish_reason") or finish
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text.append(delta["content"])
                    await on_text(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", len(calls)), {"id": None, "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    a = fn.get("arguments")
                    slot["args"] += a if isinstance(a, str) else (json.dumps(a) if a else "")
        tool_calls = [ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=parse_arguments(c["args"]))
                      for i, c in sorted(calls.items()) if c["name"]]
        return ModelResponse(text="".join(text), tool_calls=tool_calls,
                             usage=Usage(usage.get("prompt_tokens", 0) or 0, usage.get("completion_tokens", 0) or 0),
                             stop_reason=finish)

    def _parse(self, data: dict[str, Any]) -> ModelResponse:
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
