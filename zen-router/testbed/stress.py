#!/usr/bin/env python3
"""Concurrent stress harness for the relay testbed.

Fires TOTAL agent-shaped streaming requests at CONC at once and reports how the
client saw each one. The relay is expected to hide upstream failures: the only
answers a client may get are a full stream, one retryable "interrupted" frame
(stream died after frames were already sent), or a retryable 503. Anything that
looks like an upstream auth/quota error is a test failure.

Usage:
  stress.py --url http://127.0.0.1:8906/v1/chat/completions --key test_key \
            --total 10000 --conc 24 [--out results.json] [--progress 1000]
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time

import httpx

RETRYABLE_503 = {"rotator_exhausted", "zen_lane_blocked"}


def build_body(model: str, i: int) -> dict:
    return {
        "model": model,
        "stream": True,
        "max_tokens": 32,
        "messages": [
            {"role": "system", "content": "You are a coding agent running in a terminal. Be brief."},
            {"role": "user", "content": f"Reply with exactly: READY {i}"},
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
    "content-type": "application/json",
    "accept": "text/event-stream",
    "user-agent": "dsh-testbed/1.0",
}


class Tally:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttfb: list[float] = []
        self.slow: list[tuple[float, str]] = []
        self.invariants = {"auth_error_to_client": 0, "other_http": 0, "unknown_503": 0, "bad_content_type": 0}

    def add(self, outcome: str, ttfb: float | None, secs: float, note: str = "") -> None:
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        if ttfb is not None:
            self.ttfb.append(ttfb)
        if secs > 20:
            self.slow.append((round(secs, 1), outcome + (" — " + note if note else "")))

    def summary(self) -> dict:
        ttfb = sorted(self.ttfb)
        def pct(p: float) -> float:
            if not ttfb:
                return 0.0
            k = min(len(ttfb) - 1, int(round((len(ttfb) - 1) * p)))
            return round(ttfb[k], 2)
        return {
            "counts": dict(sorted(self.counts.items())),
            "ttfb_p50": pct(0.50),
            "ttfb_p90": pct(0.90),
            "ttfb_p99": pct(0.99),
            "ttfb_max": round(ttfb[-1], 2) if ttfb else 0.0,
            "slow_over_20s": self.slow[:10],
            "invariants": self.invariants,
        }


async def one(client: httpx.AsyncClient, url: str, key: str, model: str, i: int, tally: Tally, lock: asyncio.Lock) -> None:
    started = time.time()
    ttfb = None
    chars = 0
    outcome = "?"
    note = ""
    finish = None
    done_frame = False
    try:
        async with client.stream(
            "POST",
            url,
            json=build_body(model, i),
            headers={**HEADERS, "authorization": f"Bearer {key}"},
        ) as resp:
            ctype = resp.headers.get("content-type", "")
            if resp.status_code != 200:
                raw = await resp.aread()
                body = raw.decode("utf-8", "replace")
                try:
                    err = json.loads(body).get("error", {})
                    etype = err.get("type", "") if isinstance(err, dict) else ""
                except (ValueError, AttributeError):
                    etype = ""
                if resp.status_code in (401, 403):
                    outcome = f"http-{resp.status_code}"
                    async with lock:
                        tally.invariants["auth_error_to_client"] += 1
                elif resp.status_code == 503 and etype in RETRYABLE_503:
                    outcome = f"503-{etype}"
                elif resp.status_code == 503:
                    outcome = "503-unknown"
                    note = etype
                    async with lock:
                        tally.invariants["unknown_503"] += 1
                else:
                    outcome = f"http-{resp.status_code}"
                    note = body[:120]
                    async with lock:
                        tally.invariants["other_http"] += 1
                async with lock:
                    tally.add(outcome, None, time.time() - started, note)
                return
            if "event-stream" not in ctype:
                async with lock:
                    tally.invariants["bad_content_type"] += 1
            async for line in resp.aiter_lines():
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
                    outcome = "in-band-" + str((ev.get("error") or {}).get("type", "error"))
                    continue
                choices = ev.get("choices") or [{}]
                choice = choices[0] if choices else {}
                delta = choice.get("delta") or {}
                chars += len(delta.get("content") or "")
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
    except httpx.TimeoutException:
        outcome = "timeout"
    except Exception as exc:  # noqa: BLE001 — the harness reports what happened
        outcome = "transport"
        note = f"{type(exc).__name__}: {exc}"[:120]
    async with lock:
        tally.add(outcome, ttfb, time.time() - started, note)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--total", type=int, default=10000)
    ap.add_argument("--conc", type=int, default=24)
    ap.add_argument("--model", default="deepseek-v4.1-flash-free")
    ap.add_argument("--out", default="")
    ap.add_argument("--progress", type=int, default=1000)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    tally = Tally()
    lock = asyncio.Lock()
    limits = httpx.Limits(max_connections=args.conc * 2, max_keepalive_connections=args.conc)
    started = time.time()
    next_report = args.progress
    sem = asyncio.Semaphore(args.conc)

    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0), limits=limits) as client:
        async def worker(i: int) -> None:
            async with sem:
                await one(client, args.url, args.key, args.model, i, tally, lock)

        tasks = [asyncio.create_task(worker(i)) for i in range(args.total)]
        done = 0
        for fut in asyncio.as_completed(tasks):
            await fut
            done += 1
            if args.progress and done >= next_report:
                elapsed = time.time() - started
                print(
                    f"  [{done}/{args.total}] {elapsed:.1f}s  {done / max(elapsed, 1e-9):.0f} req/s  "
                    f"{json.dumps(tally.counts)}",
                    flush=True,
                )
                next_report += args.progress

    wall = time.time() - started
    summary = tally.summary()
    summary.update(
        {
            "label": args.label,
            "url": args.url,
            "total": args.total,
            "conc": args.conc,
            "wall_s": round(wall, 2),
            "rps": round(args.total / max(wall, 1e-9), 1),
            "when": time.strftime("%F %T"),
        }
    )
    print(json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    inv = summary["invariants"]
    bad = inv["auth_error_to_client"] + inv["other_http"] + inv["unknown_503"]
    print("INVARIANTS: " + ("PASS" if bad == 0 else f"FAIL {inv}"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
