# zen-router/rest — pure-Python REST relay

The original FastAPI implementation of the relay, recovered from git history
(pre-`dba1c17`), modernized, and kept as a full alternative to the Rust binary
in `zen-router/bin/zen-relay`. Same API surface, same pool semantics, same
settings file — swap-in compatible.

## What this is

`ip_relay.py` is a self-contained OpenAI-compatible REST relay (~3,700 lines,
FastAPI + httpx). It scrapes ~150 public proxy lists, real-probes every
candidate with a 1-token completion, scores lanes by EWMA, parks quota-burned
IPs, and fails over between lanes mid-request. It serves:

- `POST /v1/chat/completions`, `POST /v1/messages` — OpenAI/Anthropic relay
- `GET /v1/models` — model list through a live lane
- `GET /healthz` — `{"ok":true}` once ≥1 warm lane exists (dsh-stack probe)
- `GET /api/*` — pool, settings, burned IPs, metrics, seed, refresh
- `GET /` — live dashboard (SSE-driven)

## Changes vs the deleted upstream copy

- **TLS verification restored** — all 6 `verify=False` sites removed. A proxy
  can see destination and timing, never plaintext (the Rust 1.1.8 rule).
- **Per-lane client reuse** — `Lane.client()` caches one `AsyncClient` per lane
  with keep-alive instead of building a transport per request; evicted lanes
  close their client (`_evict_lane`).
- **Secret file hygiene** — `settings.json`, `lanes.json`, `burned.json` are
  written 0600 atomically (`_write_private`).
- **`main.py` binds 127.0.0.1** — was `0.0.0.0`, which would expose the keys.
- **`loadtest.py` round interval 1s → 4s** (`ROUND_WAIT_SEC`) plus env-driven
  `RELAY_URL` / `RELAY_API_KEY` / `PROBE_MODEL`.
- **+100 validated proxy sources** appended to `TEXT_SOURCES` (~149 total).
- **Deps updated** for Python 3.14 (`httpx[socks]>=0.28`, FastAPI ≥0.119),
  `python-dotenv` added (was imported but undeclared).

## Run

```bash
./run.sh                       # creates .venv, listens on 127.0.0.1:8901
PORT=8905 ./run.sh             # different port
```

State files default beside this directory; override with `SETTINGS_FILE`,
`LANES_FILE`, `BURNED_FILE` env vars. To share the Rust relay's key and
Webshare token, point `SETTINGS_FILE` at `zen-router/settings.json`.

## Under dsh-stack

Point the proxy component at this implementation (one-time):

```bash
python3 - <<'EOF'
import json
f = '/home/amrhares/.config/dsh-stack/config.json'
c = json.load(open(f))
rest = '/home/amrhares/Documents/zen-relay/zen-router/rest'
c['proxy']['command'] = [f'{rest}/run.sh']
c['proxy']['cwd'] = rest
c['proxy']['env']['SETTINGS_FILE'] = '/home/amrhares/Documents/zen-relay/zen-router/settings.json'
c['proxy']['env']['LANES_FILE'] = f'{rest}/lanes.json'
c['proxy']['env']['BURNED_FILE'] = f'{rest}/burned.json'
json.dump(c, open(f, 'w'), indent=2)
EOF
systemctl --user restart dsh-stack
```

`/healthz` returns `{"ok":true}` — the stack's `expectJsonOk` probe accepts it.

## Load test

```bash
RELAY_API_KEY=$(grep -oE 'rly_[0-9a-f]{32}' ../settings.json | head -1) \
  .venv/bin/python loadtest.py 10 5     # 10 parallel × 5 rounds, 4s apart
```
