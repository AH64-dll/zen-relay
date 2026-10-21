# @local/dsh-session-keeper

Crash recovery for Harness sessions: it finds turns that a crash, power loss, or
`kill -9` left open in the session log and resumes them exactly once, in order,
behind a readiness gate.

This plugin is the **authoritative** recovery component: it runs inside the Host
process that owns the session store, so it can resume a session without
contending for the single-writer lease. [The `dsh-stack` daemon](../../README.md)
does the boot ordering, supervision and watchdog; the two read each other's
status documents.

## What "interrupted" means

The session log is an fsync'd write-ahead log. A turn is opened by `turn/start`
and closed by `turn/end`; agent-loop appends synthetic closers
(`turn/end {interrupted}`, `step/end`, and error results for unanswered tool
calls) when it resumes an interrupted log.

So the predicate is a property of the **log on disk**, not of any bookkeeping:

```
the final turn has no `turn/end` after its `turn/start`
```

This is what makes recovery idempotent at any crash point: after a resume the
log is balanced, and a second scan finds nothing. The plugin's ledger carries
attempt counters only — never the source of truth.

## Safety properties

| Behaviour | How |
| --- | --- |
| Never resume a session that is live in this Host | `ctx.agents.get(id)` check before anything else |
| Never resume a session another process owns | `agents.resume()` takes the persistence write lease; `SessionAlreadyOwnedError` is treated as a skip, never retried in a loop |
| Never resume into a dead model route | waits for the proxy's `/healthz` (`requireProxy`) before spending an attempt |
| Never resurrect abandoned work | sessions whose last event is older than `maxAgeMs` (24 h) are skipped on automatic runs; `/keeper recover` bypasses this |
| Never fork a subagent's work | `origin: 'subagent'` / `delegationDepth > 0` are skipped; the parent owns them |
| Never loop forever on a poison session | per-turn and per-session attempt caps plus a 24 h quarantine |
| Never corrupt the store | no direct file access: everything goes through `ctx.sessionPersistence` |

The recovery prompt tells the model that the turn was started by the supervisor,
quotes the in-flight request (recovered from `user/message` or the
`agent/inbox/spliced` that preceded it), names the last committed assistant text
and last tool call, and asks it to re-establish ground truth before continuing —
never to replay destructive work.

## Configuration

Every field has a default; the shipped `cordis.patch.yml` sets the ones that
matter for a normal install.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` (set `DSH_SESSION_KEEPER_DISABLED=1` in the Host's environment to force it off) | register the engine |
| `autoRecover` | `true` | run a pass on Host start (false = only via command) |
| `dryRun` | `false` | decide and report, never resume |
| `settleMs` | `6000` | wait for the rest of the Host to mount before scanning |
| `maxAgeMs` | `86400000` | automatic runs ignore turns older than this; `0` disables |
| `requireProxy` | `true` | gate on the model proxy `/healthz` |
| `proxyHealthUrl` | `http://127.0.0.1:8901/healthz` | that endpoint |
| `proxyWaitMs` | `600000` | give up for this boot if the route never comes up |
| `maxConcurrent` | `2` | resumes in parallel |
| `maxResumesPerBoot` | `5` | newest-first cap per pass |
| `includeSubagents` | `false` | also recover subagent children |
| `cwds` | `[]` | restrict recovery to these workspaces |
| `stateDir` | `$DSH_HOME/session-keeper` | ledger, status, log |
| `ledger.maxAttemptsPerTurn` | `1` | confirmed attempts per open turn |
| `ledger.maxAttemptsPerSession` | `3` | attempts per session per window |
| `ledger.sessionWindowMs` | `21600000` (6 h) | the window for that count |
| `ledger.quarantineMs` | `86400000` (24 h) | how long a poison session stays out |

## Command

```
/keeper status     what the last pass saw and decided
/keeper scan       dry run: list what would be resumed, change nothing
/keeper recover    run a pass now (ignores maxAgeMs, respects the ledger)
```

## Files it writes

`$DSH_HOME/session-keeper/`

| File | Purpose |
| --- | --- |
| `status.json` | current/last pass summary, ledger snapshot; read by `dsh-stack status` |
| `ledger.json` | attempt/quarantine state, atomic temp+fsync+rename, corrupt copies preserved as `.corrupt-<ts>` |
| `keeper.log` | one JSON line per decision |

## Install into the Harness profile

The plugin must be installed as a **bundle** in the `web` profile so the loader
mounts it. Installing it by hand-editing `~/.dsh/profiles/web/*` is not
supported: the loader's patch layers and the plugin manager's enable/disable
state must stay consistent.

**Option A — from a Harness chat (recommended).** Use the plugin manager's
install action with this absolute path:

```
/home/ai/Music/dsh-stack/plugins/dsh-session-keeper
```

It runs `pnpm add <path>` in the profile, validates the manifest declares
`dsh.bundle`, enables the bundle, and reloads. The row appears as
`dsh-session-keeper` in the profile's plugin list.

The manager is the only supported installer: it runs `pnpm add` inside the
profile, records the bundle, and reloads. Do **not** hand-write the profile's
`package.json`/`cordis.patch.yml` or run pnpm in the profile directory — that
bypasses the manager's consistency checks and its enable/disable state.

**Offline installs.** `install_bundle` resolves packages from a registry fallback
chain, but a local absolute path is installed with a plain `pnpm add <path>`, so
no network is required. If the profile's pnpm store is unusable, fix that first
rather than copying files into `node_modules`: a copied bundle has no manager
record and disappears on the next dependency resolution.

### First boot after install

The first pass will resume whatever was mid-turn at that moment. To check
before committing:

1. set `"dryRun": true` in the row's config,
2. restart the Harness,
3. read `$DSH_HOME/session-keeper/status.json` (or `/keeper status`),
4. remove `dryRun` and `/keeper recover`.

## Verifying it works

```bash
# decoder parity + recovery semantics against the real backend, isolated store
node --test /home/ai/Music/dsh-stack/tests/keeper.test.mjs
node --test /home/ai/Music/dsh-stack/tests/plugin-wiring.test.mjs

# what does the store look like right now?
node /home/ai/Music/dsh-stack/bin/dsh-stack sessions
```

`tests/keeper.test.mjs` writes a genuinely interrupted log through the real
persistence backend, runs the engine, verifies the repair via the Harness's own
`interruptedTurnClosers`, and proves a second pass with an empty ledger finds
nothing to resume.

## Uninstall

Remove the bundle in the plugin manager (`remove_bundle @local/dsh-session-keeper`). State under `$DSH_HOME/session-keeper` is safe to delete; it holds no
session data.

## Fleet-wide kill switch

`DSH_SESSION_KEEPER_DISABLED=1` in the Harness process environment forces
`enabled: false`. Useful when a second Host shares the same `~/.dsh` (shadow
port, staging profile): it must never resume a session the primary Host is
still serving.
