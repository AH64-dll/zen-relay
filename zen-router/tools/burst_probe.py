#!/usr/bin/env python3
"""Concurrent burst probe for the local Zen relay.

Fires N agent-shaped streaming requests in parallel and reports how each one
ended (ok / interrupted / empty / transient-503 / error). Same shape as
tools/probe_stream.py, just N of them at once.

Usage:
    RELAY_KEY=rly_... RELAY_URL=http://127.0.0.1:8904/v1/chat/completions \
        python3 tools/burst_probe.py [N] [model]
"""
import json
import os
import sys
import threading
import time

import httpx

URL = os.environ.get("RELAY_URL", "http://127.0.0.1:8904/v1/chat/completions")
KEY = os.environ.get("RELAY_KEY", "")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 8
MODEL = sys.argv[2] if len(sys.argv) > 2 else "deepseek-v4.1-flash-free"

BODY = {
    "model": MODEL,
    "stream": True,
    "max_tokens": 48,
    "messages": [
        {"role": "system", "content": "You are a coding agent running in a terminal. Be brief."},
        {"role": "user", "content": "Reply with exactly: READY <this number>"},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a file from disk",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ],
}

HEADERS = {
    "authorization": f"Bearer {KEY}",
    "content-type": "application/json",
    "accept": "text/event-stream",
    "user-agent": "probe/1.0",
}

lock = threading.Lock()
results = []


def one(i: int) -> None:
    started = time.time()
    ttfb = None
    chars = 0
    reasoning = 0
    done_frame = False
    outcome = "?"
    finish = None
    note = ""

    def record() -> None:
        with lock:
            results.append(
                {
                    "id": i,
                    "outcome": outcome,
                    "finish": finish,
                    "chars": chars,
                    "reasoning": reasoning,
                    "ttfb": None if ttfb is None else round(ttfb, 2),
                    "secs": round(time.time() - started, 2),
                    "note": note,
                }
            )

    try:
        with httpx.Client(timeout=httpx.Timeout(240.0, connect=20.0)) as client:
            with client.stream("POST", URL, json=BODY, headers=HEADERS) as resp:
                if resp.status_code != 200:
                    raw = resp.read().decode("utf-8", "replace")
                    try:
                        err = json.loads(raw).get("error", {})
                    except ValueError:
                        err = {"raw": raw[:200]}
                    outcome = f"http-{resp.status_code}"
                    note = str(err)[:160]
                    record()
                    return
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        done_frame = True
                        continue
                    try:
                        ev = json.loads(payload)
                    except ValueError:
                        continue
                    if ttfb is None:
                        ttfb = time.time() - started
                    if ev.get("error"):
                        outcome = "in-band-error"
                        note = json.dumps(ev["error"])[:160]
                        continue
                    choices = ev.get("choices") or [{}]
                    choice = choices[0] if choices else {}
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or ""
                    chars += len(piece)
                    reasoning += len(delta.get("reasoning_content") or "")
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                if outcome == "?":
                    if finish:
                        outcome = "ok" if chars > 0 else "reasoning-only"
                    elif chars > 0:
                        outcome = "ok-truncated"
                    elif done_frame:
                        outcome = "empty"
                    else:
                        outcome = "interrupted"
    except Exception as exc:  # noqa: BLE001 — the probe just reports what happened
        outcome = "transport"
        note = f"{type(exc).__name__}: {exc}"[:160]
    record()


started_all = time.time()
threads = [threading.Thread(target=one, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
wall = time.time() - started_all

with lock:
    ordered = sorted(results, key=lambda r: r["id"])
    tally: dict[str, int] = {}
    for r in ordered:
        tally[r["outcome"]] = tally.get(r["outcome"], 0) + 1

print(f"burst N={N} model={MODEL} url={URL}")
print(f"wall={round(wall, 2)}s  tally={tally}")
ttfbs = [r["ttfb"] for r in ordered if r["ttfb"] is not None]
if ttfbs:
    ttfbs_sorted = sorted(ttfbs)
    mid = ttfbs_sorted[len(ttfbs_sorted) // 2]
    print(f"ttfb: first={ttfbs_sorted[0]}s median={mid}s last={ttfbs_sorted[-1]}s")
for r in ordered:
    line = f"  #{r['id']:<2} {r['outcome']:<15} finish={r['finish']} chars={r['chars']:<4} reasoning={r['reasoning']:<5} ttfb={r['ttfb']}s total={r['secs']}s"
    if r["note"]:
        line += f"  {r['note']}"
    print(line)
