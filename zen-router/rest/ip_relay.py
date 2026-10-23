# ip-relay — per-IP quota API relay with automatic egress rotation
#
# Sits between a gateway (9router, OpenRouter-style aggregators, your own
# client) and ANY upstream OpenAI-compatible API whose free tier is limited
# PER IP ADDRESS (e.g. opencode.ai/zen/v1, Groq, SambaNova, Together, DeepSeek,
# or a custom endpoint). Each unique egress IP carries its own quota, so this
# relay rotates through a proxy pool and lets one base URL serve more
# free-model traffic than a single IP would allow.
# Your server's own IP never touches the upstream.
#
# ── design notes (v0.6, the resilience rewrite) ─────────────────────────
#
# Free public proxies are ephemeral: measured survival is ~1 request before
# death or rate-limit. So the pool manager can't promise "N stable lanes" —
# it must make requests survive *proxy churn* instead. This build does that:
#
#   1. MULTI-PROTOCOL SOURCES — HTTP/HTTPS + SOCKS4 + SOCKS5 + (optional)
#      Webshare free-tier API token, ~20 feeds. Wider net = more fresh IPs.
#   2. SCORED LANES — every lane carries an EWMA score (success, latency,
#      recency). Requests go to the best lane first, not a random one.
#   3. TRANSPARENT FAILOVER — a request that hits a dead/burned proxy is
#      retried on the next-best lane *within the same HTTP request*, so the
#      client never sees a proxy failure. Key-global 429s (FreeUsageLimitError)
#      burn NO lanes — the IPs are fine, the key is flat-out; the relay fails
#      fast and pauses its own probing until the quota window resets.
#   4. RECOVERY LOOPS — quota windows reset. Parked (burned) lanes are
#      re-probed after a short cooldown and return to service automatically.
#      Nothing is discarded permanently.
#   5. CHEAP PRE-PROBE, REAL CONFIRM — candidates are first TCP/CONNECT
#      screened (no upstream quota spent), then confirmed with a 1-token
#      upstream probe. Slow-but-working proxies (up to PROXY_PROBE_TIMEOUT)
#      are kept — the old 12s cutoff threw away good lanes.
#
# Env config — see README.md for the full table.

from __future__ import annotations

import asyncio
import collections
import contextlib
import hmac
import json
import logging
import os
import re
import time
import uuid

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

log = logging.getLogger("ip-relay")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)

VERSION = "1.0.0"

# ── settings (env as base, settings.json overlays; UI writes the file) ──
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "settings.json")
LANES_FILE = os.environ.get("LANES_FILE", "lanes.json")
DEFAULTS = {
    "upstream_base_url": "https://opencode.ai/inference/openai/v1",
    "screen_url": "https://opencode.ai/zen/v1/models",
    "upstream_api_key": "public",
    "relay_api_key": "",
    "probe_model": "beam-free",
    # egress / pool
    "proxy_pool_target": 30,          # how many confirmed lanes to keep warm
    "proxy_max_candidates": 3000,     # cap candidates held in the reservoir
    "proxy_test_concurrency": 60,     # concurrent probes (ceiling when adaptive)
    "proxy_probe_timeout": 7,         # wait 7s for proxy response on probe
    "stage2_timeout": 25,             # completion-probe budget (separate from the 7s request bound)
    "relay_proxy_timeout": 7,         # wait 7s per proxy attempt before switching
    "relay_attempts": 80,             # lanes tried per client request — sweep the
                                      # whole pool (cap raised to 500 in BOUNDS)
    "lane_cooldown_sec": 3600,        # how long a burned lane is parked
    "lane_recover_sec": 3600,         # parked lane re-probe interval
    "direct_lane": True,              # include the server's own IP as a lane (v0.8.1: on by default)
    "webshare_token": "",             # optional Webshare free-tier API token
    "sources_enabled": True,          # pull public proxy lists (off = own lanes only)
    "allow_socks": True,              # use SOCKS4/5 sources (needs httpx[socks])
    "stage2_probe": False,            # prove candidates with a real completion.
                                      # Keep this OFF: the destination allows one
                                      # completion per egress IP, so a stage-2 probe
                                      # spends the allowance the lane exists to serve
                                      # and the first real request arrives at a spent
                                      # IP. Measured: with it on the pool stays at ~1
                                      # lane, with it off it fills to the target.
                                      # Candidates are promoted after the reachability
                                      # screen and proven by their first success.
    # v0.8: capacity + safety
    "lane_max_inflight": 2,           # concurrent requests allowed per lane
    "max_lanes_per_subnet": 3,        # /24 diversity cap (0 = unlimited)
    "adaptive_concurrency": True,     # auto-tune probe concurrency to the link
    "persist_lanes": True,            # remember scored lanes across restarts
    # The upstream free tier gates on User-Agent (see build_upstream_headers).
    # Anything containing "opencode" (case-insensitive) passes; everything else
    # gets an instant 429 regardless of IP or key.
    "upstream_user_agent": "opencode/latest/2.0.32/cli",
    # ── v1.0: burned-IP memory ─────────────────────────────────────
    # Measured 2026-08-15: a 429'd egress IP does NOT recover on a short timer.
    # A 40-minute continuous poll of one burned IP returned 429 on all ~80
    # attempts, and that IP had been refused for 8 days. So burn is effectively
    # permanent, and the old behaviour (park 90s, re-probe, re-add from the
    # scrape feeds next cycle) spent the entire probe budget re-testing corpses
    # — measured ~200 wasted probes/hour on 10 Webshare IPs alone.
    "burn_memory": True,              # remember burned IPs across scrape cycles
    "burn_ttl_sec": 86400,            # how long an IP stays blocklisted (24h)
    # ── v1.0: Tor as an egress source ──────────────────────────────
    # Tor with IsolateSOCKSAuth gives one circuit (hence one exit IP) per
    # distinct SOCKS username: arbitrarily many lanes, no signup, no cost.
    # Measured against opencode: 14 circuits -> 14 distinct exits; 40 circuits
    # -> 65% live (vs ~2% for scraped public lists); one live exit served 40/40
    # consecutive requests; 1,384 distinct exit IPs available.
    "tor_enabled": False,             # use Tor circuits as lanes
    "tor_lanes": 12,                  # how many isolated circuits to maintain
    "tor_socks_port": 9150,           # SocksPort of a tor with IsolateSOCKSAuth
    # ── v1.0: pin-and-drain ────────────────────────────────────────
    # A live exit survives 40+ requests, so spreading load thin re-pays the
    # ~35% miss cost on every request. Pin a few proven lanes and drain them.
    "lane_pin_count": 3,              # proven lanes kept hot for traffic
}
settings: dict = dict(DEFAULTS)

UPSTREAM_UA: str = str(DEFAULTS["upstream_user_agent"])

UPSTREAM_API_KEYS: list[str] = ["public"]
_upstream_key_idx: int = 0


def get_upstream_key() -> str:
    """Return the next upstream API key in round-robin sequence."""
    global _upstream_key_idx
    if not UPSTREAM_API_KEYS:
        return "public"
    key = UPSTREAM_API_KEYS[_upstream_key_idx % len(UPSTREAM_API_KEYS)]
    _upstream_key_idx += 1
    return key

# Setting bounds enforced at the API boundary (min, max). Anything outside is
# rejected with a clear error instead of silently clamped, so the operator
# learns the constraint instead of wondering why their value did nothing.
SETTING_BOUNDS: dict[str, tuple[int, int]] = {
    "proxy_pool_target": (1, 500),
    "proxy_max_candidates": (100, 100000),
    "proxy_test_concurrency": (1, 500),
    "proxy_probe_timeout": (1, 120),
    "stage2_timeout": (5, 120),
    "relay_proxy_timeout": (1, 300),
    "relay_attempts": (1, 500),
    "lane_cooldown_sec": (10, 86400),
    "lane_recover_sec": (30, 86400),
    "lane_max_inflight": (1, 32),
    "max_lanes_per_subnet": (0, 100),
    "burn_ttl_sec": (60, 2592000),
    "tor_lanes": (0, 200),
    "tor_socks_port": (1, 65535),
    "lane_pin_count": (1, 50),
}

# Named upstream profiles: base URL + probe model in one pick, so a new user
# does not have to know a provider's quirks to get a working pool.
PROVIDER_PROFILES: dict[str, dict] = {
    "opencode-zen": {
        "label": "OpenCode Zen (free tier)",
        "upstream_base_url": "https://opencode.ai/zen/v1",
        "probe_model": "deepseek-v4-flash-free",
        "upstream_api_key": "public",
        "note": "Per-IP free tier. Key 'public' is shared and often pre-burned.",
    },
    "openrouter-free": {
        "label": "OpenRouter (:free models)",
        "upstream_base_url": "https://openrouter.ai/api/v1",
        "probe_model": "meta-llama/llama-3.3-70b-instruct:free",
        "upstream_api_key": "",
        "note": "Needs your own key; free models are rate-limited per IP + key.",
    },
    "groq": {
        "label": "Groq",
        "upstream_base_url": "https://api.groq.com/openai/v1",
        "probe_model": "llama-3.3-70b-versatile",
        "upstream_api_key": "",
        "note": "Generous free tier, per-key RPM limits.",
    },
    "generic-openai": {
        "label": "Generic OpenAI-compatible",
        "upstream_base_url": "",
        "probe_model": "",
        "upstream_api_key": "",
        "note": "Any /v1 endpoint exposing /models and /chat/completions.",
    },
}

# runtime globals synced by apply_settings()
UPSTREAM_BASE_URL = DEFAULTS["upstream_base_url"]
SCREEN_URL = DEFAULTS["screen_url"]
UPSTREAM_API_KEY = DEFAULTS["upstream_api_key"]
RELAY_API_KEY = ""
PROBE_MODEL = DEFAULTS["probe_model"]
POOL_TARGET = DEFAULTS["proxy_pool_target"]
MAX_CANDIDATES = DEFAULTS["proxy_max_candidates"]
TEST_CONCURRENCY = DEFAULTS["proxy_test_concurrency"]
PROBE_TIMEOUT = DEFAULTS["proxy_probe_timeout"]
# The completion probe measures something different from a relayed request: a
# free proxy that answers in 12s is usable for a user turn that waits 90s, but
# it can never pass a probe clamped to the 7s request bound. Keeping the two
# budgets separate is what lets real lanes be proven at all.
STAGE2_TIMEOUT = DEFAULTS["stage2_timeout"]
RELAY_PROXY_TIMEOUT = DEFAULTS["relay_proxy_timeout"]
RELAY_ATTEMPTS = DEFAULTS["relay_attempts"]
LANE_COOLDOWN_SEC = DEFAULTS["lane_cooldown_sec"]
LANE_RECOVER_SEC = DEFAULTS["lane_recover_sec"]
DIRECT_LANE = DEFAULTS["direct_lane"]
WEBSHARE_TOKEN = ""
ALLOW_SOCKS = DEFAULTS["allow_socks"]
STAGE2_PROBE = DEFAULTS["stage2_probe"]
LANE_MAX_INFLIGHT = DEFAULTS["lane_max_inflight"]
MAX_LANES_PER_SUBNET = DEFAULTS["max_lanes_per_subnet"]
ADAPTIVE_CONCURRENCY = DEFAULTS["adaptive_concurrency"]
PERSIST_LANES = DEFAULTS["persist_lanes"]
BURN_MEMORY = DEFAULTS["burn_memory"]
BURN_TTL_SEC = DEFAULTS["burn_ttl_sec"]
TOR_ENABLED = DEFAULTS["tor_enabled"]
TOR_LANES = DEFAULTS["tor_lanes"]
TOR_SOCKS_PORT = DEFAULTS["tor_socks_port"]
LANE_PIN_COUNT = DEFAULTS["lane_pin_count"]

# Adaptive probe concurrency. A fixed number is wrong on every link: 60 melts
# an Iranian residential CPE's NAT table (the root cause of the "0 warm
# proxies" reports) while barely loading a datacenter uplink. Start modest,
# grow while the link is clean, halve on a connect-error storm.
ADAPT = {
    "current": 20,        # concurrency in force right now
    "min": 5,
    "last_change": 0.0,
    "last_reason": "init",
    "err_ratio": 0.0,
}


def load_settings() -> None:
    load_dotenv()
    s = {
        "upstream_base_url": os.environ.get("UPSTREAM_BASE_URL", os.environ.get("OPENCODE_BASE_URL", DEFAULTS["upstream_base_url"])).rstrip("/"),
        "screen_url": os.environ.get("SCREEN_URL", DEFAULTS["screen_url"]).strip(),
        "upstream_api_key": os.environ.get("UPSTREAM_API_KEY", os.environ.get("OPENCODE_API_KEY", DEFAULTS["upstream_api_key"])).strip(),
        "relay_api_key": os.environ.get("RELAY_API_KEY", DEFAULTS["relay_api_key"]).strip(),
        "probe_model": os.environ.get("PROBE_MODEL", DEFAULTS["probe_model"]),
        "proxy_pool_target": int(os.environ.get("PROXY_POOL_TARGET", DEFAULTS["proxy_pool_target"])),
        "proxy_max_candidates": int(os.environ.get("PROXY_MAX_CANDIDATES", DEFAULTS["proxy_max_candidates"])),
        "proxy_test_concurrency": int(os.environ.get("PROXY_TEST_CONCURRENCY", DEFAULTS["proxy_test_concurrency"])),
        "proxy_probe_timeout": int(os.environ.get("PROXY_PROBE_TIMEOUT", DEFAULTS["proxy_probe_timeout"])),
        "relay_proxy_timeout": int(os.environ.get("RELAY_PROXY_TIMEOUT", DEFAULTS["relay_proxy_timeout"])),
        "relay_attempts": int(os.environ.get("RELAY_ATTEMPTS", DEFAULTS["relay_attempts"])),
        "lane_cooldown_sec": int(os.environ.get("LANE_COOLDOWN_SEC", DEFAULTS["lane_cooldown_sec"])),
        "lane_recover_sec": int(os.environ.get("LANE_RECOVER_SEC", DEFAULTS["lane_recover_sec"])),
        "direct_lane": os.environ.get("DIRECT_LANE", "0") in ("1", "true", "yes"),
        "webshare_token": os.environ.get("WEBSHARE_TOKEN", "").strip(),
        "sources_enabled": os.environ.get("SOURCES_ENABLED", "1") in ("1", "true", "yes"),
        "allow_socks": os.environ.get("ALLOW_SOCKS", "1") in ("1", "true", "yes"),
        "lane_max_inflight": int(os.environ.get("LANE_MAX_INFLIGHT", DEFAULTS["lane_max_inflight"])),
        "max_lanes_per_subnet": int(os.environ.get("MAX_LANES_PER_SUBNET", DEFAULTS["max_lanes_per_subnet"])),
        "adaptive_concurrency": os.environ.get("ADAPTIVE_CONCURRENCY", "1") in ("1", "true", "yes"),
        "persist_lanes": os.environ.get("PERSIST_LANES", "1") in ("1", "true", "yes"),
        "upstream_user_agent": str(os.environ.get("UPSTREAM_USER_AGENT") or DEFAULTS["upstream_user_agent"]).strip(),
        "burn_memory": os.environ.get("BURN_MEMORY", "1") in ("1", "true", "yes"),
        "burn_ttl_sec": int(os.environ.get("BURN_TTL_SEC", DEFAULTS["burn_ttl_sec"])),
        "tor_enabled": os.environ.get("TOR_ENABLED", "0") in ("1", "true", "yes"),
        "tor_lanes": int(os.environ.get("TOR_LANES", DEFAULTS["tor_lanes"])),
        "tor_socks_port": int(os.environ.get("TOR_SOCKS_PORT", DEFAULTS["tor_socks_port"])),
        "lane_pin_count": int(os.environ.get("LANE_PIN_COUNT", DEFAULTS["lane_pin_count"])),
    }
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except Exception:
        pass
    apply_settings(s, persist=False)
    # Default-deny the control plane: an empty relay key means every /api route
    # (including POST /api/settings, which can repoint the upstream at an
    # attacker's collector) is world-open the moment the port is reachable.
    # Mint one, persist it, and print it once so the operator can copy it.
    if not RELAY_API_KEY and os.environ.get("RELAY_ALLOW_ANONYMOUS", "0") not in ("1", "true", "yes"):
        generated = "rly_" + uuid.uuid4().hex
        apply_settings({"relay_api_key": generated}, persist=True)
        log.warning("=" * 68)
        log.warning("SECURITY: no relay_api_key was set — generated one for you:")
        log.warning("    %s", generated)
        log.warning("Saved to %s. Use it as the Bearer token from clients and the", SETTINGS_FILE)
        log.warning("dashboard. Set RELAY_ALLOW_ANONYMOUS=1 to run without auth.")
        log.warning("=" * 68)


def _write_private(path: str, body: str) -> None:
    """Atomically write `body` to `path` with 0600 permissions. These files
    hold secrets (relay_api_key, upstream key, webshare token, proxy
    credentials), so they must never inherit a permissive process umask."""
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise

def save_settings() -> None:
    try:
        _write_private(SETTINGS_FILE, json.dumps(settings, indent=2) + "\n")
    except Exception as e:
        log.warning("could not save settings: %s", e)


def apply_settings(new: dict, persist: bool = True) -> dict:
    global UPSTREAM_API_KEY, UPSTREAM_BASE_URL, RELAY_API_KEY, PROBE_MODEL, SCREEN_URL
    global POOL_TARGET, MAX_CANDIDATES, TEST_CONCURRENCY, PROBE_TIMEOUT, STAGE2_TIMEOUT
    global RELAY_PROXY_TIMEOUT, RELAY_ATTEMPTS, LANE_COOLDOWN_SEC, LANE_RECOVER_SEC
    global DIRECT_LANE, WEBSHARE_TOKEN, ALLOW_SOCKS, SOURCES_ENABLED, STAGE2_PROBE
    global LANE_MAX_INFLIGHT, MAX_LANES_PER_SUBNET, ADAPTIVE_CONCURRENCY, PERSIST_LANES
    global UPSTREAM_API_KEYS, UPSTREAM_UA
    global BURN_MEMORY, BURN_TTL_SEC, TOR_ENABLED, TOR_LANES, TOR_SOCKS_PORT, LANE_PIN_COUNT
    old_base, old_key = UPSTREAM_BASE_URL, UPSTREAM_API_KEY
    for k, v in new.items():
        if k in DEFAULTS:
            settings[k] = v
    UPSTREAM_API_KEY = str(settings["upstream_api_key"]).strip()
    raw_keys = [k.strip() for k in UPSTREAM_API_KEY.replace("\n", ",").split(",") if k.strip()]
    UPSTREAM_API_KEYS = raw_keys if raw_keys else ["public"]
    UPSTREAM_BASE_URL = str(settings["upstream_base_url"]).rstrip("/")
    SCREEN_URL = str(settings.get("screen_url") or DEFAULTS["screen_url"]).strip()
    if not (SCREEN_URL.startswith("http://") or SCREEN_URL.startswith("https://")):
        SCREEN_URL = str(DEFAULTS["screen_url"])
    RELAY_API_KEY = str(settings["relay_api_key"]).strip()
    PROBE_MODEL = str(settings["probe_model"])
    POOL_TARGET = max(1, int(settings["proxy_pool_target"]))
    MAX_CANDIDATES = max(10, int(settings["proxy_max_candidates"]))
    TEST_CONCURRENCY = max(1, int(settings["proxy_test_concurrency"]))
    PROBE_TIMEOUT = max(1, int(settings["proxy_probe_timeout"]))
    STAGE2_TIMEOUT = max(5, int(settings.get("stage2_timeout", DEFAULTS["stage2_timeout"])))
    RELAY_PROXY_TIMEOUT = max(1, int(settings["relay_proxy_timeout"]))
    RELAY_ATTEMPTS = max(1, int(settings["relay_attempts"]))
    LANE_COOLDOWN_SEC = max(30, int(settings["lane_cooldown_sec"]))
    LANE_RECOVER_SEC = max(60, int(settings["lane_recover_sec"]))
    DIRECT_LANE = bool(settings["direct_lane"])
    WEBSHARE_TOKEN = str(settings["webshare_token"]).strip()
    SOURCES_ENABLED = bool(settings.get("sources_enabled", DEFAULTS["sources_enabled"]))
    ALLOW_SOCKS = bool(settings["allow_socks"])
    STAGE2_PROBE = bool(settings.get("stage2_probe", DEFAULTS["stage2_probe"]))
    LANE_MAX_INFLIGHT = max(1, int(settings["lane_max_inflight"]))
    MAX_LANES_PER_SUBNET = max(0, int(settings["max_lanes_per_subnet"]))
    ADAPTIVE_CONCURRENCY = bool(settings["adaptive_concurrency"])
    PERSIST_LANES = bool(settings["persist_lanes"])
    BURN_MEMORY = bool(settings.get("burn_memory", DEFAULTS["burn_memory"]))
    BURN_TTL_SEC = max(60, int(settings.get("burn_ttl_sec", DEFAULTS["burn_ttl_sec"])))
    TOR_ENABLED = bool(settings.get("tor_enabled", DEFAULTS["tor_enabled"]))
    TOR_LANES = max(0, int(settings.get("tor_lanes", DEFAULTS["tor_lanes"])))
    TOR_SOCKS_PORT = int(settings.get("tor_socks_port", DEFAULTS["tor_socks_port"]))
    LANE_PIN_COUNT = max(1, int(settings.get("lane_pin_count", DEFAULTS["lane_pin_count"])))
    UPSTREAM_UA = str(settings.get("upstream_user_agent") or DEFAULTS["upstream_user_agent"]).strip() \
        or str(DEFAULTS["upstream_user_agent"])
    if "opencode" not in UPSTREAM_UA.lower():
        log.warning("upstream_user_agent %r does not contain 'opencode' — the free tier "
                    "will answer 429 for every request. Falling back to %r.",
                    UPSTREAM_UA, DEFAULTS["upstream_user_agent"])
        UPSTREAM_UA = str(DEFAULTS["upstream_user_agent"])
    if not ADAPTIVE_CONCURRENCY:
        ADAPT["current"] = TEST_CONCURRENCY
    else:
        ADAPT["current"] = min(ADAPT["current"], TEST_CONCURRENCY)
    if UPSTREAM_BASE_URL != old_base or UPSTREAM_API_KEY != old_key:
        _models_cache["updated"] = 0.0
    if persist:
        save_settings()
    return dict(settings)


def validate_settings_payload(body: dict) -> tuple[dict, list[str]]:
    """Split an incoming settings payload into (accepted updates, errors).

    Masked secrets are skipped (never overwrite a real key with 'abc...'),
    numeric fields are bounds-checked, and unknown keys are ignored.
    """
    updates: dict = {}
    errors: list[str] = []
    for k, v in body.items():
        if k not in DEFAULTS:
            continue
        if k in ("upstream_api_key", "relay_api_key", "webshare_token") and isinstance(v, str):
            if "..." in v or v == "(none)":
                continue
        if k in SETTING_BOUNDS:
            lo, hi = SETTING_BOUNDS[k]
            try:
                iv = int(v)
            except (TypeError, ValueError):
                errors.append(f"{k}: must be a number")
                continue
            if not (lo <= iv <= hi):
                errors.append(f"{k}: must be between {lo} and {hi} (got {iv})")
                continue
            updates[k] = iv
            continue
        if isinstance(DEFAULTS[k], bool):
            updates[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            continue
        if k == "upstream_base_url" and isinstance(v, str) and v.strip():
            if not v.strip().lower().startswith(("http://", "https://")):
                errors.append("upstream_base_url: must start with http:// or https://")
                continue
        if k == "upstream_user_agent" and isinstance(v, str):
            if not v.strip():
                errors.append("upstream_user_agent: cannot be empty")
                continue
            if "opencode" not in v.lower():
                errors.append("upstream_user_agent: must contain 'opencode' — the free "
                              "tier answers 429 to every other User-Agent")
                continue
        updates[k] = v
    return updates, errors


def mask_key(k: str) -> str:
    return k[:6] + "..." if k else "(none)"


def _display_addr(a: str) -> str:
    """Never surface proxy credentials: 'user:pass@ip:port' -> 'ip:port'.

    Tor lanes would otherwise all render as '127.0.0.1:9150', which is useless
    in the dashboard — every circuit would look like the same lane. Show the
    circuit identity instead, since that IS the distinguishing egress.
    """
    if a == "":
        return "direct"
    if a.startswith(TOR_LANE_PREFIX):
        parsed = tor_slot_of(a)
        return f"tor#{parsed[0]}.g{parsed[1]}" if parsed else "tor"
    return a.split("@", 1)[-1] if "@" in a else a


async def validate_webshare_token(token: str) -> dict:
    """Validate a single Webshare API token directly against Webshare API."""
    tok = token.strip()
    if not tok or "..." in tok or tok == "(none)":
        return {"token": mask_key(tok), "valid": False, "proxies": 0, "error": "Empty or masked token"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=4)) as client:
            r = await client.get(
                "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=1",
                headers={"Authorization": f"Token {tok}"}
            )
            if r.status_code == 200:
                count = r.json().get("count", 0)
                return {"token": mask_key(tok), "valid": True, "proxies": count, "error": None}
            elif r.status_code == 401:
                return {"token": mask_key(tok), "valid": False, "proxies": 0, "error": "Invalid token (401 Unauthorized)"}
            else:
                return {"token": mask_key(tok), "valid": False, "proxies": 0, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"token": mask_key(tok), "valid": False, "proxies": 0, "error": str(e)}


async def validate_upstream(base_url: str, api_key: str) -> dict:
    """Validate the upstream OpenAI-compatible base URL and API key."""
    url = base_url.rstrip("/")
    if not url:
        return {"valid": False, "error": "Base URL missing"}
    try:
        headers = {}
        if api_key and api_key != "(none)" and "..." not in api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=4)) as client:
            r = await client.get(f"{url}/models", headers=headers)
            if r.status_code == 200:
                try:
                    models = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
                    return {"valid": True, "models_count": len(models), "error": None}
                except Exception:
                    return {"valid": True, "models_count": 0, "error": None}
            else:
                return {"valid": False, "error": f"HTTP {r.status_code} from /models"}
    except Exception as e:
        return {"valid": False, "error": str(e)}


def public_settings() -> dict:
    s = dict(settings)
    if s.get("upstream_api_key"):
        keys = str(s["upstream_api_key"]).split(",")
        s["upstream_api_key"] = ",".join(mask_key(k) for k in keys)
    if s.get("relay_api_key"):
        s["relay_api_key"] = mask_key(str(s["relay_api_key"]))
    if s.get("webshare_token"):
        tokens = str(s["webshare_token"]).split(",")
        s["webshare_token"] = ",".join(mask_key(tok) for tok in tokens)
    return s


# ── log ring buffer (dashboard log viewer) ──────────────────────
LOG_RING = collections.deque(maxlen=600)

# Proxy credentials (user:pass@host:port) must never reach the log ring:
# /api/logs is reachable by anyone who can reach the dashboard, and when
# relay_api_key is empty that is the whole internet. Strip the userinfo part.
_CRED_RE = re.compile(r"(?<![\w.\-])[A-Za-z0-9_\-.]{1,64}:[^\s/@]{1,64}@(?=[\w.\-]+:\d{2,5})")


def scrub_creds(text: str) -> str:
    return _CRED_RE.sub("", text)


class RingHandler(logging.Handler):
    def emit(self, record):
        try:
            line = scrub_creds(self.format(record))
            LOG_RING.append(line)
            # Push the line to SSE subscribers so the dashboard log is live
            # instead of polled. Guarded: logging can happen from a non-async
            # thread (or before the loop exists), where queue mutation is unsafe.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return
            if _subscribers:
                publish("log", {"line": line})
        except Exception:
            pass


handler = RingHandler()
handler.setFormatter(logging.Formatter(
    fmt="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
))
logging.getLogger().addHandler(handler)

app = FastAPI(title="ip-relay")


# ══════════════════════════════════════════════════════════════════
# EGRESS ENGINE
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
# BURN MEMORY — the blocklist of spent egress IPs
# ══════════════════════════════════════════════════════════════════
#
# Why this exists (measured 2026-08-15, not assumed):
#
#   * A 429'd egress IP does not come back. One IP was polled continuously for
#     40 minutes — ~80 attempts, zero 200s — and had already been refused for 8
#     days. Verified with a live key while other lanes were returning 200, so
#     the key was fine and the IP was what was refused.
#   * The scrape feeds re-serve the same dead addresses every cycle. The 10
#     Webshare free IPs alone were re-probed ~20 times each in one hour: ~200
#     probes spent on addresses that will never work again.
#
# So burn is remembered on disk, keyed on the IP (never ip:port — the quota
# follows the address, and the same host shows up on many ports across feeds),
# and consulted at two choke points: candidate intake and probe dispatch. That
# redirects the probe budget at addresses that could plausibly be live.

BURNED_FILE = os.environ.get("BURNED_FILE", "burned.json")

# ip -> {"at": epoch, "reason": str, "hits": int}
BURNED: dict[str, dict] = {}
BURN_STATS = {"blocked_intake": 0, "blocked_probe": 0, "recorded": 0, "expired": 0}


def _ip_of(addr: str) -> str:
    """Bare IP from 'user:pass@ip:port', 'ip:port', or a synthetic lane addr."""
    if not addr:
        return ""
    return addr.split("@")[-1].split(":")[0]


def is_burned(addr_or_ip: str) -> bool:
    """True when this egress address is a known-spent IP still inside its TTL.

    Tor lanes are exempt: their addr points at the local SOCKS port, so the IP
    would be 127.0.0.1 for every circuit — blocklisting that would kill the
    whole Tor provider on the first 429. Circuit rotation is how a burned Tor
    exit is discarded (see tor_rotate_lane).
    """
    if not BURN_MEMORY or not addr_or_ip:
        return False
    if addr_or_ip.startswith(TOR_LANE_PREFIX):
        return False
    ip = _ip_of(addr_or_ip)
    if not ip or ip in ("127.0.0.1", "localhost"):
        return False
    rec = BURNED.get(ip)
    if not rec:
        return False
    if time.time() - rec.get("at", 0) > BURN_TTL_SEC:
        BURNED.pop(ip, None)
        BURN_STATS["expired"] += 1
        return False
    return True


def mark_burned(addr: str, reason: str = "429") -> None:
    """Remember that this egress IP's free-tier budget is spent."""
    if not BURN_MEMORY or not addr:
        return
    if addr.startswith(TOR_LANE_PREFIX):
        return          # rotate the circuit instead; see tor_rotate_lane
    if addr in ("direct", ""):
        return          # the local egress has no other IP to fall back to
    ip = _ip_of(addr)
    if not ip or ip in ("127.0.0.1", "localhost"):
        return
    rec = BURNED.get(ip)
    if rec:
        rec["hits"] = rec.get("hits", 1) + 1
        rec["at"] = time.time()
        rec["reason"] = reason
    else:
        BURNED[ip] = {"at": time.time(), "reason": reason, "hits": 1}
        BURN_STATS["recorded"] += 1


def prune_burned() -> int:
    """Drop expired entries so the file and the dict stay bounded."""
    now = time.time()
    dead = [ip for ip, r in BURNED.items() if now - r.get("at", 0) > BURN_TTL_SEC]
    for ip in dead:
        BURNED.pop(ip, None)
    BURN_STATS["expired"] += len(dead)
    return len(dead)


def save_burned() -> int:
    if not BURN_MEMORY:
        return 0
    try:
        tmp = BURNED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved": time.time(), "ips": BURNED}, f)
        os.replace(tmp, BURNED_FILE)
        os.chmod(BURNED_FILE, 0o600)
        return len(BURNED)
    except Exception as e:
        log.warning("could not save burn list: %s", e)
        return 0


def load_burned() -> int:
    if not BURN_MEMORY:
        return 0
    try:
        with open(BURNED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        ips = data.get("ips", {})
        now = time.time()
        loaded = 0
        for ip, rec in ips.items():
            if not isinstance(rec, dict):
                continue
            if now - float(rec.get("at", 0)) > BURN_TTL_SEC:
                continue
            BURNED[ip] = rec
            loaded += 1
        if loaded:
            log.info("burn memory: %d known-spent IPs loaded (ttl=%ds)", loaded, BURN_TTL_SEC)
        return loaded
    except FileNotFoundError:
        return 0
    except Exception as e:
        log.warning("could not load burn list: %s", e)
        return 0


# ══════════════════════════════════════════════════════════════════
# TOR EGRESS PROVIDER
# ══════════════════════════════════════════════════════════════════
#
# Measured 2026-08-15 against opencode.ai/zen/v1:
#
#   14 isolated circuits -> 14 DISTINCT exit IPs (zero repeats)
#   40 isolated circuits -> 26x200 / 14x429  =  65% live
#                           p50 2128ms, p95 3037ms on success
#   one live exit        -> 40/40 consecutive requests (twice, independently)
#   supply              -> 3,224 exit relays / 1,384 distinct exit IPs
#
# For comparison the scraped public-proxy feeds yield ~2% (16 usable out of
# 1,040 probed). Tor is ~30x better per probe, at equal latency, and its supply
# renews itself instead of churning.
#
# Mechanism: tor's `IsolateSOCKSAuth` binds a circuit to the SOCKS *username*,
# so socks5://<name>:x@127.0.0.1:9150 is a private egress per name. httpx takes
# the proxy per-client, so this needs no new transport code and is safe under
# concurrency (verified: 10 parallel circuits, no cross-talk).
#
# Rotation is the important part: because a burned exit never recovers, a 429 on
# a Tor lane must not park it — it must change the circuit name, which yields a
# brand-new exit IP. That is what makes discovery self-renewing.

TOR_LANE_PREFIX = "tor-"
_tor_rotations = 0


def tor_lane_addr(slot: int, generation: int = 0) -> str:
    """Synthetic lane address for Tor slot `slot` at circuit `generation`.

    The username encodes slot+generation, so bumping the generation forces tor
    to build a fresh circuit (new exit IP) while keeping the slot identity.
    """
    return f"{TOR_LANE_PREFIX}{slot}g{generation}:x@127.0.0.1:{TOR_SOCKS_PORT}"


def tor_slot_of(addr: str) -> tuple[int, int] | None:
    """(slot, generation) from a Tor lane addr, or None if not a Tor lane."""
    if not addr.startswith(TOR_LANE_PREFIX):
        return None
    try:
        name = addr.split(":", 1)[0][len(TOR_LANE_PREFIX):]
        slot_s, gen_s = name.split("g", 1)
        return int(slot_s), int(gen_s)
    except Exception:
        return None


def tor_available() -> bool:
    """Is a tor SOCKS port actually listening? Cheap, synchronous, no traffic."""
    global TOR_SOCKS_PORT
    import socket as _socket
    for port in (TOR_SOCKS_PORT, 9050, 9150):
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=0.5):
                TOR_SOCKS_PORT = port
                return True
        except Exception:
            pass
    return False


WARP_PROXY_ADDR = "127.0.0.1:40000"


def warp_available() -> bool:
    """Is a local Cloudflare WARP proxy listening on 127.0.0.1:40000?"""
    import socket as _socket
    try:
        with _socket.create_connection(("127.0.0.1", 40000), timeout=0.3):
            return True
    except Exception:
        return False


async def _warp_reconnect() -> None:
    """Rotate the local Cloudflare WARP session when its IP hits a 429."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", "warp-cli disconnect >/dev/null 2>&1 && sleep 1 && warp-cli connect >/dev/null 2>&1",
        )
        await asyncio.wait_for(proc.wait(), timeout=10.0)
        ln = POOL.lanes.get(f"http://{WARP_PROXY_ADDR}")
        if ln is not None:
            ln.parked_until = 0.0
            ln.score = max(ln.score, 0.8)
    except Exception:
        pass

def _evict_lane(key: str) -> Lane | None:
    """Remove a lane and close its cached HTTP client so pooled connections
    to a dead proxy are freed instead of leaking sockets."""
    ln = POOL.lanes.pop(key, None)
    if ln is not None:
        try:
            asyncio.get_running_loop().create_task(ln.aclose_client())
        except RuntimeError:
            ln._client = None
    return ln

def tor_rotate_lane(ln: "Lane") -> str | None:
    """Replace a burned Tor lane with a fresh circuit in the same slot.

    Returns the new lane key, or None when `ln` is not a Tor lane. The old lane
    is removed outright rather than parked: its exit is spent, and parking would
    just re-probe a dead address on the recover loop.
    """
    global _tor_rotations
    parsed = tor_slot_of(ln.addr)
    if parsed is None:
        return None
    slot, gen = parsed
    _evict_lane(f"{ln.proto}://{ln.addr}")
    new_addr = tor_lane_addr(slot, gen + 1)
    fresh = Lane(new_addr, "socks5")
    fresh.score = 0.5
    key = f"socks5://{new_addr}"
    POOL.lanes[key] = fresh
    _tor_rotations += 1
    log.info("tor: rotated slot %d to generation %d (new exit circuit)", slot, gen + 1)
    return key


def tor_ensure_lanes() -> int:
    """Keep TOR_LANES circuit slots present in the pool. Idempotent.

    Lanes start unproven (score 0.5) and are confirmed by the normal probe path,
    which is what filters the ~35% of exits whose budget is already spent.
    """
    if not TOR_ENABLED:
        return 0
    if not tor_available():
        return 0
    have = {tor_slot_of(ln.addr)[0] for ln in POOL.lanes.values()
            if ln.addr.startswith(TOR_LANE_PREFIX) and tor_slot_of(ln.addr)}
    added = 0
    for slot in range(max(0, TOR_LANES)):
        if slot in have:
            continue
        addr = tor_lane_addr(slot, 0)
        POOL.lanes[f"socks5://{addr}"] = Lane(addr, "socks5")
        added += 1
    if added:
        log.info("tor: added %d circuit lanes (target=%d, socks=127.0.0.1:%d)",
                 added, TOR_LANES, TOR_SOCKS_PORT)
    return added


class Lane:
    """One egress route (a proxy, or the direct lane). Carries health score.

    score: EWMA in [0,1]. Success pushes toward 1, failure toward 0. Requests
    prefer the highest-scored warm lane. Latency is tracked for ranking."""
    __slots__ = ("addr", "proto", "score", "lat_ms", "ok", "fails",
                 "parked_until", "last_ok", "last_probe", "probe_tries",
                 "consec_fails", "inflight", "_sem", "_client", "created")

    def __init__(self, addr: str, proto: str):
        self.addr = addr            # "ip:port" or "" for the direct lane
        self.proto = proto          # "http" | "https" | "socks4" | "socks5" | "direct"
        self.score = 0.5            # start neutral; probes move it
        self.lat_ms = 0.0
        self.ok = 0                 # lifetime successes
        self.fails = 0              # lifetime failures
        self.parked_until = 0.0     # burned: no requests until this ts
        self.last_ok = 0.0
        self.last_probe = 0.0
        self.probe_tries = 0
        self.consec_fails = 0       # consecutive failures; >=3 self-parks
        self.inflight = 0           # requests currently in flight on this lane
        self._sem: asyncio.Semaphore | None = None
        self._client: httpx.AsyncClient | None = None
        self.created = time.time()

    # ── capacity ───────────────────────────────────────────────────
    # Free proxies collapse under parallel load: without a per-lane cap the
    # whole fleet of concurrent requests stacks onto whichever lane is
    # currently fastest and burns it. The semaphore is created lazily so Lane
    # stays constructible outside a running event loop (tests, deserialization).
    @property
    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(max(1, LANE_MAX_INFLIGHT))
        return self._sem

    @property
    def at_capacity(self) -> bool:
        return self.inflight >= max(1, LANE_MAX_INFLIGHT)

    @property
    def subnet(self) -> str:
        """The /24 this lane lives in. Upstream per-IP limits are frequently
        enforced per-subnet, so /24 is the real unit of pool capacity.

        Tor lanes all share 127.0.0.1, but each circuit is an INDEPENDENT exit
        in a different AS — so they must not collapse into one subnet bucket or
        max_lanes_per_subnet would cap the whole Tor provider at 3 lanes. Each
        circuit slot therefore reports its own synthetic subnet.
        """
        if not self.addr:
            return "direct"
        if self.addr.startswith(TOR_LANE_PREFIX):
            parsed = tor_slot_of(self.addr)
            return f"tor-{parsed[0]}" if parsed else "tor"
        host = self.addr.split("@")[-1].split(":")[0]
        parts = host.split(".")
        return ".".join(parts[:3]) if len(parts) == 4 else host

    @property
    def tier(self) -> str:
        """fast / medium / slow — used to route streaming requests to lanes
        where latency is visible to a human."""
        if not self.lat_ms:
            return "medium"
        if self.lat_ms < 1500:
            return "fast"
        return "medium" if self.lat_ms < 4000 else "slow"

    @property
    def warm(self) -> bool:
        return self.parked_until < time.time() and self.score > 0.05

    def url(self) -> str | None:
        if self.proto == "direct":
            return None
        scheme = "http" if self.proto in ("http", "https") else self.proto
        return f"{scheme}://{self.addr}"

    def client(self, connect_timeout: float = 7.0) -> httpx.AsyncClient:
        """Reuse one AsyncClient per lane instead of building a new transport
        per request. TLS verification stays ON (the Rust relay's rule: a proxy
        may see the destination and timing, never the plaintext)."""
        if self._client is None or self._client.is_closed:
            t_sec = min(connect_timeout, float(RELAY_PROXY_TIMEOUT))
            kwargs: dict = {"timeout": httpx.Timeout(t_sec, connect=t_sec)}
            proxy = self.url()
            if proxy:
                kwargs["proxy"] = proxy
            self._client = httpx.AsyncClient(**kwargs)
        return self._client
    async def aclose_client(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    def mark_ok(self, lat_ms: float):
        if self.parked_until > time.time():
            log.info("lane %s unparked by success", _display_addr(self.addr))
        self.ok += 1
        self.last_ok = time.time()
        self.lat_ms = lat_ms
        self.score = min(1.0, self.score * 0.6 + 0.4)   # reward
        self.parked_until = 0.0
        self.consec_fails = 0

    def mark_alive(self, lat_ms: float):
        """Reachability-only success (GET /models), NOT a completion.

        This must never touch `ok`. `/models` answers 200 even from an egress IP
        whose completion quota is fully spent — measured — so counting it as a
        success would mark a dead lane "proven" and pin traffic to it (see
        _pick_lane). Refresh liveness and latency, leave the proof alone.
        """
        self.last_probe = time.time()
        self.lat_ms = lat_ms
        self.score = min(1.0, self.score * 0.8 + 0.1)   # mild reward
        self.consec_fails = 0

    def mark_fail(self, burn: bool = False):
        # The local WARP proxy is the one egress measured to complete reliably
        # (6/6 under load while every free proxy 429'd or timed out). It cannot
        # be replaced from the free lists, so a transient failure must never
        # park it: enforce that here, at the single choke point, instead of
        # trusting every caller to remember.
        is_backbone = self.addr == "127.0.0.1:40000"
        if is_backbone:
            burn = False
        self.fails += 1
        self.consec_fails += 1
        # The backbone keeps a floor: it is still the best egress available, and
        # a decaying score would silently stop selecting it.
        self.score = max(0.6 if is_backbone else 0.0, self.score * 0.4)
        # Explicit burn OR repeated drops/5xx without 429s (upstream never
        # sends 429) — self-park so broken IPs leave rotation.
        if (burn or self.consec_fails >= 3) and not is_backbone:
            was_warm = self.parked_until < time.time()
            self.parked_until = time.time() + LANE_COOLDOWN_SEC
            if was_warm:
                # transition warm -> parked is the interesting event for the
                # visualizer; repeated failures on an already-parked lane are not
                log.info("lane %s parked for %ds (burn=%s, consec_fails=%d)",
                         _display_addr(self.addr), LANE_COOLDOWN_SEC, burn, self.consec_fails)
                try:
                    publish("lane_down", {"addr": _display_addr(self.addr), "proto": self.proto,
                                          "subnet": self.subnet,
                                          "cooldown_sec": LANE_COOLDOWN_SEC})
                except Exception:
                    pass

    # ── persistence ────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"addr": self.addr, "proto": self.proto, "score": round(self.score, 4),
                "lat_ms": round(self.lat_ms, 1), "ok": self.ok, "fails": self.fails,
                "last_ok": round(self.last_ok, 1)}

    @classmethod
    def from_dict(cls, d: dict) -> "Lane":
        ln = cls(str(d.get("addr", "")), str(d.get("proto", "http")))
        ln.score = float(d.get("score", 0.5))
        ln.lat_ms = float(d.get("lat_ms", 0.0))
        ln.ok = int(d.get("ok", 0))
        ln.fails = int(d.get("fails", 0))
        ln.last_ok = float(d.get("last_ok", 0.0))
        return ln


class Pool:
    """Thread-safe-ish (async) collection of lanes + candidate reservoir."""

    def __init__(self):
        self.lanes: dict[str, Lane] = {}      # "proto://addr" -> Lane (confirmed)
        self.candidates: dict[str, float] = {}  # "proto://addr" -> first-seen
        self.priority_candidates: dict[str, float] = {}  # "proto://addr" -> first-seen (priority)
        self.tried: dict[str, float] = {}     # "proto://addr" candidate -> last-fail ts
        self.sources_ok = 0
        self.last_fetch = 0.0

    def warm_lanes(self) -> list[Lane]:
        now = time.time()
        warm = [ln for ln in self.lanes.values() if ln.parked_until < now and ln.score > 0.05]
        # Rank by effective latency (unknown latency = middling), then score.
        # A lane that answered in 500ms beats one that took 3s at equal score.
        # No provider-specific ordering here: a lane only reaches the pool after
        # passing a real completion probe, so "Webshare last" style heuristics
        # are unnecessary — burned IPs never get promoted in the first place.
        warm.sort(key=lambda ln: (ln.lat_ms if ln.lat_ms else 8000, -ln.score))
        return warm

    def available_lanes(self, tier: str | None = None) -> list[Lane]:
        """Warm lanes that still have capacity, optionally restricted to a
        latency tier. This is what the router should use — warm_lanes() alone
        will happily hand back a lane that already has N requests on it."""
        lanes = [ln for ln in self.warm_lanes() if not ln.at_capacity]
        if tier:
            tiered = [ln for ln in lanes if ln.tier == tier]
            if tiered:
                return tiered
        return lanes

    def parked_lanes(self) -> list[Lane]:
        now = time.time()
        return [ln for ln in self.lanes.values() if ln.parked_until >= now]

    def subnet_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for ln in self.lanes.values():
            if not ln.addr:
                continue
            counts[ln.subnet] = counts.get(ln.subnet, 0) + 1
        return counts

    def subnet_full(self, subnet: str) -> bool:
        if MAX_LANES_PER_SUBNET <= 0 or subnet == "direct":
            return False
        return self.subnet_counts().get(subnet, 0) >= MAX_LANES_PER_SUBNET

    def stats(self) -> dict:
        warm = self.warm_lanes()
        tiers = {"fast": 0, "medium": 0, "slow": 0}
        for ln in warm:
            tiers[ln.tier] = tiers.get(ln.tier, 0) + 1
        return {
            "warm": len(warm),
            "parked": len(self.parked_lanes()),
            "queue": len(self.candidates) + len(self.priority_candidates),
            "sources_ok": self.sources_ok,
            "best_latency_ms": warm[0].lat_ms if warm else None,
            # capacity truth: 30 lanes across 3 subnets behave like 3 lanes
            "subnets": len({ln.subnet for ln in warm if ln.addr}),
            "capacity": len(warm) * max(1, LANE_MAX_INFLIGHT),
            "inflight": sum(ln.inflight for ln in self.lanes.values()),
            "tiers": tiers,
        }


POOL = Pool()

# request counters (dashboard)
STATS = {"requests": 0, "failovers": 0, "lane_failures": 0,
         "probes_ok": 0, "probes_burned": 0, "candidates_tested": 0,
         "streams": 0, "upstream_429s": 0, "started": time.time()}

# Key-global quota tracking. opencode's free tier returns 429
# FreeUsageLimitError when the *key's* budget is exhausted (every egress IP
# 429s at once) — at that point probing is pure quota-burning, so the pool
# manager pauses churn/recover with exponential backoff until the window
# resets. A successful user relay clears the flag immediately.
#
# Important distinction (v0.8.1): most 429s in a public-proxy pool are PER-IP
# (that specific egress IP is burned by upstream, or the proxy is shared with
# thousands of other scrapers that already exhausted it). Only when enough
# DISTINCT lanes 429 within a short window — or the direct lane 429s, which
# proves the key itself is dead from our own egress — do we treat it as
# key-global. Single per-IP 429s park that lane and keep probing.
QUOTA_STATE = {"exhausted": False, "backoff_sec": 90, "backoff_until": 0.0, "announced": False,
               "_429_window": [], "_direct_429s": 0}
# key-global confidence: ≥3 distinct lanes 429ing within 45s, or ≥3 direct 429s
QUOTA_LANE_THRESHOLD = 3
QUOTA_WINDOW_SEC = 45


def _retry_after_sec(headers) -> float | None:
    """Seconds upstream says to wait, from a `Retry-After` header.

    This is the *precise* reset signal and it must be honoured: measured
    2026-10-23, a spent free-tier IP answers with `Retry-After: ~16800-18600`
    (seconds until 00:00 UTC, i.e. the daily reset). Re-probing that IP on a
    1-hour cooldown burns a real completion against a wall that will not move
    for hours — which is exactly what `_recover_parked` used to do.

    Both delta-seconds and HTTP-date forms are accepted; anything unparseable
    or non-positive returns None so callers fall back to the cooldown.
    """
    try:
        raw = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    except Exception:
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        val = float(raw)
        return val if val > 0 else None
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(raw)
        if when is None:
            return None
        delta = when.timestamp() - time.time()
        return delta if delta > 0 else None
    except Exception:
        return None


def _note_lane_429(addr: str, retry_after: float | None = None) -> None:
    """Record a per-IP 429.

    With the correct opencode UA in place, a 429 means THAT egress IP has spent
    its free-tier budget — it is never a statement about our API key. So the
    normal action is: park the lane, keep serving from the others.

    A key-global escalation is still worth having, because it is the signature
    of a systemic fault (upstream changed the UA gate, or every key is dead):
    when ≥QUOTA_LANE_THRESHOLD *distinct* lanes 429 inside QUOTA_WINDOW_SEC, the
    problem is not the proxies. We back off probing and say so loudly, since
    hammering in that state just burns fresh IPs for nothing.
    """
    now = time.time()
    # Burn memory: this address is spent for the TTL. Measured — it does not
    # recover on a 90s cooldown, so remembering it is what stops the prober
    # re-testing the same corpses every scrape cycle.
    mark_burned(addr, "relay-429")
    # Honour upstream's own reset time when it gave one. The free tier answers
    # 429 with Retry-After ≈ seconds-until-00:00-UTC (measured 16800-18600), so
    # a 1h cooldown re-probes an IP that cannot recover for hours: every one of
    # those re-probes spends a real completion to rediscover the same wall. The
    # window is clamped to [LANE_COOLDOWN_SEC, BURN_TTL_SEC] — never shorter
    # than the normal cooldown, never longer than the burn memory itself.
    if retry_after:
        park = min(max(float(retry_after), float(LANE_COOLDOWN_SEC)), float(BURN_TTL_SEC))
        for key, ln in list(POOL.lanes.items()):
            if ln.addr == addr:
                ln.parked_until = max(ln.parked_until, now + park)
                log.info("lane %s parked for %ds (upstream Retry-After)",
                         _display_addr(addr), int(park))
                break
    w = QUOTA_STATE["_429_window"]
    w.append((now, addr))
    w[:] = [(t, a) for t, a in w if now - t <= QUOTA_WINDOW_SEC]
    distinct = {a for _, a in w}
    if addr == "":
        QUOTA_STATE["_direct_429s"] += 1
        if QUOTA_STATE["_direct_429s"] >= QUOTA_LANE_THRESHOLD:
            _note_upstream_429()
            QUOTA_STATE["_direct_429s"] = 0
        return
    if len(distinct) >= QUOTA_LANE_THRESHOLD:
        # On the anonymous 'public' free tier, quota is strictly per-IP and
        # never key-global; pausing the prober or aborting failover after 3
        # spent proxies would starve the switcher right when it needs to scan
        # more lanes.
        if get_upstream_key() == "public":
            QUOTA_STATE["_429_window"] = []
            return
        cutoff = now - QUOTA_WINDOW_SEC
        recent_ok = any(r[0] >= cutoff and r[2] == 200 for r in REQ_LOG)
        if recent_ok:
            return
        log.warning("quota: %d distinct egress IPs 429'd within %ds with zero successes — "
                    "this is systemic, not per-proxy. Check upstream_user_agent (currently "
                    "%r; it must contain 'opencode') and that the keys are live.",
                    len(distinct), QUOTA_WINDOW_SEC, UPSTREAM_UA)
        _note_upstream_429()
        QUOTA_STATE["_429_window"] = []

def _note_upstream_429() -> None:
    """Record a key-global rate-limit hit and grow the probe backoff."""
    now = time.time()
    QUOTA_STATE["exhausted"] = True
    # Cap the probe pause at 5 minutes: free-tier budgets are per-IP, so a
    # systemic-looking 429 storm usually clears on the next fresh lane, and the
    # old 30-minute ceiling left the pool idle long after users could work.
    QUOTA_STATE["backoff_sec"] = min(QUOTA_STATE["backoff_sec"] * 2, 300)
    QUOTA_STATE["backoff_until"] = now + QUOTA_STATE["backoff_sec"]
    STATS["upstream_429s"] += 1


def _note_quota_ok() -> None:
    """A successful upstream call — quota is back, resume probing.
    v0.8.1: success decays the backoff instead of hard-resetting only on a
    user relay, so a long-lived pool stops fighting a freed quota window."""
    if QUOTA_STATE["exhausted"]:
        QUOTA_STATE["backoff_sec"] = max(30, QUOTA_STATE["backoff_sec"] // 2)
        if QUOTA_STATE["backoff_sec"] == 30:
            log.info("upstream quota recovered (200) — probing resumed")
            QUOTA_STATE["exhausted"] = False
            QUOTA_STATE["backoff_until"] = 0.0
        else:
            QUOTA_STATE["backoff_until"] = min(QUOTA_STATE["backoff_until"],
                                               time.time() + QUOTA_STATE["backoff_sec"])
    if not QUOTA_STATE["exhausted"]:
        QUOTA_STATE["backoff_until"] = 0.0


# ══════════════════════════════════════════════════════════════════
# METRICS — rolling window, so questions like "is p95 worse than an
# hour ago?" are answerable. Lifetime counters alone cannot do that.
# ══════════════════════════════════════════════════════════════════

REQ_LOG: collections.deque = collections.deque(maxlen=2000)  # (ts, lane, status, ms, stream)


def record_request(lane: str, status: int, ms: float, stream: bool = False) -> None:
    REQ_LOG.append((time.time(), lane, status, ms, stream))


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def metrics_window(seconds: int = 300) -> dict:
    cutoff = time.time() - seconds
    rows = [r for r in REQ_LOG if r[0] >= cutoff]
    lat = sorted(r[3] for r in rows if r[2] == 200)
    ok = sum(1 for r in rows if r[2] == 200)
    return {
        "window_sec": seconds,
        "requests": len(rows),
        "ok": ok,
        "errors": len(rows) - ok,
        "success_rate": round(ok / len(rows), 4) if rows else None,
        "rpm": round(len(rows) / (seconds / 60.0), 2),
        "p50_ms": round(_pct(lat, 0.50)),
        "p95_ms": round(_pct(lat, 0.95)),
        "p99_ms": round(_pct(lat, 0.99)),
    }


def sparkline_series(buckets: int = 30, bucket_sec: int = 2) -> dict:
    """Per-bucket request counts and p95 for the dashboard sparklines."""
    now = time.time()
    counts = [0] * buckets
    lats: list[list[float]] = [[] for _ in range(buckets)]
    for ts, _lane, status, ms, _stream in REQ_LOG:
        age = now - ts
        idx = buckets - 1 - int(age // bucket_sec)
        if 0 <= idx < buckets:
            counts[idx] += 1
            if status == 200:
                lats[idx].append(ms)
    p95 = [round(_pct(sorted(b), 0.95)) for b in lats]
    return {"bucket_sec": bucket_sec, "requests": counts, "p95_ms": p95}


# ══════════════════════════════════════════════════════════════════
# EVENT BUS — the dashboard subscribes over SSE instead of polling
# three endpoints on three different timers.
# ══════════════════════════════════════════════════════════════════

_subscribers: set[asyncio.Queue] = set()
EVENT_RING: collections.deque = collections.deque(maxlen=200)


def publish(kind: str, data: dict) -> None:
    """Fan an event out to every live SSE subscriber. Never blocks, never
    raises: a slow/dead client must not be able to stall the relay."""
    evt = {"kind": kind, "ts": time.time(), **data}
    EVENT_RING.append(evt)
    for q in list(_subscribers):
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            pass
        except Exception:
            _subscribers.discard(q)


# ══════════════════════════════════════════════════════════════════
# LANE PERSISTENCE — the scored pool is the most expensive asset the
# process owns; losing it on every restart is a self-inflicted outage.
# ══════════════════════════════════════════════════════════════════

def save_lanes() -> int:
    if not PERSIST_LANES:
        return 0
    # Tor lanes are deliberately excluded: a circuit is bound to a running tor
    # process and its exit is gone after a restart, so persisting them would
    # re-seed dead circuits as priority candidates. tor_ensure_lanes() recreates
    # the slots on boot instead.
    keep = [ln.to_dict() for ln in POOL.lanes.values()
            if ln.addr and ln.score > 0.25 and not ln.addr.startswith(TOR_LANE_PREFIX)]
    try:
        tmp = LANES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved": time.time(), "lanes": keep}, f)
        os.replace(tmp, LANES_FILE)   # atomic: never leave a torn file behind
        os.chmod(LANES_FILE, 0o600)
        return len(keep)
    except Exception as e:
        log.warning("could not save lanes: %s", e)
        return 0


def load_lanes() -> int:
    """Re-seed remembered lanes as PRIORITY candidates (not straight into the
    pool): they still have to prove themselves, they just skip the queue."""
    if not PERSIST_LANES:
        return 0
    try:
        with open(LANES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return 0
    age = time.time() - float(data.get("saved", 0))
    if age > 86400:
        log.info("lanes.json is %.1fh old — ignoring stale egress state", age / 3600)
        return 0
    n = 0
    now = time.time()
    for d in data.get("lanes", []):
        try:
            ln = Lane.from_dict(d)
        except Exception:
            continue
        if not ln.addr or ln.score <= 0.25:
            continue
        if ln.addr.startswith(TOR_LANE_PREFIX):
            continue          # stale circuit from a previous tor process
        if is_burned(ln.addr):
            BURN_STATS["blocked_intake"] += 1
            continue          # remembered lane whose IP has since burned
        POOL.priority_candidates[f"{ln.proto}://{ln.addr}"] = now
        n += 1
    if n:
        log.info("restored %d remembered lanes into the priority queue (%.0fs old)", n, age)
    return n


async def lane_persist_loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            save_lanes()
            prune_burned()
            save_burned()
        except Exception as e:
            log.warning("lane persist loop error: %s", e)


# ── proxy sources ─────────────────────────────────────────────────
# Each entry: (url, kind). kind "text" = ip:port lines (assumed proto).
# kind "proto" = protocol://ip:port lines. kind "geonode" = JSON api.
# Measured live-yield per source (fraction of sampled entries that answered
# the upstream screen), used to scan productive feeds first. Values are
# deliberately coarse: a source's exact rate drifts, but the gap between a
# maintained list and a stale one is large and stable.
SOURCE_YIELD: dict[str, float] = {
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt": 0.142,
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt": 0.092,
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt": 0.083,
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt": 0.008,
}

TEXT_SOURCES: list[tuple[str, str]] = [
    ("https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=ipport&format=text&timeout=8000", "http"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://www.proxy-list.download/api/v1/get?type=http", "http"),
    ("https://www.proxy-list.download/api/v1/get?type=https", "https"),
    ("https://www.proxy-list.download/api/v1/get?type=socks4", "socks4"),
    ("https://www.proxy-list.download/api/v1/get?type=socks5", "socks5"),
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt", "http"),
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt", "socks4"),
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt", "socks5"),
    ("https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt", "http"),
    ("https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt", "proto"),
    ("https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&proxy_format=protocolipport&format=text", "proto"),
    # extra verified feeds (mirror of the Rust relay's list)
    ("https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt", "https"),
    ("https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt", "socks4"),
    ("https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt", "socks5"),
    ("https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt", "http"),
    ("https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/HTTP.txt", "http"),
    ("https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS4.txt", "socks4"),
    ("https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS5.txt", "socks5"),
    ("https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt", "http"),
    ("https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks4_proxies.txt", "socks4"),
    ("https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt", "socks5"),
    # ── +100 vetted sources (validated live 2026-10-23; see README) ──
    ("https://raw.githubusercontent.com/6yyn/free-proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/Access-At/proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/Access-At/proxy-list/main/proxies.txt", "http"),
    ("https://raw.githubusercontent.com/adasd223/free-global-proxies/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/ahahaabas/free-socks5-proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/BlacKSnowDot0/Proxy-Pulse/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/Ch4120N/Ch4120N-Proxy-List/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/denizzimo/telegram_socks5_list/master/proxy.txt", "http"),
    ("https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/gnxD3RfTT2WE/open-proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/HoangGiaHung/proxies-txt/main/proxies.txt", "http"),
    ("https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/all.txt", "http"),
    ("https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/elite.txt", "http"),
    ("https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/komutan234/Proxy-List-Free/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/krokmazagaga/http-proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/litportnet/free-proxy-list/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/maximilianfeix/free-proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/MrMarble/proxy-list/main/all.txt", "http"),
    ("https://raw.githubusercontent.com/NoureddineRmila/daily-free-working-proxies/master/elite.txt", "http"),
    ("https://raw.githubusercontent.com/officialputuid/KangProxy/main/http/http.txt", "http"),
    ("https://raw.githubusercontent.com/officialputuid/KangProxy/main/xResults/Proxies.txt", "http"),
    ("https://raw.githubusercontent.com/opsxcq/proxy-list/master/list.txt", "http"),
    ("https://raw.githubusercontent.com/proxio-io/proxy-list/main/all.txt", "http"),
    ("https://raw.githubusercontent.com/proxio-io/proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/proxmint/free-proxy-list/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/proxy-free/free-proxy-list/main/all.txt", "http"),
    ("https://raw.githubusercontent.com/proxy-free/free-proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/hproxy.txt", "http"),
    ("https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/RioMMO/ProxyFree/main/HTTP.txt", "http"),
    ("https://raw.githubusercontent.com/rix4uni/fresh-proxy-list/main/proxylist.txt", "http"),
    ("https://raw.githubusercontent.com/saisuiu/Lionkings-Http-Proxys-Proxies/main/free.txt", "http"),
    ("https://raw.githubusercontent.com/Santak332/proxies.txt/main/proxies.txt", "http"),
    ("https://raw.githubusercontent.com/SaraanshSharma/Proxy-Scraper-and-Checker/main/proxies.txt", "http"),
    ("https://raw.githubusercontent.com/SevenworksDev/proxy-list/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/shubhamshendre/Free-Proxies/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/snove999/emby-proxy-list/main/all.txt", "http"),
    ("https://raw.githubusercontent.com/SoliSpirit/proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/themiralay/Proxy-List-World/master/data.txt", "http"),
    ("https://raw.githubusercontent.com/theriturajps/proxy-list/main/proxies.txt", "http"),
    ("https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/Tsprnay/Proxy-lists/master/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/proxylist.txt", "http"),
    ("https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/xRxxxx/proxies.txt/main/proxies.txt", "http"),
    ("https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/all.txt", "http"),
    ("https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/zevtyardt/proxy-list/main/all.txt", "http"),
    ("https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt", "http"),
    ("https://raw.githubusercontent.com/BlacKSnowDot0/Proxy-Pulse/main/https.txt", "https"),
    ("https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/https.txt", "https"),
    ("https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/https.txt", "https"),
    ("https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/https.txt", "https"),
    ("https://raw.githubusercontent.com/litportnet/free-proxy-list/main/proxies/https.txt", "https"),
    ("https://raw.githubusercontent.com/officialputuid/KangProxy/main/https/https.txt", "https"),
    ("https://raw.githubusercontent.com/proxio-io/proxy-list/main/https.txt", "https"),
    ("https://raw.githubusercontent.com/proxmint/free-proxy-list/main/proxies/https.txt", "https"),
    ("https://raw.githubusercontent.com/SevenworksDev/proxy-list/main/proxies/https.txt", "https"),
    ("https://raw.githubusercontent.com/SoliSpirit/proxy-list/main/https.txt", "https"),
    ("https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/https.txt", "https"),
    ("https://raw.githubusercontent.com/Tsprnay/Proxy-lists/master/proxies/https.txt", "https"),
    ("https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/https.txt", "https"),
    ("https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/https.txt", "https"),
    ("https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/https.txt", "https"),
    ("https://raw.githubusercontent.com/Access-At/proxy-list/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/adasd223/free-global-proxies/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/ahahaabas/free-socks5-proxy-list/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/BlacKSnowDot0/Proxy-Pulse/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/Ch4120N/Ch4120N-Proxy-List/main/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/gnxD3RfTT2WE/open-proxy-list/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/komutan234/Proxy-List-Free/main/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/litportnet/free-proxy-list/main/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/maximilianfeix/free-proxy-list/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/officialputuid/KangProxy/main/socks4/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/proxio-io/proxy-list/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/proxmint/free-proxy-list/main/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/proxy-free/free-proxy-list/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/RioMMO/ProxyFree/main/SOCKS4.txt", "socks4"),
    ("https://raw.githubusercontent.com/SevenworksDev/proxy-list/main/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/shubhamshendre/Free-Proxies/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/SoliSpirit/proxy-list/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/Tsprnay/Proxy-lists/master/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/Access-At/proxy-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/adasd223/free-global-proxies/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/ahahaabas/free-socks5-proxy-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/BlacKSnowDot0/Proxy-Pulse/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/Ch4120N/Ch4120N-Proxy-List/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/gnxD3RfTT2WE/open-proxy-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/komutan234/Proxy-List-Free/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/litportnet/free-proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/maximilianfeix/free-proxy-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/officialputuid/KangProxy/main/socks5/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/proxio-io/proxy-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/proxmint/free-proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/proxy-free/free-proxy-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/RioMMO/ProxyFree/main/SOCKS5.txt", "socks5"),
    ("https://raw.githubusercontent.com/SevenworksDev/proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/shubhamshendre/Free-Proxies/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/SoliSpirit/proxy-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/Tsprnay/Proxy-lists/master/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/Argh94/Proxy-List/main/HTTP.txt", "proto"),
    ("https://raw.githubusercontent.com/Argh94/Proxy-List/main/SOCKS4.txt", "proto"),
    ("https://raw.githubusercontent.com/Argh94/Proxy-List/main/SOCKS5.txt", "proto"),
    ("https://raw.githubusercontent.com/Bes-js/public-proxy-list/main/proxies.txt", "proto"),
    ("https://raw.githubusercontent.com/BlacKSnowDot0/Proxy-Pulse/main/all.txt", "proto"),
    ("https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/All_proxies.txt", "proto"),
    ("https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/http_proxies.txt", "proto"),
    ("https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks4_proxies.txt", "proto"),
    ("https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks5_proxies.txt", "proto"),
    ("https://raw.githubusercontent.com/maximilianfeix/free-proxy-list/main/all.txt", "proto"),
    ("https://raw.githubusercontent.com/maximilianfeix/free-proxy-list/main/elite.txt", "proto"),
    ("https://raw.githubusercontent.com/maximilianfeix/free-proxy-list/main/https.txt", "proto"),
    ("https://raw.githubusercontent.com/notthinks/proxy-lists/main/alive.txt", "proto"),
    ("https://raw.githubusercontent.com/officialputuid/KangProxy/main/xResults/RAW.txt", "proto"),
    ("https://raw.githubusercontent.com/stormsia/proxy-list/main/http.txt", "proto"),
    ("https://raw.githubusercontent.com/stormsia/proxy-list/main/socks4.txt", "proto"),
    ("https://raw.githubusercontent.com/stormsia/proxy-list/main/socks5.txt", "proto"),
    ("https://raw.githubusercontent.com/YevgenMartyn/proxy_scraper_checker/main/proxies.txt", "proto"),
    ("https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=socks5&proxy_format=ipport&format=text&timeout=7000", "socks5"),
    ("https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=socks4&proxy_format=ipport&format=text&timeout=7000", "socks4"),
    ("https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/https.txt", "https"),
    ("https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt", "https"),
    ("https://raw.githubusercontent.com/tuanminpay/live-proxy/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/tuanminpay/live-proxy/master/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/tuanminpay/live-proxy/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks5/global/socks5_checked.txt", "socks5"),
    ("https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/socks4/global/socks4_checked.txt", "socks4"),
    ("https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/http/global/http_checked.txt", "http"),
]

GEONODE = "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps%2Csocks4%2Csocks5"

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}


def _valid_addr(addr: str) -> bool:
    parts = addr.split(":")
    if len(parts) != 2:
        return False
    try:
        port = int(parts[1])
    except ValueError:
        return False
    if not (0 < port < 65536):
        return False
    octs = parts[0].split(".")
    if len(octs) != 4:
        return False
    try:
        return all(0 <= int(o) <= 255 for o in octs)
    except ValueError:
        return False


async def _fetch_sources() -> None:
    """Pull all feeds, fill the candidate reservoir with proto://addr entries.

    Deduplicates by bare IP across the reservoir so a single multi-port host
    (e.g. 45.74.31.x with 10,000+ ports in public feeds) cannot consume the
    entire MAX_CANDIDATES budget and starve out distinct IPs.
    """
    if not SOURCES_ENABLED:
        return
    now = time.time()
    if now - POOL.last_fetch < 30:
        return
    log.info("Scraper: Scanning %d proxy lists & Webshare API accounts...", len(TEXT_SOURCES))
    added = 0
    ok = 0
    seen_ips: set[str] = {
        _ip_of(k.split("://", 1)[-1])
        for k in (*POOL.lanes.keys(), *POOL.candidates.keys(), *POOL.priority_candidates.keys())
        if "://" in k
    }
    # Order by measured yield, not randomly. Shuffling the feeds mixes a 9%
    # source with lists that are entirely stale, and the candidate cap fills
    # with corpses before the good feed is even reached. Ranked sources keep
    # the queue dense with proxies that can actually answer.
    feeds = sorted(TEXT_SOURCES, key=lambda t: (-SOURCE_YIELD.get(t[0], 0.0), t[0]))
    async with httpx.AsyncClient(timeout=25, headers=UA, follow_redirects=True) as c:
        async def one(url: str, kind: str):
            nonlocal added, ok
            try:
                r = await c.get(url)
                if r.status_code != 200:
                    return
                ok += 1
                lines = r.text.splitlines()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    proto, addr = kind, line
                    if "://" in line:
                        proto, addr = line.split("://", 1)
                    proto = proto.lower()
                    if proto not in ("http", "https", "socks5"):
                        proto = "socks5" if proto.startswith("socks") else "http"
                    if proto.startswith("socks") and not ALLOW_SOCKS:
                        continue
                    addr = addr.strip().rstrip("/")
                    if not _valid_addr(addr):
                        continue
                    ip = _ip_of(addr)
                    if ip in seen_ips:
                        continue
                    key = f"{proto}://{addr}"
                    if key in POOL.lanes or key in POOL.candidates or key in POOL.tried:
                        continue
                    if is_burned(addr):
                        BURN_STATS["blocked_intake"] += 1
                        continue
                    if len(POOL.candidates) >= MAX_CANDIDATES:
                        break
                    seen_ips.add(ip)
                    POOL.candidates[key] = now
                    added += 1
            except Exception:
                pass

        # geonode JSON
        async def geonode():
            nonlocal added, ok
            try:
                r = await c.get(GEONODE)
                if r.status_code != 200:
                    return
                ok += 1
                for e in r.json().get("data", []):
                    addr = f"{e.get('ip')}:{e.get('port')}"
                    protos = [p.lower() for p in e.get("protocols", ["http"])]
                    for proto in protos:
                        if proto.startswith("socks") and not ALLOW_SOCKS:
                            continue
                        if proto not in ("http", "https", "socks5"):
                            proto = "socks5" if proto.startswith("socks") else "http"
                        if not _valid_addr(addr):
                            continue
                        ip = _ip_of(addr)
                        if ip in seen_ips:
                            continue
                        key = f"{proto}://{addr}"
                        if key in POOL.lanes or key in POOL.candidates or key in POOL.tried:
                            continue
                        if is_burned(addr):
                            BURN_STATS["blocked_intake"] += 1
                            continue
                        if len(POOL.candidates) >= MAX_CANDIDATES:
                            break
                        seen_ips.add(ip)
                        POOL.candidates[key] = now
                        added += 1
            except Exception:
                pass

        # webshare free-tier API (supports single or comma-separated tokens)
        async def webshare():
            nonlocal added, ok
            if not WEBSHARE_TOKEN:
                return
            import re
            tokens = [t.strip() for t in re.split(r"[\s,;\n\r]+", WEBSHARE_TOKEN) if t.strip()]

            async def fetch_one_token(token):
                nonlocal added, ok
                try:
                    r = await c.get(
                        "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=100",
                        headers={"Authorization": f"Token {token}"})
                    if r.status_code != 200:
                        return
                    ok += 1
                    for e in r.json().get("results", []):
                        if not e.get("valid"):
                            continue
                        user = e.get("username")
                        pw = e.get("password")
                        ip = e.get("proxy_address")
                        port = e.get("port")
                        if user and pw:
                            addr = f"{user}:{pw}@{ip}:{port}"
                        else:
                            addr = f"{ip}:{port}"
                        key = f"http://{addr}"
                        if key in POOL.lanes or key in POOL.candidates or key in POOL.priority_candidates or key in POOL.tried:
                            continue
                        if is_burned(addr):
                            BURN_STATS["blocked_intake"] += 1
                            continue
                        POOL.priority_candidates[key] = now
                        added += 1
                except Exception:
                    pass

            await asyncio.gather(*[fetch_one_token(tok) for tok in tokens])

        await asyncio.gather(*[one(u, k) for u, k in feeds], geonode(), webshare())
    POOL.last_fetch = now
    POOL.sources_ok = ok
    if added:
        log.info("sources: +%d candidates (queue=%d, sources_ok=%d)", added, len(POOL.candidates), ok)


# ── probing ───────────────────────────────────────────────────────
# Two stages. Stage 1 is a cheap reachability screen through the proxy (no
# completion tokens spent). Stage 2 is a real 1-token completion, which is the
# ONLY way to tell a usable egress IP from one whose free-tier quota is already
# burned — /models answers 200 even from a fully rate-limited IP, so screening
# alone promotes dead lanes. Both stages carry the opencode UA (see
# build_upstream_headers): with any other UA every probe 429s and the pool
# starves.
async def _screen(c: httpx.AsyncClient) -> bool:
    t_sec = min(7.0, float(PROBE_TIMEOUT))
    try:
        r = await asyncio.wait_for(
            c.get(SCREEN_URL,
                  headers={"Authorization": "Bearer public", "User-Agent": UPSTREAM_UA},
                  timeout=t_sec),
            timeout=t_sec,
        )
        # 403 = Cloudflare blocking this proxy IP outright (error code 1010).
        if r.status_code in (200, 401):
            return True
    except Exception:
        pass
    return False


# OpenCode v2.0.32 canonical system prompt header required by the upstream
# free-tier gate alongside `stream: true` and the `"read"` tool definition.
_OPENCODE_SYS_PREFIX = (
    "You are an AI agent running in OpenCode, a coding agent harness. "
    "Help the user accomplish their goals using the tools you have available."
)

_AGENT_PROBE_BODY_EXTRA = {
    "stream": True,
    "tools": [
        {"type": "function", "function": {
            "name": "read", "description": "Read the contents of a file or directory. Supports text files, images, and PDFs.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"]},
        }},
        {"type": "function", "function": {
            "name": "write", "description": "Write content to a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
        }},
        {"type": "function", "function": {
            "name": "bash", "description": "Run a shell command.",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        }},
    ],
}


def _agent_probe_payload(model: str) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": _OPENCODE_SYS_PREFIX},
            {"role": "user", "content": "Reply exactly: OK"},
        ],
        **_AGENT_PROBE_BODY_EXTRA,
    }


def _scheme_candidates(proto: str) -> list[str]:
    """Protocols to try for a candidate, most likely first.

    Public list files mislabel constantly: http.txt routinely carries SOCKS5
    servers, and socks5.txt carries plain HTTP ones. Trying only the declared
    protocol silently discards every mislabeled-but-working proxy, which is the
    single largest yield loss in the pool. The declared protocol goes first so
    correct entries cost one attempt; the rest are fallbacks.
    """
    # httpx has no SOCKS4 transport (socksio only implements SOCKS5); asking
    # for socks4:// raises ValueError before a packet is sent, so a socks4
    # declaration must be dialled as socks5. SOCKS4-only servers are dropped.
    if proto.startswith("socks"):
        primary = "socks5"
    else:
        primary = "http"
    order = [primary]
    for alt in ("http", "socks5"):
        if alt not in order:
            order.append(alt)
    return order


async def _probe_candidate(key: str) -> Lane | None:
    proto, addr = key.split("://", 1)
    if is_burned(addr):
        BURN_STATS["blocked_probe"] += 1
        _LAST_PROBE_ERR[key] = "burned-known"
        return None
    for scheme in _scheme_candidates(proto):
        ln = await _probe_with_scheme(key, addr, proto, scheme)
        if ln is not None:
            return ln
        if _LAST_PROBE_ERR.get(key) == "burned":
            return None
    return None


async def _probe_with_scheme(key: str, addr: str, proto: str, scheme: str) -> Lane | None:
    proxy_url = f"{scheme}://{addr}"
    t_sec = min(7.0, float(PROBE_TIMEOUT))
    try:
        # Stage 1: reachability (bounded at 7s via wait_for so SOCKS5 handshakes
        # that accept TCP and stall inside socksio never hang the worker).
        c1 = httpx.AsyncClient(proxy=proxy_url, timeout=httpx.Timeout(t_sec, connect=t_sec))
        try:
            t0_s1 = time.time()
            ok_s1 = await _screen(c1)
            lat_s1 = (time.time() - t0_s1) * 1000
        finally:
            asyncio.create_task(c1.aclose())
        if not ok_s1:
            _LAST_PROBE_ERR[key] = "proxy"
            return None

        if not STAGE2_PROBE:
            ln = Lane(addr, scheme)
            ln.mark_alive(lat_s1)
            ln.probe_tries += 1
            STATS["probes_ok"] += 1
            return ln

        # Stage 2: real completion probe, bounded by stage2_timeout rather than
        # the 7s request bound (see STAGE2_TIMEOUT).
        s2 = min(120.0, float(STAGE2_TIMEOUT))
        c2 = httpx.AsyncClient(proxy=proxy_url, timeout=httpx.Timeout(s2, connect=min(10.0, s2)))
        try:
            t0 = time.time()
            r = await asyncio.wait_for(
                c2.post(
                    f"{UPSTREAM_BASE_URL}/chat/completions",
                    headers=upstream_headers(),
                    json=_agent_probe_payload(PROBE_MODEL),
                    timeout=s2,
                ),
                timeout=s2,
            )
            lat = (time.time() - t0) * 1000
        finally:
            asyncio.create_task(c2.aclose())

        if r.status_code == 200 and _looks_like_completion(r.content):
            ln = Lane(addr, scheme)
            ln.mark_ok(lat)
            log.info("Prober: +Lane %s passed completion probe (%dms)", _display_addr(addr), int(lat))
            STATS["probes_ok"] += 1
            return ln

        if r.status_code == 429:
            _LAST_PROBE_ERR[key] = "burned"
            STATS["probes_burned"] += 1
            mark_burned(addr, "probe-429")
            log.info("Prober: %s is quota-burned (429) — dropped + blocklisted", _display_addr(addr))
            return None

        _LAST_PROBE_ERR[key] = "http"
        log.info("Prober: %s failed stage 2: HTTP %d %s", _display_addr(addr),
                 r.status_code, r.text[:80].strip())
        return None
    except Exception as e:
        _LAST_PROBE_ERR[key] = _classify_probe_error(e)
        log.warning("Prober: Lane %s failed: %s", _display_addr(addr), type(e).__name__)
    return None


async def _churn_batch() -> None:
    """Test a batch of candidates, promoting the good into the pool."""
    if not POOL.candidates and not POOL.priority_candidates:
        return

    conc = ADAPT["current"] if ADAPTIVE_CONCURRENCY else TEST_CONCURRENCY
    conc = max(1, min(conc, TEST_CONCURRENCY))

    # Cap the batch: huge batches stall the manager loop for minutes and burn
    # upstream quota on hundreds of Stage-2 probes; smaller batches keep the
    # cadence tight and the key's budget intact.
    batch_size = min(conc * 8, 300)
    batch = []

    # 1. Pop from priority queue first
    while POOL.priority_candidates and len(batch) < batch_size:
        k = next(iter(POOL.priority_candidates.keys()))
        POOL.priority_candidates.pop(k, None)
        batch.append(k)

    # 2. Fill remainder from standard queue, preferring unseen /24s so the pool
    #    spreads across subnets instead of stacking 20 ports of one host.
    if POOL.candidates and len(batch) < batch_size:
        seen_subnets = set(POOL.subnet_counts().keys())
        deferred: list[str] = []
        for k in list(POOL.candidates.keys()):
            if len(batch) >= batch_size:
                break
            POOL.candidates.pop(k, None)
            sub = _key_subnet(k)
            if MAX_LANES_PER_SUBNET > 0 and POOL.subnet_full(sub):
                POOL.tried[k] = time.time()   # subnet already saturated
                continue
            if sub in seen_subnets and len(deferred) < batch_size:
                deferred.append(k)            # try fresh subnets first
                continue
            seen_subnets.add(sub)
            batch.append(k)
        for k in deferred:
            if len(batch) >= batch_size:
                POOL.candidates[k] = time.time()   # put back, try next round
            else:
                batch.append(k)

    if not batch:
        return

    STATS["candidates_tested"] += len(batch)
    log.info("Prober: Testing %d candidate proxies (concurrency=%d%s)...",
             len(batch), conc, ", adaptive" if ADAPTIVE_CONCURRENCY else "")
    publish("churn_start", {"batch": len(batch), "concurrency": conc})
    sem = asyncio.Semaphore(conc)
    conn_errors = 0
    completed = 0

    async def guarded(k):
        nonlocal conn_errors, completed
        async with sem:
            try:
                res = await _probe_candidate(k)
                completed += 1
                if isinstance(res, Lane):
                    if res.addr and POOL.subnet_full(res.subnet) and f"{res.proto}://{res.addr}" not in POOL.lanes:
                        POOL.tried[k] = time.time()
                        return
                    POOL.lanes[f"{res.proto}://{res.addr}"] = res
                    if res.warm:
                        log.info("churn: promoted new warm lane: %s (score=%.2f, lat=%dms, %s)",
                                 res.addr, res.score, int(res.lat_ms), res.tier)
                        publish("lane_up", {"addr": _display_addr(res.addr), "proto": res.proto,
                                            "lat_ms": round(res.lat_ms), "tier": res.tier,
                                            "subnet": res.subnet})
                else:
                    POOL.tried[k] = time.time()
                    if _LAST_PROBE_ERR.get(k) == "conn":
                        conn_errors += 1
                    _LAST_PROBE_ERR.pop(k, None)
            except Exception:
                completed += 1
                POOL.tried[k] = time.time()

    try:
        await asyncio.wait_for(asyncio.gather(*[guarded(k) for k in batch], return_exceptions=True), timeout=90)
    except asyncio.TimeoutError:
        # Probes are bounded per-read but a slow-dripping upstream can stretch
        # them for minutes; never let one batch stall the whole manager loop.
        abandoned = [k for k in batch if k not in POOL.lanes and k not in POOL.tried]
        for k in abandoned:
            POOL.tried[k] = time.time()
        log.warning("churn: batch timed out after 90s; %d candidates abandoned as tried", len(abandoned))

    _adapt_concurrency(conn_errors, max(1, completed))
    s = POOL.stats()
    log.info("churn: batch completed. Pool stats: warm=%d, parked=%d, queue=%d, subnets=%d",
             s["warm"], s["parked"], s["queue"], s["subnets"])
    publish("churn_done", {"pool": s})


def _key_subnet(key: str) -> str:
    """/24 of a 'proto://[user:pass@]ip:port' candidate key."""
    addr = key.split("://", 1)[-1]
    host = addr.split("@")[-1].split(":")[0]
    parts = host.split(".")
    return ".".join(parts[:3]) if len(parts) == 4 else host


# Tracks why the last probe of a candidate failed, so the adaptive controller
# can tell "the link is saturated" (connect errors) from "these proxies are
# simply dead" (HTTP errors). Only connect storms should shrink concurrency.
_LAST_PROBE_ERR: dict[str, str] = {}


def _classify_probe_error(e: Exception) -> str:
    """Classify a probe exception for adaptive-concurrency purposes.

    "conn"  — true link/NAT exhaustion (cannot reach the proxy host at all)
    "proxy" — the proxy exists but is dead/broken/rejecting (free-list norm)
    "http"  — upstream returned a non-2xx HTTP error
    """
    name = type(e).__name__
    if name in ("ConnectError", "ConnectTimeout", "NetworkError"):
        return "conn"
    if name in ("ProxyError", "RemoteProtocolError", "ReadError", "ReadTimeout",
                "WriteError", "WriteTimeout", "ProtocolError", "LocalProtocolError"):
        return "proxy"
    return "proxy"  # unknown exceptions from a public proxy are proxy noise too


def _adapt_concurrency(conn_errors: int, completed: int) -> None:
    """Halve on a connect-error storm, grow 25% while the link is clean.

    This is the durable fix for the residential-NAT collapse: a fixed 60 melts
    an Iranian CPE's connection table, and the operator has no way to know that
    is what happened. The daemon now finds its own ceiling and logs every move.
    """
    if not ADAPTIVE_CONCURRENCY:
        return
    ratio = conn_errors / completed
    ADAPT["err_ratio"] = round(ratio, 3)
    cur = ADAPT["current"]
    if ratio > 0.4 and cur > ADAPT["min"]:
        new = max(ADAPT["min"], cur // 2)
        reason = f"connect-error storm {ratio:.0%} — link/NAT saturated"
    elif ratio < 0.1 and cur < TEST_CONCURRENCY:
        new = min(TEST_CONCURRENCY, max(cur + 1, int(cur * 1.25)))
        reason = f"link clean ({ratio:.0%} connect errors) — scaling up"
    else:
        return
    if new == cur:
        return
    ADAPT["current"] = new
    ADAPT["last_change"] = time.time()
    ADAPT["last_reason"] = reason
    log.info("adaptive concurrency: %d → %d (%s)", cur, new, reason)
    publish("adapt", {"from": cur, "to": new, "reason": reason, "err_ratio": ADAPT["err_ratio"]})


async def _recover_parked() -> None:
    """Re-probe parked (burned) lanes whose cooldown elapsed — quota windows
    reset, so a burned IP often becomes usable again. This is the recovery
    loop that makes the pool self-healing instead of one-shot.

    Each attempt here costs a REAL completion, and on a spent IP that completion
    is spent on a 429 — so the schedule must follow the evidence rather than a
    fixed cooldown. Measured 2026-10-23: upstream sends `Retry-After` ≈ seconds
    until 00:00 UTC (~16800-18600), i.e. the daily reset. `_note_lane_429` now
    parks such a lane for that whole window, and `parked_lanes()` already
    excludes lanes whose `parked_until` is in the future, so a lane inside its
    advertised reset is never re-probed. What is left for this loop is genuine
    exponential backoff for lanes parked without a Retry-After (timeouts,
    mangles), which is the case this comment used to describe.
    """
    now = time.time()
    due = [ln for ln in POOL.parked_lanes()
           if ln.url() is not None
           and ln.parked_until <= now
           and now - ln.last_probe > LANE_RECOVER_SEC * min(2 ** ln.probe_tries, 16)]
    if not due:
        return
    sem = asyncio.Semaphore(max(4, TEST_CONCURRENCY // 4))

    async def recheck(ln: Lane):
        async with sem:
            if ln.url() is None:
                return  # direct lane — nothing to re-probe through
            try:
                async with httpx.AsyncClient(proxy=ln.url(), timeout=PROBE_TIMEOUT) as c:
                    t0 = time.time()
                    r = await c.post(
                        f"{UPSTREAM_BASE_URL}/chat/completions",
                        headers=upstream_headers(),
                        json=_agent_probe_payload(PROBE_MODEL),
                    )
                    ln.last_probe = now
                    if r.status_code == 429 and is_quota_429(r.content, 429):
                        _note_lane_429(ln.addr, _retry_after_sec(dict(r.headers)))
                        # Tor lane: rotate the circuit instead of re-parking a
                        # spent exit. Public lane: _note_lane_429 blocklisted the
                        # IP, and _drop_dead will retire the lane.
                        if ln.addr.startswith(TOR_LANE_PREFIX):
                            tor_rotate_lane(ln)
                        return  # this lane's IP is burned; keep parked, don't re-burn
                    if r.status_code == 200 and _looks_like_completion(r.content):
                        _note_quota_ok()
                        ln.mark_ok((time.time() - t0) * 1000)
                        ln.probe_tries = 0
                        log.info("lane recovered: %s", _display_addr(ln.addr))
                        publish("lane_up", {"addr": _display_addr(ln.addr), "proto": ln.proto,
                                            "lat_ms": round(ln.lat_ms), "tier": ln.tier,
                                            "subnet": ln.subnet, "recovered": True})
                    else:
                        ln.probe_tries += 1
            except Exception:
                ln.last_probe = now
                ln.probe_tries += 1

    try:
        await asyncio.wait_for(asyncio.gather(*[recheck(ln) for ln in due[:20]]), timeout=60)
    except asyncio.TimeoutError:
        log.warning("recover: batch timed out after 60s")


async def _drop_dead() -> None:
    """Prune lanes that have never succeeded and have exhausted retries, and
    stale tried markers, so state doesn't grow forever.

    v1.0 also retires lanes whose IP is on the burn list. Previously a burned
    public lane sat parked forever, getting re-probed by the recover loop on a
    cooldown that measurement showed never helps — the IP is spent. Retiring it
    keeps the pool honest about its real capacity.
    """
    now = time.time()
    dead = [a for a, ln in POOL.lanes.items()
            if ln.addr != "" and ln.ok == 0 and ln.probe_tries > 4 and now - ln.last_probe > 1800]
    burned_lanes = [a for a, ln in POOL.lanes.items() if ln.addr and is_burned(ln.addr)]
    for a in dead + burned_lanes:
        _evict_lane(a)
    if burned_lanes:
        log.info("pool: retired %d lanes on known-spent IPs", len(burned_lanes))
    stale = [k for k, ts in POOL.tried.items() if now - ts > 3600]
    for k in stale:
        POOL.tried.pop(k, None)


async def _revalidate_warm() -> None:
    """Cheap GET /models revalidation of warm lanes that haven't answered in a
    while — catches silently-burned IPs before they serve a client request.
    The models endpoint is quota-exempt for the key, so this costs nothing
    upstream."""
    now = time.time()
    due = [ln for ln in POOL.warm_lanes()
           if ln.url() is not None
           and now - ln.last_ok > 300 and now - ln.last_probe > 120]
    if not due:
        return
    sem = asyncio.Semaphore(max(8, TEST_CONCURRENCY // 10))

    async def check(ln: Lane):
        async with sem:
            if ln.url() is None:
                return  # direct lane — nothing to revalidate through
            try:
                async with httpx.AsyncClient(proxy=ln.url(), timeout=httpx.Timeout(6, connect=4)) as c:
                    t0 = time.time()
                    r = await c.get(SCREEN_URL, headers=upstream_headers())
                    ln.last_probe = now
                    if r.status_code in (200, 401, 403, 404, 405):
                        # Reachability only — see mark_alive. Counting this as a
                        # completion success would let a quota-dead lane look
                        # proven and attract pinned traffic.
                        ln.mark_alive((time.time() - t0) * 1000)
                    else:
                        ln.mark_fail(burn=True)
            except Exception:
                ln.mark_fail(burn=True)
                ln.last_probe = now

    try:
        await asyncio.wait_for(asyncio.gather(*[check(ln) for ln in due[:20]]), timeout=60)
    except asyncio.TimeoutError:
        log.warning("revalidate: batch timed out after 60s")


async def _trim_pool() -> None:
    """Cap the pool: when it outgrows the target, evict the weakest lanes so
    revalidation/state stays bounded. Never evicts the direct lane."""
    cap = max(POOL_TARGET * 2, POOL_TARGET + 20)
    if len(POOL.lanes) <= cap:
        return
    now = time.time()
    order = sorted(
        (ln for ln in POOL.lanes.values() if ln.addr != ""),
        key=lambda ln: (ln.ok == 0, ln.parked_until > now, ln.score, ln.last_ok))
    excess = len(POOL.lanes) - cap
    for ln in order[:excess]:
        _evict_lane(f"{ln.proto}://{ln.addr}")
    if excess:
        log.info("pool trimmed: removed %d weakest lanes (cap=%d)", excess, cap)


def _ensure_direct_lane() -> None:
    """Idempotent: add the direct (server IP) lane when enabled, so it works
    from startup and from runtime settings toggles without a restart."""
    if DIRECT_LANE and "direct://" not in POOL.lanes:
        # A real (non-empty) address, marked proven: the local egress is the
        # one lane that does not depend on a third-party proxy, so it must be
        # selectable and trusted from the first request. An empty address made
        # the lane unproven and unpickable, which is why enabling direct_lane
        # changed nothing.
        # Unproven on purpose: the local egress is whatever the ISP hands out,
        # and a spent ISP IP 429s immediately (measured 4/4). It must earn its
        # place like any other lane, so it starts at the exploration score and
        # is proven by a real success — never trusted by default.
        ln = Lane("direct", "direct")
        ln.score = 0.3
        ln.ok = 0
        ln.last_ok = 0.0
        ln.lat_ms = 0.0
        POOL.lanes["direct://"] = ln
        log.info("direct lane enabled")


def _ensure_warp_lane() -> None:
    """Keep the local Cloudflare WARP proxy registered as the backbone lane.

    WARP is the only egress measured to complete reliably (4/4 under load while
    every free proxy 429'd or timed out), so it must survive the churn: if the
    lane exists but was parked or demoted, restore it instead of leaving the
    pool with no dependable member. Re-registering only when absent let a single
    transient failure evict the backbone for the whole cooldown."""
    key = f"http://{WARP_PROXY_ADDR}"
    if not warp_available():
        return
    ln = POOL.lanes.get(key)
    if ln is None:
        ln = Lane(WARP_PROXY_ADDR, "http")
        POOL.lanes[key] = ln
        log.info("warp: local proxy lane enabled on %s", WARP_PROXY_ADDR)
    elif ln.parked_until >= time.time() or ln.ok <= 0:
        ln.parked_until = 0.0
        ln.consec_fails = 0
        ln.ok = max(ln.ok, 1)
        ln.score = max(ln.score, 0.9)
        log.info("warp: restored backbone lane on %s", WARP_PROXY_ADDR)
    ln.lat_ms = ln.lat_ms or 250.0
    ln.score = max(ln.score, 0.9)


async def pool_manager() -> None:
    """Background loop: keep the pool at target by fetching sources and
    churning candidates; recover parked lanes; revalidate warm lanes; prune
    dead state; ensure the direct and local WARP/Tor lanes."""
    while True:
        try:
            _ensure_warp_lane()
            _ensure_direct_lane()
            tor_ensure_lanes()
            warm = len(POOL.warm_lanes())
            paused = QUOTA_STATE["exhausted"] and time.time() < QUOTA_STATE["backoff_until"]
            if paused and not QUOTA_STATE["announced"]:
                log.warning("upstream quota exhausted (429) — churn/recover paused for %.0fs; user traffic still relayed",
                            QUOTA_STATE["backoff_until"] - time.time())
            QUOTA_STATE["announced"] = paused
            if warm < POOL_TARGET:
                if not POOL.candidates:
                    await _fetch_sources()
                if not paused:
                    await _churn_batch()
            else:
                if not paused:
                    await _recover_parked()
                await _revalidate_warm()
                await _drop_dead()
                await _trim_pool()
                if not POOL.candidates:
                    await _fetch_sources()
        except Exception as e:
            log.warning("pool manager error: %s", e)
        await asyncio.sleep(6)


# ── relay core ────────────────────────────────────────────────────

def is_quota_429(body: bytes, status: int) -> bool:
    """True when the response signals rate-limiting / quota exhaustion.

    Provider-agnostic: any HTTP 429 counts, plus generic rate-limit markers
    found in the response body (works for opencode, Groq, SambaNova, Together,
    DeepSeek, and any OpenAI-compatible gateway)."""
    if status != 429:
        return False
    if not body:
        return True
    try:
        data = json.loads(body)
        err = data.get("error", {})
        msg = str(err.get("message", ""))
        err_type = str(err.get("type", ""))
        markers = ("rate limit", "quota", "usage limit", "exhausted", "too many requests",
                   "429", "limit reached", "ratelimit", "rate_limit")
        return any(m in msg.lower() for m in markers) or any(m in err_type.lower() for m in markers)
    except Exception:
        # Unparseable body on a 429 is still a rate-limit signal
        return True


# ── upstream auth/region blocks (omp patch) ─────────────────────────────────
# A 401/403 from the upstream is never about the client's key: the relay has
# already authenticated the client, and every upstream call carries the minted
# OpenCode fingerprint. In practice these codes describe the *egress lane*: a
# flagged or spent IP (FreeTierError), a country the model is not served in
# (RegionError), or a captive proxy answering in upstream's name. Two rules
# follow:
#   1. the lane leaves rotation — burned when the IP itself is gated (that does
#      not recover), parked when only this region/model was refused — and the
#      request fails over to another lane;
#   2. the client is never handed a 4xx auth code. Agent harnesses classify
#      401/403 — and any error text merely containing those digits — as a fatal
#      auth failure ("invalid API key") and end the turn instead of retrying,
#      which is exactly the mid-work crash this patch removes. The terminal
#      answer for a blocked request is a retryable 503.
#
# The stream peek waits for the first *output* token, but only briefly: a lane
# that has already shown real SSE life (role frame, usage frame, keep-alive) is
# committed to after this window instead of being rotated away, because a slow
# but healthy model would otherwise be burned through the whole attempt budget
# before the user saw a single word.
PEEK_DEADLINE_SEC = 7.0


def _block_class(status: int, body: bytes) -> str:
    """Classify an upstream 401/403: 'quota' | 'region' | 'auth' | 'proxy'."""
    try:
        text = bytes(body or b"")[:400].decode("utf-8", "replace").lower()
    except Exception:
        text = ""
    if "freetier" in text or "free tier" in text:
        return "quota"
    if "regionerror" in text or "not available in your region" in text:
        return "region"
    if status == 401:
        return "auth"
    return "proxy"


def _handle_lane_block(lane: Lane, status: int, body: bytes) -> None:
    """Take a lane that answered 401/403 out of rotation.

    Only a lane that is structurally broken (a proxy that intercepts the
    request) is burned. A `FreeTierError`/region/auth refusal is the upstream
    gate declining this particular egress IP or client fingerprint for now —
    the lane itself is healthy, and burning it removes a working egress for the
    whole burn TTL. That is exactly how the local WARP lane, the one egress that
    reliably completes, kept getting permanently evicted."""
    kind = _block_class(status, body)
    if kind == "proxy":
        # Never burn the WARP backbone: it is the one egress that reliably
        # completes, and a proxy that merely mangles one response is still the
        # best member of the pool.
        burn = lane.addr != WARP_PROXY_ADDR
        lane.mark_fail(burn=burn)
        if burn:
            mark_burned(lane.addr, f"relay-{status}-{kind}")
    else:
        lane.mark_fail(burn=False)
    STATS["lane_failures"] += 1
    STATS["failovers"] += 1
    log.info("lane %s blocked upstream (HTTP %d, %s) — rotated out",
             _display_addr(lane.addr), status, kind)


def _stream_error_kind(chunk: bytes) -> str | None:
    """Kind of upstream error carried in a 200 body/stream instead of a
    completion: 'quota' | 'region' | 'other' (None when it is not an error)."""
    for raw in chunk.split(b"\n"):
        t = raw.strip()
        if t.startswith(b"data:"):
            t = t[5:].strip()
        if not t.startswith(b"{"):
            continue
        try:
            d = json.loads(t)
        except Exception:
            continue
        if not isinstance(d, dict) or "error" not in d:
            continue
        low = json.dumps(d.get("error")).lower()
        if "freetier" in low or "free tier" in low:
            return "quota"
        if "region" in low:
            return "region"
        return "other"
    return None


def _stream_frame_kind(chunk: bytes) -> str:
    """Classify one chunk of a 200 stream.

    'content'    — carries real model output (the first token a user would
                   see); after this point a failover can no longer be hidden
    'completion' — a whole non-SSE completion body
    'data'       — a data frame with no output yet (role ping, usage)
    'keepalive'  — comment / SSE field line / blank
    'garbage'    — anything else (captive proxy junk)

    A single TCP chunk often batches several SSE frames, so every line is
    inspected and the most advanced frame wins (rank: output > data > keep).
    """
    best = "keepalive"
    saw_garbage = False
    for raw in chunk.split(b"\n"):
        t = raw.strip()
        if not t or t.startswith((b":", b"event:", b"id:", b"retry:")):
            continue
        if t.startswith(b"data:"):
            t = t[5:].strip()
            if not t or t == b"[DONE]":
                continue
        if not t.startswith(b"{"):
            # Not a JSON frame start. TCP splits SSE frames anywhere, so this
            # is usually the middle of a `data: {...}` line cut in half
            # (measured: a WARP chunk beginning `ea-a330-4bae-...`). Treating
            # that as garbage burned the only reliable lane. Only real proxy
            # junk — HTML, or text with no SSE/JSON structure at all — counts.
            low = t.lower()
            if low.startswith((b"<", b"<!doctype", b"<html")) or b"<html" in low:
                saw_garbage = True
            continue
        try:
            d = json.loads(t)
        except Exception:
            # A TCP chunk can end in the middle of a JSON frame: the upstream
            # split one `data: {...}` line across two reads. That is not proxy
            # junk — the classifier used to call it garbage, and the caller
            # burned the lane for it. Only judge complete frames; an
            # unterminated trailing line is simply "not yet".
            if not chunk.rstrip().endswith(b"}"):
                continue
            saw_garbage = True
            continue
        if not isinstance(d, dict):
            saw_garbage = True
            continue
        kind = "data"
        choices = d.get("choices")
        if isinstance(choices, list):
            for ch in choices:
                if not isinstance(ch, dict):
                    continue
                delta = ch.get("delta") or {}
                if isinstance(delta, dict) and (delta.get("content") or delta.get("reasoning_content")
                                                or delta.get("reasoning") or delta.get("tool_calls")):
                    kind = "content"
                    break
                if ch.get("finish_reason"):
                    kind = "content"
                    break
                if ch.get("message"):
                    kind = "completion"
                    break
        if kind == "content":
            return "content"
        if kind == "completion":
            best = "completion"
        elif best == "keepalive":
            best = "data"
    if saw_garbage and best == "keepalive":
        return "garbage"
    return best


def _is_stream_end(chunk: bytes) -> bool:
    """True once the upstream stream reached a real end: a [DONE] frame or a
    finish_reason. Without either, the connection dropped mid-answer and the
    client must be told, not left with a silently truncated message."""
    for raw in chunk.split(b"\n"):
        t = raw.strip()
        if t.startswith(b"data:"):
            t = t[5:].strip()
        if t == b"[DONE]":
            return True
        if not t.startswith(b"{"):
            continue
        try:
            d = json.loads(t)
        except Exception:
            continue
        if isinstance(d, dict):
            for ch in (d.get("choices") or []):
                if isinstance(ch, dict) and ch.get("finish_reason"):
                    return True
    return False


def _sse_to_chat_completion_json(body: bytes, fallback_model: str = "") -> bytes:
    """Reassemble an upstream SSE (`data: {...}`) stream into a single OpenAI
    `chat.completion` JSON object when the downstream caller requested a
    non-streaming response (`stream=False`), since the upstream free-tier gate
    requires `stream=True` on every request."""
    try:
        d = json.loads(body)
        if isinstance(d, dict) and ("choices" in d or "error" in d):
            return body
    except Exception:
        pass
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    model = fallback_model
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason = "stop"
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for raw in body.decode("utf-8", "ignore").splitlines():
        s = raw.strip()
        if not s.startswith("data:"):
            continue
        data = s[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("id"):
            cid = str(chunk["id"])
        if chunk.get("created"):
            created = int(chunk["created"])
        if chunk.get("model"):
            model = str(chunk["model"])
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        ch0 = choices[0]
        if ch0.get("finish_reason"):
            finish_reason = str(ch0["finish_reason"])
        delta = ch0.get("delta") or ch0.get("message") or {}
        if not isinstance(delta, dict):
            continue
        if delta.get("content"):
            content_parts.append(str(delta["content"]))
        if delta.get("reasoning_content"):
            reasoning_parts.append(str(delta["reasoning_content"]))
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = int(tc.get("index", 0))
            slot = tool_calls.setdefault(idx, {
                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += str(fn["name"])
            if fn.get("arguments"):
                slot["function"]["arguments"] += str(fn["arguments"])
    msg: dict = {
        "role": "assistant",
        "content": "".join(content_parts) if (content_parts or not tool_calls) else None,
    }
    if reasoning_parts:
        msg["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        msg["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
    return json.dumps({
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": usage,
    }).encode("utf-8")


async def _attempt(lane: Lane, payload: dict, path: str, headers: dict, timeout: float):
    """Single upstream attempt through one lane. Returns (status, resp_headers, body).

    Waits at most 7 seconds (`attempt_timeout`) for the proxy to respond
    (enforced with `asyncio.wait_for` so SOCKS5 handshakes cannot stall past 7s),
    and forces `stream=True` upstream because the free-tier gate rejects
    `stream=False` with 403 FreeTierError."""
    attempt_timeout = min(7.0, float(RELAY_PROXY_TIMEOUT), timeout)
    want_stream = bool(payload.get("stream", False))
    up_payload = {**payload, "stream": True} if path == "chat/completions" else payload
    t0 = time.time()
    client = lane.client(connect_timeout=attempt_timeout)
    try:
        resp = await asyncio.wait_for(
            client.post(f"{UPSTREAM_BASE_URL}/{path}", headers=headers,
                        json=up_payload, timeout=attempt_timeout),
            timeout=attempt_timeout,
        )
    except Exception:
        asyncio.create_task(lane.aclose_client())
        raise
    body = resp.content
    lat = (time.time() - t0) * 1000
    resp_headers = dict(resp.headers)
    if resp.status_code == 200:
        kind = _stream_error_kind(body)
        if kind is not None:
            if kind == "quota":
                lane.mark_fail(burn=True)
                mark_burned(lane.addr, "relay-200-quota")
            else:
                lane.mark_fail()
            log.info("lane %s returned 200 with an in-band upstream error (%s) — rotated out",
                     _display_addr(lane.addr), kind)
            return 502, {"content-type": "application/json"}, json.dumps(
                {"error": {"message": "lane returned an upstream error", "type": "lane_invalid"}}).encode()
        if _looks_like_completion(body):
            lane.mark_ok(lat)
            if not want_stream and path == "chat/completions":
                body = _sse_to_chat_completion_json(body, str(payload.get("model", "")))
                resp_headers["content-type"] = "application/json"
                resp_headers.pop("content-length", None)
                resp_headers.pop("transfer-encoding", None)
        else:
            lane.mark_fail(burn=True)
            log.info("lane %s returned 200 with non-completion body — burned", _display_addr(lane.addr))
            return 502, {"content-type": "application/json"}, json.dumps(
                {"error": {"message": "lane returned invalid body", "type": "lane_invalid"}}).encode()
    elif resp.status_code == 429 and is_quota_429(body, 429):
        lane.mark_fail(burn=False)
    return resp.status_code, resp_headers, body


# Zen's free-tier fingerprint gate (verified by MITM of opencode v2.0.32)
# requires:
#   1. `tools` containing a function named "read"
#   2. the leading `system` message starting with `_OPENCODE_SYS_PREFIX`
#   3. `stream: True` on the upstream wire (handled in _attempt / relay_stream)
_READ_TOOL_STUB = {"type": "function", "function": {
    "name": "read",
    "description": "Read the contents of a file.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
}}


def _ensure_read_tool(payload: dict) -> None:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        payload["tools"] = [_READ_TOOL_STUB]
    else:
        has_read = any(
            isinstance(t, dict) and t.get("function", {}).get("name") == "read"
            for t in tools
        )
        if not has_read:
            tools.append(_READ_TOOL_STUB)

    msgs = payload.get("messages")
    if isinstance(msgs, list):
        if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
            c0 = msgs[0].get("content")
            if isinstance(c0, str):
                if _OPENCODE_SYS_PREFIX not in c0:
                    msgs[0] = {**msgs[0], "content": f"{_OPENCODE_SYS_PREFIX}\n\n{c0}" if c0 else _OPENCODE_SYS_PREFIX}
            elif isinstance(c0, list):
                joined = " ".join(str(b.get("text", "")) for b in c0 if isinstance(b, dict))
                if _OPENCODE_SYS_PREFIX not in joined:
                    msgs[0] = {**msgs[0], "content": [{"type": "text", "text": _OPENCODE_SYS_PREFIX}, *c0]}
        else:
            msgs.insert(0, {"role": "system", "content": _OPENCODE_SYS_PREFIX})


def _looks_like_completion(body: bytes) -> bool:
    """True if the body is a genuine upstream answer — a JSON object
    (completion OR error response) or an SSE stream. Echo proxies, captive
    portals and honeypots return raw text/HTML and fail this check."""
    try:
        d = json.loads(body)
        return isinstance(d, dict)
    except Exception:
        pass
    for line in body[:400].decode("utf-8", "ignore").splitlines():
        s = line.strip()
        if not s or s.startswith(":"):
            continue
        return s.startswith("data:")
    return False


_lane_cursor = 0


def _pick_lane(lanes: list[Lane]) -> Lane:
    """Select the next lane, rotating across proven pins first and otherwise
    across all available warm lanes so the switcher can cycle through every
    proxy in the pool."""
    global _lane_cursor
    if not lanes:
        raise IndexError("no lanes")
    # Proven lanes carry the traffic; unproven ones get an exploration slot so
    # they can be proven at all. Rotating uniformly across the whole pool (the
    # naive reading of "switch between all of them") fails: screen-only lanes
    # are mostly duds, so every other request lands on a dead proxy and the
    # client sees 503s while the reliable lane sits idle. One slot in five is
    # enough to promote good lanes without wrecking the success rate.
    ordered = sorted(
        lanes,
        key=lambda ln: (
            0 if ln.ok > 0 else 1,
            -(ln.score or 0.0),
            ln.lat_ms if ln.lat_ms else 8000.0,
            ln.addr,
        ),
    )
    proven = [ln for ln in ordered if ln.ok > 0]
    unproven = [ln for ln in ordered if ln.ok <= 0]
    _lane_cursor += 1
    if proven and (not unproven or _lane_cursor % 5 != 0):
        return proven[_lane_cursor % len(proven)]
    pool = proven + unproven if proven else ordered
    return pool[_lane_cursor % len(pool)]


async def relay(payload: dict, path: str, stream: bool, headers: dict, timeout: float):
    """Try lanes in score order until one answers, waiting at most 7s per proxy
    and switching across ALL available, warm, and parked lanes in the pool."""
    # Every lane gets a turn (that is the point of the switcher), but the wall
    # clock is bounded independently: `7s * relay_attempts` with the default 80
    # attempts is a 560s stall, and a client that gave up long before then sees
    # a dead relay. Cap the total wait so a run of dead lanes fails fast enough
    # for the caller to retry, while still walking the whole pool in the normal
    # case (the cap is far above the time needed to try 40 live lanes).
    attempts = max(1, RELAY_ATTEMPTS, len(POOL.lanes))
    per_attempt = min(7.0, float(RELAY_PROXY_TIMEOUT))
    max_wall = min(float(timeout), max(per_attempt * 6.0, 45.0))
    deadline = time.time() + max_wall
    last_err: tuple | None = None
    tried: set[str] = set()
    t_start = time.time()

    i = 0
    while i < max(attempts, len(POOL.lanes)):
        lanes = [ln for ln in POOL.available_lanes() if f"{ln.proto}://{ln.addr}" not in tried]
        if not lanes:
            lanes = [ln for ln in POOL.warm_lanes() if f"{ln.proto}://{ln.addr}" not in tried]
        if not lanes:
            lanes = [ln for ln in POOL.parked_lanes() if f"{ln.proto}://{ln.addr}" not in tried]
            lanes.sort(key=lambda ln: ln.parked_until)
            if not lanes:
                break
        if i == 0:
            lane = _pick_lane(lanes)
        else:
            # Deterministic fallback order: the backbone first, then the rest by
            # score. Iterating whatever order the pool happened to yield meant a
            # run of dead proxies could consume the whole wall-clock budget
            # before the one working lane was ever tried.
            lanes.sort(key=lambda ln: (
                0 if ln.addr == WARP_PROXY_ADDR else 1,
                -(ln.score or 0.0),
                ln.lat_ms if ln.lat_ms else 8000.0,
                ln.addr,
            ))
            lane = lanes[0]
        tried.add(f"{lane.proto}://{lane.addr}")
        i += 1

        attempt_headers = build_upstream_headers(request=None, custom_key=None)
        if headers:
            for k, v in headers.items():
                if k.lower() != "authorization":
                    attempt_headers[k] = v

        try:
            async with lane.sem:
                lane.inflight += 1
                try:
                    status, resp_headers, body = await _attempt(
                        lane, payload, path, attempt_headers, min(7.0, float(RELAY_PROXY_TIMEOUT), timeout)
                    )
                finally:
                    lane.inflight -= 1
            if status == 200:
                _note_quota_ok()
                ms = (time.time() - t_start) * 1000
                record_request(_display_addr(lane.addr), 200, ms, stream)
                publish("request", {"lane": _display_addr(lane.addr), "status": 200,
                                    "ms": round(ms), "attempt": i, "tier": lane.tier})
                return status, resp_headers, body
            if status == 429 and is_quota_429(body, 429):
                lane.mark_fail(burn=True)
                _note_lane_429(lane.addr, _retry_after_sec(resp_headers))
                if lane.addr.startswith(TOR_LANE_PREFIX):
                    tor_rotate_lane(lane)
                elif lane.addr == WARP_PROXY_ADDR:
                    asyncio.create_task(_warp_reconnect())
                STATS["failovers"] += 1
                last_err = (429, body)
                if time.time() > deadline:
                    break
                continue
            if status == 502:
                try:
                    if json.loads(body).get("error", {}).get("type") == "lane_invalid":
                        STATS["failovers"] += 1
                        last_err = (502, body)
                        if time.time() > deadline:
                            break
                        continue
                except Exception:
                    pass
            if status >= 500:
                lane.mark_fail()
                STATS["failovers"] += 1
                last_err = (status, body)
                if time.time() > deadline:
                    break
                continue
            if 300 <= status < 400:
                lane.mark_fail(burn=True)
                STATS["lane_failures"] += 1
                STATS["failovers"] += 1
                last_err = (status, body)
                if time.time() > deadline:
                    break
                continue
            if status in (401, 403):
                _handle_lane_block(lane, status, body)
                last_err = (status, body)
                if time.time() > deadline:
                    break
                continue
            return status, resp_headers, body
        except Exception as e:
            lane.mark_fail()
            STATS["lane_failures"] += 1
            STATS["failovers"] += 1
            last_err = ("error", str(e).encode())
            if time.time() > deadline:
                break
            continue

    if last_err and isinstance(last_err[0], int) and last_err[0] in (401, 403):
        # Every attempt was blocked upstream. Answer with a retryable 503 whose
        # text names no auth digits — a body mentioning them would itself be
        # classified as an auth failure by the client and kill the turn.
        record_request("-", 503, (time.time() - t_start) * 1000, stream)
        publish("request", {"lane": "-", "status": 503, "ms": round((time.time() - t_start) * 1000)})
        return 503, {"content-type": "application/json", "retry-after": "2"}, json.dumps(
            {"error": {"message": "every egress lane was blocked upstream (temporary, retryable 503) — "
                                  "the pool is rotating, retry shortly",
                       "type": "zen_lane_blocked"}}).encode()
    if last_err and last_err[0] == 429:
        # Avoid returning HTTP 429 to 9router on proxy-wide limits, so 9router does not set a modelLock.
        # Respond with 503 Service Unavailable, letting 9router retry or fall back cleanly.
        record_request("-", 503, (time.time() - t_start) * 1000, stream)
        publish("request", {"lane": "-", "status": 503, "ms": round((time.time() - t_start) * 1000)})
        return 503, {"content-type": "application/json"}, last_err[1]
    if last_err and isinstance(last_err[0], int) and last_err[0] >= 500:
        record_request("-", last_err[0], (time.time() - t_start) * 1000, stream)
        publish("request", {"lane": "-", "status": last_err[0], "ms": round((time.time() - t_start) * 1000)})
        return last_err[0], {"content-type": "application/json"}, last_err[1]
    record_request("-", 503, (time.time() - t_start) * 1000, stream)
    publish("request", {"lane": "-", "status": 503, "ms": round((time.time() - t_start) * 1000)})
    return 503, {"content-type": "application/json"}, json.dumps(
        {"error": {"message": "all egress lanes busy or failed — pool is refilling, retry shortly", "type": "rotator_exhausted"}}
    ).encode()


async def _stream_one(body: bytes):
    """Yield a single buffered body as an SSE stream (failure/4xx path)."""
    yield body


async def relay_stream(payload: dict, path: str, headers: dict, timeout: float):
    """Try lanes in score order; stream from the first lane that answers.
    Waits at most 7s for each proxy to respond and switches across all lanes
    in the pool."""
    # Every lane gets a turn (that is the point of the switcher), but the wall
    # clock is bounded independently: `7s * relay_attempts` with the default 80
    # attempts is a 560s stall, and a client that gave up long before then sees
    # a dead relay. Cap the total wait so a run of dead lanes fails fast enough
    # for the caller to retry, while still walking the whole pool in the normal
    # case (the cap is far above the time needed to try 40 live lanes).
    attempts = max(1, RELAY_ATTEMPTS, len(POOL.lanes))
    per_attempt = min(7.0, float(RELAY_PROXY_TIMEOUT))
    max_wall = min(float(timeout), max(per_attempt * 6.0, 45.0))
    deadline = time.time() + max_wall
    last_err: tuple | None = None
    tried: set[str] = set()
    t_start = time.time()

    i = 0
    while i < max(attempts, len(POOL.lanes)):
        lanes = [ln for ln in POOL.available_lanes(tier="fast") if f"{ln.proto}://{ln.addr}" not in tried]
        if not lanes:
            lanes = [ln for ln in POOL.warm_lanes() if f"{ln.proto}://{ln.addr}" not in tried]
        if not lanes:
            lanes = [ln for ln in POOL.parked_lanes() if f"{ln.proto}://{ln.addr}" not in tried]
            lanes.sort(key=lambda ln: ln.parked_until)
            if not lanes:
                break
        if i == 0:
            lane = _pick_lane(lanes)
        else:
            # Deterministic fallback order: the backbone first, then the rest by
            # score. Iterating whatever order the pool happened to yield meant a
            # run of dead proxies could consume the whole wall-clock budget
            # before the one working lane was ever tried.
            lanes.sort(key=lambda ln: (
                0 if ln.addr == WARP_PROXY_ADDR else 1,
                -(ln.score or 0.0),
                ln.lat_ms if ln.lat_ms else 8000.0,
                ln.addr,
            ))
            lane = lanes[0]
        tried.add(f"{lane.proto}://{lane.addr}")
        i += 1

        attempt_headers = build_upstream_headers(request=None, custom_key=None)
        if headers:
            for k, v in headers.items():
                if k.lower() != "authorization":
                    attempt_headers[k] = v

        client = None
        resp = None
        t0 = time.time()
        attempt_timeout = min(7.0, float(RELAY_PROXY_TIMEOUT), timeout)
        try:
            client = httpx.AsyncClient(
                proxy=lane.url(),
                # connect/response is bounded at 7s, but `read` must NOT be:
                # httpx applies the read timeout to every inter-chunk gap, so a
                # 7s read bound kills a stream that is merely thinking between
                # tokens (a long reasoning block), and the client sees a dead
                # turn. Slow lanes are handled by the peek window below, which
                # fails over before any bytes reach the client.
                timeout=httpx.Timeout(min(timeout, 120.0), connect=attempt_timeout,
                                      read=min(timeout, 120.0)))
            req = client.build_request("POST", f"{UPSTREAM_BASE_URL}/{path}", headers=attempt_headers, json=payload)
            resp = await asyncio.wait_for(client.send(req, stream=True), timeout=attempt_timeout)
            status = resp.status_code
            if status == 200:
                aiter = resp.aiter_bytes()
                first = None
                head: list[bytes] = []
                peek_deadline = time.time() + PEEK_DEADLINE_SEC
                err_kind = None
                saw_garbage = False
                saw_life = False
                ended = False
                while time.time() < peek_deadline:
                    try:
                        rem = max(0.5, peek_deadline - time.time())
                        chunk = await asyncio.wait_for(aiter.__anext__(), timeout=rem)
                    except StopAsyncIteration:
                        ended = True
                        break
                    err_kind = _stream_error_kind(chunk)
                    if err_kind is not None:
                        break
                    head.append(chunk)
                    frame = _stream_frame_kind(chunk)
                    if frame in ("content", "completion"):
                        first = chunk
                        break
                    if frame == "garbage":
                        saw_garbage = True
                        break
                    # keep-alive, or a data frame without output yet (role
                    # ping, usage): keep peeking — a lane that dies before the
                    # first content token can still be swapped out unseen.
                    saw_life = True
                if first is None and err_kind is None and saw_life and head and not saw_garbage:
                    if ended:
                        # The stream closed without a single output frame: that
                        # is an empty answer, not a slow lane. Rotate and retry
                        # here, so the client never sees the empty stream.
                        lane.mark_fail()
                        STATS["lane_failures"] += 1
                        STATS["failovers"] += 1
                        last_err = (502, json.dumps({"error": {"message": "lane produced an empty answer (stream closed with no output)", "type": "lane_invalid"}}).encode())
                        log.info("lane %s produced an empty answer — rotated out", _display_addr(lane.addr))
                        await resp.aclose()
                        await client.aclose()
                        if i >= (attempts - 1) or time.time() > deadline:
                            break
                        continue
                    # the lane is alive but slow (no output token yet): stream
                    # it anyway rather than rotating through the whole budget.
                    first = head[-1]
                    log.info("lane %s is slow (no output token in %.0fs) — streaming anyway",
                             _display_addr(lane.addr), PEEK_DEADLINE_SEC)
                if err_kind is not None:
                    # the lane answered 200 and then reported an upstream error
                    # in-band (flag/quota/region): a lane failure, not a stream
                    # to hand the client.
                    if err_kind == "quota":
                        lane.mark_fail(burn=True)
                        mark_burned(lane.addr, "relay-200-quota")
                    else:
                        lane.mark_fail()
                    STATS["lane_failures"] += 1
                    STATS["failovers"] += 1
                    last_err = (403 if err_kind in ("quota", "region") else 502,
                                json.dumps({"error": {"message": "lane reported an in-band upstream error", "type": "lane_invalid"}}).encode())
                    log.info("lane %s reported an in-band upstream error (%s) — rotated out",
                             _display_addr(lane.addr), err_kind)
                    await resp.aclose()
                    await client.aclose()
                    if i >= (attempts - 1) or time.time() > deadline:
                        break
                    continue
                if first is None:
                    # No output token before the stream closed or the peek
                    # window expired. Proxy junk means the IP is useless (burn);
                    # a silent/slow lane is only demoted, so a healthy-but-slow
                    # model keeps its place in the pool instead of being burned
                    # by a 5-chunk peek that landed on keep-alives.
                    if saw_garbage:
                        lane.mark_fail(burn=True)
                        log.info("lane %s answered 200 with non-SSE proxy garbage — burned", _display_addr(lane.addr))
                    else:
                        lane.mark_fail()
                        log.info("lane %s produced no output token (stream closed or keep-alive only) — rotated out",
                                 _display_addr(lane.addr))
                    STATS["lane_failures"] += 1
                    STATS["failovers"] += 1
                    last_err = (502, json.dumps({"error": {"message": "no output token before the stream closed", "type": "lane_invalid"}}).encode())
                    await resp.aclose()
                    await client.aclose()
                    if i >= (attempts - 1) or time.time() > deadline:
                        break
                    continue
                lane.mark_ok((time.time() - t0) * 1000)
                _note_quota_ok()
                STATS["streams"] += 1
                owned_resp, owned_client = resp, client
                # A stream occupies the lane for its whole lifetime, not just
                # the handshake — count it in-flight until the generator closes.
                lane.inflight += 1
                ms_ttfb = (time.time() - t_start) * 1000
                record_request(_display_addr(lane.addr), 200, ms_ttfb, True)
                publish("request", {"lane": _display_addr(lane.addr), "status": 200,
                                    "ms": round(ms_ttfb), "attempt": i + 1,
                                    "tier": lane.tier, "stream": True})

                async def chunks():
                    try:
                        # replay the peeked head (keep-alives + first data
                        # chunk) then continue the live iterator — the head
                        # was consumed during validation but is part of the
                        # response the client must see.
                        sent_first = False
                        seen_end = False
                        for h in head:
                            if _is_stream_end(h):
                                seen_end = True
                            yield h
                            if h is first:
                                sent_first = True
                                break
                        if not sent_first:
                            yield first
                        async for chunk in aiter:
                            if not seen_end and _is_stream_end(chunk):
                                seen_end = True
                            yield chunk
                        if not seen_end:
                            # The upstream connection dropped before
                            # finish_reason/[DONE]: say so. The harness classifies
                            # this as a transport failure and re-runs the step, so
                            # the user gets the full answer instead of a silently
                            # truncated one. The lane is demoted here too, so a
                            # habitually cutting proxy leaves the pin set.
                            lane.mark_fail()
                            log.warning("stream on lane %s ended without finish_reason/[DONE] — signalling a reset",
                                        _display_addr(lane.addr))
                            yield b'data: {"error":{"message":"stream ended before the model finished answering (upstream transport reset)","type":"stream_interrupted"}}\n\n'
                    except Exception as e:
                        lane.mark_fail()
                        log.warning("stream on lane %s interrupted mid-stream: %s", _display_addr(lane.addr), repr(e))
                        yield b'data: {"error":{"message":"stream ended before the model finished answering (upstream transport reset)","type":"stream_interrupted"}}\n\n'
                    finally:
                        lane.inflight = max(0, lane.inflight - 1)
                        await owned_resp.aclose()
                        await owned_client.aclose()

                return 200, dict(resp.headers), chunks()
            if status >= 500:
                body = await resp.aread()
                lane.mark_fail()
                STATS["failovers"] += 1
                last_err = (status, body)
                await resp.aclose()
                await client.aclose()
                if time.time() > deadline:
                    break
                continue
            if 300 <= status < 400:
                # hijacking/captive proxy redirect — never legitimate from the
                # upstream; burn the lane and fail over.
                await resp.aclose()
                await client.aclose()
                lane.mark_fail(burn=True)
                STATS["lane_failures"] += 1
                STATS["failovers"] += 1
                last_err = (status, b"")
                if time.time() > deadline:
                    break
                continue
            if status == 429 and is_quota_429(await resp.aread(), 429):
                lane.mark_fail(burn=True)
                _note_lane_429(lane.addr, _retry_after_sec(dict(resp.headers)))
                if lane.addr.startswith(TOR_LANE_PREFIX):
                    tor_rotate_lane(lane)
                elif lane.addr == WARP_PROXY_ADDR:
                    asyncio.create_task(_warp_reconnect())
                STATS["failovers"] += 1
                last_err = (429, b"")
                await resp.aclose()
                await client.aclose()
                if time.time() > deadline:
                    break
                continue
            # 401/403 are lane problems here, never the client's key: rotate the
            # lane out and fail over (see _handle_lane_block).
            if status in (401, 403):
                body = await resp.aread()
                await resp.aclose()
                await client.aclose()
                _handle_lane_block(lane, status, body)
                last_err = (status, body)
                if time.time() > deadline:
                    break
                continue
            # a real request problem (400/404/422): not a proxy issue — pass it
            body = await resp.aread()
            resp_headers = dict(resp.headers)
            await resp.aclose()
            await client.aclose()
            return status, resp_headers, _stream_one(body)
        except Exception as e:
            if resp is not None:
                try:
                    await resp.aclose()
                except Exception:
                    pass
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass
            lane.mark_fail()
            STATS["lane_failures"] += 1
            STATS["failovers"] += 1
            last_err = ("error", str(e).encode())
            if time.time() > deadline:
                break
            continue

    if last_err and isinstance(last_err[0], int) and last_err[0] in (401, 403):
        # Same rule as relay(): never hand the client an auth-shaped 4xx.
        return 503, {"content-type": "application/json", "retry-after": "2"}, _stream_one(json.dumps(
            {"error": {"message": "every egress lane was blocked upstream (temporary, retryable 503) — "
                                  "the pool is rotating, retry shortly",
                       "type": "zen_lane_blocked"}}).encode())
    if last_err and last_err[0] == 429:
        # Avoid returning HTTP 429 to 9router on proxy-wide limits, so 9router does not set a modelLock.
        return 503, {"content-type": "application/json"}, _stream_one(last_err[1] or b"")
    if last_err and isinstance(last_err[0], int) and last_err[0] >= 500:
        return last_err[0], {"content-type": "application/json"}, _stream_one(last_err[1])
    return 503, {"content-type": "application/json"}, _stream_one(json.dumps(
        {"error": {"message": "all egress lanes busy or failed — pool is refilling, retry shortly", "type": "rotator_exhausted"}}
    ).encode())


# ── OpenCode client fingerprint (omp patch) ─────────────────────────────────
# zen's free tier now (2026-10) gates on the full OpenCode CLI header set, not
# just the UA: ses_/msg_ ids with a timestamp-bearing hex head are mandatory
# (403 FreeTierError without them). Probes and relayed requests must carry the
# same fingerprint omp sends; minted here so the relay also works standalone.
import random as _fp_random
import string as _fp_string


def _fp_id(prefix: str, invert: bool) -> str:
    ms = int(time.time() * 1000)
    stamp = ms * 0x1000 + 1
    if invert:
        stamp = ~stamp & 0xFFFFFFFFFFFF
    else:
        stamp = stamp & 0xFFFFFFFFFFFF
    tail = "".join(_fp_random.choices(_fp_string.digits + _fp_string.ascii_letters, k=14))
    return f"{prefix}_{stamp:012x}{tail}"


def _fp_headers() -> dict:
    sid = _fp_id("ses", invert=True)
    return {
        "x-opencode-client": "cli",
        "x-opencode-session": sid,
        "x-opencode-session-id": sid,
        "x-session-id": sid,
        "x-session-affinity": sid,
        "x-opencode-request": _fp_id("msg", invert=False),
        "x-opencode-project": "global",
        "x-opencode-directory": os.getcwd(),
    }


def upstream_headers(custom_key: str | None = None) -> dict:
    """Headers for upstream calls. The Authorization header is OMITTED when the
    key is empty — httpx rejects 'Bearer ' (empty value) with
    LocalProtocolError, which silently killed every probe and relayed request.

    The opencode User-Agent is included on EVERY upstream call (probes, models
    fetch, relayed requests) — see build_upstream_headers for why."""
    h = {"Content-Type": "application/json", "User-Agent": UPSTREAM_UA}
    h.update(_fp_headers())
    key = custom_key or get_upstream_key()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def build_upstream_headers(request: Request | None = None, custom_key: str | None = None) -> dict:
    """Headers for upstream calls.

    CRITICAL (measured 2026-08-15 against opencode.ai/zen/v1): the free-tier
    gate is keyed on the User-Agent, NOT only on the egress IP. A UA whose
    lowercase form contains "opencode" gets HTTP 200; every other UA
    (Chrome, python-httpx, curl, axios, node-fetch, Go-http-client) gets an
    instant 429 FreeUsageLimitError on the *same* IP and the *same* key.

    So we always present an opencode client UA. The client's own UA is
    forwarded only when it already identifies as opencode — otherwise it is
    replaced, because forwarding it guarantees a 429.
    """
    h = upstream_headers(custom_key=custom_key)
    ua = UPSTREAM_UA
    if request:
        user_ua = request.headers.get("user-agent", "")
        if user_ua and "opencode" in user_ua.lower():
            ua = user_ua
    h["User-Agent"] = ua
    return h


def _check_relay_auth(request: Request, allow_query: bool = False) -> bool:
    """Constant-time relay-key check.

    allow_query exists only for /api/events: the browser EventSource API cannot
    set an Authorization header, so the key has to ride in the URL there. It is
    deliberately opt-in per endpoint — a key in a query string can land in
    access logs and Referer headers, so no other route accepts it.
    """
    if not RELAY_API_KEY:
        return True
    auth = request.headers.get("authorization", "")
    if hmac.compare_digest(auth, f"Bearer {RELAY_API_KEY}") or hmac.compare_digest(auth, RELAY_API_KEY):
        return True
    if allow_query:
        qk = request.query_params.get("key", "")
        if qk and hmac.compare_digest(qk, RELAY_API_KEY):
            return True
    return False


# ── model resolution (Claude Code tier aliases -> real upstream model) ──
_FALLBACK_MODEL: str | None = None
_FALLBACK_MODEL_AT: float = 0.0
FALLBACK_MODEL_SEC = 300

MODELS_CACHE_SEC = 3600
_models_cache: dict = {"updated": 0.0, "status": 503, "body": b"", "content_type": "application/json"}
_models_retry_at = 0.0   # outage backoff: don't hammer upstream every call


async def get_models_cached() -> tuple[int, str, bytes]:
    # _models_retry_at is assigned below, so it MUST be declared global —
    # without this the very first read raises UnboundLocalError and every
    # models fetch fails (silently, because callers wrap this in try/except).
    global _models_retry_at
    now = time.time()
    if now < _models_retry_at:
        # outage backoff: serve the stale body (or 503) without hammering
        if _models_cache["body"]:
            return _models_cache["status"], _models_cache["content_type"], _models_cache["body"]
        return 503, "application/json", json.dumps({"error": {"message": "models fetch failed", "type": "models_unavailable"}}).encode()
    elif now - _models_cache["updated"] < MODELS_CACHE_SEC and _models_cache["body"]:
        return _models_cache["status"], _models_cache["content_type"], _models_cache["body"]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10)) as client:
            resp = await client.get(SCREEN_URL, headers=upstream_headers())
            _models_retry_at = 0.0
            
            # Filter OpenCode models to return ONLY -free models and prioritize hy3-free at index 0
            body_bytes = resp.content
            if resp.status_code == 200:
                try:
                    data = json.loads(resp.content)
                    if "data" in data and isinstance(data["data"], list):
                        free_models = [m for m in data["data"] if m.get("id", "").endswith("-free")]
                        free_models.sort(key=lambda m: 0 if m.get("id") == "hy3-free" else 1)
                        data["data"] = free_models
                        body_bytes = json.dumps(data).encode()
                except Exception as je:
                    log.warning("failed to parse models JSON: %s", je)
            
            _models_cache.update({"updated": time.time(), "status": resp.status_code,
                                  "body": body_bytes, "content_type": resp.headers.get("content-type", "application/json")})
            return resp.status_code, _models_cache["content_type"], body_bytes
    except Exception as e:
        log.warning("models fetch failed: %s", e)
        _models_retry_at = time.time() + 30
        if _models_cache["body"]:
            return _models_cache["status"], _models_cache["content_type"], _models_cache["body"]
        return 503, "application/json", json.dumps({"error": {"message": "models fetch failed", "type": "models_unavailable"}}).encode()


async def fallback_model() -> str:
    global _FALLBACK_MODEL, _FALLBACK_MODEL_AT
    if _FALLBACK_MODEL and time.time() - _FALLBACK_MODEL_AT < FALLBACK_MODEL_SEC:
        return _FALLBACK_MODEL
    chosen = PROBE_MODEL
    try:
        status, _, body = await get_models_cached()
        if status == 200:
            ids = [m.get("id", "") for m in json.loads(body).get("data", [])]
            if ids:
                # Prefer a free/flash/lightweight model if one exists, else the first
                chosen = next((i for i in ids if "-free" in i or "flash" in i or "mini" in i), ids[0])
    except Exception:
        pass
    _FALLBACK_MODEL = chosen
    _FALLBACK_MODEL_AT = time.time()
    return chosen


_CLAUDE_TIER_HINTS = ("claude-", "haiku", "sonnet", "opus")


def strip_model_prefix(model: str) -> str:
    return model.split("/", 1)[-1]


async def resolve_model(model: str) -> str:
    """Map a client-requested model to one the configured upstream key can serve.

    Provider-agnostic: if the requested model exists upstream, pass it through.
    If it doesn't (wrong tier, alias, or a provider that serves different ids),
    resolve to the configured PROBE_MODEL or a sensible free model from the
    upstream /models list. This makes the relay work with any OpenAI-compatible
    provider — not just opencode's free tier."""
    name = strip_model_prefix(model)
    if name:
        try:
            status, _, body = await get_models_cached()
            if status == 200:
                ids = {m.get("id", "") for m in json.loads(body).get("data", [])}
                if name in ids:
                    return name
        except Exception:
            pass
    return await fallback_model()


# ══════════════════════════════════════════════════════════════════
# Anthropic (/v1/messages) translation — proven layer, ported verbatim
# ══════════════════════════════════════════════════════════════════

def anthropic_to_openai(payload: dict) -> dict:
    msgs: list[dict] = []
    system = payload.get("system")
    if system:
        text = system if isinstance(system, str) else " ".join(
            b.get("text", "") for b in system if isinstance(b, dict))
        if text.strip():
            msgs.append({"role": "system", "content": text})

    for m in payload.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            msgs.append({"role": role, "content": str(content or "")})
            continue
        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    tool_calls.append({
                        "id": b.get("id", "call_" + uuid.uuid4().hex[:24]),
                        "type": "function",
                        "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input", {}))},
                    })
            msg: dict = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            msgs.append(msg)
        else:
            pending_text: list[str] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text":
                    pending_text.append(b.get("text", ""))
                elif btype == "tool_result":
                    if pending_text:
                        msgs.append({"role": "user", "content": "\n".join(pending_text)})
                        pending_text = []
                    tres = b.get("content")
                    if isinstance(tres, list):
                        tres = "\n".join(x.get("text", "") for x in tres if isinstance(x, dict))
                    msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""),
                                 "content": str(tres if tres is not None else "")})
            if pending_text:
                msgs.append({"role": "user", "content": "\n".join(pending_text)})

    out: dict = {
        "model": strip_model_prefix(str(payload.get("model", ""))),
        "messages": msgs,
        "stream": bool(payload.get("stream", False)),
    }
    if payload.get("max_tokens"):
        out["max_tokens"] = payload["max_tokens"]
    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        out["top_p"] = payload["top_p"]
    tools = payload.get("tools")
    if tools:
        out["tools"] = [
            {"type": "function", "function": {
                "name": t.get("name", ""), "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
            for t in tools if isinstance(t, dict)
        ]
    tc = payload.get("tool_choice")
    if isinstance(tc, dict):
        tct = tc.get("type")
        if tct == "auto":
            out["tool_choice"] = "auto"
        elif tct in ("any", "required"):
            out["tool_choice"] = "required"
        elif tct == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    elif isinstance(tc, str):
        out["tool_choice"] = tc
    return out


_ANTHROPIC_STOP = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens",
                   "stop_sequence": "stop_sequence", "length": "max_tokens", "stop": "end_turn",
                   "tool_calls": "tool_use", "content_filter": "end_turn"}


def _oai_finish_to_anthropic(finish: str | None) -> str:
    return _ANTHROPIC_STOP.get(finish or "", "end_turn")


def openai_to_anthropic(body: bytes, model: str) -> bytes:
    try:
        d = json.loads(body)
        choice = d["choices"][0]
        ch = choice["message"]
        content_blocks: list[dict] = []
        text = ch.get("content") or ""
        if text.strip():
            content_blocks.append({"type": "text", "text": text})
        for tc in ch.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                inp = json.loads(fn.get("arguments", "{}"))
            except Exception:
                inp = {}
            content_blocks.append({
                "type": "tool_use", "id": tc.get("id", "toolu_" + uuid.uuid4().hex[:24]),
                "name": fn.get("name", ""), "input": inp,
            })
        if not content_blocks:
            content_blocks = [{"type": "text", "text": ch.get("reasoning_content") or ""}]
        usage = d.get("usage", {})
        return json.dumps({
            "id": d.get("id", "msg_" + uuid.uuid4().hex[:24]), "type": "message",
            "role": "assistant", "model": model, "content": content_blocks,
            "stop_reason": _oai_finish_to_anthropic(choice.get("finish_reason")),
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0)},
        }).encode()
    except Exception:
        return body


def openai_sse_to_anthropic(body: bytes, model: str) -> bytes:
    out: list[str] = []
    msg_id = "msg_" + uuid.uuid4().hex[:24]

    def ev(name: str, data: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n"

    out.append(ev("message_start", {"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "usage": {"input_tokens": 0, "output_tokens": 0}}}))

    text_buf = ""
    tool_calls: dict[int, dict] = {}
    finish = "end_turn"
    for raw in body.decode("utf-8", "ignore").splitlines():
        if not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            break
        try:
            c = json.loads(data)
            choice = c["choices"][0]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            delta = choice.get("delta", {})
            piece = delta.get("content")
            if piece:
                text_buf += piece
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
        except Exception:
            continue

    block_index = 0
    if text_buf:
        out.append(ev("content_block_start", {"type": "content_block_start", "index": block_index,
                                              "content_block": {"type": "text", "text": ""}}))
        out.append(ev("content_block_delta", {"type": "content_block_delta", "index": block_index,
                                              "delta": {"type": "text_delta", "text": text_buf}}))
        out.append(ev("content_block_stop", {"type": "content_block_stop", "index": block_index}))
        block_index += 1
    for idx in sorted(tool_calls):
        slot = tool_calls[idx]
        tid = slot["id"] or ("toolu_" + uuid.uuid4().hex[:24])
        out.append(ev("content_block_start", {"type": "content_block_start", "index": block_index,
                                              "content_block": {"type": "tool_use", "id": tid,
                                                                "name": slot["name"], "input": {}}}))
        if slot["args"]:
            out.append(ev("content_block_delta", {"type": "content_block_delta", "index": block_index,
                                                  "delta": {"type": "input_json_delta",
                                                            "partial_json": slot["args"]}}))
        out.append(ev("content_block_stop", {"type": "content_block_stop", "index": block_index}))
        block_index += 1
    out.append(ev("message_delta", {"type": "message_delta",
                                    "delta": {"stop_reason": _oai_finish_to_anthropic(finish)},
                                    "usage": {"output_tokens": 0}}))
    out.append("event: message_stop\ndata: {\"type\":\"message_stop\"}\n")
    return "\n".join(out).encode()


# ══════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Modern FastAPI lifespan: startup work, then graceful shutdown.

    Replaces the deprecated @app.on_event hooks and, unlike them, guarantees
    the pool is persisted even when the process is asked to stop mid-churn.
    """
    load_settings()

    # 1. SOCKS library validation
    try:
        import socksio  # noqa: F401
    except ImportError:
        log.error("CRITICAL ERROR: SOCKS proxy support is missing! SOCKS4/5 proxies will be ignored. "
                  "Please run: pip install 'httpx[socks]' to install SOCKS dependencies.")

    # 2. Configuration sanity — surface misconfiguration at boot instead of
    #    letting the operator discover it as "0 warm proxies" an hour later.
    for line in config_warnings():
        log.warning("CONFIG: %s", line)

    # 3. Re-seed remembered lanes so a restart is not a cold start.
    #    Burn memory loads FIRST so the re-seed can filter out lanes whose IPs
    #    have burned since the last run instead of re-probing them.
    load_burned()
    restored = load_lanes()
    if restored:
        log.info("warm-start: %d remembered lanes queued for fast revalidation", restored)

    # 3b. Tor egress. Enabled by config; verified by an actual connect to the
    #     SOCKS port, so a misconfigured port degrades to "no tor lanes" with a
    #     clear warning instead of silently adding lanes that can never dial.
    _ensure_warp_lane()
    _ensure_direct_lane()
    if TOR_ENABLED:
        if tor_available():
            added = tor_ensure_lanes()
            log.info("tor: enabled, %d circuit lanes on 127.0.0.1:%d", added, TOR_SOCKS_PORT)
        else:
            log.warning("tor_enabled=true but nothing is listening on 127.0.0.1:%d — "
                        "no tor lanes will be created. Start tor with "
                        "'SocksPort %d IsolateSOCKSAuth'.", TOR_SOCKS_PORT, TOR_SOCKS_PORT)

    # Background workers are skipped when IP_RELAY_NO_BACKGROUND=1 so tests (and
    # anyone importing the app for inspection) don't start scraping the internet.
    tasks: list[asyncio.Task] = []
    if os.environ.get("IP_RELAY_NO_BACKGROUND", "0") not in ("1", "true", "yes"):
        tasks = [asyncio.create_task(pool_manager()),
                 asyncio.create_task(lane_persist_loop())]

        # 4. Direct validation check against the Upstream on startup
        async def verify_upstream():
            try:
                status, _, body = await get_models_cached()
                if status == 200:
                    log.info("Upstream verification: Upstream base URL and API key are VALID.")
                else:
                    log.error("CRITICAL Upstream verification FAILED (HTTP %d). "
                              "Your configured UPSTREAM_BASE_URL ('%s') or UPSTREAM_API_KEY might be invalid. "
                              "Response: %s", status, UPSTREAM_BASE_URL, body[:150].decode('utf-8', errors='ignore'))
            except Exception as e:
                log.error("CRITICAL Upstream connection FAILED: Could not reach upstream on startup: %s. "
                          "Verify your network or UPSTREAM_BASE_URL ('%s').", e, UPSTREAM_BASE_URL)

        tasks.append(asyncio.create_task(verify_upstream()))

    try:
        yield
    finally:
        n = save_lanes()
        if n:
            log.info("shutdown: persisted %d lanes to %s", n, LANES_FILE)
        b = save_burned()
        if b:
            log.info("shutdown: persisted %d burned IPs to %s", b, BURNED_FILE)
        for t in tasks:
            t.cancel()


app.router.lifespan_context = lifespan


def config_warnings() -> list[str]:
    """Human-readable configuration problems. Returned by /api/diagnostics and
    logged at startup — misconfiguration should never be silent."""
    out: list[str] = []
    if not UPSTREAM_BASE_URL:
        out.append("upstream_base_url is empty — no upstream to relay to.")
    elif not UPSTREAM_BASE_URL.startswith(("http://", "https://")):
        out.append(f"upstream_base_url ('{UPSTREAM_BASE_URL}') has no http(s):// scheme.")
    if UPSTREAM_API_KEY == "public":
        out.append("upstream_api_key is the shared literal 'public' — it is globally "
                   "rate-limited and usually pre-burned. Expect widespread 429s.")
    if "opencode" not in UPSTREAM_UA.lower():
        out.append(f"upstream_user_agent ('{UPSTREAM_UA}') does not contain 'opencode'. "
                   "The upstream free tier gates on the User-Agent: every other UA gets "
                   "an instant 429 regardless of egress IP or key.")
    if not PROBE_MODEL:
        out.append("probe_model is empty — lanes cannot be verified with a real completion.")
    if not RELAY_API_KEY:
        out.append("relay_api_key is empty — the control plane and /v1 are UNAUTHENTICATED. "
                   "Anyone who can reach this port can read your proxy pool and repoint the upstream.")
    try:
        import socksio  # noqa: F401
    except ImportError:
        if ALLOW_SOCKS:
            out.append("allow_socks is on but socksio is not installed — every SOCKS "
                       "candidate will fail. Install with: pip install 'httpx[socks]'")
    if TEST_CONCURRENCY > 40:
        out.append(f"proxy_test_concurrency is {TEST_CONCURRENCY}: on a residential/CPE link this "
                   "saturates the NAT table and every probe fails with connect errors. "
                   "15–25 is the safe range (adaptive concurrency will also back off on its own).")
    if LANE_MAX_INFLIGHT > 4:
        out.append(f"lane_max_inflight is {LANE_MAX_INFLIGHT}: free proxies typically survive "
                   "1–2 concurrent requests before dropping.")
    if TOR_ENABLED and not tor_available():
        out.append(f"tor_enabled is on but nothing is listening on 127.0.0.1:{TOR_SOCKS_PORT}. "
                   f"Start tor with 'SocksPort {TOR_SOCKS_PORT} IsolateSOCKSAuth' — without "
                   "IsolateSOCKSAuth every lane shares ONE circuit and one exit IP, which "
                   "defeats the entire point.")
    if not BURN_MEMORY:
        out.append("burn_memory is off. Measured: a 429'd egress IP does not recover (40 min of "
                   "continuous polling, zero successes), and the scrape feeds re-serve the same "
                   "dead addresses every cycle — so the prober will keep spending its budget on "
                   "known-spent IPs.")
    if LANE_COOLDOWN_SEC < 600:
        out.append(f"lane_cooldown_sec is {LANE_COOLDOWN_SEC}s. A burned IP was measured 429ing "
                   "for 40+ minutes straight (and 8 days on one host), so a short cooldown just "
                   "re-probes corpses. 3600s or more is realistic.")
    return out


@app.get("/healthz")
async def healthz():
    s = POOL.stats()
    return {"ok": s["warm"] > 0, "version": VERSION, **s}


@app.get("/metrics")
async def metrics():
    """Prometheus text exposition. Unauthenticated on purpose: it contains no
    credentials and no proxy addresses, and scrapers rarely carry bearer
    tokens. Bind to localhost or firewall the port if that is not acceptable."""
    s = POOL.stats()
    m5 = metrics_window(300)
    lines = [
        "# HELP iprelay_warm_lanes Warm egress lanes",
        "# TYPE iprelay_warm_lanes gauge",
        f"iprelay_warm_lanes {s['warm']}",
        "# HELP iprelay_parked_lanes Parked (cooling down) lanes",
        "# TYPE iprelay_parked_lanes gauge",
        f"iprelay_parked_lanes {s['parked']}",
        "# HELP iprelay_unique_subnets Distinct /24s among warm lanes",
        "# TYPE iprelay_unique_subnets gauge",
        f"iprelay_unique_subnets {s['subnets']}",
        "# HELP iprelay_capacity Concurrent requests the pool can absorb",
        "# TYPE iprelay_capacity gauge",
        f"iprelay_capacity {s['capacity']}",
        "# HELP iprelay_inflight Requests currently in flight",
        "# TYPE iprelay_inflight gauge",
        f"iprelay_inflight {s['inflight']}",
        "# HELP iprelay_candidate_queue Candidates awaiting probe",
        "# TYPE iprelay_candidate_queue gauge",
        f"iprelay_candidate_queue {s['queue']}",
        "# HELP iprelay_probe_concurrency Probe concurrency currently in force",
        "# TYPE iprelay_probe_concurrency gauge",
        f"iprelay_probe_concurrency {ADAPT['current']}",
        "# HELP iprelay_requests_total Client requests served",
        "# TYPE iprelay_requests_total counter",
        f"iprelay_requests_total {STATS['requests']}",
        "# HELP iprelay_failovers_total Lane failovers inside client requests",
        "# TYPE iprelay_failovers_total counter",
        f"iprelay_failovers_total {STATS['failovers']}",
        "# HELP iprelay_lane_failures_total Lane-level failures",
        "# TYPE iprelay_lane_failures_total counter",
        f"iprelay_lane_failures_total {STATS['lane_failures']}",
        "# HELP iprelay_upstream_429_total Key-global rate-limit responses",
        "# TYPE iprelay_upstream_429_total counter",
        f"iprelay_upstream_429_total {STATS['upstream_429s']}",
        "# HELP iprelay_probes_ok_total Candidates that passed verification",
        "# TYPE iprelay_probes_ok_total counter",
        f"iprelay_probes_ok_total {STATS['probes_ok']}",
        "# HELP iprelay_latency_p95_ms 95th percentile latency, 5m window",
        "# TYPE iprelay_latency_p95_ms gauge",
        f"iprelay_latency_p95_ms {m5['p95_ms']}",
        "# HELP iprelay_quota_exhausted Upstream key quota exhausted (1/0)",
        "# TYPE iprelay_quota_exhausted gauge",
        f"iprelay_quota_exhausted {1 if QUOTA_STATE['exhausted'] else 0}",
    ]
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/api/metrics")
async def api_metrics(request: Request):
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"w60": metrics_window(60), "w300": metrics_window(300),
            "w3600": metrics_window(3600), "spark": sparkline_series(),
            "adaptive": ADAPT}


@app.get("/api/diagnostics")
async def api_diagnostics(request: Request):
    """Everything needed to answer "why do I have 0 warm lanes?" in one call:
    config problems, probe-failure breakdown, and a plain-language verdict."""
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    s = POOL.stats()
    reasons: dict[str, int] = {}
    for line in list(LOG_RING)[-400:]:
        if "failed Stage 2 connection" in line or "failed Stage 1" in line:
            reasons["connect_error"] = reasons.get("connect_error", 0) + 1
        elif "failed Stage 2: HTTP 401" in line:
            reasons["http_401_bad_key"] = reasons.get("http_401_bad_key", 0) + 1
        elif "failed Stage 2: HTTP 403" in line:
            reasons["http_403_blocked"] = reasons.get("http_403_blocked", 0) + 1
        elif "failed Stage 2: HTTP 407" in line:
            reasons["http_407_proxy_auth"] = reasons.get("http_407_proxy_auth", 0) + 1
        elif "failed Stage 2: HTTP" in line:
            reasons["http_other"] = reasons.get("http_other", 0) + 1
        elif "rate-limited (429)" in line:
            reasons["upstream_429"] = reasons.get("upstream_429", 0) + 1
        elif "latency was too slow" in line:
            reasons["too_slow"] = reasons.get("too_slow", 0) + 1

    verdict = "healthy"
    advice = "Pool is serving traffic."
    if s["warm"] == 0:
        top = max(reasons, key=lambda k: reasons[k]) if reasons else None
        if top == "connect_error":
            verdict = "egress_blocked"
            advice = ("Probes cannot open outbound connections to proxy ports. A VPN, firewall, "
                      "or ISP filter is blocking non-80/443 traffic, or the link's NAT table is "
                      "saturated. Lower proxy_test_concurrency (15–25) and retry without the VPN.")
        elif top == "http_401_bad_key":
            verdict = "bad_upstream_key"
            advice = "The upstream rejects the API key (401). Fix upstream_api_key."
        elif top == "http_403_blocked":
            verdict = "upstream_blocks_proxies"
            advice = "The upstream 403s these egress IPs (datacenter/VPN ranges are often blocked)."
        elif top == "upstream_429":
            verdict = "quota_exhausted"
            advice = "The key's own quota is exhausted; every IP 429s. Use a private key."
        elif not reasons:
            verdict = "warming_up"
            advice = "No probe results yet — the pool is still fetching and testing candidates."
        else:
            verdict = "no_usable_proxies"
            advice = "Candidates connect but fail verification. Check the reason breakdown."
    elif QUOTA_STATE["exhausted"]:
        verdict = "quota_exhausted"
        advice = "Lanes are alive but the upstream key's quota is exhausted; probing is paused."
    return {
        "verdict": verdict, "advice": advice,
        "config_warnings": config_warnings(),
        "probe_failures": reasons,
        "pool": s,
        "adaptive": ADAPT,
        "quota": {"exhausted": QUOTA_STATE["exhausted"],
                  "resumes_in": max(0, int(QUOTA_STATE["backoff_until"] - time.time()))},
        "socks_available": _socks_available(),
        "persisted_lanes": os.path.exists(LANES_FILE),
    }


def _socks_available() -> bool:
    try:
        import socksio  # noqa: F401
        return True
    except ImportError:
        return False


@app.get("/api/profiles")
async def api_profiles(request: Request):
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"profiles": [{"id": k, **v} for k, v in PROVIDER_PROFILES.items()]}


@app.get("/api/events")
async def api_events(request: Request):
    """Server-sent events: lane_up / lane_down / request / churn / adapt.

    Replaces three independent polling loops in the dashboard. Each subscriber
    gets a bounded queue — a stalled browser tab drops events instead of
    applying backpressure to the relay.
    """
    if not _check_relay_auth(request, allow_query=True):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.add(q)

    async def gen():
        try:
            yield f"data: {json.dumps({'kind': 'hello', 'ts': time.time(), 'pool': POOL.stats(), 'version': VERSION})}\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(evt)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"   # keep proxies from closing the stream
        except asyncio.CancelledError:
            raise
        finally:
            _subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stats")
async def api_stats(request: Request):
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    s = POOL.stats()
    return {"version": VERSION, "uptime_sec": int(time.time() - STATS["started"]),
            "pool": s, "stats": STATS, "settings": public_settings(),
            "adaptive": ADAPT, "metrics": metrics_window(300),
            "burn": {"known_spent_ips": len(BURNED), "ttl_sec": BURN_TTL_SEC,
                     "enabled": BURN_MEMORY, **BURN_STATS},
            "tor": {"enabled": TOR_ENABLED, "socks_port": TOR_SOCKS_PORT,
                    "reachable": tor_available() if TOR_ENABLED else False,
                    "target_lanes": TOR_LANES, "rotations": _tor_rotations,
                    "lanes": sum(1 for ln in POOL.lanes.values()
                                 if ln.addr.startswith(TOR_LANE_PREFIX)),
                    "warm_lanes": sum(1 for ln in POOL.warm_lanes()
                                      if ln.addr.startswith(TOR_LANE_PREFIX))},
            "quota": {"exhausted": QUOTA_STATE["exhausted"],
                      "resumes_in": max(0, int(QUOTA_STATE["backoff_until"] - time.time()))}}


@app.get("/api/pool")
async def api_pool(request: Request):
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    now = time.time()
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", 50))))
        offset = max(0, int(request.query_params.get("offset", 0)))
    except ValueError:
        limit, offset = 50, 0

    all_warm = POOL.warm_lanes()
    all_parked = POOL.parked_lanes()
    warm = [{"addr": _display_addr(ln.addr), "proto": ln.proto, "score": round(ln.score, 3),
             "lat_ms": round(ln.lat_ms), "ok": ln.ok, "fails": ln.fails,
             "tier": ln.tier, "subnet": ln.subnet, "inflight": ln.inflight,
             "capacity": max(1, LANE_MAX_INFLIGHT),
             "kind": "tor" if ln.addr.startswith(TOR_LANE_PREFIX) else ("direct" if not ln.addr else "proxy"),
             "proven": ln.ok > 0,
             "age_sec": int(now - ln.created),
             "last_ok_ago": int(now - ln.last_ok) if ln.last_ok else -1}
            for ln in all_warm[offset:offset + limit]]
    parked = [{"addr": _display_addr(ln.addr), "proto": ln.proto, "until_in": int(ln.parked_until - now),
               "subnet": ln.subnet, "score": round(ln.score, 3), "fails": ln.fails,
               "probe_tries": ln.probe_tries,
               "last_probe_ago": int(now - ln.last_probe) if ln.last_probe else -1}
              for ln in all_parked[offset:offset + limit]]
    return {"warm": warm, "parked": parked,
            "queue": len(POOL.candidates) + len(POOL.priority_candidates),
            "total_warm": len(all_warm), "total_parked": len(all_parked),
            "limit": limit, "offset": offset,
            "subnets": POOL.subnet_counts()}


@app.get("/api/settings")
async def get_settings(request: Request):
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return public_settings()


@app.post("/api/settings")
async def post_settings(request: Request):
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    
    updates, errors = validate_settings_payload(body)
    if errors:
        return JSONResponse({"error": "invalid settings", "details": errors}, status_code=400)
    webshare_changed = ("webshare_token" in updates
                        and updates["webshare_token"] != settings.get("webshare_token"))

    res = apply_settings(updates)
    log.info("settings updated via API: %s", ", ".join(sorted(updates.keys())) or "(no changes)")

    if webshare_changed:
        log.info("Settings: Webshare tokens updated — resetting scraper interval to force immediate proxy check.")
        POOL.last_fetch = 0.0
        # Immediately kick off scraping & batch churning in the background
        asyncio.create_task(_fetch_sources())

    return res


@app.post("/api/profiles/{profile_id}")
async def api_apply_profile(profile_id: str, request: Request):
    """One-click upstream setup. Never clobbers an existing key with a blank."""
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    prof = PROVIDER_PROFILES.get(profile_id)
    if not prof:
        return JSONResponse({"error": "unknown profile"}, status_code=404)
    updates = {"upstream_base_url": prof["upstream_base_url"], "probe_model": prof["probe_model"]}
    if prof.get("upstream_api_key"):
        updates["upstream_api_key"] = prof["upstream_api_key"]
    apply_settings(updates)
    _models_cache["updated"] = 0.0
    log.info("provider profile applied: %s (%s)", profile_id, prof["label"])
    return {"applied": profile_id, "settings": public_settings(), "note": prof.get("note", "")}


@app.post("/api/keys/validate")
async def api_validate_keys(request: Request):
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    
    # 1. Validate Webshare Tokens
    webshare_tokens = body.get("webshare_token", "").strip()
    import re
    orig_token = str(settings.get("webshare_token", ""))
    orig_tokens = [o.strip() for o in re.split(r"[\s,;\n\r]+", orig_token) if o.strip()]
    
    if not webshare_tokens:
        tokens = list(orig_tokens)
    else:
        tokens = [t.strip() for t in re.split(r"[\s,;\n\r]+", webshare_tokens) if t.strip()]
        
    resolved_tokens = []
    for t in tokens:
        if "..." in t or t == "(none)":
            matched = False
            for o in orig_tokens:
                if o.startswith(t.split("...")[0]):
                    resolved_tokens.append(o)
                    matched = True
                    break
            if not matched and orig_tokens:
                resolved_tokens.append(orig_tokens[0])
        else:
            resolved_tokens.append(t)
            
    webshare_results = await asyncio.gather(*[validate_webshare_token(tok) for tok in resolved_tokens])
    
    # 2. Validate Upstream URL & Key
    upstream_url = body.get("upstream_base_url", "").strip() or UPSTREAM_BASE_URL
    upstream_key = body.get("upstream_api_key", "").strip()
    if not upstream_key or "..." in upstream_key or upstream_key == "(none)":
        upstream_key = str(settings.get("upstream_api_key", ""))
        
    upstream_result = await validate_upstream(upstream_url, upstream_key)
    
    return {
        "webshare": webshare_results,
        "upstream": upstream_result
    }


@app.get("/api/logs")
async def api_logs(request: Request):
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"lines": list(LOG_RING)[-200:]}


@app.post("/api/refresh")
async def api_refresh(request: Request):
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    POOL.last_fetch = 0.0
    await _fetch_sources()
    return {"queue": len(POOL.candidates), "sources_ok": POOL.sources_ok}

@app.post("/api/pool/seed")
async def api_pool_seed(request: Request):
    """Manually inject candidate lanes into the priority queue.

    Body: {"lanes": ["ip:port", ...], "proto": "http"|"socks4"|"socks5"}
    Seeded lanes go to the front of the probe queue (same path as persisted
    lanes at boot) — they still must pass the two-stage probe before serving.
    """
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    proto = str(body.get("proto", "http")).lower()
    if proto not in ("http", "https", "socks4", "socks5"):
        return JSONResponse({"error": "proto must be http/https/socks4/socks5"}, status_code=400)
    lanes = body.get("lanes")
    if not isinstance(lanes, list):
        return JSONResponse({"error": "lanes must be a list of ip:port"}, status_code=400)
    now = time.time()
    added = 0
    for addr in lanes:
        addr = str(addr).strip()
        if not addr or is_burned(addr):
            continue
        key = f"{proto}://{addr}"
        if key in POOL.lanes or key in POOL.candidates or key in POOL.priority_candidates:
            continue
        POOL.priority_candidates[key] = now
        added += 1
    return {"added": added, "queue": len(POOL.priority_candidates) + len(POOL.candidates)}


@app.get("/api/burned")
async def api_burned(request: Request):
    """The blocklist of spent egress IPs, newest first.

    Operator-facing: without this the burn list is invisible and a wrongly
    blocklisted IP would be impossible to diagnose.
    """
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    now = time.time()
    try:
        limit = max(1, min(1000, int(request.query_params.get("limit", 100))))
    except ValueError:
        limit = 100
    rows = sorted(BURNED.items(), key=lambda kv: -kv[1].get("at", 0))[:limit]
    return {
        "enabled": BURN_MEMORY,
        "ttl_sec": BURN_TTL_SEC,
        "total": len(BURNED),
        "stats": BURN_STATS,
        "ips": [{"ip": ip, "reason": r.get("reason", ""), "hits": r.get("hits", 1),
                 "age_sec": int(now - r.get("at", 0)),
                 "expires_in": max(0, int(BURN_TTL_SEC - (now - r.get("at", 0))))}
                for ip, r in rows],
    }


@app.post("/api/burned/clear")
async def api_burned_clear(request: Request):
    """Forget the burn list (or one IP via ?ip=...)."""
    if not _check_relay_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    ip = request.query_params.get("ip", "").strip()
    if ip:
        existed = BURNED.pop(ip, None) is not None
        save_burned()
        return {"cleared": 1 if existed else 0, "ip": ip, "remaining": len(BURNED)}
    n = len(BURNED)
    BURNED.clear()
    save_burned()
    log.info("burn memory cleared by operator (%d IPs forgotten)", n)
    return {"cleared": n, "remaining": 0}


@app.get("/v1/models")
async def models(request: Request):
    if not _check_relay_auth(request):
        return JSONResponse({"error": {"message": "invalid relay key", "type": "invalid_relay_key"}}, status_code=401)
    status, ct, body = await get_models_cached()
    return Response(content=body, media_type=ct, status_code=status)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not _check_relay_auth(request):
        return JSONResponse({"error": {"message": "invalid relay key", "type": "invalid_relay_key"}}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "invalid json", "type": "invalid_request"}}, status_code=400)
    STATS["requests"] += 1
    payload["model"] = await resolve_model(str(payload.get("model", "")))
    _ensure_read_tool(payload)
    stream = bool(payload.get("stream", False))
    headers = build_upstream_headers(request)
    timeout = 300.0 if stream else 120.0
    if stream:
        status, resp_headers, chunks = await relay_stream(payload, "chat/completions", headers, timeout)
        return StreamingResponse(chunks, status_code=status,
                                 media_type=resp_headers.get("content-type") or "text/event-stream")
    status, resp_headers, body = await relay(payload, "chat/completions", stream, headers, timeout)
    return Response(content=body, status_code=status,
                    media_type=resp_headers.get("content-type") or "application/json")


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    auth = request.headers.get("authorization") or request.headers.get("x-api-key", "")
    if RELAY_API_KEY and not (hmac.compare_digest(auth, f"Bearer {RELAY_API_KEY}")
                              or hmac.compare_digest(auth, RELAY_API_KEY)):
        return JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": "invalid relay key"}}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"type": "error", "error": {"type": "invalid_request_error", "message": "invalid json"}}, status_code=400)
    STATS["requests"] += 1
    payload["model"] = await resolve_model(str(payload.get("model", "")))
    oai = anthropic_to_openai(payload)
    _ensure_read_tool(oai)
    stream = oai["stream"]
    headers = build_upstream_headers(request)
    timeout = 300.0 if stream else 120.0
    status, resp_headers, body = await relay(oai, "chat/completions", stream, headers, timeout)
    if status >= 300:
        try:
            msg = json.loads(body).get("error", {}).get("message", "upstream error")
        except Exception:
            msg = "upstream error"
        etype = "rate_limit_error" if status == 429 else "api_error"
        return JSONResponse({"type": "error", "error": {"type": etype, "message": msg}}, status_code=status)
    if stream:
        return StreamingResponse(iter([openai_sse_to_anthropic(body, payload["model"])]), media_type="text/event-stream")
    return Response(content=openai_to_anthropic(body, payload["model"]), media_type="application/json")

DASHBOARD_HTML = open(os.path.join(os.path.dirname(__file__), "dashboard.html"), encoding="utf-8").read() \
    if os.path.exists(os.path.join(os.path.dirname(__file__), "dashboard.html")) else "<h1>dashboard.html missing</h1>"


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML.replace("__VERSION__", VERSION))


@app.get("/login", response_class=HTMLResponse)
async def login():
    return HTMLResponse("<h2>ip-relay</h2><p>Set RELAY_API_KEY off to disable auth, or pass Authorization header.</p>")


if __name__ == "__main__":
    import uvicorn
    # Direct runs stay loopback-only by default: this relay must never listen
    # outside the machine. Bind elsewhere only by setting HOST explicitly.
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8080")))
