#!/usr/bin/env python3
"""Rate-limit contract suite against a testbed relay.

mixed  : 12/22 lanes answer 429 — every client request must still end 200 from
         a clean lane; at least one failover must be observed; no client
         response may be a 429 or carry upstream rate-limit text.
all429 : every lane answers 429 — every client request must come back a
         retryable 503 with the generic body, never a 429 and never the
         upstream rate-limit text.

Usage: rl_suite.py <base-url> <relay-key> mixed|all429
"""
import json
import os
import re
import sys

import httpx

BASE = sys.argv[1].rstrip("/")
KEY = sys.argv[2]
MODE = sys.argv[3]
AUTH = {"authorization": f"Bearer {KEY}"}
OK_LANES = set(range(9112, 9122)) if MODE == "mixed" else set()
if MODE == "mixed" and os.environ.get("RL_OK_LANES"):
    OK_LANES = {int(x) for x in os.environ["RL_OK_LANES"].split(",")}

S_PAYLOAD = json.dumps({"model": "beam-free", "messages": [{"role": "user", "content": "hi"}], "stream": True}).encode()
J_PAYLOAD = json.dumps({"model": "beam-free", "messages": [{"role": "user", "content": "hi"}], "stream": False}).encode()

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def relay_stats():
    return httpx.get(BASE + "/api/stats", headers=AUTH, timeout=10).json()["stats"]


def fake_lane_429s():
    try:
        st = httpx.get("http://127.0.0.1:8990/stats", timeout=10).json()
    except httpx.HTTPError:
        return -1
    return sum(int(v.get("429", 0)) for v in st.get("lanes", {}).values())


def one(kind):
    payload = S_PAYLOAD if kind == "stream" else J_PAYLOAD
    with httpx.Client(timeout=90) as c:
        r = c.post(BASE + "/v1/chat/completions",
                   headers={**AUTH, "content-type": "application/json"}, content=payload)
        return r.status_code, r.content


before = relay_stats()
statuses = []
for i in range(4):
    for kind in ("stream", "json"):
        status, body = one(kind)
        statuses.append(status)
        if MODE == "mixed":
            ok = status == 200 and b"READY" in body
            check(f"mixed {kind}#{i + 1}: ends 200 with a real completion", ok, f"status={status}")
            if ok:
                m = re.search(rb"READY (\d+)", body)
                lane = int(m.group(1)) if m else -1
                check(f"mixed {kind}#{i + 1}: answer came from a clean lane", lane in OK_LANES, f"lane={lane}")
        else:
            ok = (status == 503 and b"retryable" in body
                  and b"rate limit exceeded for this IP" not in body)
            check(f"all429 {kind}#{i + 1}: retryable 503, generic body", ok, f"status={status} len={len(body)}")

after = relay_stats()
delta = after["failovers"] - before["failovers"]

if MODE == "mixed":
    check("mixed: relay failed over at least once", delta >= 1, f"failovers +{delta}")
    check("mixed: no client response was a 429", all(s != 429 for s in statuses), f"statuses={sorted(set(statuses))}")
    n429 = fake_lane_429s()
    check("mixed: traps actually fired upstream", n429 >= 1, f"lane 429s={n429}")
else:
    check("all429: relay kept rotating through lanes", delta >= 4, f"failovers +{delta}")
    check("all429: no client response was a 429", all(s == 503 for s in statuses), f"statuses={sorted(set(statuses))}")
    n429 = fake_lane_429s()
    check("all429: lanes really served 429s", n429 >= 8, f"lane 429s={n429}")

print()
print(f"PASS {len(PASSED)}  FAIL {len(FAILED)}")
for name in FAILED:
    print(f"  failed: {name}")
sys.exit(1 if FAILED else 0)
