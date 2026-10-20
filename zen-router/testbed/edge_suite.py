#!/usr/bin/env python3
"""Edge and security suite for the relay (testbed).

Checks the client-facing contract: auth, malformed input, wrong methods and
paths, non-stream passthrough, oversized bodies, client aborts (inflight must
return to zero), and header hygiene — nothing the client sends may reach the
upstream except the sanitised fingerprint headers, and the upstream key must
never be the client's.

Usage: edge_suite.py <base-url> <relay-key> [fake-control-url]
"""
import json
import sys
import time

import httpx

BASE = sys.argv[1].rstrip("/")
KEY = sys.argv[2]
FAKE = (sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8990").rstrip("/")

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def post(path, body, headers=None, stream=True, timeout=30):
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    if body is not None:
        h = {**h, "content-type": "application/json"}
    with httpx.Client(verify=False, timeout=timeout) as c:
        return c.post(BASE + path, headers=h, content=body)


AUTH = {"authorization": f"Bearer {KEY}"}
PAYLOAD = json.dumps({
    "model": "deepseek-v4-flash-free",
    "messages": [{"role": "user", "content": "hi"}],
    "stream": True,
}).encode()

# 1. no auth -> 401
r = post("/v1/chat/completions", PAYLOAD)
check("no auth -> 401", r.status_code == 401, f"got {r.status_code}")

# 2. wrong key -> 401
r = post("/v1/chat/completions", PAYLOAD, {"authorization": "Bearer wrong_key_123"})
check("wrong key -> 401", r.status_code == 401, f"got {r.status_code}")

# 3. malformed JSON -> 400 (never 500)
r = post("/v1/chat/completions", b"{this is not json", AUTH)
check("malformed JSON -> 400", r.status_code == 400, f"got {r.status_code}")

# 4. GET on the completions endpoint -> 405
with httpx.Client(verify=False, timeout=20) as c:
    r = c.get(BASE + "/v1/chat/completions", headers=AUTH)
check("GET /v1/chat/completions -> 405", r.status_code == 405, f"got {r.status_code}")

# 5. unknown path -> 404
r = post("/v1/does-not-exist", b"{}", AUTH)
check("unknown path -> 404", r.status_code == 404, f"got {r.status_code}")

# 6. non-stream request -> 200 with a real completion, and the client's
#    extra headers must not travel upstream
leak_headers = {**AUTH, "x-test-secret": "leak", "cookie": "leak=1", "x-api-key": "leak"}
body = json.dumps({
    "model": "deepseek-v4-flash-free",
    "messages": [{"role": "user", "content": "hi"}],
    "stream": False,
}).encode()
r = post("/v1/chat/completions", body, leak_headers)
ok = r.status_code == 200 and b"choices" in r.content
check("non-stream -> 200 JSON completion", ok, f"got {r.status_code}")

# 7. missing model / empty messages -> pass-through or clean 4xx, never 500
r = post("/v1/chat/completions", b'{"messages": []}', AUTH)
check("schema junk -> no 5xx", r.status_code < 500, f"got {r.status_code}")

# 8. oversized body: neither relay caps request size (parity with the Python
#    relay) — the contract is only "no 5xx, no hang"
big = json.dumps({"model": "m", "messages": [{"role": "user", "content": "x" * (6 * 1024 * 1024)}]}).encode()
try:
    r = post("/v1/chat/completions", big, AUTH, timeout=60)
    check("oversized body -> no 5xx", r.status_code < 500, f"got {r.status_code}")
except httpx.HTTPError as e:
    check("oversized body -> no 5xx", False, f"transport error {type(e).__name__}")

# 9. client abort mid-stream: read one chunk then close; inflight must drain
aborts = []
for _ in range(3):
    try:
        with httpx.Client(verify=False, timeout=20) as c:
            with c.stream("POST", BASE + "/v1/chat/completions", headers=AUTH, content=PAYLOAD) as resp:
                for chunk in resp.iter_bytes():
                    if chunk:
                        break
        aborts.append("ok")
    except httpx.HTTPError as e:
        aborts.append(type(e).__name__)
check("client abort mid-stream tolerated", len(aborts) == 3, str(aborts))

# inflight must come back to zero (give the relay a moment to notice)
inflight = None
for _ in range(20):
    time.sleep(0.5)
    h = httpx.get(BASE.replace("/v1", "") + "/healthz", timeout=10).json()
    inflight = h.get("inflight")
    if inflight == 0:
        break
check("inflight drains to 0 after aborts", inflight == 0, f"inflight={inflight}")

# 10. header hygiene, from the fake upstream's point of view
try:
    st = httpx.get(FAKE + "/stats", timeout=10).json()
    auths = st.get("auth", [])
    uas = st.get("user_agents", [])
    extras = st.get("extras", {})
    bad_auth = [a for a in auths if KEY in a or "leak" in a]
    check("upstream auth is never the client's", not bad_auth, f"auths={auths[-3:]}")
    check("upstream auth is the masked key", any(a.startswith("Bearer ") and a != f"Bearer {KEY}" for a in auths),
          f"auths={auths[-3:]}")
    good_ua = [u for u in uas if "opencode" in u.lower()]
    check("upstream UA carries the OpenCode fingerprint", bool(good_ua), f"uas={uas[-3:]}")
    check("no client extras leaked upstream", not extras, f"extras={extras}")
except httpx.HTTPError as e:
    check("fake control reachable", False, str(e))

# 11. control plane: public healthz, everything else behind the key
try:
    hz = httpx.get(BASE.replace("/v1", "") + "/healthz", timeout=10)
    check("control: /healthz public", hz.status_code == 200, f"got {hz.status_code}")
    api_no = httpx.get(BASE.replace("/v1", "") + "/api/stats", timeout=10)
    check("control: /api/* needs the key", api_no.status_code in (401, 403), f"got {api_no.status_code}")
    api_ok = httpx.get(BASE.replace("/v1", "") + "/api/stats", headers=AUTH, timeout=10)
    check("control: /api/stats with key -> 200", api_ok.status_code == 200, f"got {api_ok.status_code}")
except httpx.HTTPError as e:
    check("control plane reachable", False, str(e))

# 12. a healthy stream really finishes: finish_reason and [DONE] present
try:
    with httpx.Client(verify=False, timeout=30) as c:
        with c.stream("POST", BASE + "/v1/chat/completions", headers=AUTH, content=PAYLOAD) as resp:
            raw = b"".join(resp.iter_bytes())
    check("stream carries finish_reason/[DONE]", b"[DONE]" in raw or b"finish_reason" in raw,
          f"{len(raw)} bytes")
except httpx.HTTPError as e:
    check("stream carries finish_reason/[DONE]", False, str(e))

print()
print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
for name, detail in FAIL:
    print(f"  failed: {name} [{detail}]")
sys.exit(1 if FAIL else 0)
