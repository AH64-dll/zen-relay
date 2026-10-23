#!/usr/bin/env bash
# Launch the pure-Python REST relay (ip_relay.py) under uvicorn.
#
# Used by dsh-stack as proxy.command; also runnable by hand:
#   PORT=8905 ./run.sh
#
# The venv lives next to this script (zen-router/rest/.venv). It is created on
# first run — needs python3 + venv on PATH.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
VENV="$ROOT/.venv"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "rest: creating venv at $VENV" >&2
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$ROOT/requirements.txt"
fi

# SETTINGS_FILE/LANES_FILE/BURNED_FILE default to files beside this script;
# dsh-stack's proxy.env overrides them to share the Rust relay's settings.json.
exec "$VENV/bin/python" -m uvicorn ip_relay:app \
  --host "${HOST:-127.0.0.1}" --port "${PORT:-8901}" \
  --app-dir "$ROOT" --log-level warning
