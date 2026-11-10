# Architecture

## Principles

1. **The event log is the source of truth.** Every state change is an append-only event with a gap-free
   per-session sequence number. Live views, replay, export, resumption and audits all read the same log.
2. **Explicit loop, no hidden framework.** The orchestrator is roughly 250 readable lines. Every model call,
   policy decision and tool result passes through code you can read and change.
3. **The model proposes; policy decides.** The model never sees or edits policy. Each tool call is
   schema-validated, then checked for **allow / ask / deny** before it runs.
4. **Everything is pluggable.** Providers, tools, the store and the browser sit behind small interfaces.

## Components

| Module | Responsibility |
|---|---|
| `runtime/orchestrator.py` | Think → act → observe loop, budgets, approvals, artifact capture, transcript repair |
| `runtime/manager.py` | Starts runs as background tasks; routes approvals and cancellation |
| `runtime/services.py` | Per-session resources: clearnet browser, Tor browser and Tor slot, created lazily and closed together |
| `providers/` | `ModelProvider` protocol; OpenAI-compatible and Anthropic adapters; scripted provider for tests and evals |
| `tools/` | Typed tools (pydantic `Args` → JSON schema) and the registry |
| `policy.py` | `SessionPolicy` and `PolicyEngine`: network modes, allowlists, SSRF guards, approval gates |
| `readable.py` | Browser-to-LLM bridge: main-content extraction, hidden-text removal, markdown rendering, paging |
| `browser/` | Playwright service with privacy flags; Tor port pool and control-port client |
| `memory/store.py` | SQLAlchemy async store (SQLite or Postgres): sessions, events, artifacts |
| `events.py` | Event types and the in-process fan-out bus |
| `api/app.py` | REST, SSE with replay and gap backfill, export, artifact serving, optional bearer auth |
| `web/index.html` | Dependency-free UI: timeline, plan panel, approvals, screenshots |

## Event flow

```
Orchestrator.emit(type, payload)
   ├─ Store.append_event  → durable, seq assigned under a per-session lock
   └─ EventBus.publish    → every subscriber queue (UI tabs, CLI, …)

SSE client connects with ?after=N or Last-Event-ID
   1. subscribe to the bus first (so nothing published during replay is lost)
   2. replay seq > N from the store
   3. stream live; on a seq gap (slow consumer dropped events), backfill from the store
```

`transcript` events hold the exact model-facing messages, so a follow-up run rebuilds the model context from the
log alone. `repair_transcript` fills in any tool call that a cancelled run left without a result, because
providers reject dangling tool calls.

## Prompt-injection posture

Web pages are hostile input. Defences in depth:

- Pages are reduced to their main content, and text a human can't see (CSS-hidden, off-screen, `aria-hidden`) is
  removed before the model reads it.
- Tool output is wrapped in `<tool_output trust="untrusted">`, and the system prompt forbids following instructions inside it.
- Impact is capped by policy, not by the model: network modes, allowlists, approval gates on writes and shell,
  a workspace jail, private-IP blocking that is re-checked on every redirect, and budgets.
- Approvals show the exact arguments, so a human sees what an injected instruction would actually do.

## Scaling notes

Sequence numbers are allocated in-process, so run **one API process per database**. To scale out, move runs
onto a queue (Redis/Dramatiq, Temporal), keep the log in Postgres, and let any API replica serve SSE by polling
or LISTEN/NOTIFY. This is the stateful-actor / stateless-worker split.

## Changes from the original drafts

The design follows the planning docs. These defects in the draft code were fixed along the way:

| Draft | Problem | Fix |
|---|---|---|
| Tor `Dockerfile` | `EXPOSE 9050/tcp # SOCKS5`: inline comments break the build | Comments on their own lines |
| Tor `entrypoint.sh` | Used `sudo` and `nc`, neither installed in `debian:stable-slim` | `User debian-tor` in torrc; `netcat-openbsd` installed |
| Compose | Published `9050`/`8118` to the host: an open proxy on the LAN | Internal-only networks; no published Tor ports |
| Privoxy | Unneeded (Playwright speaks SOCKS5); config used non-existent directives | Removed |
| Single SOCKS port | Every session shared circuits, so sessions were linkable | One listener per session slot |
| `ArtifactModel.metadata` | `metadata` is reserved by SQLAlchemy, so the model fails at import | Attribute `meta`, column still named `metadata` |
| `asyncio.Queue` per brain | Only one SSE viewer received each event, with no replay after reconnect | DB log plus fan-out bus with replay by sequence |
| `TorBrowseTool` | Screenshot bytes were captured but never returned; browser relaunched per call | Screenshots become artifacts; one browser per session |
| Spoofed Firefox UA on Chromium | A UA that disagrees with the engine is itself a fingerprint | Real Chrome UA with the `Headless` marker removed |
| `LLMProvider(api_key="your-key-here")` | Hardcoded secret | Environment-based config; two provider adapters |
| `Base.metadata.create_all` at import | Side effects on import; blocked tests | Explicit async `Store.init()` |
| JSON-plan prompting | Brittle with models that don't emit strict JSON; goal interpolated unescaped into prompts | Native tool calling, with the plan maintained via `update_plan` |
