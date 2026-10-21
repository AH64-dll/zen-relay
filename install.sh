#!/usr/bin/env bash
#
# One-command install of the whole stack: relay + tunnel + Harness supervisor.
#
#   ./install.sh --dry-run    # show every action, change nothing
#   ./install.sh              # install and start
#
# Run it from a terminal, not from inside the Harness Web UI: it restarts the
# Harness that may be serving the page you are typing in.
#
# It does NOT create a systemd unit for the relay: `dsh-stack` spawns the relay
# itself. `config/zen-rotator.service` is only for relay-only deployments.
#
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SETTINGS="$ROOT/zen-router/settings.json"

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'warn: %s\n' "$*" >&2; }
die()  { printf 'fatal: %s\n' "$*" >&2; exit 1; }

DRY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '3,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

[[ -f "$ROOT/zen-router/bin/zen-relay" || -f "$ROOT/zen-router/rust/target/release/zen-relay" ]] \
  || die "zen-relay binary missing; build it: cargo build --release --manifest-path zen-router/rust/Cargo.toml"

command -v node >/dev/null || die "node not found (need >= 22)"
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)' \
  || die "node >= 22 required"

# dsh is resolved by stack/install.sh (PATH, the node bin dir, ~/.local/bin),
# so do not reject a machine where it is installed but not exported here.
command -v cloudflared >/dev/null || warn "cloudflared not found; the public URL will be skipped"

if [[ ! -f "$SETTINGS" ]]; then
  key="rly_$(openssl rand -hex 16 2>/dev/null || node -e 'console.log(require("crypto").randomBytes(16).toString("hex"))')"
  if [[ $DRY -eq 1 ]]; then
    say "[dry-run] would create $SETTINGS with a new relay_api_key"
  else
    say "creating $SETTINGS"
    node -e '
      const fs = require("node:fs");
      const src = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
      src.relay_api_key = process.argv[2];
      fs.writeFileSync(process.argv[3], JSON.stringify(src, null, 2) + "\n", { mode: 0o600 });
    ' "$ROOT/config/settings.example.json" "$key" "$SETTINGS"
    printf '\n    relay key: %s\n\n' "$key"
  fi
else
  say "settings already present: $SETTINGS"
fi

say "handing over to stack/install.sh"
exec bash "$ROOT/stack/install.sh" "$@"
