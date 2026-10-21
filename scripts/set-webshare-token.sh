#!/usr/bin/env bash
#
# Store a Webshare API token in the relay's settings and restart the stack.
#
# Webshare (webshare.io) is the only proxy supply that is worth using: its
# free tier gives 10 residential proxies that are actually alive, and the
# rotating plan hands out a fresh exit IP on every connection. The public
# free-proxy lists are ~99.8% unusable (measured: 300 sampled entries, 0
# could reach the upstream).
#
# The token is written into zen-router/settings.json (mode 0600, gitignored)
# and never printed again.
#
# Usage:
#   scripts/set-webshare-token.sh [--rotating] [--country us]
#
# The token comes from Webshare → Dashboard → API Keys (or the free proxy
# page). It is asked for interactively, so it stays out of your shell history.
#
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SETTINGS="$ROOT/zen-router/settings.json"

ROTATING=0
COUNTRY=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rotating) ROTATING=1 ;;
    --country) COUNTRY="${2:-}"; shift ;;
    -h|--help) sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

[[ -f $SETTINGS ]] || { echo "fatal: $SETTINGS not found (run the relay once first)" >&2; exit 1; }

read -rsp "Webshare API token (invisible): " TOKEN
echo
[[ -n $TOKEN ]] || { echo "fatal: empty token" >&2; exit 1; }

node --input-type=module -e '
  import fs from "node:fs";
  const [file, token, rotating, country] = process.argv.slice(1);
  const settings = JSON.parse(fs.readFileSync(file, "utf8"));
  settings.webshare_token = token;
  settings.webshare_rotating = rotating === "1";
  if (country) settings.webshare_country = country;
  fs.writeFileSync(file, JSON.stringify(settings, null, 2) + "\n", { mode: 0o600 });
  fs.chmodSync(file, 0o600);
  console.log("    stored the token in " + file + " (mode 0600)");
' "$SETTINGS" "$TOKEN" "$ROTATING" "$COUNTRY"

echo "    restarting the stack so the relay picks the proxies up…"
systemctl --user restart dsh-stack

sleep 6
echo
echo "    relay log:"
journalctl --user -u dsh-stack -n 40 --no-pager -o cat | grep -iE 'webshare|proxy list|sources:' | tail -5 || true
echo
echo "    check the pool:  node stack/bin/dsh-stack status"
