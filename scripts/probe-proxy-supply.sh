#!/usr/bin/env bash
#
# Measure the free-proxy supply that can actually reach the upstream.
#
# The relay's design depends on two independent things: a supply of egress
# proxies that can carry HTTPS, and a client identity the upstream accepts.
# This script measures the FIRST one only, so the two are never confused.
#
# It samples the same public feeds the relay harvests and reports, separately:
#   - socks5 proxies (they tunnel natively; the relay can use them)
#   - http  proxies (they need CONNECT; the relay can use them too)
#
# Usage:
#   scripts/probe-proxy-supply.sh [http|socks5|both] [sample-size]
#
# Default: both, 120 per kind.
#
set -uo pipefail

KIND=${1:-both}
SIZE=${2:-120}
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT

UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36'

SOCKS_FEEDS=(
  "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"
  "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"
  "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt"
  "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt"
  "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt"
)

HTTP_FEEDS=(
  "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=ipport&format=text&timeout=8000"
  "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"
  "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"
  "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"
)

fetch() {
  local -a feeds=("$@")
  for f in "${feeds[@]}"; do
    curl -s -m 20 -A "$UA" "$f" 2>/dev/null
  done | tr -d '\r' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+$' | sort -u
}

# One candidate, one HTTPS request to the upstream's model catalogue.
# A proxy is "usable" if the tunnel completes and the upstream answers.
try_one() {
  local mode=$1 addr=$2 code
  if [ "$mode" = socks5 ]; then
    code=$(curl -s -o /dev/null -m 12 --socks5-hostname "$addr" \
      -A "$UA" https://opencode.ai/zen/v1/models -w '%{http_code}' 2>/dev/null)
  else
    code=$(curl -s -o /dev/null -m 12 --proxy "http://$addr" \
      -A "$UA" https://opencode.ai/zen/v1/models -w '%{http_code}' 2>/dev/null)
  fi
  case "$code" in
    ''|000) : ;;
    *) echo "$mode $addr -> $code" ;;
  esac
}
export -f try_one

measure() {
  local mode=$1 file=$2
  local total usable
  total=$(wc -l < "$file")
  if [ "$total" -eq 0 ]; then
    printf '  %-7s: no candidates harvested\n' "$mode"
    return
  fi
  shuf -n "$SIZE" "$file" > "$file.sample"
  xargs -a "$file.sample" -P 30 -I{} bash -c 'try_one "$@"' _ "$mode" {} > "$file.res" 2>/dev/null
  usable=$(wc -l < "$file.res")
  printf '  %-7s: %s of %s sampled can carry HTTPS to opencode.ai\n' "$mode" "$usable" "$SIZE"
  sed 's/^/     /' "$file.res" | head -5
}

echo "Free-proxy supply check (HTTPS reachability to opencode.ai)"
echo

if [ "$KIND" = socks5 ] || [ "$KIND" = both ]; then
  fetch "${SOCKS_FEEDS[@]}" > "$OUT/socks5.txt"
  measure socks5 "$OUT/socks5.txt"
fi

if [ "$KIND" = http ] || [ "$KIND" = both ]; then
  fetch "${HTTP_FEEDS[@]}" > "$OUT/http.txt"
  measure http "$OUT/http.txt"
fi

cat <<'NOTE'

Note: this measures supply only. A proxy that passes here can still be
rejected upstream, because the free tier checks the client identity:
  "OpenCode's free tier can only be used from within OpenCode."
Supply being non-zero does not make the relay work.
NOTE
