#!/usr/bin/env python3
"""Fake OpenCode Zen upstream for the relay testbed — TLS, self-signed.

A lane is identified by the preamble line the mini proxy sends before any TLS
bytes:  "LANE 9100\n". The fake answers:

  GET  <any path>  -> 200 models list        (the relay's "screen" / /v1/models)
  POST <any path>  -> chat completions, per-lane policy (stream and non-stream)

Per-lane policy: the first PASS_FIRST completions on a lane are served "ok"
(that window covers the relay's own admission probe), after that the lane's
TRAP_PLAN mode applies. JITTER adds a random trap on top of that so a long run
keeps seeing failures even on healthy lanes.

Control plane (plain HTTP, no TLS) on CONTROL_PORT:
  GET  /stats  -> JSON: per-lane counters, seen authorization/user-agent, paths
  POST /reset  -> zero all counters

Env:
  FAKE_PORT     TLS port                (default 8999)
  CONTROL_PORT  plain control port      (default 8990)
  PASS_FIRST    ok-window per lane      (default 2)
  TRAP_PLAN     "9100=ok,9114=cut,..."  (default: everything ok)
  JITTER        chance of a random trap (default 0.03)
  FAKE_LOG      log file                (optional)
"""
import json
import os
import random
import socket
import socketserver
import ssl
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_PORT = int(os.environ.get("FAKE_PORT", "8999"))
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "8990"))
PASS_FIRST = int(os.environ.get("PASS_FIRST", "2"))
JITTER = float(os.environ.get("JITTER", "0.03"))
LOG_PATH = os.environ.get("FAKE_LOG", os.path.join(HERE, "logs", "fake.log"))
CERT = os.path.join(HERE, "cert.pem")
KEY = os.path.join(HERE, "key.pem")

MODES = ("ok", "cut", "empty", "403", "429")
MODELS = ["beam-free", "deepseek-v4.1-flash-free", "mimo-v2.6-flash-free", "glm-5.3-flash-free"]

STATE = {"lanes": {}, "auth": [], "uas": [], "paths": {}, "extras": {}, "started": time.time()}
LOCK = threading.Lock()

_log_lock = threading.Lock()


def log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with _log_lock, open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def parse_plan(raw: str) -> dict:
    plan = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            port = int(k.strip())
        except ValueError:
            continue
        mode = v.strip()
        if mode in MODES:
            plan[port] = mode
    return plan


PLAN = parse_plan(os.environ.get("TRAP_PLAN", ""))


def lane_stats(lane: int) -> dict:
    return STATE["lanes"].setdefault(
        int(lane),
        {"requests": 0, "stream": 0, "nonstream": 0, "jit": 0, **{m: 0 for m in MODES}},
    )


def pick_mode(lane: int) -> tuple[str, bool]:
    """Decide what this request does, then count it."""
    st = lane_stats(lane)
    with LOCK:
        n = st["requests"]
    mode = PLAN.get(lane, "ok")
    jittered = False
    if n < PASS_FIRST:
        mode = "ok"
    elif random.random() < JITTER:
        jittered = True
        if mode == "ok":
            mode = random.choices(["cut", "empty", "429", "403"], weights=[4, 3, 2, 1])[0]
    return mode, jittered


def record(lane: int, headers: dict, path: str, mode: str, stream: bool, jittered: bool) -> None:
    st = lane_stats(lane)
    with LOCK:
        st["requests"] += 1
        st["stream" if stream else "nonstream"] += 1
        st[mode] = st.get(mode, 0) + 1
        if jittered:
            st["jit"] += 1
        STATE["auth"] = (STATE["auth"] + [headers.get("authorization", "")])[-100:]
        STATE["uas"] = (STATE["uas"] + [headers.get("user-agent", "")])[-100:]
        STATE["paths"][path] = STATE["paths"].get(path, 0) + 1
        for k, v in headers.items():
            if k.startswith("x-test") or k in ("cookie", "x-api-key", "x-forwarded-for"):
                STATE["extras"][k] = v
    log(f"lane={lane} {mode}{' jit' if jittered else ''} {'stream' if stream else 'json'} {path}")


def sse_frame(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def chunk(model: str, delta: dict, finish=None) -> bytes:
    return sse_frame(
        {
            "id": "chatcmpl-testbed",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
    )


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        raw = self.request
        try:
            raw.settimeout(120)
            pre = b""
            while not pre.endswith(b"\n") and len(pre) < 64:
                b = raw.recv(1)
                if not b:
                    return
                pre += b
            lane = 0
            if pre.startswith(b"LANE "):
                try:
                    lane = int(pre.split()[1])
                except (IndexError, ValueError):
                    lane = 0
            tls = self.server.ctx.wrap_socket(raw, server_side=True)
            self.serve(tls, lane)
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                raw.close()
            except OSError:
                pass

    def serve(self, s: socket.socket, lane: int) -> None:
        f = s.makefile("rb")
        while True:
            line = f.readline()
            if not line:
                return
            if line in (b"\r\n", b"\n"):
                continue
            parts = line.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]
            headers = {}
            while True:
                hl = f.readline()
                if not hl or hl in (b"\r\n", b"\n"):
                    break
                k, _, v = hl.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            body = b""
            try:
                clen = int(headers.get("content-length", "0") or 0)
            except ValueError:
                clen = 0
            if clen:
                body = f.read(clen)
            if method == "GET":
                self.send_json(
                    s,
                    200,
                    {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "opencode"} for m in MODELS]},
                )
                return
            if method != "POST":
                self.send_json(s, 405, {"error": {"message": "method not allowed", "type": "invalid_request"}})
                return
            self.completion(s, lane, path, headers, body)
            return

    def completion(self, s: socket.socket, lane: int, path: str, headers: dict, body: bytes) -> None:
        payload = {}
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
        model = str(payload.get("model") or "beam-free")
        stream = bool(payload.get("stream"))
        mode, jittered = pick_mode(lane)
        record(lane, headers, path, mode, stream, jittered)

        if mode == "403":
            self.send_json(
                s,
                403,
                {
                    "type": "error",
                    "error": {
                        "type": "FreeTierError",
                        "message": "OpenCode's free tier can only be used from within OpenCode.",
                    },
                },
            )
            return
        if mode == "429":
            self.send_json(
                s,
                429,
                {"error": {"type": "rate_limit_error", "message": f"rate limit exceeded for this IP ({lane})"}},
            )
            return

        if not stream:
            text = "" if mode == "empty" else f"READY {lane}"
            self.send_json(
                s,
                200,
                {
                    "id": "chatcmpl-testbed",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
                },
            )
            return

        s.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
        )

        def w(frame: bytes) -> None:
            s.sendall(b"%x\r\n" % len(frame) + frame + b"\r\n")

        if mode == "cut":
            # commit one content frame, then drop the connection mid-stream
            w(chunk(model, {"role": "assistant"}))
            w(chunk(model, {"content": f"READY {lane}"}))
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        if mode == "empty":
            w(chunk(model, {"role": "assistant"}))
            w(b"data: [DONE]\n\n")
            s.sendall(b"0\r\n\r\n")
            return

        w(chunk(model, {"role": "assistant"}))
        for piece in (f"READY {lane}", " ok", "."):
            w(chunk(model, {"content": piece}))
        w(chunk(model, {}, finish="stop"))
        w(b"data: [DONE]\n\n")
        s.sendall(b"0\r\n\r\n")

    @staticmethod
    def send_json(s: socket.socket, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        phrase = {200: "OK", 403: "Forbidden", 405: "Method Not Allowed", 429: "Too Many Requests"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {phrase}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        s.sendall(head + body)


class ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ControlHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            data = self.request.recv(4096).decode("latin-1", "replace")
        except OSError:
            return
        request_line = data.split("\r\n", 1)[0]
        parts = request_line.split()
        method = parts[0] if parts else ""
        path = parts[1].split("?")[0] if len(parts) > 1 else "/"
        if path == "/reset" and method in ("POST", "GET"):
            with LOCK:
                STATE["lanes"] = {}
                STATE["auth"] = []
                STATE["uas"] = []
                STATE["paths"] = {}
                STATE["extras"] = {}
            body = json.dumps({"ok": True}).encode()
        elif path == "/stats":
            with LOCK:
                body = json.dumps(
                    {
                        "lanes": STATE["lanes"],
                        "plan": {str(k): v for k, v in PLAN.items()},
                        "pass_first": PASS_FIRST,
                        "jitter": JITTER,
                        "auth": sorted(set(STATE["auth"])),
                        "user_agents": sorted(set(STATE["uas"])),
                        "paths": STATE["paths"],
                        "extras": STATE["extras"],
                        "uptime": round(time.time() - STATE["started"], 1),
                    }
                ).encode()
        else:
            body = json.dumps({"error": "unknown"}).encode()
        head = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        try:
            self.request.sendall(head + body)
        except OSError:
            pass


def main() -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    tls = ThreadingServer(("127.0.0.1", FAKE_PORT), Handler)
    tls.ctx = ctx
    ctl = ThreadingServer(("127.0.0.1", CONTROL_PORT), ControlHandler)
    log(f"fake zen: tls=127.0.0.1:{FAKE_PORT} control=127.0.0.1:{CONTROL_PORT} pass_first={PASS_FIRST} jitter={JITTER} plan={PLAN}")
    threading.Thread(target=ctl.serve_forever, daemon=True).start()
    tls.serve_forever()


if __name__ == "__main__":
    main()
