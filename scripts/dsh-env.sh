#!/usr/bin/env bash
#
# Environment for driving the DeepSeek Harness (dsh) from a shell.
#
# Source it:  source ~/Documents/zen-relay/scripts/dsh-env.sh
#
# It does three things:
#   1. puts the dsh CLI and the dsh-stack supervisor on PATH;
#   2. loads the shared key the Harness needs to talk to the local bridge
#      (kept 0600 in the stack's secret file, also injected into systemd);
#   3. defines `dsh`, so that typing it alone opens the Harness UI in the
#      browser, while `dsh <anything>` still runs the real CLI.
#
DSH_STACK_DIR=${DSH_STACK_DIR:-$HOME/.config/dsh-stack}
DSH_UI_PORT=${DSH_UI_PORT:-30141}

# --- the real CLI -----------------------------------------------------------
# Resolve it once, before the shell function below shadows the name.
if [ -z "${DSH_REAL_BIN:-}" ] || [ ! -x "${DSH_REAL_BIN:-/nonexistent}" ]; then
  for _d in "$HOME"/.nvm/versions/node/*/bin; do
    if [ -x "$_d/dsh" ]; then DSH_REAL_BIN="$_d/dsh"; break; fi
  done
  unset _d
fi
export DSH_REAL_BIN

# --- PATHs ------------------------------------------------------------------
[ -n "${DSH_REAL_BIN:-}" ] && PATH="$(dirname "$DSH_REAL_BIN"):$PATH"

# The stack supervisor lives in the checkout.
DSH_STACK_REPO=${DSH_STACK_REPO:-$HOME/Documents/zen-relay}
[ -x "$DSH_STACK_REPO/stack/bin/dsh-stack" ] && PATH="$DSH_STACK_REPO/stack/bin:$PATH"

export PATH

# --- secrets and URLs -------------------------------------------------------
if [ -f "$DSH_STACK_DIR/relay.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$DSH_STACK_DIR/relay.env"
  set +a
fi

if [ -f "$HOME/dsh-url.txt" ]; then
  DSH_URL=$(tr -d '\r\n' < "$HOME/dsh-url.txt")
  export DSH_URL
fi

# The sign-in token changes on every restart; read it fresh each time.
dsh_token() {
  local f t
  for f in "$HOME/dsh-url.txt" "$DSH_STACK_DIR/url.txt"; do
    [ -f "$f" ] || continue
    t=$(grep -oE 'token=[A-Za-z0-9_-]+' "$f" 2>/dev/null | head -1 | cut -d= -f2)
    if [ -n "$t" ]; then printf '%s\n' "$t"; return 0; fi
  done
  return 1
}

# Is the UI answering? Any HTTP status counts: 401 means it is up and asking
# for the token, which is exactly what a fresh browser visit supplies.
dsh_up() {
  curl -s -o /dev/null -m 2 -w '%{http_code}' "http://127.0.0.1:${DSH_UI_PORT}/" 2>/dev/null \
    | grep -qE '^[1-5][0-9][0-9]$'
}

# --- `dsh` with no arguments opens the Harness ------------------------------
dsh() {
  if [ $# -gt 0 ]; then
    if [ -z "${DSH_REAL_BIN:-}" ] || [ ! -x "$DSH_REAL_BIN" ]; then
      echo "dsh: the Harness CLI was not found (is nvm's node bin dir present?)" >&2
      return 127
    fi
    "$DSH_REAL_BIN" "$@"
    return
  fi

  local token url
  token=$(dsh_token) || {
    echo "dsh: no sign-in token yet — the stack has not published one." >&2
    echo "     try: dsh-stack status" >&2
    return 1
  }

  if ! dsh_up; then
    echo "starting dsh-stack …"
    systemctl --user start dsh-stack 2>/dev/null \
      || echo "  (could not start the user unit; start it by hand)"
    local i
    for i in $(seq 1 40); do
      dsh_up && break
      sleep 1
    done
    token=$(dsh_token) || true
  fi

  url="http://127.0.0.1:${DSH_UI_PORT}/?token=${token}"
  echo "Harness:  $url"
  [ -n "${DSH_URL:-}" ] && echo "Phone:    $DSH_URL"

  if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
  else
    echo
    echo "No graphical session detected — open the URL above in a browser."
  fi
}
