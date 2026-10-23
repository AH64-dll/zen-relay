//! The relay core: upstream fingerprint, block/quota classification, lane
//! failover, and the peeking streaming path. Ported line-for-line from the
//! Python relay so behavior (and the fix history behind it) carries over.
use crate::config::{now, Runtime, DEFAULT_UA};
use crate::state::{display_addr, logline, AppState, ChannelStream};
use axum::body::Body;
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use bytes::Bytes;
use futures_util::stream::StreamExt;
use serde_json::{json, Value};
use std::pin::Pin;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Semaphore;

pub const PEEK_DEADLINE_SEC: f64 = 1.0;
/// Vision requests need longer before their first frame: a 1s peek would
/// rotate away a healthy lane while the model is still encoding the image.
/// Text keeps the fast 1s dead-lane filter.
pub const IMAGE_PEEK_DEADLINE_SEC: f64 = 6.0;

/// True when the request carries an image part (`image_url` for chat
/// completions, `input_image` for the responses shape).
pub fn payload_has_image(v: &Value) -> bool {
    fn walk(v: &Value) -> bool {
        match v {
            Value::Object(m) => m
                .iter()
                .any(|(k, val)| k == "image_url" || k == "input_image" || walk(val)),
            Value::Array(a) => a.iter().any(walk),
            _ => false,
        }
    }
    walk(v)
}
pub const SCREEN_URL: &str = "https://opencode.ai/zen/v1/models";
const INTERRUPT_FRAME: &[u8] = b"data: {\"error\":{\"message\":\"stream ended before the model finished answering (upstream transport reset)\",\"type\":\"stream_interrupted\"}}\n\n";
// Terminal body when every attempt was rate-limited: the client gets a
// retryable 503 *without* any upstream rate-limit text (fix, not in Python).
const RATE_LIMITED_BODY: &str = "{\"error\":{\"message\":\"every egress lane was rate-limited upstream (temporary, retryable 503) — the pool is rotating, retry shortly\",\"type\":\"zen_lane_rate_limited\"}}";

pub type BoxStream = Pin<Box<dyn futures_util::Stream<Item = Result<Bytes, reqwest::Error>> + Send>>;

// ── OpenCode client fingerprint ────────────────────────────────────
pub fn fp_id(prefix: &str, invert: bool) -> String {
    let ms = (now() * 1000.0) as u64;
    let mut stamp = ms.wrapping_mul(0x1000).wrapping_add(1);
    if invert {
        stamp = !stamp & 0xFFFF_FFFF_FFFF;
    } else {
        stamp &= 0xFFFF_FFFF_FFFF;
    }
    let uuid = uuid::Uuid::new_v4().simple().to_string();
    format!("{}_{:012x}{}", prefix, stamp, &uuid[..14])
}

pub fn fp_headers() -> Vec<(&'static str, String)> {
    let sid = fp_id("ses", true);
    // `x-opencode-directory` used to carry the relay's absolute working
    // directory upstream, leaking the operator's local paths. The free tier's
    // fingerprint gate wants the header present, so send a neutral constant
    // that reveals nothing about this machine.
    vec![
        ("x-opencode-client", "cli".into()),
        ("x-opencode-session", sid.clone()),
        ("x-opencode-session-id", sid.clone()),
        ("x-session-id", sid.clone()),
        ("x-session-affinity", sid.clone()),
        ("x-opencode-request", fp_id("msg", false)),
        ("x-opencode-project", "global".into()),
        ("x-opencode-directory", "/workspace".into()),
    ]
}

pub fn upstream_headers(st: &AppState, rt: &Runtime, custom_key: Option<&str>) -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert("content-type", HeaderValue::from_static("application/json"));
    h.insert(
        "user-agent",
        HeaderValue::from_str(&rt.ua).unwrap_or(HeaderValue::from_static(DEFAULT_UA)),
    );
    for (k, v) in fp_headers() {
        if let Ok(hv) = HeaderValue::from_str(&v) {
            h.insert(k, hv);
        }
    }
    let key = match custom_key {
        Some(k) => k.to_string(),
        None => st.next_key(rt),
    };
    if !key.is_empty() {
        if let Ok(hv) = HeaderValue::from_str(&format!("Bearer {key}")) {
            h.insert("authorization", hv);
        }
    }
    h
}

pub fn build_upstream_headers(st: &AppState, rt: &Runtime, client_ua: Option<&str>) -> HeaderMap {
    let mut h = upstream_headers(st, rt, None);
    if let Some(ua) = client_ua {
        if ua.to_lowercase().contains("opencode") {
            if let Ok(hv) = HeaderValue::from_str(ua) {
                h.insert("user-agent", hv);
            }
        }
    }
    h
}

/// Flatten an error and its causes into one line: the outer message alone
/// ("error sending request for url") hides the part that matters.
pub fn error_chain(err: &dyn std::error::Error) -> String {
    let mut out = err.to_string();
    let mut source = err.source();
    let mut depth = 0;
    while let Some(inner) = source {
        if depth > 4 {
            break;
        }
        out.push_str(" <- ");
        out.push_str(&inner.to_string());
        source = inner.source();
        depth += 1;
    }
    out
}

// ── quota / block classification ───────────────────────────────────
pub fn is_quota_429(body: &[u8], status: u16) -> bool {
    if status != 429 {
        return false;
    }
    if body.is_empty() {
        return true;
    }
    if let Ok(d) = serde_json::from_slice::<Value>(body) {
        let err = d.get("error");
        let msg = err
            .and_then(|e| e.get("message"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_lowercase();
        let et = err
            .and_then(|e| e.get("type"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_lowercase();
        let markers = [
            "rate limit", "quota", "usage limit", "exhausted", "too many requests", "429",
            "limit reached", "ratelimit", "rate_limit",
        ];
        return markers.iter().any(|m| msg.contains(m) || et.contains(m));
    }
    true
}

pub fn block_class(status: u16, body: &[u8]) -> &'static str {
    let text = String::from_utf8_lossy(&body[..body.len().min(400)]).to_lowercase();
    if text.contains("freetier") || text.contains("free tier") {
        return "quota";
    }
    if text.contains("regionerror") || text.contains("not available in your region") {
        return "region";
    }
    if status == 401 {
        return "auth";
    }
    "proxy"
}

pub fn handle_lane_block(st: &AppState, rt: &Runtime, key: &str, status: u16, body: &[u8]) {
    let kind = block_class(status, body);
    let addr = lane_addr(st, key);
    if kind == "quota" || kind == "proxy" {
        st.pool.lock().unwrap().mark_fail(key, true, rt);
        st.mark_burned(&addr, &format!("relay-{status}-{kind}"));
    } else {
        st.pool.lock().unwrap().mark_fail(key, false, rt);
    }
    st.stats.lane_failures.fetch_add(1, Ordering::Relaxed);
    st.stats.failovers.fetch_add(1, Ordering::Relaxed);
    logline(&format!(
        "lane {} blocked upstream (HTTP {status}, {kind}) — rotated out",
        display_addr(&addr)
    ));
}

fn lane_addr(st: &AppState, key: &str) -> String {
    st.pool
        .lock()
        .unwrap()
        .lanes
        .get(key)
        .map(|l| l.addr.clone())
        .unwrap_or_default()
}

// ── SSE helpers (verbatim semantics of the Python versions) ────────
fn trim(b: &[u8]) -> &[u8] {
    let mut s = 0usize;
    let mut e = b.len();
    while s < e && (b[s] == b' ' || b[s] == b'\t' || b[s] == b'\r' || b[s] == b'\n') {
        s += 1;
    }
    while e > s && (b[e - 1] == b' ' || b[e - 1] == b'\t' || b[e - 1] == b'\r' || b[e - 1] == b'\n') {
        e -= 1;
    }
    &b[s..e]
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

pub fn stream_error_kind(chunk: &[u8]) -> Option<&'static str> {
    for raw in chunk.split(|b| *b == b'\n') {
        let mut t = trim(raw);
        if t.starts_with(b"data:") {
            t = trim(&t[5..]);
        }
        if !t.starts_with(b"{") {
            continue;
        }
        let Ok(d) = serde_json::from_slice::<Value>(t) else { continue };
        let Some(err) = d.get("error") else { continue };
        let low = serde_json::to_string(err).unwrap_or_default().to_lowercase();
        if low.contains("freetier") || low.contains("free tier") {
            return Some("quota");
        }
        if low.contains("region") {
            return Some("region");
        }
        return Some("other");
    }
    None
}

pub fn frame_kind(chunk: &[u8]) -> &'static str {
    let mut best = "keepalive";
    let mut saw_garbage = false;
    for raw in chunk.split(|b| *b == b'\n') {
        let mut t = trim(raw);
        if t.is_empty()
            || t.starts_with(b":")
            || t.starts_with(b"event:")
            || t.starts_with(b"id:")
            || t.starts_with(b"retry:")
        {
            continue;
        }
        if t.starts_with(b"data:") {
            t = trim(&t[5..]);
            if t.is_empty() || t == b"[DONE]" {
                continue;
            }
        }
        if !t.starts_with(b"{") {
            saw_garbage = true;
            continue;
        }
        let Ok(d) = serde_json::from_slice::<Value>(t) else {
            saw_garbage = true;
            continue;
        };
        if !d.is_object() {
            saw_garbage = true;
            continue;
        }
        let mut kind = "data";
        if let Some(choices) = d.get("choices").and_then(|c| c.as_array()) {
            for ch in choices {
                let delta = ch.get("delta");
                if let Some(dv) = delta {
                    if dv.is_object()
                        && (truthy(dv.get("content"))
                            || truthy(dv.get("reasoning_content"))
                            || truthy(dv.get("reasoning"))
                            || truthy(dv.get("tool_calls")))
                    {
                        kind = "content";
                        break;
                    }
                }
                if truthy(ch.get("finish_reason")) {
                    kind = "content";
                    break;
                }
                if truthy(ch.get("message")) {
                    kind = "completion";
                    break;
                }
            }
        }
        if kind == "content" {
            return "content";
        }
        if kind == "completion" {
            best = "completion";
        } else if best == "keepalive" {
            best = "data";
        }
    }
    if saw_garbage && best == "keepalive" {
        return "garbage";
    }
    best
}

pub fn is_stream_end(chunk: &[u8]) -> bool {
    for raw in chunk.split(|b| *b == b'\n') {
        let mut t = trim(raw);
        if t.starts_with(b"data:") {
            t = trim(&t[5..]);
        }
        if t == b"[DONE]" {
            return true;
        }
        if !t.starts_with(b"{") {
            continue;
        }
        let Ok(d) = serde_json::from_slice::<Value>(t) else { continue };
        if let Some(choices) = d.get("choices").and_then(|c| c.as_array()) {
            for ch in choices {
                if truthy(ch.get("finish_reason")) {
                    return true;
                }
            }
        }
    }
    false
}

/// True only when the body is a real chat completion: a JSON object with a
/// non-empty `choices` array (non-stream), or an SSE stream carrying at least
/// one `data:` JSON chunk with such a `choices` array. A bare JSON error body
/// (`{"error": ...}`) or proxy junk must never pass — promoting those lanes
/// only burns the next real request.
pub fn looks_like_completion(body: &[u8]) -> bool {
    fn has_choices(v: &Value) -> bool {
        v.get("choices")
            .and_then(|c| c.as_array())
            .map(|a| !a.is_empty())
            .unwrap_or(false)
    }
    if let Ok(v) = serde_json::from_slice::<Value>(body) {
        if v.is_object() && has_choices(&v) {
            return true;
        }
    }
    let head = String::from_utf8_lossy(&body[..body.len().min(4000)]).to_string();
    for line in head.lines() {
        let s = line.trim();
        if s.is_empty() || s.starts_with(':') {
            continue;
        }
        let Some(rest) = s.strip_prefix("data:") else { continue };
        let rest = rest.trim();
        if rest.is_empty() || rest == "[DONE]" {
            continue;
        }
        if let Ok(v) = serde_json::from_str::<Value>(rest) {
            if v.is_object() && has_choices(&v) {
                return true;
            }
        }
    }
    false
}

pub const OPENCODE_SYS_PREFIX: &str = "You are an AI agent running in OpenCode, a coding agent harness. Help the user accomplish their goals using the tools you have available.";
pub const READ_TOOL_STUB: &str = r#"{"type":"function","function":{"name":"read","description":"Read the contents of a file.","parameters":{"type":"object","properties":{"path":{"type":"string"}}}}}"#;

pub fn ensure_read_tool(payload: &mut Value) {
    let stub: Value = serde_json::from_str(READ_TOOL_STUB).unwrap();
    let Some(obj) = payload.as_object_mut() else { return };
    match obj.get_mut("tools") {
        Some(Value::Array(tools)) => {
            let has = tools.iter().any(|t| {
                t.get("function")
                    .and_then(|f| f.get("name"))
                    .and_then(|n| n.as_str())
                    == Some("read")
            });
            if !has {
                tools.push(stub);
            }
        }
        _ => {
            obj.insert("tools".into(), Value::Array(vec![stub]));
        }
    }
    if let Some(Value::Array(msgs)) = obj.get_mut("messages") {
        let first_is_sys = msgs
            .first()
            .and_then(|m| m.get("role"))
            .and_then(|r| r.as_str())
            == Some("system");
        if first_is_sys {
            if let Some(m0) = msgs.first_mut().and_then(|m| m.as_object_mut()) {
                if let Some(Value::String(s)) = m0.get_mut("content") {
                    if !s.contains(OPENCODE_SYS_PREFIX) {
                        if s.is_empty() {
                            *s = OPENCODE_SYS_PREFIX.to_string();
                        } else {
                            *s = format!("{OPENCODE_SYS_PREFIX}\n\n{s}");
                        }
                    }
                }
            }
        } else {
            msgs.insert(0, json!({"role": "system", "content": OPENCODE_SYS_PREFIX}));
        }
    }
}

pub fn sse_to_chat_completion_json(body: &[u8], fallback_model: &str) -> Bytes {
    if let Ok(v) = serde_json::from_slice::<Value>(body) {
        if v.is_object() && (v.get("choices").is_some() || v.get("error").is_some()) {
            return Bytes::copy_from_slice(body);
        }
    }
    let mut cid = String::from("chatcmpl-relay");
    let mut created = now() as u64;
    let mut model = fallback_model.to_string();
    let mut content = String::new();
    let mut reasoning = String::new();
    let mut finish_reason = String::from("stop");
    let mut usage = json!({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0});
    let mut tool_calls: std::collections::BTreeMap<usize, (String, String, String)> =
        std::collections::BTreeMap::new();

    let text = String::from_utf8_lossy(body);
    for line in text.lines() {
        let s = line.trim();
        let Some(rest) = s.strip_prefix("data:") else { continue };
        let rest = rest.trim();
        if rest.is_empty() || rest == "[DONE]" {
            continue;
        }
        let Ok(chunk) = serde_json::from_str::<Value>(rest) else { continue };
        if let Some(id_s) = chunk.get("id").and_then(|v| v.as_str()) {
            if !id_s.is_empty() {
                cid = id_s.to_string();
            }
        }
        if let Some(cr) = chunk.get("created").and_then(|v| v.as_u64()) {
            created = cr;
        }
        if let Some(m) = chunk.get("model").and_then(|v| v.as_str()) {
            if !m.is_empty() {
                model = m.to_string();
            }
        }
        if let Some(u) = chunk.get("usage") {
            if u.is_object() {
                usage = u.clone();
            }
        }
        let Some(ch0) = chunk
            .get("choices")
            .and_then(|c| c.as_array())
            .and_then(|a| a.first())
        else {
            continue;
        };
        if let Some(fr) = ch0.get("finish_reason").and_then(|v| v.as_str()) {
            if !fr.is_empty() {
                finish_reason = fr.to_string();
            }
        }
        let delta = ch0.get("delta").or_else(|| ch0.get("message"));
        let Some(delta) = delta else { continue };
        if let Some(c) = delta.get("content").and_then(|v| v.as_str()) {
            content.push_str(c);
        }
        if let Some(rc) = delta.get("reasoning_content").and_then(|v| v.as_str()) {
            reasoning.push_str(rc);
        }
        if let Some(tcs) = delta.get("tool_calls").and_then(|v| v.as_array()) {
            for tc in tcs {
                let idx = tc.get("index").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let entry = tool_calls
                    .entry(idx)
                    .or_insert_with(|| (format!("call_{idx}"), String::new(), String::new()));
                if let Some(id_s) = tc.get("id").and_then(|v| v.as_str()) {
                    if !id_s.is_empty() {
                        entry.0 = id_s.to_string();
                    }
                }
                if let Some(fn_obj) = tc.get("function") {
                    if let Some(n) = fn_obj.get("name").and_then(|v| v.as_str()) {
                        entry.1.push_str(n);
                    }
                    if let Some(a) = fn_obj.get("arguments").and_then(|v| v.as_str()) {
                        entry.2.push_str(a);
                    }
                }
            }
        }
    }
    let mut msg = json!({
        "role": "assistant",
        "content": if !content.is_empty() || tool_calls.is_empty() { Value::String(content) } else { Value::Null },
    });
    if !reasoning.is_empty() {
        msg["reasoning_content"] = Value::String(reasoning);
    }
    if !tool_calls.is_empty() {
        let arr: Vec<Value> = tool_calls
            .into_values()
            .map(|(id, name, args)| {
                json!({
                    "id": id,
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                })
            })
            .collect();
        msg["tool_calls"] = Value::Array(arr);
    }
    let out = json!({
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": usage,
    });
    Bytes::from(out.to_string())
}

fn json_body(status: u16, v: &Value) -> (StatusCode, HeaderMap, Bytes) {
    let mut h = HeaderMap::new();
    h.insert("content-type", HeaderValue::from_static("application/json"));
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY),
        h,
        Bytes::from(v.to_string()),
    )
}

// ── non-streaming attempt (single lane, buffered) ──────────────────
pub struct Attempt {
    pub status: u16,
    pub headers: HeaderMap,
    pub body: Bytes,
}

pub async fn attempt(
    st: &AppState,
    rt: &Runtime,
    key: &str,
    payload: &Value,
    path: &str,
    headers: &HeaderMap,
    timeout: f64,
) -> Result<Attempt, String> {
    let proxy = {
        let pool = st.pool.lock().unwrap();
        pool.lanes
            .get(key)
            .ok_or_else(|| "lane gone".to_string())?
            .proxy_url()
    };
    let t_sec = timeout.min(7.0).max(1.0);
    let want_stream = payload
        .get("stream")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let mut up_payload = payload.clone();
    if path == "chat/completions" {
        if let Some(obj) = up_payload.as_object_mut() {
            obj.insert("stream".into(), Value::Bool(true));
        }
    }
    let mut b = reqwest::Client::builder()
        .no_proxy()
        .connect_timeout(Duration::from_secs_f64(t_sec))
        .timeout(Duration::from_secs_f64(t_sec));
    if let Some(p) = &proxy {
        b = b.proxy(reqwest::Proxy::all(p).map_err(|e| e.to_string())?);
    }
    let client = b.build().map_err(|e| e.to_string())?;
    let url = format!("{}/{path}", rt.upstream_base);
    let t0 = Instant::now();
    let resp = tokio::time::timeout(
        Duration::from_secs_f64(t_sec),
        client
            .post(&url)
            .headers(headers.clone())
            .json(&up_payload)
            .send(),
    )
    .await
    .map_err(|_| "proxy timeout (7s)".to_string())?
    .map_err(|e| error_chain(&e))?;
    let status = resp.status().as_u16();
    let mut resp_headers = resp.headers().clone();
    let mut body = tokio::time::timeout(Duration::from_secs_f64(t_sec), resp.bytes())
        .await
        .map_err(|_| "proxy read timeout (7s)".to_string())?
        .map_err(|e| e.to_string())?;
    let lat = t0.elapsed().as_secs_f64() * 1000.0;

    if status == 200 {
        if let Some(kind) = stream_error_kind(&body) {
            if kind == "quota" {
                st.pool.lock().unwrap().mark_fail(key, true, rt);
                st.mark_burned(&lane_addr(st, key), "relay-200-quota");
            } else {
                st.pool.lock().unwrap().mark_fail(key, false, rt);
            }
            logline(&format!(
                "lane {} returned 200 with an in-band upstream error ({kind}) — rotated out",
                display_addr(&lane_addr(st, key))
            ));
            return Ok(Attempt {
                status: 502,
                headers: json_body(502, &json!({})).1,
                body: Bytes::from(
                    json!({"error":{"message":"lane returned an upstream error","type":"lane_invalid"}})
                        .to_string(),
                ),
            });
        }
        if looks_like_completion(&body) {
            st.pool.lock().unwrap().mark_ok(key, lat);
            if !want_stream && path == "chat/completions" {
                let fallback_model = payload
                    .get("model")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                body = sse_to_chat_completion_json(&body, fallback_model);
                resp_headers.insert("content-type", HeaderValue::from_static("application/json"));
                resp_headers.remove("content-length");
                resp_headers.remove("transfer-encoding");
            }
        } else {
            st.pool.lock().unwrap().mark_fail(key, true, rt);
            logline(&format!(
                "lane {} returned 200 with non-completion body — burned",
                display_addr(&lane_addr(st, key))
            ));
            return Ok(Attempt {
                status: 502,
                headers: json_body(502, &json!({})).1,
                body: Bytes::from(
                    json!({"error":{"message":"lane returned invalid body","type":"lane_invalid"}})
                        .to_string(),
                ),
            });
        }
    }
    // Lane accounting for 429 is the caller's job (relay_nonstream /
    // relay_stream distinguish quota-shaped bodies from odd ones); doing it
    // here as well charged a single 429 twice.
    Ok(Attempt { status, headers: resp_headers, body })
}

// ── non-streaming relay (failover loop) ────────────────────────────
pub async fn relay_nonstream(
    st: &AppState,
    payload: &Value,
    path: &str,
    client_headers: &HeaderMap,
    timeout: f64,
) -> (StatusCode, HeaderMap, Bytes) {
    let rt = st.rt();
    let attempts = (rt.attempts as usize)
        .max(st.pool.lock().unwrap().lanes.len())
        .max(1);
    // Bounded wall clock, independent of the attempt count: 7s * 80 attempts
    // is a 560s stall, long past any client's patience. The cap still lets a
    // healthy pool be walked end to end, but a run of dead lanes fails fast.
    let per_attempt = rt.relay_proxy_timeout.min(7) as f64;
    let max_wall = timeout.min((per_attempt * 6.0).max(45.0));
    let deadline = now() + max_wall;
    let mut last_err: Option<(i64, Bytes)> = None;
    let mut tried: std::collections::HashSet<String> = std::collections::HashSet::new();
    let t_start = now();

    for i in 0..attempts {
        let keys: Vec<String> = {
            let pool = st.pool.lock().unwrap();
            let mut v: Vec<String> = pool
                .available_keys(&rt, None)
                .into_iter()
                .filter(|k| !tried.contains(k))
                .collect();
            if v.is_empty() {
                v = pool.warm_keys().into_iter().filter(|k| !tried.contains(k)).collect();
            }
            if v.is_empty() {
                v = pool.parked_keys().into_iter().filter(|k| !tried.contains(k)).collect();
            }
            v
        };
        if keys.is_empty() {
            break;
        }
        let key = if i == 0 {
            st.pick_lane(&keys).unwrap_or_else(|| keys[0].clone())
        } else {
            keys[0].clone()
        };
        tried.insert(key.clone());

        let h = build_upstream_headers(
            st,
            &rt,
            client_headers.get("user-agent").and_then(|v| v.to_str().ok()),
        );
        let attempt_timeout = timeout.min(rt.relay_proxy_timeout as f64).min(7.0);

        // per-lane capacity gate (non-stream path only, like the Python relay)
        let sem: Option<Arc<Semaphore>> = {
            let mut pool = st.pool.lock().unwrap();
            match pool.lanes.get_mut(&key) {
                Some(ln) => {
                    if ln.sem.is_none() {
                        ln.sem = Some(Arc::new(Semaphore::new(rt.max_inflight.max(1) as usize)));
                    }
                    ln.sem.clone()
                }
                None => None,
            }
        };
        let _permit = match sem {
            Some(s) => Some(s.acquire_owned().await),
            None => None,
        };
        if let Some(ln) = st.pool.lock().unwrap().lanes.get_mut(&key) {
            ln.inflight += 1;
        }
        let res = attempt(st, &rt, &key, payload, path, &h, attempt_timeout).await;
        if let Some(ln) = st.pool.lock().unwrap().lanes.get_mut(&key) {
            ln.inflight = ln.inflight.saturating_sub(1);
        }

        match res {
            Ok(a) => {
                if a.status == 200 {
                    st.note_quota_ok();
                    let ms = (now() - t_start) * 1000.0;
                    st.record_request(&display_addr(&lane_addr(st, &key)), 200, ms, false);
                    return (StatusCode::OK, a.headers, a.body);
                }
                // Any 429 is an egress-lane condition: a retry on the same
                // lane would hit the same limit, so rotate unconditionally.
                // Only quota-shaped bodies burn the lane (24h memory); an odd
                // non-quota 429 stays a soft park so it cannot churn the
                // burn memory — but it still never reaches the client.
                if a.status == 429 {
                    if is_quota_429(&a.body, 429) {
                        st.pool.lock().unwrap().mark_fail(&key, true, &rt);
                        st.note_lane_429(&lane_addr(st, &key));
                    } else {
                        st.pool.lock().unwrap().mark_fail(&key, false, &rt);
                    }
                    st.stats.failovers.fetch_add(1, Ordering::Relaxed);
                    last_err = Some((429, a.body));
                    if i >= attempts - 1 || now() > deadline {
                        break;
                    }
                    continue;
                }
                if a.status == 502 {
                    if let Ok(d) = serde_json::from_slice::<Value>(&a.body) {
                        if d.get("error").and_then(|e| e.get("type")).and_then(|t| t.as_str())
                            == Some("lane_invalid")
                        {
                            st.stats.failovers.fetch_add(1, Ordering::Relaxed);
                            last_err = Some((502, a.body));
                            if now() > deadline {
                                break;
                            }
                            continue;
                        }
                    }
                }
                if a.status >= 500 {
                    st.pool.lock().unwrap().mark_fail(&key, false, &rt);
                    st.stats.failovers.fetch_add(1, Ordering::Relaxed);
                    last_err = Some((a.status as i64, a.body));
                    if now() > deadline {
                        break;
                    }
                    continue;
                }
                if (300..400).contains(&a.status) {
                    st.pool.lock().unwrap().mark_fail(&key, true, &rt);
                    st.stats.lane_failures.fetch_add(1, Ordering::Relaxed);
                    st.stats.failovers.fetch_add(1, Ordering::Relaxed);
                    last_err = Some((a.status as i64, a.body));
                    if now() > deadline {
                        break;
                    }
                    continue;
                }
                if a.status == 401 || a.status == 403 {
                    handle_lane_block(st, &rt, &key, a.status, &a.body);
                    last_err = Some((a.status as i64, a.body));
                    if i >= attempts - 1 || now() > deadline {
                        break;
                    }
                    continue;
                }
                // a real request problem (400/404/422): not a proxy issue
                return (StatusCode::from_u16(a.status).unwrap_or(StatusCode::BAD_GATEWAY), a.headers, a.body);
            }
            Err(e) => {
                st.pool.lock().unwrap().mark_fail(&key, false, &rt);
                st.stats.lane_failures.fetch_add(1, Ordering::Relaxed);
                st.stats.failovers.fetch_add(1, Ordering::Relaxed);
                last_err = Some((-1, Bytes::from(e.clone())));
                logline(&format!(
                    "lane {} failed in transport: {err}",
                    display_addr(&lane_addr(st, &key)),
                    err = e
                ));
                if now() > deadline {
                    break;
                }
                continue;
            }
        }
    }

    if let Some((code, body)) = &last_err {
        if *code == 401 || *code == 403 {
            st.record_request("-", 503, (now() - t_start) * 1000.0, false);
            let mut h = HeaderMap::new();
            h.insert("content-type", HeaderValue::from_static("application/json"));
            h.insert("retry-after", HeaderValue::from_static("2"));
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                h,
                Bytes::from(
                    json!({"error":{"message":"every egress lane was blocked upstream (temporary, retryable 503) — the pool is rotating, retry shortly","type":"zen_lane_blocked"}})
                        .to_string(),
                ),
            );
        }
        if *code == 429 {
            st.record_request("-", 503, (now() - t_start) * 1000.0, false);
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                json_body(503, &json!({})).1,
                Bytes::from(RATE_LIMITED_BODY),
            );
        }
        if *code >= 500 {
            st.record_request("-", *code as u16, (now() - t_start) * 1000.0, false);
            return (
                StatusCode::from_u16(*code as u16).unwrap_or(StatusCode::BAD_GATEWAY),
                json_body(*code as u16, &json!({})).1,
                body.clone(),
            );
        }
    }
    st.record_request("-", 503, (now() - t_start) * 1000.0, false);
    (
        StatusCode::SERVICE_UNAVAILABLE,
        json_body(503, &json!({})).1,
        Bytes::from(
            json!({"error":{"message":"all egress lanes busy or failed — pool is refilling, retry shortly","type":"rotator_exhausted"}})
                .to_string(),
        ),
    )
}

// ── streaming relay ────────────────────────────────────────────────
pub async fn relay_stream(
    st: Arc<AppState>,
    payload: Value,
    path: &str,
    client_headers: HeaderMap,
    timeout: f64,
) -> (StatusCode, HeaderMap, Bytes, Option<Body>) {
    let rt = st.rt();
    let attempts = (rt.attempts as usize)
        .max(st.pool.lock().unwrap().lanes.len())
        .max(1);
    // Bounded wall clock, independent of the attempt count: 7s * 80 attempts
    // is a 560s stall, long past any client's patience. The cap still lets a
    // healthy pool be walked end to end, but a run of dead lanes fails fast.
    let per_attempt = rt.relay_proxy_timeout.min(7) as f64;
    let max_wall = timeout.min((per_attempt * 6.0).max(45.0));
    let deadline = now() + max_wall;
    let mut last_err: Option<(i64, Bytes)> = None;
    let mut tried: std::collections::HashSet<String> = std::collections::HashSet::new();
    let t_start = now();
    let peek_sec = if payload_has_image(&payload) {
        IMAGE_PEEK_DEADLINE_SEC
    } else {
        PEEK_DEADLINE_SEC
    };

    for i in 0..attempts {
        let keys: Vec<String> = {
            let pool = st.pool.lock().unwrap();
            let mut v: Vec<String> = pool
                .available_keys(&rt, Some("fast"))
                .into_iter()
                .filter(|k| !tried.contains(k))
                .collect();
            if v.is_empty() {
                v = pool.warm_keys().into_iter().filter(|k| !tried.contains(k)).collect();
            }
            if v.is_empty() {
                v = pool.parked_keys().into_iter().filter(|k| !tried.contains(k)).collect();
            }
            v
        };
        if keys.is_empty() {
            break;
        }
        let key = if i == 0 {
            st.pick_lane(&keys).unwrap_or_else(|| keys[0].clone())
        } else {
            keys[0].clone()
        };
        tried.insert(key.clone());

        let h = build_upstream_headers(
            &st,
            &rt,
            client_headers.get("user-agent").and_then(|v| v.to_str().ok()),
        );

        let client = {
            let mut pool = st.pool.lock().unwrap();
            st.lane_client(&mut pool, &key)
        };
        let client = match client {
            Ok(c) => c,
            Err(_) => {
                last_err = Some((-1, Bytes::from_static(b"lane client failed")));
                continue;
            }
        };

        let url = format!("{}/{path}", rt.upstream_base);
        let t0 = Instant::now();
        let attempt_timeout = timeout.min(rt.relay_proxy_timeout as f64).min(7.0);
        let send_res = tokio::time::timeout(
            Duration::from_secs_f64(attempt_timeout),
            client.post(&url).headers(h).json(&payload).send(),
        )
        .await;
        let resp = match send_res {
            Ok(Ok(r)) => r,
            Ok(Err(e)) => {
                st.pool.lock().unwrap().mark_fail(&key, false, &rt);
                st.stats.lane_failures.fetch_add(1, Ordering::Relaxed);
                st.stats.failovers.fetch_add(1, Ordering::Relaxed);
                last_err = Some((-1, Bytes::from(e.to_string())));
                if now() > deadline {
                    break;
                }
                continue;
            }
            Err(_) => {
                st.pool.lock().unwrap().mark_fail(&key, false, &rt);
                st.stats.lane_failures.fetch_add(1, Ordering::Relaxed);
                st.stats.failovers.fetch_add(1, Ordering::Relaxed);
                last_err = Some((-1, Bytes::from_static(b"proxy timeout (7s)")));
                if now() > deadline {
                    break;
                }
                continue;
            }
        };
        let status = resp.status().as_u16();

        if status == 200 {
            let mut aiter: BoxStream = Box::pin(resp.bytes_stream());
            let peek_deadline = Instant::now() + Duration::from_secs_f64(peek_sec);
            let mut head: Vec<Bytes> = Vec::new();
            // SSE framing does not respect HTTP chunk boundaries: a proxy can
            // split a `data: {...}` line across two chunks. Classifying each
            // chunk on its own marks the continuation as "garbage" and burns a
            // healthy lane, so accumulate complete lines before classifying.
            let mut line_buf: Vec<u8> = Vec::new();
            let mut first_idx: Option<usize> = None;
            let mut err_kind: Option<&'static str> = None;
            let mut saw_garbage = false;
            let mut saw_life = false;
            let mut ended = false;
            let mut peek_err: Option<String> = None;
            loop {
                let left = peek_deadline.saturating_duration_since(Instant::now());
                if left.is_zero() {
                    break;
                }
                match tokio::time::timeout(left, aiter.next()).await {
                    Err(_) => break,
                    Ok(None) => {
                        ended = true;
                        break;
                    }
                    Ok(Some(Err(e))) => {
                        peek_err = Some(e.to_string());
                        break;
                    }
                    Ok(Some(Ok(chunk))) => {
                        head.push(chunk.clone());
                        line_buf.extend_from_slice(&chunk);
                        let complete_end = line_buf
                            .iter()
                            .rposition(|b| *b == b'\n')
                            .map(|i| i + 1)
                            .unwrap_or(0);
                        if complete_end == 0 {
                            // Only a partial line so far — wait for the rest.
                            saw_life = true;
                            continue;
                        }
                        let complete = &line_buf[..complete_end];
                        if let Some(k) = stream_error_kind(complete) {
                            err_kind = Some(k);
                            break;
                        }
                        match frame_kind(complete) {
                            "content" | "completion" => {
                                // pump() replays head[0..=first_idx]; the frame
                                // may span several chunks, so the last chunk
                                // read is the only safe boundary.  Using the
                                // first chunk of the line dropped continuation
                                // chunks and truncated the SSE JSON mid-value.
                                first_idx = Some(head.len() - 1);
                                break;
                            }
                            "garbage" => {
                                saw_garbage = true;
                                break;
                            }
                            _ => {
                                saw_life = true;
                            }
                        }
                        line_buf.drain(..complete_end);
                    }
                }
            }

            if let Some(e) = peek_err {
                st.pool.lock().unwrap().mark_fail(&key, false, &rt);
                st.stats.lane_failures.fetch_add(1, Ordering::Relaxed);
                st.stats.failovers.fetch_add(1, Ordering::Relaxed);
                last_err = Some((-1, Bytes::from(e)));
                if now() > deadline {
                    break;
                }
                continue;
            }

            if first_idx.is_none() && err_kind.is_none() && saw_life && !head.is_empty() && !saw_garbage
            {
                if ended {
                    // stream closed with no output frame at all: an empty answer,
                    // not a slow lane. Rotate and retry here; the client never
                    // sees the empty stream.
                    st.pool.lock().unwrap().mark_fail(&key, false, &rt);
                    st.stats.lane_failures.fetch_add(1, Ordering::Relaxed);
                    st.stats.failovers.fetch_add(1, Ordering::Relaxed);
                    last_err = Some((
                        502,
                        Bytes::from(json!({"error":{"message":"lane produced an empty answer (stream closed with no output)","type":"lane_invalid"}}).to_string()),
                    ));
                    logline(&format!(
                        "lane {} produced an empty answer — rotated out",
                        display_addr(&lane_addr(&st, &key))
                    ));
                    if i >= attempts - 1 || now() > deadline {
                        break;
                    }
                    continue;
                }
                // Replay the whole peek buffer: the first frame may span
                // chunks, and dropping a later chunk would truncate a line.
                first_idx = Some(head.len() - 1);
                logline(&format!(
                    "lane {} is slow (no output token in {:.0}s) — streaming anyway",
                    display_addr(&lane_addr(&st, &key)),
                    peek_sec
                ));
            }

            if let Some(kind) = err_kind {
                if kind == "quota" {
                    st.pool.lock().unwrap().mark_fail(&key, true, &rt);
                    st.mark_burned(&lane_addr(&st, &key), "relay-200-quota");
                } else {
                    st.pool.lock().unwrap().mark_fail(&key, false, &rt);
                }
                st.stats.lane_failures.fetch_add(1, Ordering::Relaxed);
                st.stats.failovers.fetch_add(1, Ordering::Relaxed);
                last_err = Some((
                    if kind == "quota" || kind == "region" { 403 } else { 502 },
                    Bytes::from(json!({"error":{"message":"lane reported an in-band upstream error","type":"lane_invalid"}}).to_string()),
                ));
                logline(&format!(
                    "lane {} reported an in-band upstream error ({kind}) — rotated out",
                    display_addr(&lane_addr(&st, &key))
                ));
                if i >= attempts - 1 || now() > deadline {
                    break;
                }
                continue;
            }

            if first_idx.is_none() {
                if saw_garbage {
                    st.pool.lock().unwrap().mark_fail(&key, true, &rt);
                    logline(&format!(
                        "lane {} answered 200 with non-SSE proxy garbage — burned",
                        display_addr(&lane_addr(&st, &key))
                    ));
                } else {
                    st.pool.lock().unwrap().mark_fail(&key, false, &rt);
                    logline(&format!(
                        "lane {} produced no output token (stream closed or keep-alive only) — rotated out",
                        display_addr(&lane_addr(&st, &key))
                    ));
                }
                st.stats.lane_failures.fetch_add(1, Ordering::Relaxed);
                st.stats.failovers.fetch_add(1, Ordering::Relaxed);
                last_err = Some((
                    502,
                    Bytes::from(json!({"error":{"message":"no output token before the stream closed","type":"lane_invalid"}}).to_string()),
                ));
                if i >= attempts - 1 || now() > deadline {
                    break;
                }
                continue;
            }

            // ── commit ──
            let lat = t0.elapsed().as_secs_f64() * 1000.0;
            st.pool.lock().unwrap().mark_ok(&key, lat);
            st.note_quota_ok();
            st.stats.streams.fetch_add(1, Ordering::Relaxed);
            if let Some(ln) = st.pool.lock().unwrap().lanes.get_mut(&key) {
                ln.inflight += 1;
            }
            let addr = lane_addr(&st, &key);
            let ms_ttfb = (now() - t_start) * 1000.0;
            st.record_request(&display_addr(&addr), 200, ms_ttfb, true);

            let (tx, rx) =
                tokio::sync::mpsc::channel::<Result<Bytes, std::io::Error>>(64);
            let st2 = st.clone();
            let key2 = key.clone();
            let addr2 = addr.clone();
            let first = first_idx.unwrap();
            let keep_client = Some(client.clone());
            tokio::spawn(async move {
                pump(st2, key2, addr2, head, first, aiter, tx, keep_client).await;
            });

            let mut rh = HeaderMap::new();
            rh.insert("content-type", HeaderValue::from_static("text/event-stream"));
            return (
                StatusCode::OK,
                rh,
                Bytes::new(),
                Some(Body::from_stream(ChannelStream { rx })),
            );
        }

        // non-200 paths (mirror the Python stream branch)
        if status >= 500 {
            let body = resp.bytes().await.unwrap_or_default();
            st.pool.lock().unwrap().mark_fail(&key, false, &rt);
            st.stats.failovers.fetch_add(1, Ordering::Relaxed);
            last_err = Some((status as i64, body));
            if now() > deadline {
                break;
            }
            continue;
        }
        if (300..400).contains(&status) {
            st.pool.lock().unwrap().mark_fail(&key, true, &rt);
            st.stats.lane_failures.fetch_add(1, Ordering::Relaxed);
            st.stats.failovers.fetch_add(1, Ordering::Relaxed);
            last_err = Some((status as i64, Bytes::new()));
            if now() > deadline {
                break;
            }
            continue;
        }
        let ct = resp
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string());
        let body = resp.bytes().await.unwrap_or_default();
        // Same rule as the non-stream ladder: every 429 rotates; only a
        // quota-shaped body burns the lane.
        if status == 429 {
            if is_quota_429(&body, 429) {
                st.pool.lock().unwrap().mark_fail(&key, true, &rt);
                st.note_lane_429(&lane_addr(&st, &key));
            } else {
                st.pool.lock().unwrap().mark_fail(&key, false, &rt);
            }
            st.stats.failovers.fetch_add(1, Ordering::Relaxed);
            last_err = Some((429, Bytes::new()));
            if i >= attempts - 1 || now() > deadline {
                break;
            }
            continue;
        }
        if status == 401 || status == 403 {
            handle_lane_block(&st, &rt, &key, status, &body);
            last_err = Some((status as i64, body));
            if i >= attempts - 1 || now() > deadline {
                break;
            }
            continue;
        }
        // a real request problem (400/404/422): pass it through buffered
        let mut rh = HeaderMap::new();
        if let Some(ct) = ct {
            if let Ok(hv) = HeaderValue::from_str(&ct) {
                rh.insert("content-type", hv);
            }
        }
        return (
            StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY),
            rh,
            body,
            None,
        );
    }

    // terminal mapping (never hand the client an auth-shaped 4xx)
    if let Some((code, body)) = &last_err {
        if *code == 401 || *code == 403 {
            let mut h = HeaderMap::new();
            h.insert("content-type", HeaderValue::from_static("application/json"));
            h.insert("retry-after", HeaderValue::from_static("2"));
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                h,
                Bytes::from(
                    json!({"error":{"message":"every egress lane was blocked upstream (temporary, retryable 503) — the pool is rotating, retry shortly","type":"zen_lane_blocked"}})
                        .to_string(),
                ),
                None,
            );
        }
        if *code == 429 {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                json_body(503, &json!({})).1,
                Bytes::from(RATE_LIMITED_BODY),
                None,
            );
        }
        if *code >= 500 {
            return (
                StatusCode::from_u16(*code as u16).unwrap_or(StatusCode::BAD_GATEWAY),
                json_body(*code as u16, &json!({})).1,
                body.clone(),
                None,
            );
        }
    }
    (
        StatusCode::SERVICE_UNAVAILABLE,
        json_body(503, &json!({})).1,
        Bytes::from(
            json!({"error":{"message":"all egress lanes busy or failed — pool is refilling, retry shortly","type":"rotator_exhausted"}})
                .to_string(),
        ),
        None,
    )
}

#[allow(clippy::too_many_arguments)]
async fn pump(
    st: Arc<AppState>,
    key: String,
    addr: String,
    head: Vec<Bytes>,
    first_idx: usize,
    mut aiter: BoxStream,
    tx: tokio::sync::mpsc::Sender<Result<Bytes, std::io::Error>>,
    _keep_client: Option<reqwest::Client>,
) {
    let mut seen_end = false;
    let mut sent_first = false;
    let mut client_gone = false;
    let mut failed = false;

    for (i, h) in head.iter().enumerate() {
        if is_stream_end(h) {
            seen_end = true;
        }
        if tx.send(Ok(h.clone())).await.is_err() {
            client_gone = true;
            break;
        }
        if i == first_idx {
            sent_first = true;
            break;
        }
    }

    if sent_first && !client_gone {
        loop {
            match aiter.next().await {
                None => break,
                Some(Err(_)) => {
                    failed = true;
                    break;
                }
                Some(Ok(chunk)) => {
                    if !seen_end && is_stream_end(&chunk) {
                        seen_end = true;
                    }
                    if tx.send(Ok(chunk)).await.is_err() {
                        client_gone = true;
                        break;
                    }
                }
            }
        }
    }

    if !client_gone && sent_first && (failed || !seen_end) {
        let rt = st.rt();
        st.pool.lock().unwrap().mark_stream_break(&key, &rt);
        logline(&format!(
            "stream on lane {} ended without finish_reason/[DONE] — signalling a reset",
            display_addr(&addr)
        ));
        let _ = tx.send(Ok(Bytes::from_static(INTERRUPT_FRAME))).await;
    } else if !client_gone && sent_first && seen_end {
        st.pool.lock().unwrap().mark_stream_ok(&key);
    }
    if let Some(ln) = st.pool.lock().unwrap().lanes.get_mut(&key) {
        ln.inflight = ln.inflight.saturating_sub(1);
    }
}
