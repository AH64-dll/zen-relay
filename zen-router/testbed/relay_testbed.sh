#!/usr/bin/env bash
# Starts one relay against the testbed: relay_testbed.sh <rust|python> <port>
# State files live under testbed/state/, logs under testbed/logs/.
set -euo pipefail
TB="$(cd "$(dirname "$0")" && pwd)"
VARIANT="${1:?usage: relay_testbed.sh <rust|python> <port>}"
PORT="${2:?usage: relay_testbed.sh <rust|python> <port>}"
KEY=test_key

mkdir -p "$TB/state" "$TB/logs"
fuser -k -TERM "${PORT}/tcp" >/dev/null 2>&1 || true
sleep 0.5

export PORT
export SETTINGS_FILE="$TB/settings.json"
export LANES_FILE="$TB/state/$VARIANT-lanes.json"
export BURNED_FILE="$TB/state/$VARIANT-burned.json"
rm -f "$LANES_FILE" "$BURNED_FILE"

case "$VARIANT" in
  rust)
    BIN=/home/ai/.omp/zen-router/rust/target/release/zen-relay
    setsid nohup "$BIN" >"$TB/logs/$VARIANT.log" 2>&1 &
    ;;
  python)
    PY=/home/ai/.omp/zen-router/.venv/bin/python
    cd /home/ai/.omp/zen-router/legacy-python
    setsid nohup "$PY" -c "from ip_relay import app; import uvicorn; uvicorn.run(app, host='127.0.0.1', port=$PORT, log_level='warning')" \
      >"$TB/logs/$VARIANT.log" 2>&1 &
    ;;
  *)
    echo "unknown variant $VARIANT" >&2
    exit 2
    ;;
esac

# wait for health, then seed the lanes and wait for warmth
for i in $(seq 1 60); do
  if curl -s --max-time 1 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.5
done

LANES=$(python3 - <<'PY'
print(",".join(f'"127.0.0.1:{p}"' for p in range(9100, 9122)))
PY
)
curl -s --max-time 5 -X POST "http://127.0.0.1:$PORT/api/pool/seed" \
  -H "Authorization: Bearer $KEY" -H "content-type: application/json" \
  -d "{\"lanes\": [$LANES], \"proto\": \"http\"}" >/dev/null

for i in $(seq 1 120); do
  H=$(curl -s --max-time 1 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || true)
  W=$(printf '%s' "$H" | python3 -c "import sys,json;print(json.load(sys.stdin).get('warm',0))" 2>/dev/null || echo 0)
  if [ "${W:-0}" -ge 12 ]; then
    echo "$VARIANT relay on $PORT: warm=$W"
    exit 0
  fi
  sleep 0.5
done
echo "$VARIANT relay on $PORT: warm never reached 12 (last=$W)" >&2
exit 1
