# Contributing to OpenOnion

Thanks for helping build OpenOnion. This guide covers how to get set up and what we look for in a pull request.

## Ground rules

- Be kind. Everyone here follows the [Code of Conduct](CODE_OF_CONDUCT.md).
- **Security issues go to [SECURITY.md](SECURITY.md)**, not public issues. That especially includes anything that could
  leak a user's IP, DNS lookups, or traffic outside Tor.
- For larger changes, open an issue first so we can agree on the approach before you write code.

## Development setup

```bash
git clone https://github.com/brandononchain/openonion.git && cd openonion
python -m venv .venv && source .venv/bin/activate
make install        # pip install -e ".[dev,browser]" && playwright install chromium
make test           # pytest -q
```

You don't need a model or Tor running to run the tests. They use a scripted provider and fake Tor endpoints.
To try the full stack, use `make up` (Docker Compose).

## Making a change

1. Branch from `main`.
2. Keep the change focused. One fix or feature per pull request.
3. Add or update tests. The suite should stay fast and must not need network access.
4. Update docs (`README.md`, `docs/`) and add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md).
5. Run `make test` and open a pull request using the template.

## Design principles to keep

These come from [docs/architecture.md](docs/architecture.md). Pull requests should preserve them:

1. **The event log is the source of truth.** New state changes become events.
2. **Explicit loop, no hidden framework.** Prefer readable code over clever abstractions.
3. **The model proposes; policy decides.** Never let model output change policy.
4. **Everything is pluggable.** Providers, tools, store and browser stay behind small interfaces.

## Adding a tool

Subclass `openonion.tools.base.Tool` with a pydantic `Args` model and an `async run()`, register it, and decide its
default policy (allow / ask / deny) and which network modes it needs. Tool output goes back to the model as untrusted
data, so never treat it as instructions.

## Adding a model provider

Implement the `ModelProvider` protocol in `openonion/providers/base.py`, add a wire-format test next to the existing
ones in `tests/test_tor_and_providers.py`, and wire it into `build_provider`.

## Commit messages

Use short, imperative subject lines (`Add Firefox fingerprint profile`, `Fix SSE gap backfill`). Explain the *why* in the body when it isn't obvious.
