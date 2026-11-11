"""Adapter for the Anthropic Messages API (native tool use)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ._http import post_json, stream_sse
from .base import Message, ModelResponse, ProviderError, TextSink, ToolCall, Usage

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


def to_anthropic(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Returns (system_prompt, messages). Consecutive tool results are merged into
    one user turn, as the API requires results to directly follow the tool_use turn."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        elif m.role == "user":
            push("user", [{"type": "text", "text": m.content}])
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
            if blocks:
                push("assistant", blocks)
        elif m.role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}])
    return "\n\n".join(system_parts), out


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str, max_tokens: int = 4096, timeout: float = 180.0,
                 url: str = API_URL) -> None:
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY is required for the anthropic provider")
        self.model = model or "claude-sonnet-5-5"
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.url = url
        self._client = httpx.AsyncClient(timeout=timeout)

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]],
                       on_text: TextSink = None) -> ModelResponse:
        system, msgs = to_anthropic(messages)
        body: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "messages": msgs}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                             for t in tools]
        headers = {"x-api-key": self.api_key, "anthropic-version": API_VERSION,
                   "content-type": "application/json"}
        if on_text:
            return await self._stream(body, headers, on_text)
        data = await post_json(self._client, self.url, body, headers)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {}))
        usage = data.get("usage") or {}
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=calls,
            usage=Usage(usage.get("input_tokens", 0) or 0, usage.get("output_tokens", 0) or 0),
            stop_reason=data.get("stop_reason"),
        )

    async def _stream(self, body: dict[str, Any], headers: dict[str, str], on_text: Any) -> ModelResponse:
        text: list[str] = []
        blocks: dict[int, dict[str, Any]] = {}
        usage = {"input_tokens": 0, "output_tokens": 0}
        stop = None
        async for e in stream_sse(self._client, self.url, {**body, "stream": True}, headers):
            kind = e.get("type")
            if kind == "message_start":
                usage.update({k: v for k, v in ((e.get("message") or {}).get("usage") or {}).items() if k in usage})
            elif kind == "content_block_start":
                b = e.get("content_block") or {}
                blocks[e.get("index", 0)] = {"type": b.get("type"), "id": b.get("id"), "name": b.get("name"), "json": ""}
            elif kind == "content_block_delta":
                d = e.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    text.append(d["text"])
                    await on_text(d["text"])
                elif d.get("type") == "input_json_delta":
                    blocks.setdefault(e.get("index", 0), {"type": "tool_use", "json": ""})["json"] += d.get("partial_json", "")
            elif kind == "message_delta":
                stop = (e.get("delta") or {}).get("stop_reason") or stop
                usage["output_tokens"] = (e.get("usage") or {}).get("output_tokens", usage["output_tokens"])
            elif kind == "error":
                raise ProviderError(f"stream error: {(e.get('error') or {}).get('message', e)}")
        calls = []
        for _, b in sorted(blocks.items()):
            if b.get("type") == "tool_use":
                try:
                    args = json.loads(b["json"]) if b["json"] else {}
                except json.JSONDecodeError:
                    args = {"_unparseable_arguments": b["json"][:2000]}
                calls.append(ToolCall(id=b["id"], name=b["name"], arguments=args))
        return ModelResponse(text="".join(text), tool_calls=calls,
                             usage=Usage(usage["input_tokens"] or 0, usage["output_tokens"] or 0), stop_reason=stop)

    async def aclose(self) -> None:
        await self._client.aclose()
