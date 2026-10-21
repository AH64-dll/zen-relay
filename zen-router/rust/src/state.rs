//! Lanes, pool, burn memory, quota tracking, request metrics — the relay's
//! runtime state. Mirrors the Python relay's structures key-for-key so the two
//! can share state files during migration.
use crate::config::{now, Paths, Runtime, Settings};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet, VecDeque};
use std::io::Write;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::Semaphore;

pub const QUOTA_LANE_THRESHOLD: usize = 3;
pub const QUOTA_WINDOW_SEC: f64 = 45.0;
pub const REQ_LOG_MAX: usize = 2000;

// Park reasons. The distinction matters on success: a lane parked for a blocked
// or spent answer is proven working again by a completion (so it returns to the
// pool at once), while a lane parked for breaking streams is not — every such
// lane commits first and dies right after, so a commit proves nothing.
pub const PARK_NONE: u8 = 0;
pub const PARK_BLOCKED: u8 = 1;
pub const PARK_BREAKS: u8 = 2;

/// Committed streams that may die in a row before the lane is parked. One or
/// two mid-stream resets are upstream weather; three in a row on the same lane
/// is a proxy that cuts every answer — without this it would keep its place in
/// the pool forever, because each attempt commits (mark_ok) before it dies.
pub const STREAM_BREAK_LIMIT: u32 = 3;

pub fn logline(msg: &str) {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let h = (secs / 3600) % 24;
    let m = (secs / 60) % 60;
    let s = secs % 60;
    let mut out = std::io::stdout();
    let _ = writeln!(out, "{h:02}:{m:02}:{s:02} | {msg}");
    let _ = out.flush();
}

// ── address helpers ────────────────────────────────────────────────
pub fn ip_of(addr: &str) -> String {
    if addr.is_empty() {
        return String::new();
    }
    addr.rsplit('@').next().unwrap_or("").split(':').next().unwrap_or("").to_string()
}

pub fn display_addr(addr: &str) -> String {
    if addr.is_empty() {
        return "direct".into();
    }
    match addr.rsplit_once('@') {
        Some((_, host)) => host.to_string(),
        None => addr.to_string(),
    }
}

/// Keep the host:port visible but never echo proxy credentials. Used by the
/// management API, where `raw_addr` used to hand out `user:pass@host:port`.
pub fn redact_addr(addr: &str) -> String {
    match addr.rsplit_once('@') {
        Some((userinfo, host)) => {
            let user = userinfo.split(':').next().unwrap_or(userinfo);
            format!("{user}:***@{host}")
        }
        None => addr.to_string(),
    }
}

pub fn key_subnet(key: &str) -> String {
    let addr = key.split("://").nth(1).unwrap_or(key);
    let host = addr.rsplit('@').next().unwrap_or(addr).split(':').next().unwrap_or(addr);
    let parts: Vec<&str> = host.split('.').collect();
    if parts.len() == 4 {
        parts[..3].join(".")
    } else {
        host.to_string()
    }
}

pub fn valid_addr(addr: &str) -> bool {
    // Proxy URLs may carry `user:pass@` (webshare does); validate the host part.
    let host = addr.rsplit('@').next().unwrap_or(addr);
    let parts: Vec<&str> = host.split(':').collect();
    if parts.len() != 2 {
        return false;
    }
    let Ok(port) = parts[1].parse::<u32>() else { return false };
    if port == 0 || port > 65535 {
        return false;
    }
    let name = parts[0];
    let octs: Vec<&str> = name.split('.').collect();
    if octs.len() == 4 && octs.iter().all(|o| o.parse::<u32>().is_ok()) {
        return octs.iter().all(|o| o.parse::<u32>().map(|v| v <= 255).unwrap_or(false));
    }
    // DNS hostnames are valid proxy endpoints — the Webshare rotating
    // gateway is p.webshare.io, and public lists occasionally carry names.
    // Keep the bar at "plausible FQDN", not "reachable": the probe decides.
    name.len() <= 253
        && name.contains('.')
        && name.chars().any(|c| c.is_ascii_alphabetic())
        && name
            .split('.')
            .all(|l| !l.is_empty()
                && l.len() <= 63
                && l.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
                && !l.starts_with('-')
                && !l.ends_with('-'))
}

pub fn ct_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    let mut x = 0u8;
    for i in 0..a.len() {
        x |= a[i] ^ b[i];
    }
    x == 0
}

// ── lanes ──────────────────────────────────────────────────────────
#[derive(Clone, Debug)]
pub struct Lane {
    pub addr: String,
    pub proto: String,
    pub score: f64,
    pub lat_ms: f64,
    pub ok: u64,
    pub fails: u64,
    pub parked_until: f64,
    /// When the current park episode was armed (0.0 = never parked). Recovery
    /// is scheduled from here, not from `last_probe`: a probe of a *warm* lane
    /// must not delay the re-test of a lane that parks a moment later.
    pub parked_at: f64,
    /// Why the lane is parked — see PARK_* below. A lane parked for breaking
    /// streams stays out on success (a commit is not proof: the very next
    /// thing it does is die again); every other park is cleared by a success.
    pub park_reason: u8,
    /// Consecutive committed streams that died before finish_reason/[DONE].
    pub breaks: u32,
    pub last_ok: f64,
    pub last_probe: f64,
    pub probe_tries: u32,
    pub consec_fails: u32,
    pub inflight: u32,
    /// A rotating lane hands out a fresh exit IP on every connection, so a
    /// failure says nothing about the *next* attempt: it must not be parked
    /// for the long cooldown and its address must not be burned.
    pub rotating: bool,
    pub created: f64,
    pub sem: Option<Arc<Semaphore>>,
    pub cached_client: Option<Arc<reqwest::Client>>,
}

/// A Webshare rotating entry carries `-rotate` in its username: the gateway
/// hands out a new exit IP on every new connection.
pub fn is_rotating_addr(addr: &str) -> bool {
    addr.contains("-rotate")
}

impl Lane {
    pub fn new(addr: &str, proto: &str) -> Lane {
        Lane {
            addr: addr.to_string(),
            proto: proto.to_string(),
            score: 0.5,
            lat_ms: 0.0,
            ok: 0,
            fails: 0,
            parked_until: 0.0,
            parked_at: 0.0,
            park_reason: PARK_NONE,
            breaks: 0,
            last_ok: 0.0,
            last_probe: 0.0,
            probe_tries: 0,
            consec_fails: 0,
            inflight: 0,
            rotating: is_rotating_addr(addr),
            created: now(),
            sem: None,
            cached_client: None,
        }
    }

    pub fn key(&self) -> String {
        format!("{}://{}", self.proto, self.addr)
    }

    pub fn subnet(&self) -> String {
        if self.addr.is_empty() {
            return "direct".into();
        }
        key_subnet(&format!("{}://{}", self.proto, self.addr))
    }

    pub fn tier(&self) -> &'static str {
        if self.lat_ms == 0.0 {
            "medium"
        } else if self.lat_ms < 1500.0 {
            "fast"
        } else if self.lat_ms < 4000.0 {
            "medium"
        } else {
            "slow"
        }
    }

    pub fn warm(&self) -> bool {
        self.parked_until < now() && self.score > 0.05
    }

    pub fn proxy_url(&self) -> Option<String> {
        if self.proto == "direct" || self.addr.is_empty() {
            return None;
        }
        let scheme = if self.proto == "http" || self.proto == "https" {
            "http"
        } else {
            "socks5" // socks4 has no transport either side; dialled as socks5, like the Python relay
        };
        Some(format!("{scheme}://{}", self.addr))
    }

    pub fn to_dict(&self) -> Value {
        json!({
            "addr": self.addr, "proto": self.proto,
            "score": (self.score * 10000.0).round() / 10000.0,
            "lat_ms": (self.lat_ms * 10.0).round() / 10.0,
            "ok": self.ok, "fails": self.fails,
            "last_ok": self.last_ok.round(),
        })
    }

    pub fn from_dict(d: &Value) -> Lane {
        let mut ln = Lane::new(
            d.get("addr").and_then(|v| v.as_str()).unwrap_or(""),
            d.get("proto").and_then(|v| v.as_str()).unwrap_or("http"),
        );
        ln.score = d.get("score").and_then(|v| v.as_f64()).unwrap_or(0.5);
        ln.lat_ms = d.get("lat_ms").and_then(|v| v.as_f64()).unwrap_or(0.0);
        ln.ok = d.get("ok").and_then(|v| v.as_u64()).unwrap_or(0);
        ln.fails = d.get("fails").and_then(|v| v.as_u64()).unwrap_or(0);
        ln.last_ok = d.get("last_ok").and_then(|v| v.as_f64()).unwrap_or(0.0);
        ln
    }
}

/// Arm (or extend) a lane's park timer.
///
/// `parked_at` opens a new recovery episode only on the warm -> parked
/// transition. A lane that is already parked and fails again (which happens
/// when the pool is empty and the fallback dials parked lanes) extends
/// `parked_until` but must not push its recovery probe further into the
/// future, or a busy dead lane would never be re-tested.
fn arm_park(ln: &mut Lane, reason: u8, rt: &Runtime, burn: bool) {
    let was_warm = ln.parked_until < now();
    // A rotating lane gets a new exit IP on the next connection, so parking
    // it for the full cooldown would throw away a working gateway. Give it a
    // short breath instead.
    let cooldown = if ln.rotating { rt.rotating_cooldown } else { rt.cooldown };
    ln.parked_until = now() + cooldown;
    ln.park_reason = reason;
    if was_warm {
        ln.parked_at = now();
        logline(&format!(
            "lane {} parked for {:.0}s (burn={}, consec_fails={}, reason={})",
            display_addr(&ln.addr),
            cooldown,
            burn,
            ln.consec_fails,
            if reason == PARK_BREAKS { "stream-breaks" } else { "blocked" }
        ));
    }
}

#[derive(Default)]
pub struct Pool {
    pub lanes: HashMap<String, Lane>,
    pub candidates: VecDeque<String>,
    pub candidates_set: HashSet<String>,
    pub priority: VecDeque<String>,
    pub priority_set: HashSet<String>,
    pub tried: HashMap<String, f64>,
    pub sources_ok: usize,
    pub last_fetch: f64,
}

impl Pool {
    pub fn queue_len(&self) -> usize {
        self.candidates.len() + self.priority.len()
    }

    pub fn contains_key(&self, key: &str) -> bool {
        self.lanes.contains_key(key) || self.candidates_set.contains(key) || self.priority_set.contains(key)
    }

    pub fn push_candidate(&mut self, key: String, priority: bool, max_candidates: usize) {
        if priority {
            if self.priority_set.insert(key.clone()) {
                self.priority.push_back(key);
            }
        } else if !self.candidates_set.contains(&key) && self.candidates.len() < max_candidates {
            self.candidates_set.insert(key.clone());
            self.candidates.push_back(key);
        }
    }

    pub fn pop_candidate(&mut self) -> Option<String> {
        while let Some(k) = self.candidates.pop_front() {
            self.candidates_set.remove(&k);
            return Some(k);
        }
        None
    }

    pub fn pop_priority(&mut self) -> Option<String> {
        while let Some(k) = self.priority.pop_front() {
            self.priority_set.remove(&k);
            return Some(k);
        }
        None
    }

    pub fn warm_keys(&self) -> Vec<String> {
        let t = now();
        let mut v: Vec<(f64, f64, String)> = self
            .lanes
            .iter()
            .filter(|(_, ln)| ln.parked_until < t && ln.score > 0.05)
            .map(|(k, ln)| (if ln.lat_ms > 0.0 { ln.lat_ms } else { 8000.0 }, -ln.score, k.clone()))
            .collect();
        v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal)
            .then(a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal)));
        v.into_iter().map(|x| x.2).collect()
    }

    pub fn available_keys(&self, rt: &Runtime, tier: Option<&str>) -> Vec<String> {
        let avail: Vec<String> = self
            .warm_keys()
            .into_iter()
            .filter(|k| {
                self.lanes
                    .get(k)
                    .map(|ln| ln.inflight < rt.max_inflight.max(1))
                    .unwrap_or(false)
            })
            .collect();
        if let Some(t) = tier {
            let tiered: Vec<String> = avail
                .iter()
                .filter(|k| self.lanes.get(*k).map(|ln| ln.tier() == t).unwrap_or(false))
                .cloned()
                .collect();
            if !tiered.is_empty() {
                return tiered;
            }
        }
        avail
    }

    pub fn parked_keys(&self) -> Vec<String> {
        let t = now();
        let mut v: Vec<(f64, String)> = self
            .lanes
            .iter()
            .filter(|(_, ln)| ln.parked_until >= t)
            .map(|(k, ln)| (ln.parked_until, k.clone()))
            .collect();
        v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
        v.into_iter().map(|x| x.1).collect()
    }

    pub fn subnet_counts(&self) -> HashMap<String, usize> {
        let mut counts: HashMap<String, usize> = HashMap::new();
        for ln in self.lanes.values() {
            if ln.addr.is_empty() {
                continue;
            }
            *counts.entry(ln.subnet()).or_insert(0) += 1;
        }
        counts
    }

    pub fn subnet_full(&self, subnet: &str, rt: &Runtime) -> bool {
        if rt.max_per_subnet == 0 || subnet == "direct" {
            return false;
        }
        self.subnet_counts().get(subnet).copied().unwrap_or(0) >= rt.max_per_subnet
    }

    pub fn mark_ok(&mut self, key: &str, lat_ms: f64) {
        if let Some(ln) = self.lanes.get_mut(key) {
            // A lane parked for breaking streams stays out: the success that
            // gets here is the commit of the very stream that is about to die
            // (or of a sibling on the same lane), so it proves nothing. Every
            // other park means "this IP was refused once" — an answer proves it
            // works again, so it returns to the pool at once.
            if ln.parked_until > now() && ln.park_reason != PARK_BREAKS {
                logline(&format!(
                    "lane {} unparked by success",
                    display_addr(&ln.addr)
                ));
                ln.parked_until = 0.0;
                ln.parked_at = 0.0;
                ln.park_reason = PARK_NONE;
            }
            ln.ok += 1;
            ln.last_ok = now();
            ln.lat_ms = lat_ms;
            ln.score = (ln.score * 0.6 + 0.4).min(1.0);
            ln.consec_fails = 0;
            ln.probe_tries = 0;
        }
    }

    pub fn mark_alive(&mut self, key: &str, lat_ms: f64) {
        if let Some(ln) = self.lanes.get_mut(key) {
            ln.last_probe = now();
            ln.lat_ms = lat_ms;
            ln.score = (ln.score * 0.8 + 0.1).min(1.0);
            ln.consec_fails = 0;
        }
    }

    pub fn mark_fail(&mut self, key: &str, burn: bool, rt: &Runtime) {
        if let Some(ln) = self.lanes.get_mut(key) {
            ln.fails += 1;
            ln.consec_fails += 1;
            ln.score = (ln.score * 0.4).max(0.0);
            if burn || ln.consec_fails >= 3 {
                arm_park(ln, PARK_BLOCKED, rt, burn);
            }
        }
    }

    /// A committed stream died before finish_reason/[DONE]. Punishes the score
    /// like any failure and counts consecutive breaks: a proxy that cuts every
    /// answer commits first (mark_ok) and only then dies, so without this
    /// counter it would never leave rotation and, once the healthy lanes are
    /// parked, would serve every request as a truncated answer.
    pub fn mark_stream_break(&mut self, key: &str, rt: &Runtime) {
        let breaks = match self.lanes.get_mut(key) {
            Some(ln) => {
                ln.fails += 1;
                ln.consec_fails += 1;
                ln.score = (ln.score * 0.4).max(0.0);
                ln.breaks += 1;
                ln.breaks
            }
            None => return,
        };
        if breaks >= STREAM_BREAK_LIMIT {
            if let Some(ln) = self.lanes.get_mut(key) {
                ln.breaks = 0;
                arm_park(ln, PARK_BREAKS, rt, true);
            }
        }
    }

    /// A committed stream reached the end of the answer — the lane keeps its
    /// place, so the consecutive-break counter resets.
    pub fn mark_stream_ok(&mut self, key: &str) {
        if let Some(ln) = self.lanes.get_mut(key) {
            ln.breaks = 0;
        }
    }

    /// Put a parked lane back into rotation: the recovery probe answered with a
    /// real completion, which overrides any park reason.
    pub fn unpark(&mut self, key: &str) {
        if let Some(ln) = self.lanes.get_mut(key) {
            ln.parked_until = 0.0;
            ln.parked_at = 0.0;
            ln.park_reason = PARK_NONE;
            ln.breaks = 0;
        }
    }

    pub fn stats(&self, rt: &Runtime) -> Value {
        let warm = self.warm_keys();
        let mut tiers = json!({"fast": 0, "medium": 0, "slow": 0});
        let mut best: Option<f64> = None;
        for k in &warm {
            if let Some(ln) = self.lanes.get(k) {
                let t = ln.tier();
                if let Some(o) = tiers.get_mut(t) {
                    *o = json!(o.as_u64().unwrap_or(0) + 1);
                }
                if best.is_none() && ln.lat_ms > 0.0 {
                    best = Some(ln.lat_ms);
                }
            }
        }
        let subs: HashSet<String> = self
            .lanes
            .values()
            .filter(|ln| !ln.addr.is_empty())
            .map(|ln| ln.subnet())
            .collect();
        let inflight: u64 = self.lanes.values().map(|ln| ln.inflight as u64).sum();
        // "warm" counts lanes that are reachable; "proven" counts the ones that
        // have actually served a completion. With `stage2_probe` off the two
        // differ on purpose: a lane promoted by the screen check is a
        // candidate, not a guarantee.
        let proven = self.lanes.values().filter(|ln| ln.ok > 0).count();
        json!({
            "warm": warm.len(),
            "proven": proven,
            "parked": self.parked_keys().len(),
            "queue": self.queue_len(),
            "sources_ok": self.sources_ok,
            "best_latency_ms": best,
            "subnets": subs.len(),
            "capacity": warm.len() * rt.max_inflight.max(1) as usize,
            "inflight": inflight,
            "tiers": tiers,
        })
    }
}

// ── burn memory ────────────────────────────────────────────────────
#[derive(Default)]
pub struct BurnState {
    pub ips: HashMap<String, Value>,
    pub blocked_intake: u64,
    pub blocked_probe: u64,
    pub recorded: u64,
    pub expired: u64,
}

// ── quota tracking ─────────────────────────────────────────────────
pub struct Quota {
    pub exhausted: bool,
    pub backoff_sec: f64,
    pub backoff_until: f64,
    pub direct_429s: u32,
    pub window: Vec<(f64, String)>,
}

impl Default for Quota {
    fn default() -> Self {
        Quota { exhausted: false, backoff_sec: 90.0, backoff_until: 0.0, direct_429s: 0, window: Vec::new() }
    }
}

// ── adaptive probe concurrency ─────────────────────────────────────
pub struct Adapt {
    pub current: usize,
    pub min: usize,
    pub last_change: f64,
    pub last_reason: String,
    pub err_ratio: f64,
}

impl Default for Adapt {
    fn default() -> Self {
        Adapt { current: 20, min: 5, last_change: 0.0, last_reason: "init".into(), err_ratio: 0.0 }
    }
}

// ── stats + request log ────────────────────────────────────────────
#[derive(Default)]
pub struct Stats {
    pub requests: AtomicU64,
    pub failovers: AtomicU64,
    pub lane_failures: AtomicU64,
    pub probes_ok: AtomicU64,
    pub probes_burned: AtomicU64,
    pub candidates_tested: AtomicU64,
    pub streams: AtomicU64,
    pub upstream_429s: AtomicU64,
}

pub struct ReqRec {
    pub ts: f64,
    pub lane: String,
    pub status: u16,
    pub ms: f64,
    pub stream: bool,
}

// ── models cache (see models.rs) ───────────────────────────────────
pub struct ModelsCache {
    pub updated: f64,
    pub status: u16,
    pub body: bytes::Bytes,
    pub content_type: String,
    pub retry_at: f64,
    pub fallback: Option<String>,
    pub fallback_at: f64,
}

impl Default for ModelsCache {
    fn default() -> Self {
        ModelsCache {
            updated: 0.0,
            status: 503,
            body: bytes::Bytes::new(),
            content_type: "application/json".into(),
            retry_at: 0.0,
            fallback: None,
            fallback_at: 0.0,
        }
    }
}

// ── application state ──────────────────────────────────────────────
pub struct AppState {
    pub settings: RwLock<Settings>,
    pub paths: Paths,
    pub pool: Mutex<Pool>,
    pub burned: Mutex<BurnState>,
    pub stats: Stats,
    pub quota: Mutex<Quota>,
    pub adapt: Mutex<Adapt>,
    pub reqlog: Mutex<VecDeque<ReqRec>>,
    pub probe_err: Mutex<HashMap<String, String>>,
    pub models: tokio::sync::Mutex<ModelsCache>,
    pub cursor: AtomicUsize,
    pub key_idx: AtomicUsize,
    pub start: f64,
    pub client: reqwest::Client,
}

impl AppState {
    pub fn new(settings: Settings, paths: Paths) -> Arc<AppState> {
        let client = reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(10))
            .build()
            .expect("http client");
        Arc::new(AppState {
            settings: RwLock::new(settings),
            paths,
            pool: Mutex::new(Pool::default()),
            burned: Mutex::new(BurnState::default()),
            stats: Stats::default(),
            quota: Mutex::new(Quota::default()),
            adapt: Mutex::new(Adapt::default()),
            reqlog: Mutex::new(VecDeque::with_capacity(REQ_LOG_MAX)),
            probe_err: Mutex::new(HashMap::new()),
            models: tokio::sync::Mutex::new(ModelsCache::default()),
            cursor: AtomicUsize::new(0),
            key_idx: AtomicUsize::new(0),
            start: now(),
            client,
        })
    }

    pub fn rt(&self) -> Runtime {
        self.settings.read().unwrap().rt()
    }

    pub fn next_key(&self, rt: &Runtime) -> String {
        let keys = &rt.api_keys;
        if keys.is_empty() {
            return "public".into();
        }
        let i = self.key_idx.fetch_add(1, Ordering::Relaxed);
        keys[i % keys.len()].clone()
    }

    // ── burn memory ────────────────────────────────────────────────
    pub fn is_burned(&self, addr_or_ip: &str) -> bool {
        let rt = self.rt();
        if !rt.burn_memory || addr_or_ip.is_empty() {
            return false;
        }
        let ip = ip_of(addr_or_ip);
        if ip.is_empty() || ip == "127.0.0.1" || ip == "localhost" {
            return false;
        }
        let mut b = self.burned.lock().unwrap();
        let Some(rec) = b.ips.get(&ip) else { return false };
        let at = rec.get("at").and_then(|v| v.as_f64()).unwrap_or(0.0);
        if now() - at > rt.ttl {
            b.ips.remove(&ip);
            b.expired += 1;
            return false;
        }
        true
    }

    pub fn mark_burned(&self, addr: &str, reason: &str) {
        let rt = self.rt();
        if !rt.burn_memory || addr.is_empty() {
            return;
        }
        // A rotating gateway is not an IP: burning it would block the whole
        // account, and its next connection has a different exit IP anyway.
        if is_rotating_addr(addr) {
            return;
        }
        let ip = ip_of(addr);
        if ip.is_empty() || ip == "127.0.0.1" || ip == "localhost" {
            return;
        }
        let mut b = self.burned.lock().unwrap();
        let t = now();
        if let Some(rec) = b.ips.get_mut(&ip) {
            let hits = rec.get("hits").and_then(|v| v.as_u64()).unwrap_or(1) + 1;
            *rec = json!({"at": t, "reason": reason, "hits": hits});
        } else {
            b.ips.insert(ip, json!({"at": t, "reason": reason, "hits": 1}));
            b.recorded += 1;
        }
    }

    pub fn prune_burned(&self) {
        let rt = self.rt();
        let mut b = self.burned.lock().unwrap();
        let dead: Vec<String> = b
            .ips
            .iter()
            .filter(|(_, r)| now() - r.get("at").and_then(|v| v.as_f64()).unwrap_or(0.0) > rt.ttl)
            .map(|(k, _)| k.clone())
            .collect();
        let n = dead.len();
        for ip in dead {
            b.ips.remove(&ip);
        }
        b.expired += n as u64;
    }

    pub fn save_burned(&self) {
        let rt = self.rt();
        if !rt.burn_memory {
            return;
        }
        let b = self.burned.lock().unwrap();
        let body = json!({"saved": now(), "ips": b.ips});
        if let Ok(txt) = serde_json::to_string(&body) {
            let _ = crate::config::write_private(&self.paths.burned, &txt);
        }
    }

    pub fn load_burned(&self) {
        let rt = self.rt();
        if !rt.burn_memory {
            return;
        }
        let Ok(txt) = std::fs::read_to_string(&self.paths.burned) else { return };
        let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&txt) else { return };
        let Some(Value::Object(ips)) = obj.get("ips") else { return };
        let mut b = self.burned.lock().unwrap();
        let mut loaded = 0;
        for (ip, rec) in ips {
            if !rec.is_object() {
                continue;
            }
            let at = rec.get("at").and_then(|v| v.as_f64()).unwrap_or(0.0);
            if now() - at > rt.ttl {
                continue;
            }
            b.ips.insert(ip.clone(), rec.clone());
            loaded += 1;
        }
        if loaded > 0 {
            logline(&format!("burn memory: {loaded} known-spent IPs loaded (ttl={}s)", rt.ttl as u64));
        }
    }

    // ── lanes persistence ──────────────────────────────────────────
    pub fn save_lanes(&self) {
        let rt = self.rt();
        if !rt.persist {
            return;
        }
        let pool = self.pool.lock().unwrap();
        let keep: Vec<Value> = pool
            .lanes
            .values()
            .filter(|ln| !ln.addr.is_empty() && ln.score > 0.25)
            .map(|ln| ln.to_dict())
            .collect();
        let body = json!({"saved": now(), "lanes": keep});
        if let Ok(txt) = serde_json::to_string(&body) {
            let _ = crate::config::write_private(&self.paths.lanes, &txt);
        }
    }

    pub fn load_lanes(&self) -> usize {
        let rt = self.rt();
        if !rt.persist {
            return 0;
        }
        let Ok(txt) = std::fs::read_to_string(&self.paths.lanes) else { return 0 };
        let Ok(body) = serde_json::from_str::<Value>(&txt) else { return 0 };
        let age = now() - body.get("saved").and_then(|v| v.as_f64()).unwrap_or(0.0);
        if age > 86400.0 {
            logline(&format!("lanes.json is {:.1}h old — ignoring stale egress state", age / 3600.0));
            return 0;
        }
        let mut pool = self.pool.lock().unwrap();
        let mut n = 0;
        for d in body.get("lanes").and_then(|v| v.as_array()).cloned().unwrap_or_default() {
            let ln = Lane::from_dict(&d);
            if ln.addr.is_empty() || ln.score <= 0.25 {
                continue;
            }
            if self.is_burned(&ln.addr) {
                self.burned.lock().unwrap().blocked_intake += 1;
                continue;
            }
            pool.push_candidate(ln.key(), true, rt.max_candidates);
            n += 1;
        }
        if n > 0 {
            logline(&format!("restored {n} remembered lanes into the priority queue ({age:.0}s old)"));
        }
        n
    }

    // ── request bookkeeping ────────────────────────────────────────
    pub fn record_request(&self, lane: &str, status: u16, ms: f64, stream: bool) {
        let mut log = self.reqlog.lock().unwrap();
        if log.len() >= REQ_LOG_MAX {
            log.pop_front();
        }
        log.push_back(ReqRec { ts: now(), lane: lane.to_string(), status, ms, stream });
    }

    pub fn metrics_window(&self, seconds: f64) -> Value {
        let cutoff = now() - seconds;
        let log = self.reqlog.lock().unwrap();
        let rows: Vec<&ReqRec> = log.iter().filter(|r| r.ts >= cutoff).collect();
        let mut lat: Vec<f64> = rows.iter().filter(|r| r.status == 200).map(|r| r.ms).collect();
        lat.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let ok = rows.iter().filter(|r| r.status == 200).count();
        let n = rows.len();
        let pct = |q: f64| -> f64 {
            if lat.is_empty() {
                return 0.0;
            }
            let idx = ((q * (lat.len() - 1) as f64).round() as usize).min(lat.len() - 1);
            lat[idx]
        };
        json!({
            "window_sec": seconds,
            "requests": n,
            "ok": ok,
            "errors": n - ok,
            "success_rate": if n > 0 { Some(((ok as f64 / n as f64) * 10000.0).round() / 10000.0) } else { None },
            "rpm": ((n as f64) / (seconds / 60.0) * 100.0).round() / 100.0,
            "p50_ms": pct(0.50).round(),
            "p95_ms": pct(0.95).round(),
            "p99_ms": pct(0.99).round(),
        })
    }

    // ── quota tracking ─────────────────────────────────────────────
    pub fn note_quota_ok(&self) {
        let mut q = self.quota.lock().unwrap();
        if q.exhausted {
            q.backoff_sec = (q.backoff_sec / 2.0).max(30.0);
            if q.backoff_sec == 30.0 {
                logline("upstream quota recovered (200) — probing resumed");
                q.exhausted = false;
                q.backoff_until = 0.0;
            } else {
                q.backoff_until = q.backoff_until.min(now() + q.backoff_sec);
            }
        }
        if !q.exhausted {
            q.backoff_until = 0.0;
        }
    }

    pub fn note_upstream_429(&self) {
        let mut q = self.quota.lock().unwrap();
        q.exhausted = true;
        q.backoff_sec = (q.backoff_sec * 2.0).min(300.0);
        q.backoff_until = now() + q.backoff_sec;
        self.stats.upstream_429s.fetch_add(1, Ordering::Relaxed);
    }

    pub fn note_lane_429(&self, addr: &str) {
        self.mark_burned(addr, "relay-429");
        let t = now();
        let recent_ok = {
            let log = self.reqlog.lock().unwrap();
            log.iter().any(|r| r.ts >= t - QUOTA_WINDOW_SEC && r.status == 200)
        };
        let mut q = self.quota.lock().unwrap();
        q.window.push((t, addr.to_string()));
        q.window.retain(|(ts, _)| t - ts <= QUOTA_WINDOW_SEC);
        if addr.is_empty() {
            q.direct_429s += 1;
            if q.direct_429s >= QUOTA_LANE_THRESHOLD as u32 {
                q.direct_429s = 0;
                drop(q);
                self.note_upstream_429();
            }
            return;
        }
        let distinct_count = {
            let distinct: HashSet<&String> = q.window.iter().map(|(_, a)| a).collect();
            distinct.len()
        };
        if distinct_count >= QUOTA_LANE_THRESHOLD {
            if recent_ok {
                return;
            }
            q.window.clear();
            drop(q);
            logline(&format!(
                "quota: {distinct_count} distinct egress IPs 429'd within {}s with zero successes — systemic, not per-proxy",
                QUOTA_WINDOW_SEC as u64
            ));
            self.note_upstream_429();
        }
    }

    pub fn quota_paused(&self) -> bool {
        let q = self.quota.lock().unwrap();
        q.exhausted && now() < q.backoff_until
    }

    // ── lane selection (pin-and-drain, ported from _pick_lane) ─────
    pub fn pick_lane(&self, keys: &[String]) -> Option<String> {
        if keys.is_empty() {
            return None;
        }
        let pin_count = self.rt().pin_count;
        let pool = self.pool.lock().unwrap();
        let proven: Vec<&String> = keys
            .iter()
            .filter(|k| pool.lanes.get(*k).map(|ln| ln.ok > 0).unwrap_or(false))
            .collect();
        let has_proven = !proven.is_empty();
        let mut ranked: Vec<&String> = if has_proven {
            proven
        } else {
            keys.iter().take(3).collect()
        };
        if ranked.is_empty() {
            ranked = keys.iter().collect();
        }
        if has_proven {
            ranked.sort_by(|a, b| {
                let la = pool.lanes.get(*a).unwrap();
                let lb = pool.lanes.get(*b).unwrap();
                lb.score
                    .partial_cmp(&la.score)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then(
                        (if la.lat_ms > 0.0 { la.lat_ms } else { 8000.0 })
                            .partial_cmp(&(if lb.lat_ms > 0.0 { lb.lat_ms } else { 8000.0 }))
                            .unwrap_or(std::cmp::Ordering::Equal),
                    )
            });
            ranked.truncate(pin_count.max(1));
        }
        let i = self.cursor.fetch_add(1, Ordering::Relaxed);
        Some(ranked[i % ranked.len()].clone())
    }

    /// Per-lane HTTP client, built once per lane (proxy, TLS verified).
    /// Non-stream attempts are wrapped in a 15s timeout by the caller; the
    /// read timeout here matches the Python 120s streaming window.
    pub fn lane_client(&self, pool: &mut Pool, key: &str) -> Result<reqwest::Client, String> {
        if let Some(ln) = pool.lanes.get_mut(key) {
            if let Some(c) = &ln.cached_client {
                return Ok((**c).clone());
            }
            let proxy = ln.proxy_url();
            let mut b = reqwest::Client::builder()
                .no_proxy()
                .read_timeout(Duration::from_secs(120))
                // A dead/black-holed proxy must be abandoned fast, not after
                // 12s: eight attempts at 12s is ~96s of a client waiting.
                // Good proxies connect well under a second.
                .connect_timeout(Duration::from_secs(1));
            if let Some(p) = &proxy {
                b = b.proxy(reqwest::Proxy::all(p).map_err(|e| e.to_string())?);
            }
            if ln.rotating {
                // A kept-alive connection would keep the same exit IP, which
                // defeats the rotation: close every connection so the next
                // request lands on a new one.
                b = b.pool_max_idle_per_host(0);
            }
            let c = b.build().map_err(|e| e.to_string())?;
            ln.cached_client = Some(Arc::new(c.clone()));
            Ok(c)
        } else {
            Err("lane gone".into())
        }
    }

    pub fn pool_stats(&self) -> Value {
        let rt = self.rt();
        let pool = self.pool.lock().unwrap();
        pool.stats(&rt)
    }

    pub fn save_settings(&self) {
        let s = self.settings.read().unwrap().clone();
        if let Err(e) = s.save(&self.paths.settings) {
            logline(&format!("could not save settings: {e}"));
        }
    }
}

/// A small Stream adapter over the bytes channel used for relayed responses.
pub struct ChannelStream {
    pub rx: tokio::sync::mpsc::Receiver<Result<bytes::Bytes, std::io::Error>>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rotating_gateways_are_recognised_and_rest_briefly() {
        assert!(is_rotating_addr("user-rotate:pass@p.webshare.io:80"));
        assert!(is_rotating_addr("user-us-rotate:pass@p.webshare.io:80"));
        assert!(!is_rotating_addr("user:pass@1.2.3.4:8080"));
        assert!(!is_rotating_addr("1.2.3.4:1080"));

        let mut ln = Lane::new("user-rotate:pass@p.webshare.io:80", "http");
        assert!(ln.rotating, "a rotating gateway must be flagged");
        let rt = crate::config::Settings::default().rt();
        ln.consec_fails = 3;
        let mut pool = Pool::default();
        pool.lanes.insert(ln.key(), ln);
        pool.mark_fail(&pool.lanes.keys().next().unwrap().clone(), true, &rt);
        let parked = pool.lanes.values().next().unwrap().parked_until;
        assert!(
            parked - now() <= 6.0,
            "a rotating lane must rest briefly, not for an hour (parked {parked})"
        );
    }

    #[test]
    fn redacts_proxy_credentials_but_keeps_host() {
        assert_eq!(
            redact_addr("bob:hunter2@10.9.8.7:3128"),
            "bob:***@10.9.8.7:3128"
        );
        assert_eq!(redact_addr("10.9.8.7:3128"), "10.9.8.7:3128");
        assert_eq!(redact_addr(""), "");
        // the log/display form drops credentials entirely
        assert_eq!(display_addr("bob:hunter2@10.9.8.7:3128"), "10.9.8.7:3128");
    }

    #[test]
    fn constant_time_compare_is_exact() {
        assert!(ct_eq("Bearer rly_abc", "Bearer rly_abc"));
        assert!(!ct_eq("Bearer rly_abc", "Bearer rly_abd"));
        assert!(!ct_eq("Bearer rly_abc", "Bearer rly_abc "));
    }

    #[test]
    fn credentialed_proxy_addresses_are_valid() {
        // webshare hands out `user:pass@ip:port`; this used to be dropped on
        // intake, so webshare lanes could never enter the pool at all.
        assert!(valid_addr("1.2.3.4:8080"));
        assert!(valid_addr("bob:hunter2@1.2.3.4:8080"));
        assert!(!valid_addr("bob:hunter2@1.2.3.4:0"));
        assert!(!valid_addr("bob:hunter2@1.2.3.4"));
        assert!(!valid_addr("bob:hunter2@1.2.3:8080"));
        assert!(!valid_addr("bob:hunter2@1.2.3.999:8080"));
        // DNS hostnames are legitimate proxies: the webshare rotating gateway
        // is `user-rotate:pass@p.webshare.io:80`, and a dotted-quad validator
        // silently dropped it, making webshare_rotating a dead feature.
        assert!(valid_addr("user-rotate:pass@p.webshare.io:80"));
        assert!(valid_addr("proxy.example.com:3128"));
        assert!(!valid_addr("not a host:8080"));
        assert!(!valid_addr("-bad-.example.com:8080"));
        assert!(!valid_addr("example..com:8080"));
        assert_eq!(key_subnet("http://user-rotate:pass@p.webshare.io:80"), "p.webshare.io");
        // subnet grouping and IP extraction keep working with credentials
        assert_eq!(key_subnet("http://bob:hunter2@1.2.3.4:8080"), "1.2.3");
        assert_eq!(ip_of("bob:hunter2@1.2.3.4:8080"), "1.2.3.4");
    }
}

impl futures_util::Stream for ChannelStream {
    type Item = Result<bytes::Bytes, std::io::Error>;
    fn poll_next(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Option<Self::Item>> {
        self.rx.poll_recv(cx)
    }
}
