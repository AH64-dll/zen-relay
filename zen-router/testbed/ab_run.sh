#!/usr/bin/env bash
# One measured A/B run: restarts the relay clean, samples its CPU/RSS/fds while
# 10k requests hit it, and saves the client results + both sides' stats.
#   ab_run.sh <rust|python> <port> <total> <conc> [label]
set -euo pipefail
TB="$(cd "$(dirname "$0")" && pwd)"
PY=/home/ai/.omp/zen-router/.venv/bin/python
VARIANT="${1:?variant}"; PORT="${2:?port}"; TOTAL="${3:-10000}"; CONC="${4:-24}"; LABEL="${5:-$VARIANT}"
OUT="$TB/logs/ab-$LABEL.json"; CSV="$TB/logs/ab-$LABEL.csv"

# Fair protocol: the fake starts empty, then the relay alone warms up against it,
# so every lane's probe consumes exactly its PASS_FIRST window. Both relays then
# begin the run from the same fake state (1 completion per lane, no reset after).
curl -s -X POST http://127.0.0.1:8990/reset >/dev/null

"$TB/relay_testbed.sh" "$VARIANT" "$PORT"
PID=$(fuser "$PORT/tcp" 2>/dev/null | tr -d ' ')
echo "relay $VARIANT pid=$PID port=$PORT"

curl -s http://127.0.0.1:8990/stats > "$TB/logs/ab-$LABEL-fake-prewarm.json" || true
curl -s -H "Authorization: Bearer test_key" "http://127.0.0.1:$PORT/api/stats" > "$TB/logs/ab-$LABEL-stats-before.json" || true
curl -s "http://127.0.0.1:$PORT/healthz" > "$TB/logs/ab-$LABEL-health-before.json" || true

pkill -f "sample_proc.py $PID" 2>/dev/null || true
setsid "$PY" "$TB/sample_proc.py" "$PID" 900 1 > "$CSV" 2>&1 &
SAMPLER=$!
sleep 1

"$PY" "$TB/stress.py" --url "http://127.0.0.1:$PORT/v1/chat/completions" --key test_key \
  --total "$TOTAL" --conc "$CONC" --out "$OUT" --label "$LABEL"

pkill -f "sample_proc.py $PID" 2>/dev/null || true
kill "$SAMPLER" 2>/dev/null || true
curl -s -H "Authorization: Bearer test_key" "http://127.0.0.1:$PORT/api/stats" > "$TB/logs/ab-$LABEL-stats-after.json" || true
curl -s "http://127.0.0.1:$PORT/healthz" > "$TB/logs/ab-$LABEL-health-after.json" || true
curl -s http://127.0.0.1:8990/stats > "$TB/logs/ab-$LABEL-fake.json" || true
echo "saved $OUT and $CSV"
