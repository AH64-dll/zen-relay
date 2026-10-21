//! HTTP surface: the OpenAI-compatible relay endpoint plus the small
//! management API the operations scripts and the UI use.
use crate::config::{now, Settings, VERSION};
use crate::models::{get_models_cached, resolve_model};
use crate::relay::{ensure_read_tool, relay_nonstream, relay_stream};
use crate::state::{ct_eq, display_addr, redact_addr, AppState};
use axum::body::Body;
use axum::extract::{Query, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use bytes::Bytes;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::atomic::Ordering;
use std::sync::Arc;

pub fn check_auth(st: &AppState, headers: &HeaderMap) -> bool {
    let key = st.rt().relay_key;
    if key.is_empty() {
        return true;
    }
    let auth = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    ct_eq(auth, &format!("Bearer {key}")) || ct_eq(auth, &key)
}

fn json_resp(status: u16, v: &Value) -> Response {
    Response::builder()
        .status(StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR))
        .header("content-type", "application/json")
        .body(Body::from(v.to_string()))
        .unwrap()
}

fn bytes_resp(status: u16, ct: &str, body: Bytes) -> Response {
    Response::builder()
        .status(StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR))
        .header(
            "content-type",
            HeaderValue::from_str(ct).unwrap_or(HeaderValue::from_static("application/json")),
        )
        .body(Body::from(body))
        .unwrap()
}

/// Hop-by-hop and transport headers that must not be copied onto our own
/// response: we always send a buffered body, so upstream framing (chunked
/// encoding, a content-length for a different representation) would corrupt it.
const DROP_HEADERS: &[&str] = &[
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
];

/// Build a response from upstream status + headers without ever panicking.
/// The old code fed upstream headers into `Response::builder()` and unwrapped,
/// so one malformed header from a hostile proxy killed the request task.
fn upstream_response(status: StatusCode, headers: &HeaderMap, body: Body) -> Response {
    let mut resp = Response::new(body);
    *resp.status_mut() = status;
    let out = resp.headers_mut();
    for (k, v) in headers.iter() {
        if DROP_HEADERS.contains(&k.as_str()) {
            continue;
        }
        out.insert(k.clone(), v.clone());
    }
    if !out.contains_key("content-type") {
        out.insert(
            axum::http::header::CONTENT_TYPE,
            HeaderValue::from_static("application/json"),
        );
    }
    resp
}

pub async fn healthz(State(st): State<Arc<AppState>>) -> Response {
    let mut v = st.pool_stats();
    if let Some(o) = v.as_object_mut() {
        let warm = o.get("warm").and_then(|w| w.as_u64()).unwrap_or(0);
        o.insert("ok".into(), json!(warm > 0));
        o.insert("version".into(), json!(VERSION));
    }
    Json(v).into_response()
}

pub async fn list_models(State(st): State<Arc<AppState>>, req: Request) -> Response {
    if !check_auth(&st, req.headers()) {
        return json_resp(401, &json!({"error": {"message": "unauthorized", "type": "unauthorized"}}));
    }
    let (status, ct, body) = get_models_cached(&st).await;
    bytes_resp(status, &ct, body)
}

pub async fn chat_completions(State(st): State<Arc<AppState>>, req: Request) -> Response {
    if !check_auth(&st, req.headers()) {
        return json_resp(
            401,
            &json!({"error": {"message": "invalid relay key", "type": "invalid_api_key"}}),
        );
    }
    let headers = req.headers().clone();
    st.stats.requests.fetch_add(1, Ordering::Relaxed);
    let body = match axum::body::to_bytes(req.into_body(), 64 << 20).await {
        Ok(b) => b,
        Err(_) => {
            return json_resp(
                400,
                &json!({"error": {"message": "invalid request body", "type": "invalid_request"}}),
            )
        }
    };
    let mut payload: Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(_) => {
            return json_resp(
                400,
                &json!({"error": {"message": "invalid json", "type": "invalid_request"}}),
            )
        }
    };

    // resolve the model against what the configured key can actually serve
    let model = payload
        .get("model")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let resolved = resolve_model(&st, &model).await;
    if let Some(o) = payload.as_object_mut() {
        o.insert("model".into(), json!(resolved));
    }
    // the free tier's fingerprint gate demands an agent-shaped body
    ensure_read_tool(&mut payload);

    let stream = payload
        .get("stream")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);

    if stream {
        let (status, h, bytes, body_opt) =
            relay_stream(st.clone(), payload, "chat/completions", headers, 300.0).await;
        let b = match body_opt {
            Some(b) => b,
            None => Body::from(bytes),
        };
        return upstream_response(status, &h, b);
    }

    let (status, h, bytes) =
        relay_nonstream(&st, &payload, "chat/completions", &headers, 120.0).await;
    upstream_response(status, &h, Body::from(bytes))
}

pub async fn api_stats(State(st): State<Arc<AppState>>, req: Request) -> Response {
    if !check_auth(&st, req.headers()) {
        return json_resp(401, &json!({"error": "unauthorized"}));
    }
    let s = &st.stats;
    let adapt = st.adapt.lock().unwrap();
    let quota = st.quota.lock().unwrap();
    let burn = st.burned.lock().unwrap();
    json_resp(
        200,
        &json!({
            "version": VERSION,
            "uptime_sec": (now() - st.start) as u64,
            "pool": st.pool_stats(),
            "stats": {
                "requests": s.requests.load(Ordering::Relaxed),
                "failovers": s.failovers.load(Ordering::Relaxed),
                "lane_failures": s.lane_failures.load(Ordering::Relaxed),
                "probes_ok": s.probes_ok.load(Ordering::Relaxed),
                "probes_burned": s.probes_burned.load(Ordering::Relaxed),
                "candidates_tested": s.candidates_tested.load(Ordering::Relaxed),
                "streams": s.streams.load(Ordering::Relaxed),
                "upstream_429s": s.upstream_429s.load(Ordering::Relaxed),
                "started": st.start,
            },
            "settings": st.settings.read().unwrap().public(),
            "adaptive": {
                "adaptive": st.rt().adaptive,
                "current": adapt.current,
                "min": adapt.min,
                "last_change": adapt.last_change,
                "last_reason": adapt.last_reason,
                "err_ratio": adapt.err_ratio,
            },
            "metrics": st.metrics_window(300.0),
            "burn": {
                "known_spent_ips": burn.ips.len(),
                "ttl_sec": st.rt().ttl as u64,
                "enabled": st.rt().burn_memory,
                "blocked_intake": burn.blocked_intake,
                "blocked_probe": burn.blocked_probe,
                "recorded": burn.recorded,
                "expired": burn.expired,
            },
            "quota": {
                "exhausted": quota.exhausted,
                "backoff_sec": quota.backoff_sec,
                "backoff_until": quota.backoff_until,
                "announced": false,
            },
        }),
    )
}

pub async fn api_pool(
    State(st): State<Arc<AppState>>,
    Query(q): Query<HashMap<String, String>>,
    req: Request,
) -> Response {
    if !check_auth(&st, req.headers()) {
        return json_resp(401, &json!({"error": "unauthorized"}));
    }
    let limit: usize = q
        .get("limit")
        .and_then(|v| v.parse().ok())
        .unwrap_or(100)
        .min(5000);
    let offset: usize = q.get("offset").and_then(|v| v.parse().ok()).unwrap_or(0);
    let pool = st.pool.lock().unwrap();
    let warm_keys = pool.warm_keys();
    let parked_keys = pool.parked_keys();
    let mk = |k: &String| -> Value {
        let ln = pool.lanes.get(k).unwrap();
        json!({
            "addr": display_addr(&ln.addr),
            "raw_addr": redact_addr(&ln.addr),
            "proto": ln.proto,
            "score": (ln.score * 10000.0).round() / 10000.0,
            "lat_ms": (ln.lat_ms * 10.0).round() / 10.0,
            "ok": ln.ok,
            "fails": ln.fails,
            "inflight": ln.inflight,
            "tier": ln.tier(),
            "subnet": ln.subnet(),
            "parked_until": ln.parked_until,
        })
    };
    let slice = |keys: &[String]| -> Vec<Value> {
        keys.iter().skip(offset).take(limit).map(mk).collect()
    };
    json_resp(
        200,
        &json!({
            "total_lanes": pool.lanes.len(),
            "warm": warm_keys.len(),
            "parked": parked_keys.len(),
            "queue": pool.queue_len(),
            "sources_ok": pool.sources_ok,
            "lanes": slice(&warm_keys),
            "parked_lanes": slice(&parked_keys),
        }),
    )
}

pub async fn settings_get(State(st): State<Arc<AppState>>, req: Request) -> Response {
    if !check_auth(&st, req.headers()) {
        return json_resp(401, &json!({"error": "unauthorized"}));
    }
    Json(st.settings.read().unwrap().public()).into_response()
}

pub async fn settings_post(State(st): State<Arc<AppState>>, req: Request) -> Response {
    if !check_auth(&st, req.headers()) {
        return json_resp(401, &json!({"error": "unauthorized"}));
    }
    let body = match axum::body::to_bytes(req.into_body(), 1 << 20).await {
        Ok(b) => b,
        Err(_) => return json_resp(400, &json!({"error": "invalid body"})),
    };
    let map = match serde_json::from_slice::<Value>(&body) {
        Ok(Value::Object(m)) => m,
        _ => return json_resp(400, &json!({"error": "invalid json"})),
    };
    let mut applied: Vec<String> = Vec::new();
    let mut errors: Vec<String> = Vec::new();
    {
        let mut s = st.settings.write().unwrap();
        for (k, v) in map.iter() {
            if !Settings::KEYS.contains(&k.as_str()) {
                continue;
            }
            // masked secrets echoed back from a GET must not overwrite the real value
            if let Value::String(t) = v {
                if t.contains("...") || t == "(none)" {
                    continue;
                }
            }
            match s.validate_and_set(k, v) {
                Ok(()) => applied.push(k.clone()),
                Err(e) => errors.push(e),
            }
        }
    }
    if !errors.is_empty() {
        return json_resp(400, &json!({"error": "invalid settings", "details": errors}));
    }
    st.save_settings();
    // Never echo secrets back: `public()` masks the relay key, upstream keys
    // and the webshare token (this handler used to serialize the raw struct).
    let s = st.settings.read().unwrap();
    json_resp(200, &s.public())
}

pub async fn api_refresh(State(st): State<Arc<AppState>>, req: Request) -> Response {
    if !check_auth(&st, req.headers()) {
        return json_resp(401, &json!({"error": "unauthorized"}));
    }
    st.pool.lock().unwrap().last_fetch = 0.0;
    crate::pool_mgr::fetch_sources(&st).await;
    let pool = st.pool.lock().unwrap();
    json_resp(
        200,
        &json!({"queue": pool.queue_len(), "sources_ok": pool.sources_ok}),
    )
}

pub async fn pool_seed(State(st): State<Arc<AppState>>, req: Request) -> Response {
    if !check_auth(&st, req.headers()) {
        return json_resp(401, &json!({"error": "unauthorized"}));
    }
    let body = match axum::body::to_bytes(req.into_body(), 1 << 20).await {
        Ok(b) => b,
        Err(_) => return json_resp(400, &json!({"error": "invalid body"})),
    };
    let v: Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(_) => return json_resp(400, &json!({"error": "invalid json"})),
    };
    let proto = v
        .get("proto")
        .and_then(|p| p.as_str())
        .unwrap_or("http")
        .to_lowercase();
    if !matches!(proto.as_str(), "http" | "https" | "socks4" | "socks5") {
        return json_resp(400, &json!({"error": "unsupported proto"}));
    }
    let lanes = v
        .get("lanes")
        .and_then(|l| l.as_array())
        .cloned()
        .unwrap_or_default();
    let rt = st.rt();
    let mut added = 0usize;
    let mut blocked = 0u64;
    {
        let mut pool = st.pool.lock().unwrap();
        for item in lanes {
            let Some(addr) = item.as_str() else { continue };
            if addr.trim().is_empty() {
                continue;
            }
            let key = format!("{proto}://{}", addr.trim());
            if pool.contains_key(&key) {
                continue;
            }
            if st.is_burned(addr) {
                blocked += 1;
                continue;
            }
            pool.push_candidate(key, true, rt.max_candidates);
            added += 1;
        }
    }
    if blocked > 0 {
        st.burned.lock().unwrap().blocked_intake += blocked;
    }
    let queue = st.pool.lock().unwrap().queue_len();
    json_resp(200, &json!({"added": added, "queue": queue}))
}

pub async fn api_burned(State(st): State<Arc<AppState>>, req: Request) -> Response {
    if !check_auth(&st, req.headers()) {
        return json_resp(401, &json!({"error": "unauthorized"}));
    }
    st.prune_burned();
    let rt = st.rt();
    let b = st.burned.lock().unwrap();
    let mut ips: Vec<(&String, &Value)> = b.ips.iter().collect();
    ips.sort_by(|a, b| {
        let ta = a.1.get("at").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let tb = b.1.get("at").and_then(|v| v.as_f64()).unwrap_or(0.0);
        tb.partial_cmp(&ta).unwrap_or(std::cmp::Ordering::Equal)
    });
    let map: serde_json::Map<String, Value> = ips
        .into_iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    json_resp(
        200,
        &json!({
            "count": b.ips.len(),
            "ttl_sec": rt.ttl as u64,
            "enabled": rt.burn_memory,
            "ips": map,
        }),
    )
}

pub async fn api_burned_clear(State(st): State<Arc<AppState>>, req: Request) -> Response {
    if !check_auth(&st, req.headers()) {
        return json_resp(401, &json!({"error": "unauthorized"}));
    }
    let n = {
        let mut b = st.burned.lock().unwrap();
        let n = b.ips.len();
        b.ips.clear();
        n
    };
    st.save_burned();
    json_resp(200, &json!({"cleared": n}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upstream_response_drops_framing_headers_and_never_panics() {
        let mut h = HeaderMap::new();
        h.insert("content-length", HeaderValue::from_static("999"));
        h.insert("transfer-encoding", HeaderValue::from_static("chunked"));
        h.insert("connection", HeaderValue::from_static("keep-alive"));
        h.insert("x-upstream", HeaderValue::from_static("ok"));
        let r = upstream_response(StatusCode::OK, &h, Body::from("hi"));
        assert_eq!(r.status(), StatusCode::OK);
        assert!(r.headers().get("content-length").is_none());
        assert!(r.headers().get("transfer-encoding").is_none());
        assert!(r.headers().get("connection").is_none());
        assert_eq!(r.headers().get("x-upstream").unwrap(), "ok");
        assert_eq!(r.headers().get("content-type").unwrap(), "application/json");
    }
}
