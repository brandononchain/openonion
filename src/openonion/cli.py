"""Command line: `openonion serve`, `openonion run "goal"`, `openonion export <id>`, `openonion tor-check`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import Settings


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


def _print_event(e) -> None:
    p, t = e.payload, e.type
    if t == "assistant_message":
        if p.get("text"):
            print(f"\n🧅 {p['text']}")
        for tc in p.get("tool_calls", []):
            print(f"   → {tc['name']}({json.dumps(tc['arguments'])[:200]})")
    elif t == "plan_updated":
        print("\n📋 plan" + (f" — {p['note']}" if p.get("note") else ""))
        icons = {"done": "✔", "in_progress": "▶", "failed": "✖", "skipped": "–", "pending": "·"}
        for s in p["steps"]:
            print(f"   {icons.get(s['status'], '·')} {s['id']}. {s['description']}")
    elif t == "tool_finished":
        mark = "✔" if p["ok"] else "✖"
        first = (p.get("preview") or "").strip().splitlines()[:1]
        print(f"   {mark} {p['tool']} ({p['duration_ms']} ms) {first[0][:140] if first else ''}")
    elif t == "policy_denied":
        print(f"   ⛔ {p['tool']}: {p['reason']}")
    elif t == "artifact_created":
        print(f"   📎 {p['kind']} saved ({p['metadata'].get('bytes', 0)} B)")
    elif t in {"run_finished", "run_failed", "run_stopped", "run_cancelled"}:
        print(f"\n[{t}] " + json.dumps({k: v for k, v in p.items() if k != "answer"}))


async def _run(args: argparse.Namespace) -> int:
    from .events import TERMINAL_EVENTS, EventBus
    from .memory.store import Store
    from .policy import SessionPolicy
    from .runtime.manager import SessionManager

    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.database_url)
    await store.init()
    bus = EventBus()
    manager = SessionManager(store, bus, settings)
    policy = SessionPolicy(network=args.network, approval_required=[] if args.yes else SessionPolicy().approval_required)

    if args.session:
        session = await store.get_session(args.session)
        if session is None:
            print(f"no such session: {args.session}", file=sys.stderr)
            return 2
    else:
        session = await store.create_session(args.goal[:80], {"policy": policy.model_dump()})
    sid = session["id"]
    print(f"session {sid}")

    queue = bus.subscribe(sid)
    await manager.start_run(sid, args.goal)
    try:
        while True:
            event = await queue.get()
            if event.type == "approval_requested":
                p = event.payload
                answer = await asyncio.to_thread(
                    input, f"\n⚠ approve {p['tool']}({json.dumps(p['arguments'])[:300]})? [y/N] ")
                manager.resolve_approval(sid, p["approval_id"], answer.strip().lower() in {"y", "yes"})
                continue
            _print_event(event)
            if event.type in TERMINAL_EVENTS:
                if event.type == "run_finished":
                    print("\n" + (event.payload.get("answer") or ""))
                return 0 if event.type == "run_finished" else 1
    except KeyboardInterrupt:
        await manager.cancel(sid)
        return 130
    finally:
        await manager.shutdown()
        await store.close()


async def _export(args: argparse.Namespace) -> int:
    from .memory.store import Store

    settings = Settings.from_env()
    store = Store(settings.database_url)
    await store.init()
    session = await store.get_session(args.session)
    if session is None:
        print(f"no such session: {args.session}", file=sys.stderr)
        return 2
    print(json.dumps({"session": session}))
    for e in await store.events(args.session, limit=1_000_000):
        print(json.dumps(e.to_dict()))
    await store.close()
    return 0


async def _tor_check(args: argparse.Namespace) -> int:
    from .browser.tor import tor_bootstrap_progress, tor_check

    s = Settings.from_env()
    if s.tor_control_password:
        try:
            progress = await tor_bootstrap_progress(s.tor_control_host, s.tor_control_port, s.tor_control_password)
            print(f"bootstrap: {progress}%")
        except Exception as exc:
            print(f"control port: {exc}")
    port = s.tor_socks_ports[0]
    try:
        result = await tor_check(s.tor_socks_host, port)
    except ImportError:
        print("install httpx[socks] for the exit check: pip install 'httpx[socks]'")
        return 1
    except Exception as exc:
        print(f"SOCKS {s.tor_socks_host}:{port} check failed: {exc}")
        return 1
    print(f"SOCKS {s.tor_socks_host}:{port} → IsTor={result.get('IsTor')} exit IP={result.get('IP')}")
    return 0 if result.get("IsTor") else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="openonion", description="OpenOnion liberated agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the API + web UI")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    p_run = sub.add_parser("run", help="run a goal in the terminal")
    p_run.add_argument("goal")
    p_run.add_argument("--session", help="continue an existing session")
    p_run.add_argument("--network", choices=["off", "clearnet", "tor", "both"], default="both")
    p_run.add_argument("-y", "--yes", action="store_true", help="skip approval prompts (auto-approve)")

    p_exp = sub.add_parser("export", help="print a session's event log as JSON Lines")
    p_exp.add_argument("session")

    sub.add_parser("tor-check", help="verify the Tor sidecar is bootstrapped and exiting via Tor")

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        _cmd_serve(args)
    elif args.cmd == "run":
        sys.exit(asyncio.run(_run(args)))
    elif args.cmd == "export":
        sys.exit(asyncio.run(_export(args)))
    elif args.cmd == "tor-check":
        sys.exit(asyncio.run(_tor_check(args)))


if __name__ == "__main__":
    main()
