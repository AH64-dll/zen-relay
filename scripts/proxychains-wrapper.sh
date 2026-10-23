#!/usr/bin/env bash
# Launch `opencode serve` under proxychains-ng, fed from the zen-relay pool's
# warm lanes. This preserves the real OpenCode client identity (the upstream
# free-tier gate checks it) while rotating the egress IP per upstream TCP
# connection via proxychains' `random_chain`.
#
# Requires: proxychains-ng, a running zen-relay REST pool (port 8901), and
# $OMP_ZEN_RELAY_KEY in the environment for lane access.
set -euo pipefail

RELAY="${RELAY_URL:-http://127.0.0.1:8901}"
CONF="${PC_CONF:-$HOME/.config/zen-relay/proxychains.conf}"
OPENCODE="${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}"
PORT="${OPENCODE_PORT:-49374}"

mkdir -p "$(dirname "$CONF")"

# Build the chain from the relay's warm lanes. proxychains reads this file once
# at load, so capture the freshest warm set at launch; random_chain then picks
# a different lane per connection for the life of the process.
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

if [ -s /tmp/zr-pc-list ]; then
  printf 'random_chain\nchain_len = 1\nquiet_mode\n[ProxyList]\n%s\n' "$(cat /tmp/zr-pc-list)" > "$CONF"
else
  # Pool empty: fall back to a single direct-capable entry so opencode still runs.
  printf 'strict_chain\nquiet_mode\n[ProxyList]\n' > "$CONF"
  echo "warn: no live lanes from relay; running unproxied" >&2
fi

exec proxychains4 -q -f "$CONF" "$OPENCODE" serve --service --hostname 127.0.0.1 --port "$PORT"
