#!/bin/sh
# Generates torrc at start-up (so the control password is never baked into the
# image) and execs tor in the foreground. Tor starts as root only long enough to
# bind its ports, then drops to the debian-tor user via the `User` option.
set -eu

: "${TOR_CONTROL_PASSWORD:?TOR_CONTROL_PASSWORD must be set (use letters and digits only)}"
: "${TOR_SOCKS_PORTS:=9100-9109}"
: "${TOR_CONTROL_PORT:=9051}"

case "$TOR_CONTROL_PASSWORD" in
  *[!A-Za-z0-9_-]*)
    echo "TOR_CONTROL_PASSWORD may only contain letters, digits, '-' and '_'" >&2
    exit 1 ;;
esac

start="${TOR_SOCKS_PORTS%-*}"
end="${TOR_SOCKS_PORTS#*-}"

hash="$(tor --quiet --hash-password "$TOR_CONTROL_PASSWORD" | tail -n 1)"

TORRC=/etc/tor/torrc.generated
{
  echo "User debian-tor"
  echo "DataDirectory /var/lib/tor"
  echo "Log notice stdout"
  echo "AvoidDiskWrites 1"
  echo ""
  echo "# Control port: password auth, internal network only (never published)."
  echo "ControlPort 0.0.0.0:${TOR_CONTROL_PORT}"
  echo "HashedControlPassword ${hash}"
  echo "CookieAuthentication 0"
  echo ""
  echo "# Only accept SOCKS clients from private (container-network) addresses."
  echo "SocksPolicy accept 10.0.0.0/8"
  echo "SocksPolicy accept 172.16.0.0/12"
  echo "SocksPolicy accept 192.168.0.0/16"
  echo "SocksPolicy accept 127.0.0.0/8"
  echo "SocksPolicy reject *"
  echo ""
  echo "# One listener per agent session slot (stream isolation by listener)."
  p="$start"
  while [ "$p" -le "$end" ]; do
    echo "SocksPort 0.0.0.0:${p}"
    p=$((p + 1))
  done
} > "$TORRC"

mkdir -p /var/lib/tor
chown -R debian-tor:debian-tor /var/lib/tor
chmod 700 /var/lib/tor

echo "OpenOnion Tor: SOCKS ${start}-${end}, control ${TOR_CONTROL_PORT}"
exec tor -f "$TORRC"
