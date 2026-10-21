#!/usr/bin/env bash
#
# End-to-end verification for the DSH stack + session keeper.
#
# Safe by default: it inspects, probes, and runs the recovery test suite against
# throwaway stores. Nothing is restarted or killed.
#
# With --live it additionally restarts dsh-stack.service and waits for
# readiness — that DOES interrupt the Harness you may be talking through, so run
# it from a terminal.
#
# Usage:
#   scripts/verify-recovery.sh [--dry-run] [--live] [--json]
#
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DSH_STACK="node $ROOT/bin/dsh-stack"
DSH_HOME=${DSH_HOME:-$HOME/.dsh}
KEEPER_STATUS=$DSH_HOME/session-keeper/status.json
STATE_DIR=${DSH_STACK_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/dsh-stack}

LIVE=0
DRY_RUN=0
JSON=0
FAILURES=0
CHECKS=()

for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --json) JSON=1 ;;
    -h|--help) sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

pass() { CHECKS+=("PASS|$1"); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { CHECKS+=("FAIL|$1"); FAILURES=$((FAILURES + 1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
skip() { CHECKS+=("SKIP|$1"); printf '  \033[2mSKIP\033[0m %s\n' "$1"; }
info() { printf '       \033[2m%s\033[0m\n' "$1"; }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ---------------------------------------------------------------------------
section "1. static checks"

if bash -n "$ROOT/install.sh"; then pass "install.sh parses"; else fail "install.sh has a syntax error"; fi
if node --check "$ROOT/bin/dsh-stack"; then pass "bin/dsh-stack parses"; else fail "bin/dsh-stack has a syntax error"; fi
if node --check "$ROOT/plugins/dsh-session-keeper/index.js"; then pass "plugin entry parses"; else fail "plugin entry has a syntax error"; fi

config_error=$($DSH_STACK doctor 2>&1 | grep -c '^FAIL' || true)
if [[ ${config_error:-0} -eq 0 ]]; then pass "dsh-stack doctor reports no blocking problem"; else
  fail "dsh-stack doctor reports $config_error blocking problem(s)"; $DSH_STACK doctor | sed 's/^/       /'
fi

# ---------------------------------------------------------------------------
section "2. recovery semantics (isolated stores, real persistence backend)"

if [[ $DRY_RUN -eq 1 ]]; then
  skip "would run: node --test $ROOT/tests/*.test.mjs"
else
  if test_output=$(cd "$ROOT" && node --test tests/*.test.mjs 2>&1); then
    pass "recovery test suite ($(printf '%s' "$test_output" | grep -E '^ℹ pass' | tail -1 | tr -d 'ℹ '))"
  else
    fail "recovery test suite"
    printf '%s\n' "$test_output" | tail -25 | sed 's/^/       /'
  fi
fi

# ---------------------------------------------------------------------------
section "3. runtime state"

if $DSH_STACK status --json >/tmp/dsh-stack-status.$$ 2>/dev/null; then
  pass "supervisor status document present"
  node -e '
    const doc = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
    const ago = (at) => (typeof at === "number" ? Math.round((Date.now() - at) / 1000) + "s ago" : "—");
    console.log("       phase=" + doc.phase + " ready=" + doc.ready + " pid=" + doc.pid + " updated " + ago(doc.updatedAt));
    for (const [name, c] of Object.entries(doc.components ?? {})) console.log("       " + name.padEnd(8) + " " + String(c.state).padEnd(8) + " pid=" + (c.pid ?? "—"));
  ' /tmp/dsh-stack-status.$$
  if node -e 'process.exit(JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8")).ready === true ? 0 : 1)' /tmp/dsh-stack-status.$$; then
    pass "supervisor reports readiness"
  else
    fail "supervisor does not report readiness"
  fi
  rm -f /tmp/dsh-stack-status.$$
else
  skip "no supervisor status document (dsh-stack.service not started yet)"
fi

if [[ -f $KEEPER_STATUS ]]; then
  keeper_phase=$(node -e 'try{console.log(JSON.parse(require("node:fs").readFileSync(process.argv[1],"utf8")).summary.phase)}catch{console.log("unreadable")}' "$KEEPER_STATUS")
  keeper_line=$(node -e '
    try {
      const doc = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
      const s = doc.summary ?? {};
      console.log(`phase=${s.phase} scanned=${s.scanned} interrupted=${s.candidates} resumed=${(s.resumed??[]).length} skipped=${(s.skipped??[]).length}`);
    } catch { console.log("unreadable"); }
  ' "$KEEPER_STATUS")
  if [[ $keeper_phase == "unreadable" ]]; then
    fail "keeper status is unreadable: $KEEPER_STATUS"
  else
    pass "keeper status: $keeper_line"
    stuck=$(node -e '
      try {
        const doc = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
        const s = doc.summary ?? {};
        const failed = (s.skipped ?? []).filter((entry) => /failed|corrupt|owned/.test(String(entry.reason))).length;
        console.log(failed);
      } catch { console.log(0); }
    ' "$KEEPER_STATUS")
    if [[ ${stuck:-0} -gt 0 ]]; then
      fail "$stuck session(s) could not be recovered; see $KEEPER_STATUS"
    else
      pass "no recovery failures recorded in the last boot pass"
    fi
  fi
else
  skip "no keeper status yet ($KEEPER_STATUS) — the plugin has not run since install"
fi

# ---------------------------------------------------------------------------
section "4. session store"

sessions_json=$($DSH_STACK sessions --json 2>/dev/null)
if [[ -n $sessions_json ]]; then
  open_count=$(printf '%s' "$sessions_json" | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{try{const doc=JSON.parse(d);console.log(doc.resumable + " " + doc.children + " " + doc.openTurn)}catch{console.log("-1 -1 -1")}})')
  if [[ $open_count != "-1 -1 -1" ]]; then
    read -r resumable_count child_count total_count <<< "$open_count"
    pass "session store decodes ($resumable_count resumable candidate(s), $child_count subagent child, $total_count open-turn log(s) total)"
    info "a live Harness session also looks open; the keeper skips live agents and children"
  else
    fail "session store scan failed"
  fi
else
  fail "dsh-stack sessions produced no output"
fi

# ---------------------------------------------------------------------------
section "5. post-reboot proof"

if [[ $LIVE -eq 0 ]]; then
  skip "live restart (pass --live from a terminal to prove crash recovery end to end)"
  info "the safe proof is section 2: it interrupts real session logs in a temp store, resumes them,"
  info "verifies the repair, and proves a second pass finds nothing to do."
else
  if [[ $DRY_RUN -eq 1 ]]; then
    skip "would restart dsh-stack.service and wait for readiness"
  else
    printf '  \033[33mThis stops the Harness (and this shell if it runs inside it).\033[0m\n'
    systemctl --user restart dsh-stack.service && pass "dsh-stack.service restarted" || fail "dsh-stack.service did not restart"
    if $DSH_STACK wait --target all --timeout 300000; then
      pass "proxy + harness reachable after restart"
    else
      fail "proxy + harness not reachable within 5 minutes"
    fi
    if [[ -f $KEEPER_STATUS ]]; then
      node -e '
        const doc = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
        const s = doc.summary ?? {};
        console.log(`       keeper after reboot: phase=${s.phase} scanned=${s.scanned} interrupted=${s.candidates} resumed=${(s.resumed??[]).length}`);
      ' "$KEEPER_STATUS"
    fi
  fi
fi

# ---------------------------------------------------------------------------
if [[ $JSON -eq 1 ]]; then
  json_tmp=$(mktemp)
  printf '%s\n' "${CHECKS[@]}" > "$json_tmp"
  node -e '
    const fs = require("node:fs");
    const lines = fs.readFileSync(process.argv[1], "utf8").trim().split("\n").filter(Boolean);
    const checks = lines.map((line) => {
      const at = line.indexOf("|");
      return { status: line.slice(0, at), check: line.slice(at + 1) };
    });
    console.log(JSON.stringify({ failures: Number(process.argv[2]), checks }));
  ' "$json_tmp" "$FAILURES"
  rm -f "$json_tmp"
fi

section "summary"
if [[ $FAILURES -eq 0 ]]; then
  printf '  \033[32mall checks passed\033[0m  (%d checks)\n\n' "${#CHECKS[@]}"
  exit 0
fi
printf '  \033[31m%d check(s) failed\033[0m of %d\n\n' "$FAILURES" "${#CHECKS[@]}"
exit 1
