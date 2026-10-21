#!/usr/bin/env bash
#
# Hand the machine over from the legacy units to dsh-stack — safely.
#
# This is the only step that interrupts the Harness, so it is written to be
# runnable, observable and reversible:
#
#   1. record what it is about to stop;
#   2. stop the legacy units and wait for ports 30141/8901 to be released;
#   3. kill a stray quick tunnel bound to the Harness port (owned by the legacy
#      supervisor, outside dsh-stack's control);
#   4. start dsh-stack.service and wait for READY (systemd Type=notify);
#   5. verify HTTP on the Harness port, the status document, and the keeper;
#   6. on success publish the new sign-in URL, on failure stop dsh-stack and
#      put the legacy units back exactly as they were.
#
# Usage:
#   scripts/handover.sh [--dry-run] [--verbose] [--timeout 300]
#
# Run it from a terminal, or detached (`nohup scripts/handover.sh &`): it stops
# the Harness that serves the Web UI you may be typing in.
#
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DSH_STACK=(node "$ROOT/bin/dsh-stack")
LOG_FILE=${DSH_STACK_HANDOVER_LOG:-$HOME/dsh-stack-handover.log}
STATE_HOME=${XDG_STATE_HOME:-$HOME/.local/state}
STATE_DIR=${DSH_STACK_STATE_DIR:-$STATE_HOME/dsh-stack}
LEGACY_RECORD=$STATE_DIR/legacy-units.json
LEGACY_UNITS=(dsh-harness.service zen-rotator.service cloudflared-tunnel.service)
HARNESS_PORT=${DSH_STACK_PORT:-30141}
PROXY_PORT=${DSH_STACK_PROXY_PORT:-8901}
TIMEOUT=300

DRY_RUN=0
VERBOSE=0
ROLLBACK_ONLY=0
PUBLIC_TIMEOUT=${PUBLIC_TIMEOUT:-60}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --rollback) ROLLBACK_ONLY=1 ;;
    --verbose|-v) VERBOSE=1 ;;
    --timeout) TIMEOUT=${2:-300}; shift ;;
    --timeout=*) TIMEOUT=${1#*=} ;;
    -h|--help) sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
  printf '%s\n' "$line" >> "$LOG_FILE"
  if [[ $VERBOSE -eq 1 || $DRY_RUN -eq 1 ]]; then printf '%s\n' "$line"; fi
}

# Run something, or describe it in a dry run.
step() {
  if [[ $DRY_RUN -eq 1 ]]; then log "[dry-run] $*"; return 0; fi
  log "run: $*"
  "$@" >> "$LOG_FILE" 2>&1
}

port_free() {
  local port=$1
  (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && { exec 3>&-; return 1; }
  return 0
}

wait_ports_free() {
  local deadline=$((SECONDS + 30))
  while (( SECONDS < deadline )); do
    if port_free "$HARNESS_PORT" && port_free "$PROXY_PORT"; then return 0; fi
    sleep 1
  done
  return 1
}

snapshot_legacy() {
  local unit out='[]' enabled active
  for unit in "${LEGACY_UNITS[@]}"; do
    systemctl --user list-unit-files --no-legend "$unit" 2>/dev/null | grep -q . || continue
    enabled=$(systemctl --user is-enabled "$unit" 2>/dev/null | head -n 1 || true)
    active=$(systemctl --user is-active "$unit" 2>/dev/null | head -n 1 || true)
    [[ -n $enabled ]] || enabled=unknown
    [[ -n $active ]] || active=unknown
    out=$(node -e '
      const [record, unit, enabled, active] = process.argv.slice(1);
      const list = JSON.parse(record);
      list.push({ unit, enabled, active });
      console.log(JSON.stringify(list));
    ' "$out" "$unit" "$enabled" "$active")
  done
  if [[ $DRY_RUN -eq 0 ]]; then
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$out" > "$LEGACY_RECORD"
  fi
  LEGACY_JSON=$out
}

stop_legacy() {
  local unit
  for unit in "${LEGACY_UNITS[@]}"; do
    systemctl --user list-unit-files --no-legend "$unit" 2>/dev/null | grep -q . || continue
    if systemctl --user is-enabled "$unit" >/dev/null 2>&1 || systemctl --user is-active "$unit" >/dev/null 2>&1; then
      step systemctl --user disable --now "$unit"
    fi
  done
}

# The legacy supervisor started its own quick tunnel; it is not in dsh-stack's
# cgroup, so a stop of the unit may leave it bound to the port.
kill_stray_tunnel() {
  local pids
  pids=$(pgrep -f "cloudflared tunnel --url http://127.0.0.1:$HARNESS_PORT" 2>/dev/null || true)
  [[ -n $pids ]] || return 0
  log "killing stray tunnel(s) for port $HARNESS_PORT: $pids"
  if [[ $DRY_RUN -eq 0 ]]; then
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
    pids=$(pgrep -f "cloudflared tunnel --url http://127.0.0.1:$HARNESS_PORT" 2>/dev/null || true)
    # shellcheck disable=SC2086
    [[ -n $pids ]] && kill -9 $pids 2>/dev/null || true
  fi
}

status_field() {
  node -e '
    const fs = require("node:fs");
    const [file, field] = process.argv.slice(1);
    try {
      const doc = JSON.parse(fs.readFileSync(file, "utf8"));
      const value = field.split(".").reduce((acc, key) => (acc == null ? acc : acc[key]), doc);
      console.log(value === true ? "true" : value === false ? "false" : String(value ?? ""));
    } catch { console.log(""); }
  ' "$1" "$2"
}

wait_ready() {
  local status_file=$1 deadline=$((SECONDS + TIMEOUT))
  while (( SECONDS < deadline )); do
    if [[ $(status_field "$status_file" ready) == "true" ]]; then return 0; fi
    sleep 2
  done
  return 1
}

http_ok() {
  node --input-type=module -e '
    const [url, timeout] = process.argv.slice(1);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Number(timeout));
    try {
      const response = await fetch(url, { signal: controller.signal, redirect: "manual" });
      process.exit(response.status < 500 ? 0 : 1);
    } catch { process.exit(1); } finally { clearTimeout(timer); }
  ' "$1" "${2:-5000}"
}

restore_legacy_units() {
  if [[ ! -f $LEGACY_RECORD ]]; then
    log "no legacy record found; start the old units by hand"
    return 1
  fi
  local unit active
  while read -r unit enabled active; do
    [[ -n $unit ]] || continue
    [[ $enabled == "enabled" ]] && step systemctl --user enable "$unit"
  done < <(node -e '
    const list = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
    for (const item of list) console.log([item.unit, item.enabled, item.active].join(" "));
  ' "$LEGACY_RECORD")
  # Start in dependency order: proxy first, then the harness that wants it.
  for unit in "${LEGACY_UNITS[@]}"; do
    active=$(node -e '
      const list = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
      const found = list.find((item) => item.unit === process.argv[2]);
      console.log(found ? found.active : "");
    ' "$LEGACY_RECORD" "$unit")
    [[ $active == "active" ]] && step systemctl --user start "$unit"
  done
  log "legacy units restored"
  return 0
}

rollback() {
  local reason=$1
  log "FAILED: $reason"
  log "rolling back to the legacy units"
  if [[ $DRY_RUN -eq 0 ]]; then
    systemctl --user stop dsh-stack.service >> "$LOG_FILE" 2>&1 || true
  fi
  restore_legacy_units
  log "handover aborted"
  exit 1
}

log "=== handover start (root=$ROOT dryRun=$DRY_RUN timeout=${TIMEOUT}s) ==="

# Preconditions ------------------------------------------------------------
if ! systemctl --user list-unit-files --no-legend dsh-stack.service 2>/dev/null | grep -q .; then
  log "dsh-stack.service is not installed yet; run install.sh --defer first"
  exit 2
fi
if ! node -e 'process.exit(0)' 2>/dev/null; then
  log "node is not on PATH"
  exit 2
fi

# Snapshot before touching anything ---------------------------------------
snapshot_legacy
log "legacy state: $LEGACY_JSON"

CFG_FILE=$(mktemp)
if ! "${DSH_STACK[@]}" config > "$CFG_FILE" 2>>"$LOG_FILE"; then
  log "could not read the config (run install.sh first)"
  exit 2
fi
cfg_field() {
  node -e '
    const fs = require("node:fs");
    const [file, field] = process.argv.slice(1);
    let doc; try { doc = JSON.parse(fs.readFileSync(file, "utf8")); } catch { console.log(""); process.exit(0); }
    const value = field.split(".").reduce((acc, key) => (acc == null ? acc : acc[key]), doc);
    console.log(value === undefined || value === null ? "" : String(value));
  ' "$CFG_FILE" "$1"
}
state_dir=$(cfg_field stateDir)
[[ -n $state_dir ]] || state_dir=$STATE_DIR
STATUS_FILE=$state_dir/status.json
LOG_PATH=$state_dir/stack.log
[[ -n $(cfg_field harness.port) ]] && HARNESS_PORT=$(cfg_field harness.port)
proxy_port=$(node -e '
  const fs = require("node:fs");
  try {
    const doc = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    console.log(new URL(doc.proxy.healthUrl).port || "80");
  } catch { console.log(""); }
' "$CFG_FILE")
[[ -n $proxy_port ]] && PROXY_PORT=$proxy_port
log "status document: $STATUS_FILE (harness :$HARNESS_PORT, proxy :$PROXY_PORT)"

if [[ $ROLLBACK_ONLY -eq 1 ]]; then
  log "=== rollback requested: returning the machine to the legacy units ==="
  if [[ $DRY_RUN -eq 0 ]]; then
    systemctl --user disable --now dsh-stack.service >> "$LOG_FILE" 2>&1 || true
  else
    log "[dry-run] would disable --now dsh-stack.service"
  fi
  restore_legacy_units || true
  log "=== rollback complete ==="
  exit 0
fi

# 1-3. Free the resources --------------------------------------------------
stop_legacy
kill_stray_tunnel
if [[ $DRY_RUN -eq 0 ]] && ! wait_ports_free; then
  # A port still held means something outside systemd owns it; continuing would
  # only make dsh-stack fail its own readiness check for minutes.
  log "ports still busy after 30s (harness=$HARNESS_PORT proxy=$PROXY_PORT)"
  if port_free "$HARNESS_PORT"; then :; else
    log "pid holding $HARNESS_PORT: $(ss -lptn "sport = :$HARNESS_PORT" 2>/dev/null | tail -n +2 | tr -s ' ')"
  fi
  rollback "ports not released by the legacy stack"
fi

# 4. Start the supervised stack -------------------------------------------
step systemctl --user reset-failed dsh-stack.service
step systemctl --user start dsh-stack.service

if [[ $DRY_RUN -eq 1 ]]; then
  log "[dry-run] would wait for ready, verify, and publish the URL"
  log "=== handover dry run complete ==="
  exit 0
fi

if ! wait_ready "$STATUS_FILE"; then
  log "dsh-stack did not report ready within ${TIMEOUT}s"
  log "last status: $(status_field "$STATUS_FILE" phase) / $(status_field "$STATUS_FILE" fatal.reason)"
  if [[ -f $LOG_PATH ]]; then tail -n 20 "$LOG_PATH" >> "$LOG_FILE"; fi
  rollback "stack not ready"
fi
log "stack ready"

# 5. Verify the user-visible surface --------------------------------------
HARNESS_URL=$(cfg_field harness.healthUrl)
if ! http_ok "$HARNESS_URL" 10000; then
  rollback "harness at $HARNESS_URL did not answer"
fi
log "harness answers at $HARNESS_URL"

sign_in=$(head -n 1 "$HOME/dsh-url.txt" 2>/dev/null || true)
tunnel=$(status_field "$STATUS_FILE" tunnel.host)
log "tunnel host: ${tunnel:-none}"
log "sign-in url: ${sign_in:-not published yet}"

public_warn=0
if [[ -n ${tunnel:-} ]]; then
  log "verifying the public URL https://$tunnel/ (through the tunnel) ..."
  public_ok=0
  deadline=$((SECONDS + PUBLIC_TIMEOUT))
  while (( SECONDS < deadline )); do
    if http_ok "https://$tunnel/" 15000; then public_ok=1; break; fi
    sleep 3
  done
  if [[ $public_ok -eq 1 ]]; then
    log "public URL answers"
  else
    public_warn=1
    log "WARNING: https://$tunnel/ did not answer within ${PUBLIC_TIMEOUT}s"
    log "         the stack is healthy locally; the tunnel may still be propagating"
  fi
fi

keeper_status="$HOME/.dsh/session-keeper/status.json"
if [[ -f $keeper_status ]]; then
  log "keeper: phase=$(status_field "$keeper_status" summary.phase) candidates=$(status_field "$keeper_status" summary.candidates) resumed=$(status_field "$keeper_status" summary.resumed)"
else
  log "keeper: no status document (plugin not installed or not yet run)"
fi

log "=== handover complete ==="
printf '\n  The Web UI is now served by dsh-stack.\n'
if [[ -n $sign_in ]]; then printf '  New sign-in URL: %s\n' "$sign_in"; fi
if [[ $public_warn -eq 1 ]]; then
  printf '\n  WARNING: the public URL is not answering yet. If you reach this machine\n'
  printf '  only through the tunnel, wait a minute and retry, or revert with:\n'
  printf '    %s --rollback\n' "$0"
fi
printf '\n' 
exit 0
