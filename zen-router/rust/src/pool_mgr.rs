//! The supplier: proxy feeds, two-stage probing, churn, recovery,
//! revalidation and the pool manager loop. Ported from the Python relay's
//! `_fetch_sources` / `_probe_candidate` / `_churn_batch` / `pool_manager`.
use crate::config::{now, Runtime};
use crate::relay::{is_quota_429, looks_like_completion, upstream_headers};
use crate::state::{display_addr, key_subnet, logline, valid_addr, AppState, Lane};
use bytes::Bytes;
use futures_util::stream::StreamExt;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const SOURCE_UA: &str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36";

pub const TEXT_SOURCES: &[(&str, &str)] = &[
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
    // extra verified feeds (fresh raw lists, all protocols)
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
];

pub const GEONODE: &str = "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps%2Csocks4%2Csocks5";

fn norm_proto(kind: &str, allow_socks: bool) -> Option<String> {
    let mut p = kind.trim().to_lowercase();
    if p.is_empty() {
        p = "http".into();
    }
    if p == "proto" {
        return None; // caller must take the embedded scheme
    }
    if !matches!(p.as_str(), "http" | "https" | "socks5") {
        p = if p.starts_with("socks") { "socks5".into() } else { "http".into() };
    }
    if p.starts_with("socks") && !allow_socks {
        return None;
    }
    Some(p)
}

/// Try to queue one candidate line. Returns (added, blocked).
fn intake(
    st: &AppState,
    pool: &mut crate::state::Pool,
    rt: &Runtime,
    proto: &str,
    addr: &str,
    priority: bool,
) -> (bool, bool) {
    let addr = addr.trim().trim_end_matches('/').to_string();
    let key = format!("{proto}://{addr}");
    if !valid_addr(&addr) {
        return (false, false);
    }
    if pool.contains_key(&key) || pool.tried.contains_key(&key) {
        return (false, false);
    }
    if st.is_burned(&addr) {
        return (false, true);
    }
    pool.push_candidate(key, priority, rt.max_candidates);
    (true, false)
}

pub async fn fetch_sources(st: &AppState) {
    if !st.rt().sources {
        return;
    }
    let t = now();
    {
        let pool = st.pool.lock().unwrap();
        if t - pool.last_fetch < 30.0 {
            return;
        }
    }
    let rt = st.rt();
    logline("sources: scanning proxy lists…");
    let mut ok = 0usize;

    let mut texts: Vec<(String, String)> = Vec::new();
    let futs = TEXT_SOURCES.iter().map(|(url, kind)| {
        let url = url.to_string();
        let kind = kind.to_string();
        async move {
            let r = st
                .client
                .get(&url)
                .header("user-agent", SOURCE_UA)
                .timeout(Duration::from_secs(25))
                .send()
                .await;
            match r {
                Ok(resp) if resp.status().as_u16() == 200 => {
                    Ok((kind, resp.text().await.unwrap_or_default()))
                }
                _ => Err(()),
            }
        }
    });
    for res in futures_util::future::join_all(futs).await {
        if let Ok((kind, text)) = res {
            ok += 1;
            texts.push((kind, text));
        }
    }

    // geonode JSON feed
    let mut geonode_entries: Vec<(String, String)> = Vec::new(); // (proto, addr)
    if let Ok(resp) = st
        .client
        .get(GEONODE)
        .header("user-agent", SOURCE_UA)
        .timeout(Duration::from_secs(25))
        .send()
        .await
    {
        if resp.status().as_u16() == 200 {
            ok += 1;
            let body = resp.text().await.unwrap_or_default();
            if let Ok(d) = serde_json::from_str::<Value>(&body) {
                for e in d.get("data").and_then(|v| v.as_array()).cloned().unwrap_or_default() {
                    let addr = format!(
                        "{}:{}",
                        e.get("ip").and_then(|v| v.as_str()).unwrap_or(""),
                        e.get("port").and_then(|v| v.as_str()).unwrap_or("")
                    );
                    let protos: Vec<String> = e
                        .get("protocols")
                        .and_then(|v| v.as_array())
                        .map(|a| {
                            a.iter()
                                .filter_map(|p| p.as_str())
                                .map(|s| s.to_lowercase())
                                .collect()
                        })
                        .unwrap_or_else(|| vec!["http".into()]);
                    for p in protos {
                        if let Some(np) = norm_proto(&p, rt.allow_socks) {
                            geonode_entries.push((np, addr.clone()));
                        }
                    }
                }
            }
        }
    }

    // webshare free-tier API (single or comma-separated tokens)
    let mut webshare_entries: Vec<String> = Vec::new();
    if !rt.webshare.is_empty() {
        let tokens: Vec<String> = rt
            .webshare
            .split(|c: char| c.is_whitespace() || c == ',' || c == ';')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();
        for tok in tokens {
            if let Ok(resp) = st
                .client
                .get("https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=100")
                .header("authorization", format!("Token {tok}"))
                .timeout(Duration::from_secs(25))
                .send()
                .await
            {
                if resp.status().as_u16() == 200 {
                    ok += 1;
                    let body = resp.text().await.unwrap_or_default();
                    if let Ok(d) = serde_json::from_str::<Value>(&body) {
                        for e in d.get("results").and_then(|v| v.as_array()).cloned().unwrap_or_default() {
                            if !e.get("valid").and_then(|v| v.as_bool()).unwrap_or(false) {
                                continue;
                            }
                            let user = e.get("username").and_then(|v| v.as_str()).unwrap_or("");
                            let pw = e.get("password").and_then(|v| v.as_str()).unwrap_or("");
                            let ip = e.get("proxy_address").and_then(|v| v.as_str()).unwrap_or("");
                            let port = e.get("port");
                            let port = port
                                .and_then(|v| v.as_u64().map(|n| n.to_string()).or_else(|| v.as_str().map(|s| s.to_string())))
                                .unwrap_or_default();
                            if ip.is_empty() || port.is_empty() {
                                continue;
                            }
                            let addr = if !user.is_empty() && !pw.is_empty() {
                                format!("{user}:{pw}@{ip}:{port}")
                            } else {
                                format!("{ip}:{port}")
                            };
                            webshare_entries.push(addr);
                        }
                    }
                }
            }
        }
    }

    // Queue everything under one lock. Sources are drained round-robin so a
    // single huge list cannot starve the other feeds when max_candidates is
    // small; per-source reads are capped to bound memory.
    const PER_SOURCE_CAP: usize = 8_000;
    const ROUND_LINES: usize = 5;
    let mut added = 0usize;
    let mut blocked = 0u64;
    {
        let mut pool = st.pool.lock().unwrap();
        let source_lines: Vec<(&str, Vec<&str>)> = texts
            .iter()
            .map(|(kind, text)| {
                (
                    kind.as_str(),
                    text.lines()
                        .map(str::trim)
                        .filter(|l| !l.is_empty())
                        .take(PER_SOURCE_CAP)
                        .collect(),
                )
            })
            .collect();
        let mut pos = vec![0usize; source_lines.len()];
        let mut scanning = true;
        'fill: while scanning {
            scanning = false;
            for (si, (kind, lines)) in source_lines.iter().enumerate() {
                for _ in 0..ROUND_LINES {
                    if pos[si] >= lines.len() {
                        break;
                    }
                    let line = lines[pos[si]];
                    pos[si] += 1;
                    scanning = true;
                    let (mut proto, mut addr) = ((*kind).to_string(), line.to_string());
                    if let Some((p, a)) = line.split_once("://") {
                        proto = p.to_string();
                        addr = a.to_string();
                    }
                    let Some(np) = norm_proto(&proto, rt.allow_socks) else { continue };
                    let (a, b) = intake(st, &mut pool, &rt, &np, &addr, false);
                    if a {
                        added += 1;
                    }
                    if b {
                        blocked += 1;
                    }
                    if pool.queue_len() >= rt.max_candidates {
                        break 'fill;
                    }
                }
            }
        }
        for (proto, addr) in &geonode_entries {
            let (a, b) = intake(st, &mut pool, &rt, proto, addr, false);
            if a {
                added += 1;
            }
            if b {
                blocked += 1;
            }
            if pool.queue_len() >= rt.max_candidates {
                break;
            }
        }
        for addr in &webshare_entries {
            let (a, b) = intake(st, &mut pool, &rt, "http", addr, true);
            if a {
                added += 1;
            }
            if b {
                blocked += 1;
            }
        }
        pool.last_fetch = now();
        pool.sources_ok = ok;
    }
    if blocked > 0 {
        st.burned.lock().unwrap().blocked_intake += blocked;
    }
    if added > 0 {
        let q = st.pool.lock().unwrap().queue_len();
        logline(&format!("sources: +{added} candidates (queue={q}, sources_ok={ok})"));
    }
}

// ── probing ────────────────────────────────────────────────────────
fn agent_probe_payload(model: &str) -> Value {
    let mut system = String::from("You are a coding assistant. ");
    for _ in 0..200 {
        system.push_str("context ");
    }
    json!({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Reply exactly: OK"},
        ],
        "max_completion_tokens": 4,
        "stream": true,
        "tools": [
            {"type": "function", "function": {
                "name": "read", "description": "Read the contents of a file or directory. Supports text files, images, and PDFs.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"]}
            }},
            {"type": "function", "function": {
                "name": "write", "description": "Write content to a file.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}
            }},
            {"type": "function", "function": {
                "name": "bash", "description": "Run a shell command.",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
            }},
        ],
    })
}

fn proxy_scheme(proto: &str) -> &'static str {
    if proto.starts_with("socks") {
        "socks5" // httpx/reqwest have no socks4 transport; socks* is dialled as socks5
    } else {
        "http"
    }
}

async fn screen(proxy_url: &str, rt: &Runtime) -> bool {
    let Ok(proxy) = reqwest::Proxy::all(proxy_url) else { return false };
    let Ok(c) = reqwest::Client::builder()
        .danger_accept_invalid_certs(true)
        .no_proxy()
        .proxy(proxy)
        .timeout(Duration::from_secs(4))
        .build()
    else {
        return false;
    };
    match c
        .get(rt.screen_url.as_str())
        .header("authorization", "Bearer public")
        .header("user-agent", rt.ua.as_str())
        .send()
        .await
    {
        Ok(r) => matches!(r.status().as_u16(), 200 | 401),
        Err(_) => false,
    }
}

/// Stage 2: a real 1-token agent-shaped completion through the proxy.
async fn stage2(
    st: &AppState,
    rt: &Runtime,
    proto: &str,
    addr: &str,
) -> Result<(u16, f64, Bytes), &'static str> {
    let proxy_url = format!("{}://{addr}", proxy_scheme(proto));
    let Ok(proxy) = reqwest::Proxy::all(&proxy_url) else {
        return Err("proxy");
    };
    let Ok(client) = reqwest::Client::builder()
        .danger_accept_invalid_certs(true)
        .no_proxy()
        .proxy(proxy)
        .timeout(Duration::from_secs(rt.probe_timeout))
        .build()
    else {
        return Err("proxy");
    };
    let url = format!("{}/chat/completions", rt.upstream_base);
    let t0 = Instant::now();
    let resp = client
        .post(&url)
        .headers(upstream_headers(st, rt, None))
        .json(&agent_probe_payload(&rt.probe_model))
        .send()
        .await
        .map_err(|e| if e.is_connect() { "conn" } else { "proxy" })?;
    let status = resp.status().as_u16();
    let body = resp.bytes().await.unwrap_or_default();
    Ok((status, t0.elapsed().as_secs_f64() * 1000.0, body))
}

pub async fn probe_candidate(st: &AppState, key: &str) -> Option<Lane> {
    let (proto, addr) = key.split_once("://")?;
    let rt = st.rt();
    if st.is_burned(addr) {
        st.burned.lock().unwrap().blocked_probe += 1;
        st.probe_err
            .lock()
            .unwrap()
            .insert(key.to_string(), "burned-known".into());
        return None;
    }
    let proxy_url = format!("{}://{addr}", proxy_scheme(proto));
    if !screen(&proxy_url, &rt).await {
        st.probe_err
            .lock()
            .unwrap()
            .insert(key.to_string(), "proxy".into());
        return None;
    }
    match stage2(st, &rt, proto, addr).await {
        Err(kind) => {
            st.probe_err
                .lock()
                .unwrap()
                .insert(key.to_string(), kind.to_string());
            logline(&format!("prober: lane {} failed: {kind}", display_addr(addr)));
            None
        }
        Ok((status, lat, body)) => {
            if status == 200 && looks_like_completion(&body) {
                let cutoff =
                    (rt.relay_proxy_timeout.min(rt.probe_timeout) as f64) * 1000.0 * 0.9;
                if lat > cutoff {
                    st.probe_err
                        .lock()
                        .unwrap()
                        .insert(key.to_string(), "slow".into());
                    logline(&format!(
                        "prober: {} answered but too slow ({:.0}ms > {:.0}ms) — skipped",
                        display_addr(addr),
                        lat,
                        cutoff
                    ));
                    return None;
                }
                let mut ln = Lane::new(addr, proto);
                // Lane.mark_ok semantics from the Python relay:
                ln.ok = 1;
                ln.last_ok = now();
                ln.lat_ms = lat;
                ln.score = 0.7;
                st.stats.probes_ok.fetch_add(1, Ordering::Relaxed);
                logline(&format!(
                    "prober: +lane {} passed completion probe ({:.0}ms)",
                    display_addr(addr),
                    lat
                ));
                return Some(ln);
            }
            if status == 429 {
                st.probe_err
                    .lock()
                    .unwrap()
                    .insert(key.to_string(), "burned".into());
                st.stats.probes_burned.fetch_add(1, Ordering::Relaxed);
                st.mark_burned(addr, "probe-429");
                logline(&format!(
                    "prober: {} is quota-burned (429) — dropped + blocklisted",
                    display_addr(addr)
                ));
                return None;
            }
            st.probe_err
                .lock()
                .unwrap()
                .insert(key.to_string(), "http".into());
            logline(&format!(
                "prober: {} failed stage 2: HTTP {status}",
                display_addr(addr)
            ));
            None
        }
    }
}

pub async fn churn_batch(st: &AppState) {
    let rt = st.rt();
    {
        let pool = st.pool.lock().unwrap();
        if pool.candidates.is_empty() && pool.priority.is_empty() {
            return;
        }
    }
    let conc = if rt.adaptive {
        st.adapt.lock().unwrap().current
    } else {
        rt.test_concurrency
    };
    let conc = conc.max(1).min(rt.test_concurrency);
    let batch_size = (conc * 8).min(300);
    let mut batch: Vec<String> = Vec::new();

    {
        let mut pool = st.pool.lock().unwrap();
        while batch.len() < batch_size {
            match pool.pop_priority() {
                Some(k) => batch.push(k),
                None => break,
            }
        }
        if batch.len() < batch_size && !pool.candidates.is_empty() {
            let mut seen: HashSet<String> = pool.subnet_counts().keys().cloned().collect();
            let cand: Vec<String> = pool.candidates.drain(..).collect();
            pool.candidates_set.clear();
            let mut deferred: Vec<String> = Vec::new();
            let mut idx = 0usize;
            while idx < cand.len() {
                if batch.len() >= batch_size {
                    break;
                }
                let k = cand[idx].clone();
                idx += 1;
                let sub = key_subnet(&k);
                if rt.max_per_subnet > 0 && pool.subnet_full(&sub, &rt) {
                    pool.tried.insert(k, now());
                    continue;
                }
                if seen.contains(&sub) && deferred.len() < batch_size {
                    deferred.push(k);
                    continue;
                }
                seen.insert(sub);
                batch.push(k);
            }
            let mut leftovers: Vec<String> = cand[idx.min(cand.len())..].to_vec();
            leftovers.extend(deferred);
            for k in leftovers {
                if batch.len() < batch_size {
                    batch.push(k);
                } else {
                    pool.push_candidate(k, false, rt.max_candidates);
                }
            }
        }
    }

    if batch.is_empty() {
        return;
    }
    st.stats
        .candidates_tested
        .fetch_add(batch.len() as u64, Ordering::Relaxed);
    logline(&format!(
        "prober: testing {} candidate proxies (concurrency={conc}{})…",
        batch.len(),
        if rt.adaptive { ", adaptive" } else { "" }
    ));

    // Lanes enter the pool the moment their own probe passes — not when the
    // slowest probe of the batch finishes. A restart (or an empty pool) then
    // recovers in a second or two instead of waiting out the batch.
    let conn_errors = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let completed = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let st_ref: &AppState = st;
    let rt_ref: &Runtime = &rt;
    let ce = Arc::clone(&conn_errors);
    let cp = Arc::clone(&completed);
    let futs = futures_util::stream::iter(batch.iter().cloned())
        .map(|k| async move {
            let r = probe_candidate(st, &k).await;
            (k, r)
        })
        .buffer_unordered(conc)
        .for_each(move |(k, res)| {
            let conn_errors = Arc::clone(&ce);
            let completed = Arc::clone(&cp);
            async move {
                let st = st_ref;
                let rt = rt_ref;
                completed.fetch_add(1, Ordering::Relaxed);
                match res {
                    Some(lane) => {
                        let key = lane.key();
                        let sub = lane.subnet();
                        let warm = lane.warm();
                        let disp = display_addr(&lane.addr);
                        let tier = lane.tier();
                        let lat = lane.lat_ms;
                        let score = lane.score;
                        let mut pool = st.pool.lock().unwrap();
                        let saturated = !lane.addr.is_empty()
                            && rt.max_per_subnet > 0
                            && pool.subnet_full(&sub, rt)
                            && !pool.lanes.contains_key(&key);
                        if saturated {
                            pool.tried.insert(k, now());
                        } else {
                            pool.lanes.insert(key, lane);
                            drop(pool);
                            if warm {
                                logline(&format!(
                                    "churn: promoted new warm lane: {disp} (score={score:.2}, lat={lat:.0}ms, {tier})"
                                ));
                            }
                        }
                    }
                    None => {
                        st.pool.lock().unwrap().tried.insert(k.clone(), now());
                        let err = st.probe_err.lock().unwrap().remove(&k);
                        if err.as_deref() == Some("conn") {
                            conn_errors.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                }
            }
        });
    let timed_out = tokio::time::timeout(Duration::from_secs(90), futs).await.is_err();
    if timed_out {
        let mut pool = st.pool.lock().unwrap();
        let mut abandoned = 0usize;
        for k in batch.iter() {
            if !pool.lanes.contains_key(k) && !pool.tried.contains_key(k) {
                pool.tried.insert(k.clone(), now());
                abandoned += 1;
            }
        }
        drop(pool);
        logline(&format!(
            "churn: batch timed out after 90s; {abandoned} candidates abandoned as tried"
        ));
    }

    adapt_concurrency(
        st,
        conn_errors.load(Ordering::Relaxed),
        completed.load(Ordering::Relaxed).max(1),
        &rt,
    );
    let s = st.pool_stats();
    logline(&format!(
        "churn: batch completed. warm={} parked={} queue={}",
        s["warm"], s["parked"], s["queue"]
    ));
}

fn adapt_concurrency(st: &AppState, conn_errors: usize, completed: usize, rt: &Runtime) {
    if !rt.adaptive || completed == 0 {
        return;
    }
    let ratio = conn_errors as f64 / completed as f64;
    let mut a = st.adapt.lock().unwrap();
    a.err_ratio = (ratio * 1000.0).round() / 1000.0;
    let cur = a.current;
    let (new, reason) = if ratio > 0.4 && cur > a.min {
        (
            (cur / 2).max(a.min),
            format!("connect-error storm {:.0}% — link/NAT saturated", ratio * 100.0),
        )
    } else if ratio < 0.1 && cur < rt.test_concurrency {
        (
            (cur + 1).max((cur as f64 * 1.25) as usize).min(rt.test_concurrency),
            format!("link clean ({:.0}% connect errors) — scaling up", ratio * 100.0),
        )
    } else {
        return;
    };
    if new == cur {
        return;
    }
    a.current = new;
    a.last_change = now();
    a.last_reason = reason.clone();
    logline(&format!("adaptive concurrency: {cur} → {new} ({reason})"));
}

pub async fn recover_parked(st: &AppState) {
    let rt = st.rt();
    let items: Vec<(String, String, String)> = {
        let pool = st.pool.lock().unwrap();
        let t = now();
        pool.parked_keys()
            .into_iter()
            .filter_map(|k| {
                let ln = pool.lanes.get(&k)?;
                if ln.addr.is_empty() {
                    return None;
                }
                let factor = 2f64.powi(ln.probe_tries.min(4) as i32).min(16.0);
                // Recovery is scheduled from the moment the lane was parked:
                // `last_probe` can be recent (a warm-lane health check) and
                // would otherwise push the re-test of a parked lane out by a
                // whole extra recovery interval.
                let since = if ln.parked_at > 0.0 { ln.parked_at } else { ln.last_probe };
                if t - since > rt.recover * factor {
                    Some((k.clone(), ln.addr.clone(), ln.proto.clone()))
                } else {
                    None
                }
            })
            .take(20)
            .collect()
    };
    if items.is_empty() {
        return;
    }
    let conc = (rt.test_concurrency / 4).max(4);
    let futs = futures_util::stream::iter(items.into_iter())
        .map(|(k, addr, proto)| {
            let rt2 = rt.clone();
            async move {
                let r = stage2(st, &rt2, &proto, &addr).await;
                (k, addr, r)
            }
        })
        .buffer_unordered(conc)
        .collect::<Vec<_>>();
    let results = match tokio::time::timeout(Duration::from_secs(60), futs).await {
        Ok(v) => v,
        Err(_) => {
            logline("recover: batch timed out after 60s");
            return;
        }
    };
    for (k, addr, res) in results {
        match res {
            Ok((status, _lat, body)) if status == 429 && is_quota_429(&body, 429) => {
                if let Some(ln) = st.pool.lock().unwrap().lanes.get_mut(&k) {
                    ln.last_probe = now();
                }
                st.note_lane_429(&addr);
            }
            Ok((status, lat, body)) if status == 200 && looks_like_completion(&body) => {
                let mut pool = st.pool.lock().unwrap();
                if let Some(ln) = pool.lanes.get_mut(&k) {
                    ln.last_probe = now();
                    ln.probe_tries = 0;
                }
                pool.mark_ok(&k, lat);
                // mark_ok keeps a stream-breaks park in place (a commit proves
                // nothing for such a lane); a real completion from the probe
                // does prove it, so clear the park explicitly.
                pool.unpark(&k);
                drop(pool);
                st.note_quota_ok();
                logline(&format!("lane recovered: {}", display_addr(&addr)));
            }
            Ok((status, lat, _)) => {
                let mut pool = st.pool.lock().unwrap();
                if let Some(ln) = pool.lanes.get_mut(&k) {
                    ln.last_probe = now();
                    ln.probe_tries += 1;
                }
                let _ = (status, lat);
            }
            Err(_) => {
                if let Some(ln) = st.pool.lock().unwrap().lanes.get_mut(&k) {
                    ln.last_probe = now();
                    ln.probe_tries += 1;
                }
            }
        }
    }
}

pub async fn revalidate_warm(st: &AppState) {
    let rt = st.rt();
    let items: Vec<(String, String, String)> = {
        let pool = st.pool.lock().unwrap();
        let t = now();
        pool.warm_keys()
            .into_iter()
            .filter_map(|k| {
                let ln = pool.lanes.get(&k)?;
                if ln.addr.is_empty() {
                    return None;
                }
                if t - ln.last_ok > 300.0 && t - ln.last_probe > 120.0 {
                    Some((k.clone(), ln.addr.clone(), ln.proto.clone()))
                } else {
                    None
                }
            })
            .take(20)
            .collect()
    };
    if items.is_empty() {
        return;
    }
    let conc = (rt.test_concurrency / 10).max(8);
    let futs = futures_util::stream::iter(items.into_iter())
        .map(|(k, addr, proto)| {
            let rt2 = rt.clone();
            async move {
                let proxy_url = format!("{}://{addr}", proxy_scheme(&proto));
                let result: Result<(u16, f64), ()> = async {
                    let Ok(proxy) = reqwest::Proxy::all(&proxy_url) else { return Err(()) };
                    let Ok(c) = reqwest::Client::builder()
                        .danger_accept_invalid_certs(true)
                        .no_proxy()
                        .proxy(proxy)
                        .connect_timeout(Duration::from_secs(4))
                        .timeout(Duration::from_secs(6))
                        .build()
                    else {
                        return Err(());
                    };
                    let t0 = Instant::now();
                    match c
                        .get(rt2.screen_url.as_str())
                        .headers(upstream_headers(st, &rt2, None))
                        .send()
                        .await
                    {
                        Ok(r) => Ok((r.status().as_u16(), t0.elapsed().as_secs_f64() * 1000.0)),
                        Err(_) => Err(()),
                    }
                }
                .await;
                (k, addr, result)
            }
        })
        .buffer_unordered(conc)
        .collect::<Vec<_>>();
    let results = match tokio::time::timeout(Duration::from_secs(60), futs).await {
        Ok(v) => v,
        Err(_) => {
            logline("revalidate: batch timed out after 60s");
            return;
        }
    };
    for (k, addr, res) in results {
        match res {
            Ok((status, lat)) if matches!(status, 200 | 401 | 403 | 404 | 405) => {
                st.pool.lock().unwrap().mark_alive(&k, lat);
            }
            Ok(_) | Err(()) => {
                st.pool.lock().unwrap().mark_fail(&k, true, &rt);
                if let Some(ln) = st.pool.lock().unwrap().lanes.get_mut(&k) {
                    ln.last_probe = now();
                }
                let _ = &addr;
            }
        }
    }
}

pub fn drop_dead(st: &AppState) {
    let t = now();
    let mut pool = st.pool.lock().unwrap();
    let dead: Vec<String> = pool
        .lanes
        .iter()
        .filter(|(_, ln)| {
            !ln.addr.is_empty() && ln.ok == 0 && ln.probe_tries > 4 && t - ln.last_probe > 1800.0
        })
        .map(|(k, _)| k.clone())
        .collect();
    let burned_lanes: Vec<String> = pool
        .lanes
        .iter()
        .filter(|(_, ln)| !ln.addr.is_empty() && st.is_burned(&ln.addr))
        .map(|(k, _)| k.clone())
        .collect();
    for k in dead {
        pool.lanes.remove(&k);
    }
    for k in &burned_lanes {
        pool.lanes.remove(k);
    }
    if !burned_lanes.is_empty() {
        logline(&format!(
            "pool: retired {} lanes on known-spent IPs",
            burned_lanes.len()
        ));
    }
    let stale: Vec<String> = pool
        .tried
        .iter()
        .filter(|(_, ts)| t - **ts > 3600.0)
        .map(|(k, _)| k.clone())
        .collect();
    for k in stale {
        pool.tried.remove(&k);
    }
}

pub fn trim_pool(st: &AppState) {
    let rt = st.rt();
    let cap = (rt.pool_target * 2).max(rt.pool_target + 20);
    let mut pool = st.pool.lock().unwrap();
    if pool.lanes.len() <= cap {
        return;
    }
    let t = now();
    let mut order: Vec<(String, (u8, u8, i64, i64))> = pool
        .lanes
        .iter()
        .filter(|(_, ln)| !ln.addr.is_empty())
        .map(|(k, ln)| {
            (
                k.clone(),
                (
                    (ln.ok == 0) as u8,
                    (ln.parked_until > t) as u8,
                    (ln.score * 1_000_000.0) as i64,
                    ln.last_ok as i64,
                ),
            )
        })
        .collect();
    order.sort_by_key(|(_, key)| *key);
    let excess = pool.lanes.len().saturating_sub(cap);
    for (k, _) in order.into_iter().take(excess) {
        pool.lanes.remove(&k);
    }
    if excess > 0 {
        logline(&format!(
            "pool trimmed: removed {excess} weakest lanes (cap={cap})"
        ));
    }
}

pub fn ensure_direct_lane(st: &AppState, rt: &Runtime) {
    if rt.direct {
        let mut pool = st.pool.lock().unwrap();
        if !pool.lanes.contains_key("direct://") {
            let mut ln = Lane::new("", "direct");
            ln.score = 1.0;
            pool.lanes.insert("direct://".into(), ln);
            logline("direct lane enabled");
        }
    }
}

pub async fn pool_manager(st: Arc<AppState>) {
    loop {
        {
            let rt = st.rt();
            let warm = st.pool.lock().unwrap().warm_keys().len();
            let paused = st.quota_paused();
            if warm < rt.pool_target {
                if st.pool.lock().unwrap().queue_len() == 0 {
                    fetch_sources(&st).await;
                }
                if !paused {
                    churn_batch(&st).await;
                }
            } else {
                if !paused {
                    recover_parked(&st).await;
                }
                revalidate_warm(&st).await;
                drop_dead(&st);
                trim_pool(&st);
                if st.pool.lock().unwrap().queue_len() == 0 {
                    fetch_sources(&st).await;
                }
            }
            ensure_direct_lane(&st, &rt);
        }
        tokio::time::sleep(Duration::from_secs(6)).await;
    }
}

pub async fn persist_loop(st: Arc<AppState>) {
    loop {
        tokio::time::sleep(Duration::from_secs(30)).await;
        st.save_lanes();
        st.prune_burned();
        st.save_burned();
    }
}
