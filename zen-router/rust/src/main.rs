//! zen-relay — the egress relay (fingerprint masking + per-request IP rotation)
//! rewritten in Rust. One program, one loopback port, everything in parallel.
//!
//! Same settings file, same lane state files, same API as the Python relay it
//! replaces, so the harness keeps pointing at the same address.
mod api;
mod config;
mod models;
mod pool_mgr;
mod relay;
mod state;

use axum::routing::{get, post};
use axum::Router;
use config::{Paths, Settings, VERSION};
use state::{logline, AppState};

#[tokio::main]
async fn main() {
    let paths = Paths::from_env();
    let mut settings = Settings::load(&paths.settings);

    // Refuse to run as an open relay on the loopback port: bind a generated
    // key into the settings file so every client (the harness included) has
    // to send it. Same rule as the Python relay.
    if settings.relay_api_key.trim().is_empty() {
        let allow_anon = std::env::var("RELAY_ALLOW_ANONYMOUS")
            .map(|v| matches!(v.to_lowercase().as_str(), "1" | "true" | "yes" | "on"))
            .unwrap_or(false);
        if !allow_anon {
            let key = format!("rly_{}", uuid::Uuid::new_v4().simple());
            settings.relay_api_key = key.clone();
            let _ = settings.save(&paths.settings);
            logline(&format!(
                "SECURITY: no relay_api_key was configured — generated {key} and saved it to {}",
                paths.settings.display()
            ));
        }
    }

    let st = AppState::new(settings, paths);
    st.load_burned();
    st.load_lanes();

    let background = std::env::var("IP_RELAY_NO_BACKGROUND")
        .map(|v| !matches!(v.to_lowercase().as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(true);
    if background {
        let s1 = st.clone();
        tokio::spawn(async move { pool_mgr::pool_manager(s1).await });
        let s2 = st.clone();
        tokio::spawn(async move { pool_mgr::persist_loop(s2).await });
    }

    let app = Router::new()
        .route("/healthz", get(api::healthz))
        .route("/v1/models", get(api::list_models))
        .route("/v1/chat/completions", post(api::chat_completions))
        .route("/api/settings", get(api::settings_get).post(api::settings_post))
        .route("/api/stats", get(api::api_stats))
        .route("/api/pool", get(api::api_pool))
        .route("/api/pool/seed", post(api::pool_seed))
        .route("/api/refresh", post(api::api_refresh))
        .route("/api/burned", get(api::api_burned))
        .route("/api/burned/clear", post(api::api_burned_clear))
        .with_state(st.clone());

    let host = std::env::var("HOST").unwrap_or_else(|_| "127.0.0.1".into());
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8901);
    let addr = format!("{host}:{port}");
    let listener = match tokio::net::TcpListener::bind(&addr).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("zen-relay: cannot bind {addr}: {e}");
            std::process::exit(1);
        }
    };
    logline(&format!("zen-relay {VERSION} listening on {addr} (loopback only)"));
    if let Err(e) = axum::serve(listener, app).await {
        eprintln!("zen-relay: server error: {e}");
        std::process::exit(1);
    }
}
