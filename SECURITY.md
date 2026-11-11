# Security policy

OpenOnion routes an autonomous agent's traffic through Tor and runs tools on your machine, so we take security reports seriously.

## Reporting a vulnerability

**Please don't open a public issue.** Report privately through
[GitHub private vulnerability reporting](https://github.com/brandononchain/openonion/security/advisories/new).

Include what you found, how to reproduce it, and the impact you expect. We aim to acknowledge reports within
3 business days and to agree on a disclosure timeline with you.

## What's in scope

- **Anonymity leaks:** DNS resolved outside Tor, WebRTC/QUIC/UDP leaks, traffic bypassing the session's SOCKS listener,
  or two sessions sharing a circuit.
- **Policy bypass:** a tool running without the approval its policy requires, a model changing its own policy, or
  network-mode or allowlist bypass.
- **Sandbox escapes:** reads or writes outside the session workspace, or SSRF to private or internal hosts.
- **API issues:** authentication bypass when `OPENONION_API_TOKEN` is set, or exposure of the Tor SOCKS/control ports.
- **Prompt injection** that gets past a guard above. Injection that only changes the model's text, with no effect outside the policy, is a known limitation.

## Known limitations (not vulnerabilities)

These are documented in the [Tor threat model](docs/tor-threat-model.md): headless Chromium's fingerprint differs from
Tor Browser, the model provider sees what the agent reads and writes, clearnet tools aren't anonymous, and activity
within one session is linkable by design.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | ✅ |
