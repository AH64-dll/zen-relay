#!/usr/bin/env bash
#
# Install (or remove) the DSH stack supervisor as a systemd *user* unit.
#
# What it does:
#   1. discovers the node, dsh, zen-relay and cloudflared binaries on this machine;
#   2. writes ~/.config/dsh-stack/config.json (a readable, diffable copy of the
#      effective configuration, with discovered paths filled in);
#   3. backs up and disables the legacy units that would otherwise fight this
#      supervisor for the same port / tunnel (dsh-harness, zen-rotator,
#      cloudflared-tunnel), recording their prior enabled/active state;
#   4. installs systemd/dsh-stack.service (Type=notify, WatchdogSec=90),
#      enables it for the next boot and starts it now.
#
# IMPORTANT: step 3 stops the Harness you may be talking through right now.
# Run this from a terminal (or an SSH session), not from inside the Web UI.
#
# Usage:
#   ./install.sh [--dry-run] [--no-start] [--keep-legacy] [--no-tunnel]
#                [--no-bridge] [--force] [--wait] [--uninstall [--purge]]
#
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# In a checkout the relay sits next to stack/; resolve it so a fresh machine
# never inherits the historical ~/.omp path from config.mjs defaults.
PROXY_DIR=$(cd "$ROOT/../zen-router" 2>/dev/null && pwd || true)
CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
STATE_HOME=${XDG_STATE_HOME:-$HOME/.local/state}
CONFIG_DIR=$CONFIG_HOME/dsh-stack
CONFIG_FILE=$CONFIG_DIR/config.json
UNIT_DIR=$CONFIG_HOME/systemd/user
UNIT_FILE=$UNIT_DIR/dsh-stack.service
STATE_DIR=${DSH_STACK_STATE_DIR:-$STATE_HOME/dsh-stack}
LEGACY_RECORD=$STATE_DIR/legacy-units.json
LEGACY_UNITS=(dsh-harness.service zen-rotator.service zen-relay.service cloudflared-tunnel.service)

DRY_RUN=0
KEEP_LEGACY=0
START=1
TUNNEL=own
BRIDGE=1
FORCE=0
WAIT=0
UNINSTALL=0
PURGE=0

# ----------------------------------------------------------------- output

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_STEP=$'\033[36m'
else
  C_RESET=''; C_DIM=''; C_BOLD=''; C_OK=''; C_WARN=''; C_ERR=''; C_STEP=''
fi

step() { printf '%s==>%s %s%s%s\n' "$C_STEP" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
say()  { printf '    %s\n' "$*"; }
dim()  { printf '    %s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }
ok()   { printf '    %sok%s %s\n' "$C_OK" "$C_RESET" "$*"; }
warn() { printf '    %swarn%s %s\n' "$C_WARN" "$C_RESET" "$*" >&2; }
die()  { printf '%sfatal%s %s\n' "$C_ERR" "$C_RESET" "$*" >&2; exit 1; }

usage() {
  sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

# Run a command, or print it when dry-running.
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    %s[dry-run]%s %s\n' "$C_DIM" "$C_RESET" "$*"
    return 0
  fi
  "$@"
}

# ----------------------------------------------------------------- arguments

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-start) START=0 ;;
    --keep-legacy) KEEP_LEGACY=1 ;;
    --no-tunnel) TUNNEL=off ;;
    --no-bridge) BRIDGE=0 ;;
    --force) FORCE=1 ;;
    --wait) WAIT=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

# --------------------------------------------------------------- preflight

preflight() {
  step "preflight"
  [[ ${EUID:-$(id -u)} -ne 0 ]] || die "run as your own user, not root: this installs a systemd --user unit"
  [[ -n ${XDG_RUNTIME_DIR:-} ]] || die "XDG_RUNTIME_DIR is not set; log in normally (or export it) before installing a user unit"
  command -v systemctl >/dev/null || die "systemctl not found; this installer targets systemd"
  command -v node >/dev/null || die "node not found on PATH; put the Harness's node on PATH first"

  NODE_BIN=$(command -v node)
  NODE_REAL=$(readlink -f "$NODE_BIN" 2>/dev/null || printf '%s' "$NODE_BIN")
  NODE_VERSION=$(node -p 'process.versions.node')
  say "node        $NODE_BIN ($NODE_VERSION)"
  node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)' \
    || die "node >= 22 required (found $NODE_VERSION)"

  DSH_BIN=$(command -v dsh || true)
  if [[ -z $DSH_BIN ]]; then
    for candidate in "$(dirname "$NODE_REAL")/dsh" "$HOME/.local/node24"/*/bin/dsh "$HOME/.local/bin/dsh"; do
      [[ -x $candidate ]] && { DSH_BIN=$candidate; break; }
    done
  fi
  [[ -n $DSH_BIN ]] || die "the 'dsh' binary was not found; install the Harness or add it to PATH"
  say "harness     $DSH_BIN"

  RELAY_BIN=$(command -v zen-relay || true)
  if [[ -z $RELAY_BIN ]]; then
    for candidate in \
      "$ROOT/../zen-router/bin/zen-relay" \
      "$ROOT/../zen-router/rust/target/release/zen-relay" \
      "$HOME/.omp/zen-router/rust/target/release/zen-relay"; do
      [[ -x $candidate ]] && { RELAY_BIN=$candidate; break; }
    done
  fi
  [[ -n $RELAY_BIN ]] && say "proxy       $RELAY_BIN" || warn "zen-relay not found; set proxy.command in $CONFIG_FILE or use proxy.mode=external"

  CLOUDFLARED_BIN=$(command -v cloudflared || true)
  [[ -n $CLOUDFLARED_BIN ]] && say "cloudflared $CLOUDFLARED_BIN" || warn "cloudflared not found; the public URL will be skipped"

  # The bridge puts the Harness in front of a local OpenCode server, so the
  # models OpenCode is allowed to use reach the Harness as well.
  OPENCODE_BIN=$(command -v opencode || true)
  if [[ -z $OPENCODE_BIN ]]; then
    for candidate in "$HOME/.opencode/bin/opencode" "$HOME/.local/bin/opencode"; do
      [[ -x $candidate ]] && { OPENCODE_BIN=$candidate; break; }
    done
  fi
  if [[ $BRIDGE -eq 1 ]]; then
    [[ -n $OPENCODE_BIN ]] || die "opencode not found; install it or pass --no-bridge to use the relay route only"
    OPENCODE_URL=$("$OPENCODE_BIN" service status 2>/dev/null | grep -oE 'https?://[^ ]+' | head -1)
    [[ -n $OPENCODE_URL ]] || OPENCODE_URL=http://127.0.0.1:49374
    say "opencode    $OPENCODE_BIN ($OPENCODE_URL)"
    BRIDGE_MODE=own
  else
    BRIDGE_MODE=off
  fi

  if command -v loginctl >/dev/null; then
    linger=$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)
    if [[ $linger != "yes" ]]; then
      warn "Linger is not enabled for $(id -un); the stack will not start at boot before login."
      warn "Enable it once with:  sudo loginctl enable-linger $(id -un)"
    else
      say "linger      enabled (boot-time start works without a login)"
    fi
  fi
}

# ------------------------------------------------------------------- config

write_config() {
  step "configuration"
  local existed=0
  [[ -f $CONFIG_FILE ]] && existed=1

  if [[ $existed -eq 1 && $FORCE -eq 0 && $DRY_RUN -eq 0 ]]; then
    local backup="$CONFIG_FILE.bak.$(date +%Y%m%d%H%M%S)"
    cp -p "$CONFIG_FILE" "$backup"
    say "existing config backed up to $backup"
  fi

  run mkdir -p "$CONFIG_DIR" "$STATE_DIR"

  if [[ $DRY_RUN -eq 1 ]]; then
    say "[dry-run] would write $CONFIG_FILE from discovered paths:"
    dim "harness.command = $DSH_BIN"
    dim "proxy.command   = ${RELAY_BIN:-<unset>}"
    dim "proxy.cwd       = ${PROXY_DIR:-<default>}"
    dim "tunnel.command  = ${CLOUDFLARED_BIN:-<unset>} tunnel --url http://127.0.0.1:30141 --no-autoupdate"
    dim "tunnel.mode     = $TUNNEL"
    dim "bridge.mode     = $BRIDGE_MODE"
    dim "bridge.opencode = ${OPENCODE_URL:-<unset>}"
    return 0
  fi

  ROOT="$ROOT" CONFIG_FILE="$CONFIG_FILE" DSH_BIN="$DSH_BIN" RELAY_BIN="${RELAY_BIN:-}" \
  PROXY_DIR="${PROXY_DIR:-}" CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-}" TUNNEL_MODE="$TUNNEL" \
  BRIDGE_MODE="$BRIDGE_MODE" OPENCODE_BIN="${OPENCODE_BIN:-}" OPENCODE_URL="${OPENCODE_URL:-}" \
  node --input-type=module -e '
    import fs from "node:fs";
    const { defaults, merge, toInstallDocument } = await import(
      new URL("lib/config.mjs", "file://" + process.env.ROOT + "/").href
    );

    const file = process.env.CONFIG_FILE;
    let existing = {};
    try { existing = JSON.parse(fs.readFileSync(file, "utf8")); } catch { existing = {}; }
    if (existing === null || typeof existing !== "object" || Array.isArray(existing)) existing = {};

    // Discovered paths are authoritative for binaries; everything else in an
    // existing file wins over the computed default.
    const base = toInstallDocument(defaults());
    base.installDir = process.env.ROOT;
    base.harness.command = process.env.DSH_BIN;
    if (process.env.RELAY_BIN) base.proxy.command = [process.env.RELAY_BIN];
    if (process.env.CLOUDFLARED_BIN) base.tunnel.command = [process.env.CLOUDFLARED_BIN, "tunnel", "--url", "http://127.0.0.1:30141", "--no-autoupdate"];
    base.tunnel.mode = process.env.TUNNEL_MODE;

    const next = merge(base, existing);
    const executable = (value) => {
      try { fs.accessSync(value, fs.constants.X_OK); return true; } catch { return false; }
    };
    // A stale path from an earlier install (old node version, moved checkout)
    // must not survive silently: fall back to what is actually installed now.
    if (!executable(String(next.harness.command ?? ""))) {
      console.log("    note: harness.command is not executable; using " + process.env.DSH_BIN);
      next.harness.command = process.env.DSH_BIN;
    }
    if (process.env.RELAY_BIN && !executable(String((next.proxy.command ?? [])[0] ?? ""))) {
      console.log("    note: proxy.command is not executable; using " + process.env.RELAY_BIN);
      next.proxy.command = [process.env.RELAY_BIN];
    }
    next.installDir = process.env.ROOT;
    // The relay and its state belong to this checkout. Force the paths (an
    // older config must not keep pointing at the historical ~/.omp location,
    // which usually does not exist and makes spawn fail with ENOENT).
    if (process.env.PROXY_DIR) {
      const relayDir = process.env.PROXY_DIR;
      if (next.proxy.cwd !== relayDir) {
        console.log("    note: proxy.cwd " + next.proxy.cwd + " -> " + relayDir);
      }
      next.proxy.cwd = relayDir;
      next.proxy.env = {
        ...(next.proxy.env ?? {}),
        SETTINGS_FILE: relayDir + "/settings.json",
        LANES_FILE: relayDir + "/lanes.json",
        BURNED_FILE: relayDir + "/burned.json",
      };
    }
    // An explicit --no-tunnel must win over an older config file.
    next.tunnel.mode = process.env.TUNNEL_MODE;
    // The bridge is the OpenAI-compatible front end for a local OpenCode
    // server. Its mode is decided by the installer, not by an old config.
    next.bridge = {
      ...(next.bridge ?? {}),
      mode: process.env.BRIDGE_MODE,
    };
    if (process.env.BRIDGE_MODE !== "off") {
      next.bridge.env = {
        ...(next.bridge.env ?? {}),
        OPENCODE_BASE_URL: process.env.OPENCODE_URL,
        OPENCODE_BIN: process.env.OPENCODE_BIN,
      };
    }
    next.configFile = undefined;
    next.$comment = "dsh-stack supervisor configuration. Edit freely; `dsh-stack doctor` validates it.";

    fs.mkdirSync(file.slice(0, file.lastIndexOf("/")), { recursive: true, mode: 0o700 });
    fs.writeFileSync(file, JSON.stringify(next, null, 2) + "\n", { mode: 0o600 });
    console.log("    wrote " + file);
  '
}

# ------------------------------------------------------------- legacy units

legacy_snapshot() {
  local out='[]'
  local unit enabled active existed
  for unit in "${LEGACY_UNITS[@]}"; do
    existed=0
    if systemctl --user list-unit-files --no-legend "$unit" 2>/dev/null | grep -q .; then existed=1; fi
    [[ -f $UNIT_DIR/$unit ]] && existed=1
    [[ $existed -eq 1 ]] || continue
    # is-enabled/is-active exit non-zero for disabled/inactive units, so take
    # the first line of stdout and default it explicitly.
    enabled=$(systemctl --user is-enabled "$unit" 2>/dev/null | head -n 1 || true)
    [[ -n $enabled ]] || enabled=unknown
    active=$(systemctl --user is-active "$unit" 2>/dev/null | head -n 1 || true)
    [[ -n $active ]] || active=unknown
    printf '    %-28s enabled=%-10s active=%s\n' "$unit" "$enabled" "$active"
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
  [[ $KEEP_LEGACY -eq 1 ]] && { warn "--keep-legacy: leaving the legacy units running (they will fight this supervisor for the port)"; return 0; }
  step "disabling legacy units (this stops the currently running Harness)"
  legacy_snapshot
  local count
  count=$(node -e 'console.log(JSON.parse(process.argv[1]).length)' "${LEGACY_JSON:-[]}")
  if [[ $count -eq 0 ]]; then
    say "no legacy units installed"
    return 0
  fi
  local unit
  for unit in "${LEGACY_UNITS[@]}"; do
    systemctl --user list-unit-files --no-legend "$unit" 2>/dev/null | grep -q . || continue
    if systemctl --user is-enabled "$unit" >/dev/null 2>&1 || systemctl --user is-active "$unit" >/dev/null 2>&1; then
      say "disabling $unit"
      run systemctl --user disable --now "$unit" || warn "could not disable $unit (continuing)"
    fi
  done
  say "prior state recorded in $LEGACY_RECORD (restored by --uninstall)"
}

restore_legacy() {
  [[ -f $LEGACY_RECORD ]] || { say "no legacy record to restore"; return 0; }
  step "restoring legacy units"
  local unit enabled active
  while read -r unit enabled active; do
    [[ -n $unit ]] || continue
    case "$enabled" in
      enabled) run systemctl --user enable "$unit" || warn "could not re-enable $unit" ;;
      *) : ;;
    esac
    case "$active" in
      active) run systemctl --user start "$unit" || warn "could not start $unit" ;;
      *) : ;;
    esac
    say "$unit restored (enabled=$enabled active=$active)"
  done < <(node -e '
    const list = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
    for (const item of list) console.log([item.unit, item.enabled, item.active].join(" "));
  ' "$LEGACY_RECORD")
  run rm -f "$LEGACY_RECORD"
}

# -------------------------------------------------------------------- unit

install_unit() {
  step "systemd user unit"
  run mkdir -p "$UNIT_DIR"

  if [[ $DRY_RUN -eq 1 ]]; then
    say "[dry-run] would install $ROOT/systemd/dsh-stack.service -> $UNIT_FILE"
    dim "ExecStart=/usr/bin/env node $ROOT/bin/dsh-stack run"
    return 0
  fi

  # Install the shipped unit, anchoring every %h-relative path to this checkout
  # so an install from ~/Music/dsh-stack and one from /opt both work.
  local escaped escaped_node node_dir
  escaped=$(printf '%s' "$ROOT" | sed -e 's/[&|\\]/\\&/g')
  # The shipped PATH pins one node build; substitute the node this checkout was
  # installed with (following a symlink), otherwise `env node` cannot be found
  # on a machine whose node lives anywhere else.
  node_dir=$(dirname "$(readlink -f "$NODE_BIN" 2>/dev/null || printf '%s' "$NODE_BIN")")
  escaped_node=$(printf '%s' "$node_dir" | sed -e 's/[&|\\]/\\&/g')
  sed -e "s|%h/Music/dsh-stack|$escaped|g" \
      -e "s|/home/[[:alnum:]_.-]*/Music/dsh-stack|$escaped|g" \
      -e "s|%h/.local/node24/node-v24.22.0-linux-x64/bin|$escaped_node|g" \
      -e "s|/home/[[:alnum:]_.-]*/.local/node24/node-v24.22.0-linux-x64/bin|$escaped_node|g" \
      "$ROOT/systemd/dsh-stack.service" > "$UNIT_FILE"
  chmod 0644 "$UNIT_FILE"
  say "installed $UNIT_FILE"

  run systemctl --user daemon-reload
  run systemctl --user enable dsh-stack.service
}

start_unit() {
  step "start"
  if [[ $START -eq 0 ]]; then
    say "installed and enabled for the next boot (--no-start); start now with: systemctl --user start dsh-stack.service"
    return 0
  fi
  run systemctl --user restart dsh-stack.service || warn "the unit did not start cleanly; see: journalctl --user -u dsh-stack -n 50"
  if [[ $DRY_RUN -eq 1 ]]; then return 0; fi

  say "waiting for the stack to report ready..."
  local deadline=$((SECONDS + 120))
  while (( SECONDS < deadline )); do
    if node "$ROOT/bin/dsh-stack" status --json >/dev/null 2>&1; then
      local ready
      ready=$(node "$ROOT/bin/dsh-stack" status --json 2>/dev/null | node -e '
        let data = "";
        process.stdin.on("data", (chunk) => (data += chunk));
        process.stdin.on("end", () => {
          try { const doc = JSON.parse(data); console.log(doc.ready === true ? "yes" : "no"); }
          catch { console.log("no"); }
        });
      ')
      [[ $ready == "yes" ]] && break
    fi
    sleep 2
  done

  node "$ROOT/bin/dsh-stack" status || warn "status not available yet; check: journalctl --user -u dsh-stack -n 50"

  if [[ $WAIT -eq 1 ]]; then
    step "waiting for proxy + harness readiness (up to 5 minutes)"
    node "$ROOT/bin/dsh-stack" wait --target all --timeout 300000 || warn "components did not all report ready in time"
  fi
}

# --------------------------------------------------------------- uninstall

do_uninstall() {
  step "uninstalling"
  if systemctl --user list-unit-files --no-legend dsh-stack.service 2>/dev/null | grep -q .; then
    run systemctl --user disable --now dsh-stack.service || warn "could not stop dsh-stack.service"
  fi
  run rm -f "$UNIT_FILE"
  run systemctl --user daemon-reload
  restore_legacy
  if [[ $PURGE -eq 1 ]]; then
    step "purging state"
    run rm -rf "$STATE_DIR"
    say "removed $STATE_DIR"
    dim "the plugin's own state ($HOME/.dsh/session-keeper) and the config were kept"
  else
    say "kept $STATE_DIR (use --purge to remove) and $CONFIG_FILE"
  fi
  ok "uninstalled"
}

# -------------------------------------------------------------------- main

main() {
  printf '%sDSH stack supervisor%s  %s\n' "$C_BOLD" "$C_RESET" "$ROOT"
  [[ $DRY_RUN -eq 1 ]] && warn "dry run: nothing will be changed"
  preflight

  if [[ $UNINSTALL -eq 1 ]]; then
    do_uninstall
    exit 0
  fi

  write_config
  stop_legacy
  install_unit
  start_unit

  step "next steps"
  cat <<EOF
    1. Install the in-process recovery plugin (it must live in the Harness
       profile, not in this daemon). From a Harness chat, use the plugin
       manager's install action with this absolute path:

         $ROOT/plugins/dsh-session-keeper

       The manager installs from that absolute local path (no registry needed)
       and enables the bundle. Do not copy it into the profile's node_modules.
       See $ROOT/plugins/dsh-session-keeper/README.md for details.

    2. Verify:

         node $ROOT/bin/dsh-stack doctor
         node $ROOT/bin/dsh-stack selftest
         node $ROOT/bin/dsh-stack sessions
         node $ROOT/bin/dsh-stack status

    3. Watch it:

         journalctl --user -u dsh-stack -f
         tail -f $STATE_DIR/stack.log

    4. Test a reboot safely:  scripts/verify-recovery.sh --dry-run
EOF
  ok "install complete"
}

main "$@"
