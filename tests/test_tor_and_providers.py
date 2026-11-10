from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from openonion.browser.tor import TorPortPool, tor_bootstrap_progress, tor_new_identity
from openonion.config import parse_port_range
from openonion.providers.anthropic import AnthropicProvider, to_anthropic
from openonion.providers.base import Message, ToolCall
from openonion.providers.openai_compat import OpenAICompatProvider, parse_arguments, to_openai_messages


def test_parse_port_range():
    assert parse_port_range("9100-9103") == [9100, 9101, 9102, 9103]
    assert parse_port_range("9050") == [9050]
    assert parse_port_range("9100, 9105") == [9100, 9105]
    with pytest.raises(ValueError):
        parse_port_range("9105-9100")


async def test_port_pool_is_sticky_and_exclusive():
    pool = TorPortPool([1, 2])
    assert await pool.acquire("a") == 1
    assert await pool.acquire("a") == 1
    assert await pool.acquire("b") == 2
    with pytest.raises(RuntimeError, match="isolation slots"):
        await pool.acquire("c")
    await pool.release("a")
    assert await pool.acquire("c") == 1


class FakeTorControl:
    """Minimal Tor control-port server implementing the bits we use."""

    def __init__(self, password: str) -> None:
        self.password = password
        self.commands: list[str] = []

    async def handle(self, reader, writer):
        authed = False
        while line := await reader.readline():
            cmd = line.decode().strip()
            self.commands.append(cmd)
            if cmd.startswith("AUTHENTICATE"):
                authed = cmd == f'AUTHENTICATE "{self.password}"'
                writer.write(b"250 OK\r\n" if authed else b"515 Authentication failed\r\n")
            elif not authed:
                writer.write(b"514 Authentication required.\r\n")
            elif cmd == "SIGNAL NEWNYM":
                writer.write(b"250 OK\r\n")
            elif cmd == "GETINFO status/bootstrap-phase":
                writer.write(b'250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS=100 TAG=done SUMMARY="Done"\r\n'
                             b"250 OK\r\n")
            elif cmd == "QUIT":
                writer.write(b"250 closing connection\r\n")
                await writer.drain()
                break
            await writer.drain()
        writer.close()


async def test_tor_control_newnym_and_bootstrap():
    fake = FakeTorControl("s3cret")
    server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        await tor_new_identity("127.0.0.1", port, "s3cret")
        assert await tor_bootstrap_progress("127.0.0.1", port, "s3cret") == 100
        with pytest.raises(RuntimeError, match="515"):
            await tor_new_identity("127.0.0.1", port, "wrong")
    assert "SIGNAL NEWNYM" in fake.commands


def test_openai_message_conversion():
    msgs = [Message("system", "sys"), Message("user", "hi"),
            Message("assistant", "", [ToolCall("c1", "read_file", {"path": "a"})]),
            Message("tool", "data", tool_call_id="c1", name="read_file")]
    out = to_openai_messages(msgs)
    assert out[2]["content"] is None
    assert out[2]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "a"}'}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "data"}
    assert parse_arguments('{"a": 1}') == {"a": 1}
    assert "_unparseable_arguments" in parse_arguments("{not json")


def test_anthropic_conversion_merges_tool_results():
    msgs = [Message("system", "sys"), Message("user", "hi"),
            Message("assistant", "thinking", [ToolCall("t1", "a", {}), ToolCall("t2", "b", {"x": 1})]),
            Message("tool", "r1", tool_call_id="t1"), Message("tool", "r2", tool_call_id="t2")]
    system, out = to_anthropic(msgs)
    assert system == "sys"
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [b["type"] for b in out[1]["content"]] == ["text", "tool_use", "tool_use"]
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["t1", "t2"]


async def test_openai_provider_roundtrip_with_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "m" and body["tools"][0]["function"]["name"] == "read_file"
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
                {"id": "c9", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"x"}'}}]}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3}})

    p = OpenAICompatProvider("http://llm/v1", "m", "k")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = await p.complete([Message("user", "hi")], [{"name": "read_file", "description": "d", "parameters": {}}])
    assert r.tool_calls[0].arguments == {"path": "x"} and r.usage.total == 14
    await p.aclose()


async def test_anthropic_provider_roundtrip_with_retry():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(529, headers={"retry-after": "0"}, json={"error": "overloaded"})
        assert request.headers["x-api-key"] == "k"
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "Let me look."},
                        {"type": "tool_use", "id": "tu1", "name": "web_fetch", "input": {"url": "https://x.org"}}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 20, "output_tokens": 5}})

    p = AnthropicProvider("m", "k")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = await p.complete([Message("user", "hi")], [])
    assert attempts["n"] == 2
    assert r.text == "Let me look." and r.tool_calls[0].name == "web_fetch"
    await p.aclose()
