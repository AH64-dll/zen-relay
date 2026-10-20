//! Model metadata cache + resolution. Same behavior as the Python relay:
//! the models list is fetched from the Zen endpoint through the machine's own
//! egress (no proxy, no quota cost), cached, and clients asking for a model
//! the key cannot serve are resolved to a free one.
use crate::config::now;
use crate::relay::upstream_headers;
use crate::state::AppState;
use bytes::Bytes;
use serde_json::{json, Value};
use std::time::Duration;

const MODELS_TTL_SEC: f64 = 3600.0;
const MODELS_RETRY_SEC: f64 = 30.0;
const FALLBACK_TTL_SEC: f64 = 300.0;

fn unavailable() -> Bytes {
    Bytes::from(
        json!({"error": {"message": "models list unavailable", "type": "models_unavailable"}})
            .to_string(),
    )
}

pub async fn get_models_cached(st: &AppState) -> (u16, String, Bytes) {
    let rt = st.rt();
    {
        let cache = st.models.lock().await;
        if now() < cache.retry_at {
            if !cache.body.is_empty() {
                return (cache.status, cache.content_type.clone(), cache.body.clone());
            }
            return (503, "application/json".to_string(), unavailable());
        }
        if !cache.body.is_empty() && now() - cache.updated < MODELS_TTL_SEC {
            return (cache.status, cache.content_type.clone(), cache.body.clone());
        }
    }

    let headers = upstream_headers(st, &rt, None);
    let res = st
        .client
        .get(rt.screen_url.as_str())
        .headers(headers)
        .timeout(Duration::from_secs(20))
        .send()
        .await;

    let mut cache = st.models.lock().await;
    match res {
        Ok(resp) => {
            let status = resp.status().as_u16();
            let ct = resp
                .headers()
                .get("content-type")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("application/json")
                .to_string();
            let body = resp.bytes().await.unwrap_or_default();
            let mut out = body.clone();
            if status == 200 {
                if let Ok(mut d) = serde_json::from_slice::<Value>(&body) {
                    if let Some(arr) = d.get_mut("data").and_then(|v| v.as_array_mut()) {
                        let mut free: Vec<Value> = arr
                            .iter()
                            .filter(|m| {
                                m.get("id")
                                    .and_then(|v| v.as_str())
                                    .map(|s| s.ends_with("-free"))
                                    .unwrap_or(false)
                            })
                            .cloned()
                            .collect();
                        free.sort_by_key(|m| {
                            if m.get("id").and_then(|v| v.as_str()) == Some("hy3-free") {
                                0
                            } else {
                                1
                            }
                        });
                        d["data"] = Value::Array(free);
                        out = Bytes::from(serde_json::to_vec(&d).unwrap_or_default());
                    }
                }
            }
            cache.updated = now();
            cache.status = status;
            cache.content_type = ct.clone();
            cache.retry_at = 0.0;
            cache.body = out.clone();
            (status, ct, out)
        }
        Err(_) => {
            cache.retry_at = now() + MODELS_RETRY_SEC;
            if !cache.body.is_empty() {
                (cache.status, cache.content_type.clone(), cache.body.clone())
            } else {
                (503, "application/json".to_string(), unavailable())
            }
        }
    }
}

pub async fn fallback_model(st: &AppState) -> String {
    let rt = st.rt();
    {
        let c = st.models.lock().await;
        if let Some(f) = &c.fallback {
            if now() - c.fallback_at < FALLBACK_TTL_SEC {
                return f.clone();
            }
        }
    }
    let mut chosen = if rt.probe_model.is_empty() {
        "beam-free".to_string()
    } else {
        rt.probe_model.clone()
    };
    let (status, _, body) = get_models_cached(st).await;
    if status == 200 {
        if let Ok(d) = serde_json::from_slice::<Value>(&body) {
            if let Some(ids) = d.get("data").and_then(|v| v.as_array()) {
                if !ids.is_empty() {
                    let pick = ids
                        .iter()
                        .filter_map(|m| m.get("id").and_then(|v| v.as_str()))
                        .find(|i| i.contains("-free") || i.contains("flash") || i.contains("mini"))
                        .or_else(|| ids[0].get("id").and_then(|v| v.as_str()));
                    if let Some(p) = pick {
                        chosen = p.to_string();
                    }
                }
            }
        }
    }
    let mut c = st.models.lock().await;
    c.fallback = Some(chosen.clone());
    c.fallback_at = now();
    chosen
}

pub async fn resolve_model(st: &AppState, model: &str) -> String {
    let name: &str = model.splitn(2, '/').last().unwrap_or(model).trim();
    if !name.is_empty() {
        let (status, _, body) = get_models_cached(st).await;
        if status == 200 {
            if let Ok(d) = serde_json::from_slice::<Value>(&body) {
                let known = d
                    .get("data")
                    .and_then(|v| v.as_array())
                    .map(|a| {
                        a.iter().any(|m| {
                            m.get("id").and_then(|v| v.as_str()) == Some(name)
                        })
                    })
                    .unwrap_or(false);
                if known {
                    return name.to_string();
                }
            }
        }
    }
    fallback_model(st).await
}
