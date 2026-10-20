# zen-relay (Rust)

A from-scratch Rust rewrite of `../ip_relay.py` — the same relay in one binary:
it carries the OpenCode CLI fingerprint upstream and rotates the egress IP per
request, so nothing here ever leaves the machine except through a proxy lane.

Same settings file, same lane/burn state files, same API surface as the Python
relay, so `dsh` keeps talking to `http://127.0.0.1:8901/v1` and nothing on the
client side changed.

## Run

```bash
cd /home/ai/.omp/zen-router/rust && cargo build --release
```

Production (systemd, port 8901):

```bash
systemctl --user restart zen-rotator      # unit runs target/release/zen-relay
curl -s http://127.0.0.1:8901/healthz     # {"version":"1.1.1", "warm":.., "ok":true, ..}
```

Staging / a second instance on another port (own state, production untouched):

```bash
PORT=8904 /home/ai/.omp/zen-router/staging/start.sh   # state files in staging/
```

Environment: `PORT` (default 8901), `HOST` (default 127.0.0.1), `SETTINGS_FILE`,
`LANES_FILE`, `BURNED_FILE`, `IP_RELAY_NO_BACKGROUND=1` (serve only, no
scraping/probing loops), `RELAY_ALLOW_ANONYMOUS=1` (no relay key at all).

## Roll back to the Python relay

```bash
cp /home/ai/.omp/zen-router/staging/zen-rotator.service.python.bak \
   /home/ai/.config/systemd/user/zen-rotator.service
systemctl --user daemon-reload && systemctl --user restart zen-rotator
```

State files are shared and both directions parse them, so the pool is not lost
in either direction.

## Probes (need the venv: `/home/ai/.omp/zen-router/.venv/bin/python`)

```bash
export RELAY_KEY=rly_... RELAY_URL=http://127.0.0.1:8901/v1/chat/completions
.venv/bin/python tools/probe_stream.py                    # one agent-shaped streaming turn
.venv/bin/python tools/burst_probe.py 8                   # eight at once, tallied
.venv/bin/python tools/upstream_shape_probe.py HOST:PORT socks5   # what upstream thinks
```

The probe user-agent must **not** contain `opencode`: the relay forwards a
client UA that claims to be OpenCode and drops its own fingerprint in that case
(same rule as the Python relay) — upstream then answers `426 UpgradeRequired`.

## Behavior notes carried over from Python

* 401/403 from upstream → lane parked/burned, request retried on another lane;
  the client never sees an auth-shaped error (terminal answer is 503
  `zen_lane_blocked`, retryable).
* Empty answer, or a stream that dies mid-way, → rotate internally; a cut after
  frames were already sent becomes one `stream_interrupted` frame, which `dsh`
  retries.
* The free tier answers `403 FreeTierError` per egress IP once that IP's quota
  is spent, for both stream and non-stream; probes spend that quota too.
* `/v1/models` is fetched over the machine's own IP (no lane, no quota cost).

## 1.1.1

Probed lanes enter the pool the moment their own probe passes instead of when
the whole batch finishes: a restart now rebuilds a usable pool in ~2 s instead
of leaving requests on a retryable 503 for ~30 s.
