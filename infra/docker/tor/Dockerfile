# OpenOnion Tor sidecar.
# Exposes N isolated SOCKS5 listeners (one per concurrent agent session) and a
# password-protected ControlPort, reachable ONLY on the compose-internal network.
#
# Note: Dockerfile instructions do not support trailing comments, so every
# comment lives on its own line.

FROM debian:bookworm-slim

# Use the Tor Project's own repository: Debian's tor package lags behind
# security releases. Set USE_TORPROJECT_REPO=0 to fall back to Debian's package.
ARG USE_TORPROJECT_REPO=1

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg netcat-openbsd tini; \
    if [ "$USE_TORPROJECT_REPO" = "1" ]; then \
      curl -fsSL https://deb.torproject.org/torproject.org/A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89.asc \
        | gpg --dearmor -o /usr/share/keyrings/deb.torproject.org-keyring.gpg; \
      echo "deb [signed-by=/usr/share/keyrings/deb.torproject.org-keyring.gpg] https://deb.torproject.org/torproject.org bookworm main" \
        > /etc/apt/sources.list.d/tor.list; \
      apt-get update; \
      apt-get install -y --no-install-recommends tor deb.torproject.org-keyring; \
    else \
      apt-get install -y --no-install-recommends tor; \
    fi; \
    apt-get purge -y --auto-remove curl gnupg; \
    rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh healthcheck.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.sh

# One SOCKS listener per isolation slot. Tor never shares circuits between
# streams that arrive on different listeners, so each agent session gets its
# own circuits and sessions cannot be linked to each other by exit traffic.
ENV TOR_SOCKS_PORTS=9100-9109 \
    TOR_CONTROL_PORT=9051

# Documentation only; compose does not publish these to the host.
EXPOSE 9051
EXPOSE 9100-9109

# Healthy means "bootstrapped to 100%", not merely "port is open".
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=20 \
    CMD ["/usr/local/bin/healthcheck.sh"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
