#!/usr/bin/env bash
#
# Print a QR code for the current Harness sign-in URL (and the URL itself).
#
# The public address of the Harness lives in ~/dsh-url.txt. The quick tunnel
# gives it a new name at every restart, so this is the quickest way to get the
# current link onto a phone: scan the code, sign in with the token, done.
#
# Usage:
#   scripts/qr-url.sh              # QR + URL
#   scripts/qr-url.sh --url        # URL only
#
set -euo pipefail

URL_FILE=${DSH_STACK_URL_FILE:-$HOME/dsh-url.txt}
[[ -f $URL_FILE ]] || { echo "fatal: $URL_FILE not found (is dsh-stack running?)" >&2; exit 1; }
URL=$(tr -d '\r\n' < "$URL_FILE")
[[ -n $URL ]] || { echo "fatal: $URL_FILE is empty" >&2; exit 1; }

if [[ ${1:-} == "--url" ]]; then
  echo "$URL"
  exit 0
fi

echo "Harness: $URL"
echo
if command -v qrencode >/dev/null; then
  qrencode -t ANSIUTF8 "$URL"
elif python3 -c 'import segno' 2>/dev/null; then
  python3 - "$URL" <<'PY'
import io, sys, segno
buf = io.StringIO()
segno.make(sys.argv[1], error="m").save(buf, kind="txt", border=1)
print(buf.getvalue())
PY
else
  echo "(no QR tool: install 'qrencode' or 'pip3 install segno')"
fi
