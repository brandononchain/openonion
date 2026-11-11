"""Live text streaming: provider adapters, fallbacks, and the live-only text_delta events."""

from __future__ import annotations

import asyncio
import json

import httpx

from openonion import events as ev
from openonion.api import create_app
from openonion.providers.anthropic import AnthropicProvider
from openonion.providers.base import Message, ModelResponse
from openonion.providers.openai_compat import OpenAICompatProvider
from openonion.providers.scripted import ScriptedProvider


def sse(*objs) -> bytes:
    return "".join(f"data: {json.dumps(o)}\n\n" for o in objs).encode() + b"data: [DONE]\n\n"


async def collect():
    seen: list[str] = []

    async def sink(t: str) -> None:
        seen.append(t)
    return seen, sink


async def test_openai_streams_text_and_assembles_tool_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(
            {"choices": [{"delta": {"content": "Checking "}}]},
            {"choices": [{"delta": {"content": "the page."}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "web_fetch", "arguments": '{"url":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"https://x.org"}'}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}},
        ))

    p = OpenAICompatProvider("http://llm/v1", "m")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    seen, sink = await collect()
    r = await p.complete([Message("user", "hi")], [], on_text=sink)
    assert seen == ["Checking ", "the page."] and r.text == "Checking the page."
    assert r.tool_calls[0].name == "web_fetch" and r.tool_calls[0].arguments == {"url": "https://x.org"}
    assert r.usage.total == 13 and r.stop_reason == "tool_calls"
    await p.aclose()


async def test_openai_falls_back_when_server_cannot_stream():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream"):
            return httpx.Response(400, json={"error": "stream is not supported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}]})

    p = OpenAICompatProvider("http://llm/v1", "m")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    _seen, sink = await collect()
    assert (await p.complete([Message("user", "hi")], [], on_text=sink)).text == "Hello"
    # Streaming with usage, streaming without it, then plain; after that it stops trying to stream.
    assert [("stream_options" in b, b.get("stream", False)) for b in bodies] == [(True, True), (False, True), (False, False)]
    await p.complete([Message("user", "hi")], [], on_text=sink)
    assert not bodies[-1].get("stream")
    await p.aclose()


async def test_openai_server_that_ignores_stream_flag():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "Whole reply"}, "finish_reason": "stop"}]})

    p = OpenAICompatProvider("http://llm/v1", "m")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    seen, sink = await collect()
    r = await p.complete([Message("user", "hi")], [], on_text=sink)
    assert r.text == "Whole reply" and seen == ["Whole reply"]
    await p.aclose()


async def test_anthropic_streams_text_and_tool_use():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 20, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me "}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "look."}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu1", "name": "web_fetch", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"url": "https://x'}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '.org"}'}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
            {"type": "message_stop"},
        ))

    p = AnthropicProvider("m", "k")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    seen, sink = await collect()
    r = await p.complete([Message("user", "hi")], [], on_text=sink)
    assert seen == ["Let me ", "look."] and r.text == "Let me look."
    assert r.tool_calls[0].id == "tu1" and r.tool_calls[0].arguments == {"url": "https://x.org"}
    assert r.usage.input_tokens == 20 and r.usage.output_tokens == 7 and r.stop_reason == "tool_use"
    await p.aclose()


async def test_text_deltas_are_live_only(settings):
    answer = "Onion routing wraps each message in layers of encryption, one per relay."
    factory = lambda _s: ScriptedProvider([ModelResponse(answer, [])])
    app = create_app(settings, provider_factory=factory)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    try:
        sid = (await client.post("/api/sessions", json={"title": "t", "policy": {}})).json()["id"]
        q = app.state.bus.subscribe(sid)
        await client.post(f"/api/sessions/{sid}/runs", json={"goal": "explain onion routing"})
        live = []
        while True:
            e = await asyncio.wait_for(q.get(), 10)
            live.append(e)
            if e.type in ev.TERMINAL_EVENTS:
                break
        deltas = [e for e in live if e.type == ev.TEXT_DELTA]
        assert deltas and all(e.seq == 0 for e in deltas)
        assert "".join(e.payload["text"] for e in deltas) == answer
        # Deltas all arrive before the stored assistant message.
        types = [e.type for e in live]
        assert max(i for i, t in enumerate(types) if t == ev.TEXT_DELTA) < types.index(ev.ASSISTANT_MESSAGE)
        stored = (await client.get(f"/api/sessions/{sid}/events")).json()
        assert not any(e["type"] == ev.TEXT_DELTA for e in stored)
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
