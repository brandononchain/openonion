# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
