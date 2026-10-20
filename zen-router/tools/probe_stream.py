#!/usr/bin/env python3
"""Agent-shaped streaming probe for the local Zen relay (Rust or Python).

Sends one request that looks like a harness turn (system prompt + a `read`
tool + stream), and reports what came back: status, time to first byte, the
text, and how the stream ended. Read-only, one request per run.

Usage:
    RELAY_KEY=rly_... RELAY_URL=http://127.0.0.1:8904/v1/chat/completions \
        python3 tools/probe_stream.py [model]
"""
import json
import os
import sys
import time

import httpx

URL = os.environ.get("RELAY_URL", "http://127.0.0.1:8904/v1/chat/completions")
KEY = os.environ.get("RELAY_KEY", "")
MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek-v4.1-flash-free"

BODY = {
    "model": MODEL,
    "stream": True,
    "max_tokens": 64,
    "messages": [
        {
            "role": "system",
            "content": (
                "You are a coding agent running in a terminal. Be brief. "
                "If you need a file, call the read tool."
            ),
        },
        {
            "role": "user",
            "content": "Reply with exactly one short line: the word READY then a number.",
        },
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a file from disk",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ],
}

HEADERS = {
    "authorization": f"Bearer {KEY}",
    "content-type": "application/json",
    "accept": "text/event-stream",
    # Deliberately NOT opencode-shaped: the relay only replaces the fingerprint
    # when the client UA does not claim to be OpenCode already.
    "user-agent": "probe/1.0",
}

started = time.time()
ttfb = None
text = []
events = 0
kinds = {}
finished = None
error = None

with httpx.Client(timeout=httpx.Timeout(180.0, connect=20.0)) as client:
    with client.stream("POST", URL, json=BODY, headers=HEADERS) as resp:
        status = resp.status_code
        ctype = resp.headers.get("content-type", "")
        if status != 200:
            raw = resp.read().decode("utf-8", "replace")
            print(f"HTTP {status}  ({ctype})\n{raw[:800]}")
            sys.exit(2)
        for line in resp.iter_lines():
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                finished = finished or "done"
                continue
            try:
                ev = json.loads(payload)
            except ValueError:
                continue
            events += 1
            if ttfb is None:
                ttfb = time.time() - started
            choices = ev.get("choices") or [{}]
            choice = choices[0] if choices else {}
            kind = "delta"
            delta = choice.get("delta") or {}
            if "role" in delta and len(delta) == 1:
                kind = "role"
            if choice.get("finish_reason"):
                finished = choice["finish_reason"]
                kind = "finish"
            if ev.get("error"):
                kind = "error"
                error = json.dumps(ev["error"])[:400]
            kinds[kind] = kinds.get(kind, 0) + 1
            piece = delta.get("content") or ""
            if piece:
                text.append(piece)

elapsed = time.time() - started
joined = "".join(text).strip()
print(f"HTTP {status}  model={MODEL}  url={URL}")
print(f"ttfb={ttfb and round(ttfb, 2)}s  total={round(elapsed, 2)}s  events={events}  kinds={kinds}")
print(f"finish={finished}  chars={len(joined)}")
print(f"text: {joined[:400] if joined else '(EMPTY)'}")
if error:
    print(f"error frame: {error}")
if not joined and finished == "done":
    print("RESULT: empty answer")
elif joined and finished in ("stop", "length", "tool_calls", "done"):
    print("RESULT: ok")
