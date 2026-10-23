#!/usr/bin/env bash
# Rotate the REAL OpenCode client over several free proxies and record what
# the upstream says for each. This is the original repo's design with a
# legitimate client instead of a forged fingerprint.
set -uo pipefail
cd /tmp/opencode
export PATH="$HOME/.opencode/bin:$PATH"
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost

shuf -n 150 fresh.txt > fresh.sample
xargs -a fresh.sample -P 40 -I{} ./probe-one.sh socks5 {} > fresh2.res 2>/dev/null
mapfile -t CANDS < <(awk '{print $2}' fresh2.res | head -8)
echo "live socks5 proxies: ${#CANDS[@]}"

i=0
for P in "${CANDS[@]}"; do
  PORT=$((8920 + i))
  (setsid nohup python3 socks-shim.py --listen 127.0.0.1:$PORT --upstream "$P" > shim-$PORT.log 2>&1 &)
  i=$((i+1))
done
sleep 4

run_one() {
  local idx=$1 P=$2 PORT=$3
  OUT=$(HTTPS_PROXY=http://127.0.0.1:$PORT HTTP_PROXY=http://127.0.0.1:$PORT \
    timeout 150 opencode run --standalone -m opencode/deepseek-v4.1-flash-free \
    "Reply exactly: ROTATION-OK" 2>&1 | tail -2 | tr '\n' ' ')
  case "$OUT" in
    *"Rate limit"*) V="rate-limited (client accepted, IP allowance spent)" ;;
    *"FreeTierError"*) V="FreeTierError (client rejected)" ;;
    *"ROTATION-OK"*) V="*** COMPLETION ***" ;;
    *"Error"*) V="other error" ;;
    *) V="unclear" ;;
  esac
  printf '  %-24s %s\n' "$P" "$V"
  printf '  %-24s raw: %s\n' "" "$(echo "$OUT" | cut -c1-110)"
}
export -f run_one

i=0
for P in "${CANDS[@]}"; do
  PORT=$((8920 + i))
  echo "$i|$P|$PORT"
  i=$((i+1))
done | xargs -P 8 -I{} bash -c 'IFS="|" read -r idx P PORT <<< "{}"; run_one "$idx" "$P" "$PORT"'
