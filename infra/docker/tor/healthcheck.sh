#!/bin/sh
# Healthy only once Tor reports it has fully bootstrapped (circuits can be built).
set -eu
printf 'AUTHENTICATE "%s"\r\nGETINFO status/bootstrap-phase\r\nQUIT\r\n' "$TOR_CONTROL_PASSWORD" \
  | nc -w 5 127.0.0.1 "${TOR_CONTROL_PORT:-9051}" \
  | grep -q 'PROGRESS=100'
