#!/usr/bin/env bash
# Staging launcher for the Rust rewrite of the Zen relay.
# Runs the Rust binary on port 8904 with its own copy of the production state,
# so production (Python on 8901) stays untouched until the switch.
set -euo pipefail

ROOT=/home/ai/.omp/zen-router
STAGE=$ROOT/staging
BIN=$ROOT/rust/target/release/zen-relay

PORT="${PORT:-8904}"
export PORT
export SETTINGS_FILE="$STAGE/settings.json"
export LANES_FILE="$STAGE/lanes.json"
export BURNED_FILE="$STAGE/burned.json"
export IP_RELAY_NO_BACKGROUND="${IP_RELAY_NO_BACKGROUND:-}"

mkdir -p "$STAGE"
exec "$BIN" >> "$STAGE/relay.log" 2>&1
