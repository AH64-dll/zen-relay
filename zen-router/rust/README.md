# zen-relay (Rust source)

Source code for the prebuilt binary at `../bin/zen-relay`.

## Build

```bash
cargo build --release
# output: target/release/zen-relay
```

## Run

From `zen-router/`:

```bash
PORT=8901 ./bin/zen-relay
```

Or run the binary built from source:

```bash
PORT=8901 ./rust/target/release/zen-relay
```

The relay reads and writes `settings.json`, `lanes.json`, and `burned.json` in the current working directory. On first run it creates `settings.json` with a random relay API key.

Environment variables: `PORT` (default `8901`), `HOST` (default `127.0.0.1`), `SETTINGS_FILE`, `LANES_FILE`, `BURNED_FILE`, `IP_RELAY_NO_BACKGROUND=1`, `RELAY_ALLOW_ANONYMOUS=1`.

## Behavior

- OpenAI-compatible `/v1/models` and `/v1/chat/completions`.
- Rotating egress lanes with an OpenCode-style upstream fingerprint.
- 429/403/5xx/network/empty/stream failures rotate silently.
- SSE streaming passthrough with completion validation.
- Image requests are detected and given a longer peek deadline.
