#!/usr/bin/env bash
#
# Publish the relay on a public HTTPS URL with a Cloudflare quick tunnel.
#
# Why: the relay listens on loopback and speaks plain HTTP. Other devices must
# not reach port 8901 directly (no TLS, one shared key, no rate limiting), so
# the supported way to use it from a laptop/phone is an HTTPS tunnel.
#
# Quick tunnels get a NEW hostname every time they restart. Clients must be
# given the new base URL each time; for a stable hostname use a named tunnel
# (see docs/OTHER-DEVICES.md).
#
# Usage:
#   scripts/expose-relay.sh                # relay on 127.0.0.1:8901
#   PORT=9000 scripts/expose-relay.sh
#   scripts/expose-relay.sh --print-only   # print URL+key, do not tunnel
#
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PORT=${PORT:-8901}
SETTINGS=${SETTINGS_FILE:-$ROOT/zen-router/settings.json}
CLOUDFLARED=${CLOUDFLARED:-$(command -v cloudflared || true)}

key() {
  if [[ -f $SETTINGS ]]; then
    grep -oE 'rly_[0-9a-f]{32}' "$SETTINGS" | head -1 || true
  fi
}

if [[ ${1:-} == "--print-only" ]]; then
  printf 'base URL: http://127.0.0.1:%s/v1\napi key : %s\n' "$PORT" "$(key)"
  exit 0
fi

[[ -x $CLOUDFLARED ]] || { echo "cloudflared not found; install it or set CLOUDFLARED=" >&2; exit 1; }

log=$(mktemp)
trap 'rm -f "$log"' EXIT

"$CLOUDFLARED" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate >"$log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; rm -f "$log"' EXIT INT TERM

host=""
for _ in $(seq 1 60); do
  host=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$log" | head -1 || true)
  [[ -n $host ]] && break
  kill -0 "$pid" 2>/dev/null || { echo "cloudflared exited; see $log" >&2; exit 1; }
  sleep 1
done
[[ -n $host ]] || { echo "no tunnel hostname after 60s" >&2; exit 1; }

printf '\n  base URL : %s/v1\n  api key  : %s\n\n' "$host" "$(key)"
printf '  test     : curl -s %s/v1/models -H "authorization: Bearer %s"\n\n' "$host" "$(key)"
printf '  hostname changes when this script restarts — send the new URL to clients.\n'
wait "$pid"
