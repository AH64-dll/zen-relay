#!/usr/bin/env python3
"""Ask the real Zen endpoint, through one egress lane, the same body twice:
once with stream:false and once with stream:true. Used to find out what the
free-tier gate actually rejects. Read-only; two requests, one lane.

Usage: python3 tools/upstream_shape_probe.py <lane-addr> <http|socks5>
"""
import json
import sys
import uuid

import httpx

LANE = sys.argv[1]
PROTO = sys.argv[2] if len(sys.argv) > 2 else "socks5"
UPSTREAM = "https://opencode.ai/inference/openai/v1/chat/completions"
MODEL = "deepseek-v4.1-flash-free"


def headers() -> dict:
    sid = f"ses_{uuid.uuid4().hex[:24]}"
    return {
        "content-type": "application/json",
        "user-agent": "opencode/latest/2.0.32/cli",
        "authorization": "Bearer public",
        "x-opencode-client": "cli",
        "x-opencode-session": sid,
        "x-opencode-session-id": sid,
        "x-session-id": sid,
        "x-session-affinity": sid,
        "x-opencode-request": f"msg_{uuid.uuid4().hex[:24]}",
        "x-opencode-project": "global",
        "x-opencode-directory": "/home/ai",
    }


def body(stream: bool) -> dict:
    return {
        "model": MODEL,
        "stream": stream,
        "max_tokens": 32,
        "messages": [
            {"role": "system", "content": "You are a coding agent running in a terminal. Be brief."},
            {"role": "user", "content": "Reply with the single word: READY"},
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


proxy = f"{PROTO}://{LANE}"
with httpx.Client(proxy=proxy, verify=False, timeout=httpx.Timeout(90.0, connect=15.0)) as client:
    for stream_flag in (False, True):
        print(f"── stream={stream_flag} via {proxy} ──")
        try:
            with client.stream("POST", UPSTREAM, json=body(stream_flag), headers=headers()) as r:
                print("status:", r.status_code, r.headers.get("content-type"))
                if stream_flag:
                    frames = []
                    for line in r.iter_lines():
                        if line.strip():
                            frames.append(line.strip()[:200])
                        if len(frames) >= 4:
                            break
                    print("frames:", json.dumps(frames, indent=1)[:600])
                else:
                    txt = r.read().decode("utf-8", "replace")
                    print("body:", txt[:500])
        except Exception as exc:  # noqa: BLE001 — this probe only reports what happened
            print("failed:", type(exc).__name__, str(exc)[:200])
