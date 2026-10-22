#!/usr/bin/env bash
#
# Environment for driving the DeepSeek Harness (dsh) from a shell.
#
# Source it:  source ~/Documents/zen-relay/scripts/dsh-env.sh
#
# It puts the dsh CLI on PATH and loads the shared key the Harness needs to
# talk to the local bridge (the key is stored 0600 in the stack's secret file,
# and is also injected into the systemd units via a drop-in).
#
DSH_STACK_DIR=${DSH_STACK_DIR:-$HOME/.config/dsh-stack}

# The dsh CLI lives in the nvm node bin dir, which is not on the default PATH.
for d in "$HOME"/.nvm/versions/node/*/bin; do
  [ -x "$d/dsh" ] && PATH="$d:$PATH" && break
done
export PATH

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

cat <<'EOF'
dsh environment ready:
  dsh        -> the Harness CLI (dsh headless "your prompt")
  DSH_URL    -> the sign-in URL for this machine's Harness
  key        -> OMP_ZEN_RELAY_KEY loaded
EOF
