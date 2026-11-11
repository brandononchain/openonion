# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Browser-to-LLM bridge (`openonion.readable`): `web_fetch`, `browse` and `tor_browse` now return the page's main
  content as compact markdown, with nav, headers, footers, sidebars, forms, cookie banners, modals and ads removed.
  Links become `[Ln]` references with the URLs listed under the text.
- The browser removes elements hidden by CSS (display, visibility, opacity, off-screen, zero font size) before reading
  the DOM, which closes a common prompt-injection channel. Screenshots are taken first and still show the page as rendered.
- `offset` argument on all page tools: long pages arrive in 12k-character chunks, served from a per-session page
  cache so paging doesn't refetch over Tor.
- `POST /api/sessions/{id}/runs` accepts an optional `policy`, so each message can carry the composer's settings.
- Manrope is bundled with the app, so the UI makes no third-party requests.
- Settings window (gear in the sidebar, Ctrl+,) for the model, Tor, safety limits and connectors. Presets for
  Unsloth, Ollama, LM Studio, OpenAI, OpenRouter and Anthropic; loads the model list from the server; tests the
  connection and whether the model can call tools; checks Tor. Opens on first run when no model is set.
  Saved to `settings.json` (mode 600), applied live, overriding the environment. Secrets are write-only.
- Connectors: MCP servers over stdio or Streamable HTTP. Per-tool Allow / Ask first / Off, read-only tools allowed
  by default. Only the most relevant connector tools are offered per message (BM25 ranking, `OPENONION_MCP_MAX_TOOLS`),
  and `find_tools` lets the agent search for more. Connectors that reach the internet are off in Tor-only and
  Offline chats. The working card names the app ("Used add note in Notes").
- `tools_selected` event records which connector tools each run was offered.
- Docker image includes Node.js and uv so `npx` and `uvx` connectors run inside it.

### Changed
- The web UI is now a conversation instead of a control panel. You chat with the agent; when a reply needs tools,
  a working card shows the plan, each step, screenshots and a Stop button, then folds into a one-line summary.
  Approvals appear as a question in the chat. Network mode and approvals are chips on the message box, and every
  conversation keeps its own URL.
- The agent replies conversationally: greetings and simple questions get a direct answer, and it only plans when a
  task takes several tool calls. Replies are written in Markdown.
- Page tools no longer cut text off at 20,000 characters; the agent pages through it instead.

## [0.1.0] - 2026-11-10

### Added
- Transparent think → act → observe orchestrator with step, tool-call and token budgets.
- Append-only event log (SQLite or Postgres) with live SSE streaming, replay by sequence number, and JSON Lines export.
- Per-session policy engine: network modes, allowlists, SSRF guards, and allow / ask / deny approval gates.
- Provider adapters for any OpenAI-compatible endpoint and the Anthropic API, plus a scripted provider for tests.
- Tools: `update_plan`, `read_file`, `list_files`, `write_file`, `web_fetch`, `browse`, `tor_browse`, `tor_new_identity`, and opt-in `run_shell`.
- Tor sidecar with one isolated SOCKS listener per session and a password-protected ControlPort.
- Dependency-free web UI with timeline, plan panel, approvals and screenshots.
- `openonion` CLI: `serve`, `run`, `export`, `tor-check`.
- Docker Compose stack with internal-only networks.

[Unreleased]: https://github.com/brandononchain/openonion/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/brandononchain/openonion/releases/tag/v0.1.0
