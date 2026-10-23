#!/usr/bin/env bash
# Launch `opencode serve` under proxychains-ng, fed from the zen-relay pool's
# warm + parked lanes. This preserves the real OpenCode client identity (the
# upstream free-tier gate checks it) while rotating the egress IP across *all*
# proxies via proxychains `random_chain` + `chain_len 1`: every new upstream
# TCP connection picks a different lane, so the switcher sweeps the whole pool.
# Per-connection response wait is 7 s (tcp_*_time_out) so a dead lane is
# abandoned fast and the next connect picks another.
#
# Requires: proxychains-ng, a running zen-relay REST pool (port 8901), and
# $OMP_ZEN_RELAY_KEY in the environment for lane access.
set -euo pipefail

RELAY="${RELAY_URL:-http://127.0.0.1:8901}"
CONF="${PC_CONF:-$HOME/.config/zen-relay/proxychains.conf}"
OPENCODE="${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}"
PORT="${OPENCODE_PORT:-49374}"

mkdir -p "$(dirname "$CONF")"

# Build the chain list from the relay's live lanes (warm first, then parked —
# a parked lane may still answer a CONNECT). proxychains reads the conf once at
# load, so we capture the freshest set at launch.
build_list() {
  curl -fsS -m 5 -H "Authorization: Bearer ${OMP_ZEN_RELAY_KEY:-}" "$RELAY/api/pool" 2>/dev/null \
    | python3 -c '
import json,sys
d=json.load(sys.stdin)
seen=set(); lines=[]
for l in d.get("warm",[])+d.get("parked",[]):
    if l.get("proto")=="direct": continue
    p={"http":"http","https":"http","socks4":"socks4","socks5":"socks5"}.get(l["proto"])
    if not p: continue
    host,port=l["addr"].rsplit(":",1)
    if (host,port) in seen: continue
    seen.add((host,port))
    lines.append(f"{p} {host} {port}")
print("\n".join(lines))
' > /tmp/zr-pc-list 2>/dev/null || true
}

# The relay warms its pool over ~60 s after a restart; retry so we never exec
# with an empty chain (which would drop every proxied connection).
for _ in $(seq 1 24); do
  build_list
  [ -s /tmp/zr-pc-list ] && break
  sleep 5
done

if [ -s /tmp/zr-pc-list ]; then
  printf 'random_chain\nchain_len = 1\ntcp_connect_time_out 7000\ntcp_read_time_out 7000\nquiet_mode\n[ProxyList]\n%s\n' "$(cat /tmp/zr-pc-list)" > "$CONF"
else
  # No lanes at all after the retries: run direct so opencode still serves.
  printf 'strict_chain\nquiet_mode\n[ProxyList]\n' > "$CONF"
  echo "warn: no live lanes from relay; running unproxied" >&2
fi

exec proxychains4 -q -f "$CONF" "$OPENCODE" serve --service --hostname 127.0.0.1 --port "$PORT"
