from __future__ import annotations

import asyncio

from openonion import events as ev
from openonion.providers.base import Message, ModelResponse, ToolCall, Usage
from openonion.providers.scripted import ScriptedProvider
from openonion.runtime.manager import SessionManager
from openonion.runtime.orchestrator import repair_transcript


def call(name, args, id_):
    return ToolCall(id=id_, name=name, arguments=args)


async def run_to_end(manager, store, sid, goal, on_event=None):
    q = manager.bus.subscribe(sid)
    task = await manager.start_run(sid, goal)
    seen = []
    while True:
        e = await asyncio.wait_for(q.get(), 10)
        seen.append(e)
        if on_event:
            await on_event(e)
        if e.type in ev.TERMINAL_EVENTS:
            break
    await task
    return seen


async def test_full_loop_with_plan_write_and_read(settings, store, bus):
    script = [
        ModelResponse("Planning.", [call("update_plan", {"steps": [
            {"id": 1, "description": "write notes", "status": "in_progress"},
            {"id": 2, "description": "read back", "status": "pending"}]}, "c1")], Usage(100, 20)),
        ModelResponse("", [call("write_file", {"path": "notes/a.md", "content": "hello onion"}, "c2")], Usage(120, 10)),
        ModelResponse("", [call("read_file", {"path": "notes/a.md"}, "c3")], Usage(130, 10)),
        ModelResponse("Done: the file says hello onion.", [], Usage(140, 15)),
    ]
    provider = ScriptedProvider(script)
    manager = SessionManager(store, bus, settings, provider_factory=lambda s: provider)
    session = await store.create_session("t", {"policy": {"approval_required": ["write_file"]}})
    sid = session["id"]

    async def approve(e):
        if e.type == ev.APPROVAL_REQUESTED:
            assert e.payload["tool"] == "write_file"
            assert manager.resolve_approval(sid, e.payload["approval_id"], True)
        if e.type == ev.RUN_FINISHED:
            # Clients refresh on the terminal event, so the status must already be final.
            assert (await store.get_session(sid))["status"] == "completed"

    seen = await run_to_end(manager, store, sid, "write and read a note", approve)
    types = [e.type for e in seen]

    assert types[0] == ev.RUN_STARTED
    assert ev.PLAN_UPDATED in types
    assert ev.APPROVAL_REQUESTED in types and ev.APPROVAL_RESOLVED in types
    assert types[-1] == ev.RUN_FINISHED
    assert seen[-1].payload["answer"].startswith("Done")
    assert seen[-1].payload["tokens"] == 545
    assert (settings.workspace_root / sid / "notes" / "a.md").read_text() == "hello onion"
    read = [e for e in seen if e.type == ev.TOOL_FINISHED and e.payload["tool"] == "read_file"][0]
    assert read.payload["ok"] and "hello onion" in read.payload["preview"]
    assert (await store.get_session(sid))["status"] == "completed"

    # Tool output reaches the model wrapped as untrusted data.
    last_prompt = provider.calls[-1]
    tool_msgs = [m for m in last_prompt if m.role == "tool" and m.name == "read_file"]
    assert tool_msgs and tool_msgs[0].content.startswith('<tool_output tool="read_file" trust="untrusted">')

    # Sequence numbers are gap-free and monotonic.
    all_events = await store.events(sid)
    assert [e.seq for e in all_events] == list(range(1, len(all_events) + 1))


async def test_denied_approval_is_reported_to_model(settings, store, bus):
    provider = ScriptedProvider([
        ModelResponse("", [call("write_file", {"path": "x.txt", "content": "x"}, "c1")]),
        ModelResponse("Understood, not writing.", []),
    ])
    manager = SessionManager(store, bus, settings, provider_factory=lambda s: provider)
    sid = (await store.create_session("t", {"policy": {}}))["id"]

    async def deny(e):
        if e.type == ev.APPROVAL_REQUESTED:
            manager.resolve_approval(sid, e.payload["approval_id"], False)

    await run_to_end(manager, store, sid, "write x", deny)
    assert not (settings.workspace_root / sid / "x.txt").exists()
    tool_msg = [m for m in provider.calls[-1] if m.role == "tool"][0]
    assert "declined" in tool_msg.content


async def test_policy_denies_network_and_private_hosts(settings, store, bus):
    provider = ScriptedProvider([
        ModelResponse("", [call("tor_browse", {"url": "http://example.onion"}, "c1"),
                           call("web_fetch", {"url": "http://169.254.169.254/latest/meta-data"}, "c2")]),
        ModelResponse("ok", []),
    ])
    manager = SessionManager(store, bus, settings, provider_factory=lambda s: provider)
    sid = (await store.create_session("t", {"policy": {"network": "clearnet"}}))["id"]
    seen = await run_to_end(manager, store, sid, "go")
    denials = [e.payload for e in seen if e.type == ev.POLICY_DENIED]
    assert len(denials) == 2
    assert "Tor access is disabled" in denials[0]["reason"]
    assert "private/internal host" in denials[1]["reason"]


async def test_invalid_arguments_and_unknown_tool(settings, store, bus):
    provider = ScriptedProvider([
        ModelResponse("", [call("read_file", {}, "c1"), call("launch_missiles", {}, "c2")]),
        ModelResponse("recovered", []),
    ])
    manager = SessionManager(store, bus, settings, provider_factory=lambda s: provider)
    sid = (await store.create_session("t", {"policy": {}}))["id"]
    seen = await run_to_end(manager, store, sid, "go")
    assert seen[-1].type == ev.RUN_FINISHED
    tool_msgs = [m.content for m in provider.calls[-1] if m.role == "tool"]
    assert "invalid arguments" in tool_msgs[0]
    assert "unknown or unavailable tool" in tool_msgs[1]


async def test_step_budget_stops_run(settings, store, bus):
    looping = [ModelResponse("", [call("list_files", {}, f"c{i}")]) for i in range(5)]
    manager = SessionManager(store, bus, settings, provider_factory=lambda s: ScriptedProvider(looping))
    sid = (await store.create_session("t", {"policy": {"max_steps": 3}}))["id"]
    seen = await run_to_end(manager, store, sid, "loop forever")
    assert seen[-1].type == ev.RUN_STOPPED
    assert "step limit" in seen[-1].payload["reason"]


async def test_follow_up_run_sees_prior_transcript(settings, store, bus):
    first = ScriptedProvider([ModelResponse("The capital is Paris.", [])])
    second = ScriptedProvider([ModelResponse("Population ~2.1M.", [])])
    providers = iter([first, second])
    manager = SessionManager(store, bus, settings, provider_factory=lambda s: next(providers))
    sid = (await store.create_session("t", {"policy": {}}))["id"]
    await run_to_end(manager, store, sid, "capital of France?")
    await run_to_end(manager, store, sid, "and its population?")
    convo = [(m.role, m.content) for m in second.calls[0] if m.role != "system"]
    assert convo == [("user", "capital of France?"), ("assistant", "The capital is Paris."),
                     ("user", "and its population?")]


async def test_cancel_mid_approval(settings, store, bus):
    provider = ScriptedProvider([ModelResponse("", [call("write_file", {"path": "a", "content": "b"}, "c1")])])
    manager = SessionManager(store, bus, settings, provider_factory=lambda s: provider)
    sid = (await store.create_session("t", {"policy": {}}))["id"]
    q = bus.subscribe(sid)
    await manager.start_run(sid, "go")
    while (await asyncio.wait_for(q.get(), 5)).type != ev.APPROVAL_REQUESTED:
        pass
    assert await manager.cancel(sid)
    types = [e.type for e in await store.events(sid)]
    assert types[-1] == ev.RUN_CANCELLED
    assert (await store.get_session(sid))["status"] == "cancelled"


def test_repair_transcript_fills_dangling_tool_calls():
    msgs = [Message("user", "hi"),
            Message("assistant", "", [ToolCall("a", "read_file", {}), ToolCall("b", "list_files", {})]),
            Message("tool", "ok", tool_call_id="a", name="read_file"),
            Message("user", "next")]
    fixed = repair_transcript(msgs)
    assert [(m.role, m.tool_call_id) for m in fixed] == [
        ("user", None), ("assistant", None), ("tool", "a"), ("tool", "b"), ("user", None)]
    assert "interrupted" in fixed[3].content
