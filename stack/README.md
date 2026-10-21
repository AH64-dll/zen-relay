# dsh-stack + session-keeper

Production supervision for the DeepSeek Harness on this machine: the Harness, its
model proxy, and its public tunnel start in dependency order at boot, are proven
ready before anything depends on them, are restarted when they crash or wedge, and
**sessions that were interrupted by a crash resume themselves** once the stack is
back up.

> Paths such as `/home/ai/Music/dsh-stack` and `~/.omp/zen-router` below are the
> machine this was built on. `install.sh` discovers the real paths and rewrites
> the unit; nothing here needs editing by hand. Node.js **22+** is required
> (zstd in `node:zlib`).

```
                         systemd (user manager, linger enabled)
                                        │  Type=notify, WatchdogSec=90
                                        ▼
                         ┌──────────────────────────────┐
                         │        dsh-stack             │  supervisor
                         │  network gate → proxy →      │  owns 3 process groups
                         │  tunnel → harness → READY    │
                         └───┬──────────┬───────────┬───┘
                             │          │           │
                   spawn+probe│   spawn  │   spawn+probe
                             ▼          ▼           ▼
                      ┌──────────┐ ┌──────────┐ ┌──────────────┐
                      │ zen-relay│ │cloudflared│ │ dsh web :30141│
                      │ :8901    │ │ quick tunnel│ │  (Harness)   │
                      └──────────┘ └──────────┘ └──────┬───────┘
                             ▲                          │ in-process Cordis plugin
                             │  /healthz gate           ▼
                             │                  ┌────────────────────┐
                             └──────────────────│ dsh-session-keeper │
                                                │ scan → decide →    │
                                                │ resume → verify    │
                                                └─────────┬──────────┘
                                                          │
                                                ~/.dsh/sessions/<project>/<id>/
                                                session.v4.jsonl.zstd  (the WAL)
                                                session.lock           (flock lease)
```

Two components, deliberately separated by *what they can own*:

| | `dsh-stack` (this daemon) | `@local/dsh-session-keeper` (Host plugin) |
| --- | --- | --- |
| Lives outside the Harness | yes — it must survive a Harness crash | no |
| Boot ordering, readiness gating | ✔ | — |
| Crash/hang detection, restarts | ✔ | — |
| Resumes interrupted sessions | — | ✔ (only it can take the session write lease cleanly) |
| Reports what recovery did | reads the plugin's status | writes `status.json` |

Neither half can do the other's job: a daemon cannot resume a session without
racing the Host's writer, and a plugin cannot restart a dead Host.

---

## 1. Boot orchestration

`dsh-stack run` is the only process systemd starts. It performs, in order:

1. **Instance lock** — `$STATE_DIR/stack.lock` (`O_EXCL` + `/proc` cmdline check).
   A second instance exits instead of fighting for the port.
2. **Network gate** — a real TCP connect to `opencode.ai:443`, retried for
   `network.timeoutMs` (120 s). `network-online.target` alone is not trusted:
   it is satisfied by an interface being up, not by the model provider being
   reachable. Offline boots fail open (the local UI still works).
3. **Proxy** — spawn `zen-relay` in its own process group, then poll
   `GET http://127.0.0.1:8901/healthz` until it answers
   `{"ok":true,...}` (timeout 180 s).
4. **Tunnel** — spawn `cloudflared tunnel --url http://127.0.0.1:30141`, parse
   the `https://<name>.trycloudflare.com` hostname from its output
   (timeout 90 s). Optional: a tunnel failure degrades the public URL only.
5. **Bridge** — spawn `opencode-bridge` (see below) and poll
   `GET http://127.0.0.1:8902/healthz` until it reports `ok:true` (timeout
   60 s). Optional unless `bridge.required` is set.
6. **Harness** — spawn `dsh web --port 30141 --no-open --trusted-host <tunnel
   host>`, capture the sign-in token from stdout, poll `GET /` until it answers
   (timeout 300 s), and publish `~/dsh-url.txt`.
7. **`READY=1`** — sent to systemd only here. Until this point
   `systemctl start dsh-stack` blocks, so anything ordered after it (and any
   operator looking at `is-active`) sees a *proven* stack.

The dependency is enforced in **both directions**: when the proxy is restarted,
the Harness is stopped first and restarted only after the proxy is ready again.
A Harness booted against a dead model route turns every session it opens into an
error turn, so "started" is never used as a proxy for "usable".

### The OpenCode bridge

`stack/bridge/opencode-bridge.mjs` is an OpenAI-compatible front end for a
local OpenCode server. It exists because the model provider now answers
`FreeTierError: ... can only be used from within OpenCode` to anything that is
not the official client — the rotating-proxy relay included.

The bridge does not forge that identity. It *is* the official client:

```
dsh web :30141 ──OpenAI chat──▶ opencode-bridge :8902 ──session API──▶ opencode
                                                                            │
                                                            opencode.ai ◀───┘
```

Each chat completion becomes one OpenCode session prompt; the assistant's text
comes back as a chat completion. The upstream therefore sees a request from
OpenCode itself, with OpenCode's own credentials and headers.

The trade-off, stated plainly: OpenCode runs *its* agent loop with *its* tools
inside that session. The Harness receives the answer, not the tool calls. The
Harness becomes a front end to OpenCode rather than a second agent on top of it.

- Enabled automatically when `opencode` is on the machine; `--no-bridge` turns
  it off and leaves the relay route as the only model route.
- The bridge finds the local server itself (`opencode service status`) and its
  password (`~/.config/opencode/service.json`), and starts the server if it is
  not running.
- It accepts the relay key (`OMP_ZEN_RELAY_KEY`) so one secret covers both
  routes; without a key it serves loopback callers only.

### Health, restarts, watchdog

- Every 15 s each component is probed through the interface it serves (HTTP
  status < 500 for the Harness; JSON `{"ok":true}` for the proxy; captured
  hostname for the tunnel). Three consecutive failures restart it.
- Restart backoff is 1 s → 60 s (exponential, jittered), reset after 120 s
  healthy. A component restarted 12 times in 5 minutes makes the daemon exit
  non-zero on purpose: systemd + journald record it instead of an invisible loop.
- `WatchdogSec=90`: the daemon pings only while it is *making progress*
  (`lastProgressAt`), so a wedged event loop stops pinging and systemd restarts
  the unit — while a legitimately slow Harness boot keeps pinging.
- `KillMode=control-group`: stopping the unit kills the proxy, the tunnel, the
  Harness **and their MCP children**, even if the supervisor is wedged.
- Each child is `detached: true` (its own process group); restarts SIGTERM the
  group, then SIGKILL after 15 s.

## 2. Session persistence and crash recovery

### The log is the state

A Harness session is an event-sourced log, flushed (fsync) after each batch:
`~/.dsh/sessions/<project>/<session-id>/session.v4.jsonl.zstd`, plus
`session.lock` — a POSIX `flock` held by the single writer and released by the
kernel when that process dies.

A turn is opened by `turn/start` and closed by `turn/end`. `agent-loop` repairs
an interrupted log on resume by appending synthetic closers
(`turn/end {interrupted}`, `step/end`, and conservative error results for
unanswered tool calls). Therefore:

> **Interrupted** = the log's final `turn/start` has no later `turn/end`.

This predicate is a property of the durable bytes, which makes recovery
*idempotent at every crash point*: the repair writes the closers, the log becomes
balanced, and a second scan (even with all bookkeeping deleted) finds nothing.
That is the property `tests/keeper.test.mjs` asserts explicitly.

Nothing in the Host resumes sessions at boot by itself — the UI resumes a session
when a browser opens it. Recovery is what makes a machine's work survive a reboot
without a browser.

### What is persisted, and where

| Artifact | Owner | Contents | Crash behaviour |
| --- | --- | --- | --- |
| `session.v4.jsonl.zstd` | Harness | the session: header + contiguous events | fsync'd WAL; torn tail truncated by the write path |
| `session.lock` | Harness | `flock` write lease | released by the kernel on process death |
| `session-keeper/ledger.json` | plugin | per-turn attempts, unresolved intents, quarantines | atomic temp+fsync+rename+dir-fsync; corrupt file preserved as `.corrupt-<ts>` |
| `session-keeper/status.json` | plugin | last pass: scanned/candidates/resumed/skipped | atomic |
| `stack/status.json` | daemon | components, readiness, sign-in URL, recovered counts | atomic |
| `stack/stack.lock` | daemon | single-supervisor guard | stale lock detected via `/proc/<pid>/cmdline` |

The ledger is deliberately **not** a transaction log for sessions: it holds only
attempt counters (`intent` → `started` → `resumed`/`failed`). Writing `intent`
before calling `resume` means a crash *inside* recovery is visible next boot;
`maxUnresolvedIntents` (3) bounds a session that keeps dying mid-resume.

### The recovery pass

Runs 6 s after the plugin mounts (settle), then on demand via `/keeper`:

1. **Gate** — probe the proxy `/healthz`; a dead route would burn the only
   allowed attempt, so recovery waits (up to 10 min) rather than trying.
2. **Scan** — `ctx.sessionPersistence.list()`; for each session: skip
   live-in-this-Host (`agents.get(id)`), skip subagent children, skip missing
   workspaces, skip logs over 32 MiB.
3. **Analyse** — read the stored log through a `read` handle and apply the
   predicate. `list()` snapshots expose `sizeBytes` but not `eventCount`, so the
   engine reads the full log; the whole store here (112 sessions, ~30 MiB) takes
   ~2 s at boot.
4. **Decide** — ledger: already resumed? attempt cap? unresolved intents?
   per-session cap? automatic runs also skip turns whose last event is older than
   `maxAgeMs` (24 h) — an open turn from last week is abandoned work, not a crash.
5. **Resume** — `ctx.agents.resume({ resumeSessionId, agentOptions, setup })`
   where `setup` mounts the session's preset (the same composition the Web host
   performs when a user opens a session). The lease is the mutex: if another
   process owns the session, `SessionAlreadyOwnedError` is recorded as a skip and
   the attempt is **not** spent.
6. **Continue** — `agent.followup(<recovery prompt>)`. The prompt states that the
   turn was started by the supervisor, quotes the in-flight request (recovered
   from `user/message` or the `agent/inbox/spliced` that preceded the turn),
   names the last committed assistant text and last tool call, and instructs the
   model to re-establish ground truth before continuing and never to replay
   destructive work blindly.
7. **Report** — `status.json` (summaries, skips with reasons, ledger snapshot),
   which the daemon republishes in `dsh-stack status`.

Bounded: `maxConcurrent` 2, `maxResumesPerBoot` 5 (newest first), per-turn 1,
per-session 3 in 6 h, then a 24 h quarantine.

### Crash matrix

| Event | Result |
| --- | --- |
| Harness crashes mid-turn | daemon restarts it; plugin's pass resumes the log's open turn |
| Proxy crashes | daemon stops the Harness, restarts the proxy, restarts the Harness; sessions' open turns are then recovered |
| Machine power loss | systemd starts the stack at boot (linger enabled); proxy → tunnel → Harness; plugin resumes open turns |
| Daemon wedges | watchdog stops pinging; systemd kills the cgroup and restarts the stack |
| Session log corrupt (bad bytes) | the probe reports `open-failed:…` and the session is skipped; `dsh-stack sessions` names the file |
| Recovery itself crashes | ledger `intent` remains; next boot counts it against `maxUnresolvedIntents` |
| Poison session (crashes every resume) | per-turn/per-session caps → 24 h quarantine |

---

## 3. Layout

```
dsh-stack/
├── bin/dsh-stack                 CLI: run | status | wait | selftest | doctor | sessions | config
├── lib/
│   ├── config.mjs                defaults, deep merge, env overrides, absolute binary resolution
│   ├── log.mjs                   JSONL log + rotation (stack.log, .1 … .5)
│   ├── atomic.mjs                temp+fsync+rename+dir-fsync writes
│   ├── probe.mjs                 httpProbe / tcpProbe / waitUntil
│   ├── sdnotify.mjs              READY / WATCHDOG / STATUS via systemd-notify
│   ├── component.mjs             one supervised child process group
│   ├── stack.mjs                 boot sequence, reconcile loop, restarts, status
│   └── sessionscan.mjs           offline decode of session logs (advisory)
├── plugins/dsh-session-keeper/   the in-process Cordis plugin
│   ├── index.js                  apply(): injects persistence, starts the engine, /keeper
│   ├── lib/events.mjs            the crash predicate + recovery prompt (pure)
│   ├── lib/ledger.mjs            durable attempt accounting + quarantine
│   ├── lib/keeper.mjs            scan → decide → resume → report
│   ├── cordis.patch.yml          bundle patch (config defaults)
│   └── README.md                 plugin-specific install/behaviour
├── bridge/opencode-bridge.mjs    OpenAI-compatible front end for a local OpenCode server
├── systemd/dsh-stack.service     Type=notify, WatchdogSec=90, KillMode=control-group
├── install.sh                    idempotent install/uninstall, legacy-unit handling
├── scripts/verify-recovery.sh    safe end-to-end verification (--live to restart)
└── tests/                        node:test — 74 checks incl. real-backend integration
```

## 4. Install

> **Run the installer from a terminal, not from inside the Web UI.** It stops
> `dsh-harness.service`, which is what serves the page you would be typing in.

```bash
cd /home/ai/Music/dsh-stack

./install.sh --dry-run     # show every action, change nothing
./install.sh               # configure, install unit, disable legacy units, start
```

What it does:

1. discovers `node`, `dsh`, `zen-relay`, `cloudflared`; checks `node >= 22`,
   `XDG_RUNTIME_DIR`, and `linger`;
2. writes `~/.config/dsh-stack/config.json` (existing file backed up, deep-merged);
3. records and disables the legacy units that would fight this supervisor —
   `dsh-harness.service`, `zen-rotator.service`, `cloudflared-tunnel.service` —
   storing their prior `enabled`/`active` state in
   `~/.local/state/dsh-stack/legacy-units.json` for `--uninstall`;
4. installs `~/.config/systemd/user/dsh-stack.service`, `daemon-reload`,
   `enable`, `restart`, then waits for readiness and prints `dsh-stack status`;
5. prints the plugin step.

Then install the **recovery plugin** into the Harness profile (it cannot be part
of the daemon). From a Harness chat, use the plugin manager's install action with:

```
/home/ai/Music/dsh-stack/plugins/dsh-session-keeper
```

The manager runs `pnpm add` against that absolute path, records the bundle, and
reloads; nothing needs to be copied into the profile by hand. Full detail: [`plugins/dsh-session-keeper/README.md`](plugins/dsh-session-keeper/README.md).

If `Linger=no`, enable it once: `sudo loginctl enable-linger $(id -un)`.

### First boot after installing the plugin

The first pass recovers whatever was mid-turn at that moment. To preview instead:

```bash
# in the profile row's config:  "dryRun": true
# then restart and inspect:
node bin/dsh-stack status
cat ~/.dsh/session-keeper/status.json
```

### Uninstall

```bash
./install.sh --uninstall          # stop, disable, remove unit, restore legacy units
./install.sh --uninstall --purge  # also delete ~/.local/state/dsh-stack
```

The plugin is removed through the plugin manager (`remove_bundle`).

## 5. Verify

```bash
node bin/dsh-stack doctor       # binaries, paths, permissions, units
node bin/dsh-stack selftest     # live probes + session-store decode
node bin/dsh-stack status       # component table, readiness, recovery summary
node bin/dsh-stack sessions     # sessions whose last turn never closed
./scripts/verify-recovery.sh    # all of the above + the isolated recovery suite
npm test                        # 63 checks: unit, integration, loader parity
```

`tests/keeper.test.mjs` is the real proof: it writes an interrupted log through
the production persistence backend, runs the engine against fake Host services,
then verifies — using the Harness's own `interruptedTurnClosers` — that the log
is repaired and that a second pass with an empty ledger finds nothing to resume.
It also asserts the single-writer lease is respected rather than raced.

To prove the boot path itself **on a real restart** (disruptive):

```bash
./scripts/verify-recovery.sh --live
```

## 6. Operations

```bash
systemctl --user status dsh-stack          # phase, pids, watchdog
journalctl --user -u dsh-stack -f          # warnings + lifecycle
tail -f ~/.local/state/dsh-stack/stack.log # every line, JSON
node bin/dsh-stack status --probe          # live UP/DOWN + status document
node bin/dsh-stack wait --target all       # block until proxy+harness answer
systemctl --user restart dsh-stack         # planned restart
systemctl --user kill -s SIGHUP  dsh-stack # restart just the Harness
systemctl --user kill -s SIGUSR2 dsh-stack # restart just the proxy
systemctl --user kill -s SIGUSR1 dsh-stack # dump status into the log
```

Config lives at `~/.config/dsh-stack/config.json`; the daemon re-reads it on
restart (`systemctl --user restart dsh-stack`).

Common knobs:

```jsonc
{
  "proxy": { "mode": "external" },        // probe an existing proxy, don't spawn
  "tunnel": { "mode": "off" },            // local UI only
  "harness": { "port": 30141, "extraArgs": [] },
  "health": { "intervalMs": 15000, "failureThreshold": 3 },
  "keeper": { "verifyScan": true, "verifyScanDelayMs": 45000 }
}
```

## 7. Troubleshooting

| Symptom | Check | Fix |
| --- | --- | --- |
| `systemctl start` times out | `journalctl --user -u dsh-stack -n 80` | usually the proxy: `/healthz` not ready → read `stack.log` for `proxy-not-ready` |
| Stack ready, page 502/blank | `dsh-stack status --probe` | Harness lists as ready but the tunnel host changed; restart the unit (`--trusted-host` is rewired from the parsed hostname) |
| Recovery did nothing | `cat ~/.dsh/session-keeper/status.json` (`phase`, `skipped[].reason`) | `live-in-this-host` → the session was open in the UI, not crashed; `session-too-old` → use `/keeper recover`; `writer-held-elsewhere` → another harness still owns it |
| Session keeps crashing the Host | `ledger.quarantined` entries in status | delete that session's ledger entry after fixing the cause (`ledger.json` is plain JSON) |
| `instance-already-running` | `cat ~/.local/state/dsh-stack/stack.lock` | a supervisor is already running; `systemctl --user restart dsh-stack` |
| Two tunnels / URL flaps | `pgrep -af cloudflared` | a legacy unit was re-enabled: `systemctl --user disable --now cloudflared-tunnel` |
| Plugin not mounted | plugin manager list, `fiberPhase` | the bundle is installed but not enabled, or the row id differs |
| Recovery resumed an old session | `status.json` `resumed[]` | expected for `< maxAgeMs`; lower `maxAgeMs` or run with `dryRun` first |

## 8. Design notes and trade-offs

- **Why the log is the source of truth.** Any external journal can disagree with
  the durable log after a crash — an fsync'd WAL cannot be "wrong". Deriving the
  predicate from bytes makes the whole system re-entrant: the worst outcome of a
  crash during recovery is a second scan that sees a balanced log.
- **Why two processes.** A single in-process plugin cannot restart its own Host;
  a single external supervisor cannot take a session write lease without racing
  the Host. Splitting on "who can own the resource" removes all cross-process
  locking on the recovery path.
- **Why `agentOptions` + `setup` are passed explicitly.** A bare resume would
  mount a session without its preset (no tools, no prompt sections) and on the
  default model rather than the session's route. The plugin composes exactly what
  the Web host composes, so a recovered session is indistinguishable from one the
  user reopened.
- **Why the recency gate exists.** "Interrupted" is a fact; "should be resumed"
  is a judgement. An open turn whose last event is a week old is abandoned work,
  and resuming it spends the user's tokens without their consent. Automatic runs
  apply the gate; `/keeper recover` is the explicit override.
- **Trade-off: full-log reads.** The JSONL backend does not expose `eventCount`
  in `list()`, so detection reads each log. It is bounded (`maxScanBytes`,
  `maxScanSessions`) and measured (~2 s for the current store). If a future
  backend exposes `eventCount`, `inspect()` already uses the cheaper tail-window
  path automatically.
- **Trade-off: one tunnel hostname.** The quick tunnel's hostname is parsed from
  `cloudflared` output and passed to `dsh web --trusted-host`. A hostname change
  therefore restarts the Harness — correct, and visible in status as
  `harness-retrust`.

## 9. Honest scope

- The supervisor and the plugin are implemented, tested (74 checks, including the
  real persistence backend and the real Cordis loader path), and dry-run verified
  against this machine.
- **Nothing was installed or restarted in this session**: the legacy units are
  untouched, the profile is unchanged, and the live Harness on port 30141 is
  still the one started by `/home/ai/dsh-supervisor.sh`. Installing stops that
  unit, which is why it is left as your step (`./install.sh`).
- `zen-relay` is treated as a locally built binary at
  `~/.omp/zen-router/rust/target/release/zen-relay`; if you rebuild it elsewhere,
  update `proxy.command` (or use `proxy.mode: "external"`).
- Session recovery assumes the `web` profile's `@deepseek-ai/dsh-agent-loop` +
  `session-persistence-jsonl` versions installed here. The integration tests skip
  themselves on a machine without them, rather than pretending to pass.
- The OpenCode bridge gives the Harness a working model route while the
  provider restricts its free tier to the official client. It does **not** give
  the Harness its own tool loop: OpenCode's agent runs inside the session, so
  the Harness sees the answer and OpenCode's session holds the work. The bridge
  also depends on OpenCode's local server API, which is not a stable public
  interface — a change there can break the route.
