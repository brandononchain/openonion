# 🧅 OpenOnion

**A liberated AI agent: open source, self-hosted, provider-agnostic, with per-session Tor-isolated browsing.**

OpenOnion runs a transparent think → act → observe loop. Every thought, plan revision, tool call, approval and
result is an append-only event you can watch live, replay, export, and audit. There is no hidden orchestration,
no lock-in, and no closed infrastructure: swap the model, the database, or the tools without touching the agent.

- **Provider-agnostic.** Use any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, OpenRouter, OpenAI…) or the Anthropic API.
- **Tor browsing done carefully.** Each session gets its own Tor listener, so its circuits are never shared with another session. DNS for every hostname, `.onion` included, resolves inside Tor. WebRTC and QUIC side channels are closed. Circuits rotate on demand.
- **Transparent by construction.** The event log is the source of truth. Watch it stream live over SSE, export any session as JSON Lines, and resume it with a follow-up goal.
- **Safe by architecture.** A per-session policy decides **allow / ask a human / deny** for every tool call. Filesystem access is jailed to the session workspace, SSRF protection blocks private hosts, and tool output is labelled as untrusted data.
- **Self-hostable.** A single `docker compose up` starts it, or run it straight from Python with SQLite.

## Quick start (Docker)

```bash
git clone https://github.com/YOUR_USERNAME/openonion.git && cd openonion
cp .env.example .env
#  1. pick a model provider in .env (Ollama on your host works out of the box)
#  2. set TOR_CONTROL_PASSWORD:  openssl rand -hex 24
docker compose up --build
```

Open **http://127.0.0.1:8000**. Type a goal and pick a network mode. Watch the agent plan, act, and ask before it writes files.

Check that Tor is really exiting through Tor:

```bash
docker compose exec api openonion tor-check
# bootstrap: 100%
# SOCKS tor:9100 → IsTor=True exit IP=185.x.x.x
```

## Quick start (local Python, no Docker)

```bash
pip install -e ".[browser,dev]"
playwright install chromium
export OPENONION_PROVIDER=openai OPENONION_BASE_URL=http://localhost:11434/v1 OPENONION_MODEL=qwen2.5:14b
# Optional Tor: run a local tor with `SocksPort 9050` and `ControlPort 9051` + HashedControlPassword
export TOR_SOCKS_PORTS=9050 TOR_CONTROL_PASSWORD=...

openonion serve                      # web UI + API on 127.0.0.1:8000
openonion run "Summarize the front page of the Tor Project's onion site" --network tor
```

## How it works

```
 Browser UI / CLI / your app
          │  REST + SSE (replayable by sequence number)
          ▼
 ┌─ API gateway (FastAPI) ─────────────────────────────────────────────┐
 │  SessionManager ── Orchestrator (think → act → observe)             │
 │                      ├─ Model gateway ── OpenAI-compatible │ Anthropic
 │                      ├─ Policy engine ── allow / ask human / deny    │
 │                      ├─ Tool router ──── fs · web · browser · tor · plan · shell
 │                      └─ Event log ────── Store (SQLite │ Postgres) + live EventBus
 └──────────────────────────────────────────────────────────────────────┘
          │ internal network only                 │
          ▼                                       ▼
   Tor sidecar: N isolated SOCKS listeners    Postgres
   + password-protected ControlPort
```

1. You give a session a goal. The orchestrator sends the model the goal, the full prior transcript, and the tool schemas.
2. The model keeps a visible plan with `update_plan` and acts through tool calls.
3. Each call is validated against its typed schema, then checked by the session's policy. Gated tools pause until you approve or deny them in the UI or CLI.
4. Results go back to the model wrapped in `<tool_output trust="untrusted">`. Screenshots and files become artifacts.
5. The run ends when the model answers without tools, or when a step, tool-call or token budget trips.

Design notes: [docs/architecture.md](docs/architecture.md) · Tor threat model: [docs/tor-threat-model.md](docs/tor-threat-model.md)

## Tools

| Tool | What it does | Default policy |
|---|---|---|
| `update_plan` | Create or revise the visible plan | always allowed |
| `read_file`, `list_files` | Read the session workspace | allow |
| `write_file` | Write into the session workspace | **ask** |
| `web_fetch` | Fast clearnet HTTP fetch → readable text; re-checks policy on every redirect, blocks private IPs | allow (clearnet modes) |
| `browse` | Headless Chromium on the clearnet, JS + screenshot | allow (clearnet modes) |
| `tor_browse` | Headless Chromium through this session's own Tor circuits; `.onion` supported | allow (tor modes) |
| `tor_new_identity` | `SIGNAL NEWNYM`: fresh circuits | allow (tor modes) |
| `run_shell` | Bash in the workspace. **Off** unless `OPENONION_ENABLE_SHELL=true` | **ask** |

Add your own tool by subclassing `openonion.tools.base.Tool` (a pydantic `Args` model plus `async run()`) and registering it.

## Session policy

Every session carries a policy, set when you create it. The model can't see it or change it.

```json
{
  "network": "both",                 // off | clearnet | tor | both
  "approval_required": ["write_file", "run_shell"],
  "allowed_tools": null,             // or a whitelist, e.g. ["tor_browse", "write_file"]
  "clearnet_allowlist": [],          // e.g. ["wikipedia.org"]; empty = any public host
  "tor_allowlist": [],
  "javascript": true,
  "max_steps": 25, "max_tool_calls": 60, "max_total_tokens": 400000
}
```

## API

| Method & path | Purpose |
|---|---|
| `POST /api/sessions` | Create a session `{title, policy, goal?}`; a `goal` starts the first run immediately |
| `GET /api/sessions` · `GET /api/sessions/{id}` | List / inspect (incl. `running`, `pending_approvals`) |
| `POST /api/sessions/{id}/runs` | Start a (follow-up) run `{goal}` |
| `POST /api/sessions/{id}/approvals/{approval_id}` | `{approved: true|false}` |
| `POST /api/sessions/{id}/cancel` | Stop the active run |
| `GET /api/sessions/{id}/stream?after=N` | Live SSE; resumes from `Last-Event-ID`; `follow=false` replays and closes |
| `GET /api/sessions/{id}/events?after=N` | Event log page (JSON) |
| `GET /api/sessions/{id}/export` | Full log as JSON Lines (includes the model transcript) |
| `GET /api/sessions/{id}/artifacts` · `GET /api/artifacts/{id}` | Screenshots and other outputs |

Set `OPENONION_API_TOKEN` to require `Authorization: Bearer <token>` (or `?token=` for EventSource).

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `OPENONION_PROVIDER` | `openai` | `openai` (any compatible endpoint) or `anthropic` |
| `OPENONION_MODEL` | — | Required for `openai` |
| `OPENONION_BASE_URL` | `https://api.openai.com/v1` | e.g. `http://localhost:11434/v1` for Ollama |
| `OPENONION_API_KEY` / `ANTHROPIC_API_KEY` | — | |
| `DATABASE_URL` | SQLite in the data dir | `postgresql+asyncpg://…` for Postgres |
| `OPENONION_DATA_DIR` | `./data` | Workspaces, artifacts, SQLite |
| `TOR_SOCKS_HOST` / `TOR_SOCKS_PORTS` | `127.0.0.1` / `9050` | Range like `9100-9109` = 10 concurrent isolated sessions |
| `TOR_CONTROL_HOST` / `TOR_CONTROL_PORT` / `TOR_CONTROL_PASSWORD` | `127.0.0.1` / `9051` / — | Needed for `tor_new_identity` and `tor-check` |
| `OPENONION_API_TOKEN` | — | Enables API auth |
| `OPENONION_ENABLE_SHELL` | `false` | Registers `run_shell` |

## Development

```bash
pip install -e ".[dev,browser]" && playwright install chromium
pytest -q
```

The suite covers the orchestrator loop and budgets, approvals, cancellation and transcript repair, policy and SSRF rules, the filesystem jail, both provider wire formats, and the Tor control protocol. It also covers the API and SSE replay, plus a browser test that proves hostnames are never resolved locally when browsing through Tor.

## Roadmap

- **Phase 2: subagents.** Research, coding, browser and validation subagents with handoff contracts.
- **Phase 3: skills and plugins.** `SKILL.md` task contracts, and MCP servers as tool backends.
- **Phase 4: multi-agent workflows.** Parallel DAGs, a shared artifact store, signed action receipts.
- **Hardening.** Per-session gVisor or microVM sandboxes for `run_shell`, a Tor Browser–grade fingerprint profile (Firefox + `resistFingerprinting`), and a queue-backed runner for horizontal scale.

## Responsible use

OpenOnion hides *where* the agent connects from. It doesn't make *what the agent does* legitimate. You're responsible for complying with the laws that apply to you and with the terms of the services you access. Read the [threat model](docs/tor-threat-model.md) before relying on it for anonymity.

## License

Apache-2.0
