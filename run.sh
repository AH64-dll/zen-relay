#!/usr/bin/env bash
# One-command launcher for zen-router.
# Uses the bundled Linux x86_64 binary when possible, otherwise builds from
# source with cargo. The relay itself stays unmodified.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT/zen-router"

PORT="${PORT:-8901}"
HOST="${HOST:-127.0.0.1}"
export PORT HOST

if [ ! -f settings.json ]; then
  echo "First run: creating zen-router/settings.json with a random relay_api_key."
fi

os="$(uname -s)"
arch="$(uname -m)"

if [ "$os" = "Linux" ] && { [ "$arch" = "x86_64" ] || [ "$arch" = "amd64" ]; } && [ -x bin/zen-relay ]; then
  # ldd also catches a glibc version mismatch (e.g. GLIBC_2.34 not found).
  if ! command -v ldd >/dev/null 2>&1 || ldd bin/zen-relay >/dev/null 2>&1; then
    exec ./bin/zen-relay
  fi
fi

if command -v cargo >/dev/null 2>&1; then
  echo "Prebuilt binary is not usable on $os/$arch; building from source..."
  ( cd rust && cargo build --release )
  if [ -x rust/target/release/zen-relay.exe ]; then
    exec ./rust/target/release/zen-relay.exe
  fi
  exec ./rust/target/release/zen-relay
fi

cat <<'EOF'
The prebuilt binary is not usable here (wrong platform or glibc older than 2.34), and cargo was not found.

- Linux x86_64: bin/zen-relay is included and runs directly.
- macOS / Windows / other architectures:
  1) Install Rust: https://rustup.rs
  2) Re-run this script, or:
       cd zen-router/rust && cargo build --release
       cd .. && ./rust/target/release/zen-relay
EOF
exit 1
