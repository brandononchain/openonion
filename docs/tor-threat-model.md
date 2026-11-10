# Tor threat model

OpenOnion routes agent browsing through Tor. This document states plainly what that protects and what it does not.

## What you get

| Property | How |
|---|---|
| **Your IP is hidden** from sites the agent visits via `tor_browse`. | Chromium is launched with a `socks5://` proxy pointing at the Tor sidecar. |
| **No DNS leaks.** Hostnames, including `.onion`, are resolved by Tor, never by the host's resolver. | Chromium sends unresolved hostnames to SOCKS5 proxies. `tests/test_browser_privacy.py` asserts this with a SOCKS sniffer. |
| **No UDP side channels.** | `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` and `--disable-quic`. |
| **Sessions are unlinkable by circuit.** Two sessions never share an exit. | The sidecar opens one SOCKS listener per slot (`TOR_SOCKS_PORTS`). Tor never mixes streams from different listeners on one circuit, and each live session holds its own slot. |
| **Cookies and storage are isolated per session.** | One Playwright browser context per session, torn down at the end of each run. |
| **Fresh identity on demand.** | `tor_new_identity` sends `SIGNAL NEWNYM` over the password-protected ControlPort and recycles the browser so keep-alive connections don't pin old circuits. |
| **The Tor proxy isn't exposed.** | SOCKS and control ports exist only on an `internal: true` Docker network. Nothing is published to the host, and `SocksPolicy` rejects non-private clients. |

Tor logs warnings at start-up that the SOCKS and control ports listen on `0.0.0.0`. That's expected inside the container. Those ports are reachable only on the internal compose network, so the warnings do not indicate exposure.

## What you do NOT get

- **It is not Tor Browser.** Headless Chromium's fingerprint (canvas, fonts, screen, TLS and HTTP/2 behaviour, JS engine) is distinctive and differs from the crowd of Tor Browser users. A motivated site can tell this traffic apart and link visits within a session. Treat it as **IP-anonymized, not anonymous**. To harden it, set `javascript: false` in the session policy, or (on the roadmap) switch to a Firefox profile with `privacy.resistFingerprinting`.
- **Content can deanonymize.** If the goal or the agent's form input contains identifying information, Tor can't help. The model provider also sees everything the agent reads and writes. For full confidentiality, use a local model.
- **The model provider sees your traffic metadata.** Calls to a hosted LLM API go over the clearnet from the API container. Use a local model (Ollama or vLLM on your host) to keep that traffic off third-party servers.
- **Clearnet tools are not anonymous.** `web_fetch` and `browse` go out directly. Choose network mode `tor` to disable them for a session.
- **Within a session, everything is linkable**, by design: one listener and one cookie jar per session. Use separate sessions for activities that shouldn't be linked.
- **Global adversaries.** Tor doesn't defend against an observer who can watch both ends of a circuit.

## Operational checklist

1. Generate a strong control password (`openssl rand -hex 24`). The entrypoint only accepts characters in `[A-Za-z0-9_-]`.
2. Run `openonion tor-check` after start-up. It reports bootstrap progress and the exit IP, and it confirms `IsTor=True`.
3. Keep the Tor image current. The Dockerfile installs from deb.torproject.org by default rather than Debian's slower-moving package.
4. Size `TOR_SOCKS_PORTS` to the number of concurrent sessions. When all slots are taken, `tor_browse` fails with a clear error instead of sharing circuits.
