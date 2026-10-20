#!/usr/bin/env bash
# Boots the hermetic relay testbed on loopback:
#   * one TLS fake upstream (8999) with a control port (8990)
#   * 22 lane proxies (9100-9121): each looks like one egress IP to a relay
# No internet traffic is involved; the relays are pointed at 127.0.0.1:8999.
set -euo pipefail
TB="$(cd "$(dirname "$0")" && pwd)"
PY=/home/ai/.omp/zen-router/.venv/bin/python
mkdir -p "$TB/logs"

if [ ! -f "$TB/cert.pem" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$TB/key.pem" -out "$TB/cert.pem" \
    -days 60 -subj "/CN=opencode.ai" -addext "subjectAltName=DNS:opencode.ai" >/dev/null 2>&1
fi

stop_port() { fuser -k -TERM "${1}/tcp" >/dev/null 2>&1 || true; }
stop_port 8999; stop_port 8990
for p in $(seq 9100 9121); do stop_port "$p"; done
sleep 1

# Trap plan: 15 clean lanes, 3 that cut mid-stream, 2 that answer empty,
# 1 that is out of quota (403), 1 that is rate limited (429), 1 clean.
TRAP_PLAN="${TRAP_PLAN:-9100=ok,9101=ok,9102=ok,9103=ok,9104=ok,9105=ok,9106=ok,9107=ok,9108=ok,9109=ok,9110=ok,9111=ok,9112=ok,9113=ok,9114=cut,9115=cut,9116=cut,9117=empty,9118=empty,9119=403,9120=429,9121=ok}"

FAKE_PORT=8999 CONTROL_PORT=8990 PASS_FIRST="${PASS_FIRST:-1}" JITTER="${JITTER:-0.03}" \
  TRAP_PLAN="$TRAP_PLAN" FAKE_LOG="$TB/logs/fake.log" \
  setsid nohup "$PY" "$TB/fake_zen.py" >"$TB/logs/fake.out" 2>&1 &

for p in $(seq 9100 9121); do
  PROXY_PORT="$p" FAKE_HOST=127.0.0.1 FAKE_PORT=8999 \
    setsid nohup "$PY" "$TB/mini_proxy.py" >"$TB/logs/proxy-$p.out" 2>&1 &
done

for i in $(seq 1 40); do
  if curl -s --max-time 1 -X POST "http://127.0.0.1:8990/reset" >/dev/null 2>&1; then
    echo "testbed up: fake 8999 (control 8990), lanes 9100-9121"
    echo "trap plan: $TRAP_PLAN"
    exit 0
  fi
  sleep 0.25
done
echo "testbed did not come up" >&2
exit 1
