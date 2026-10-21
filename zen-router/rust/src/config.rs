//! Settings: same keys, same file format, same semantics as the Python relay.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::RwLock;

/// These files hold secrets (`relay_api_key`, `upstream_api_key`,
/// `webshare_token`, and proxy `user:pass` pairs in `lanes.json`), so they are
/// written 0600 and never inherit the process umask.
pub fn write_private(path: &Path, body: &str) -> std::io::Result<()> {
    let tmp = path.with_extension("json.tmp");
    {
        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut f = opts.open(&tmp)?;
        f.write_all(body.as_bytes())?;
    }
    std::fs::rename(&tmp, path)?;
    restrict_permissions(path);
    Ok(())
}

/// Best-effort 0600 on an existing file (fixes installs created before this).
pub fn restrict_permissions(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if path.exists() {
            let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
        }
    }
    #[cfg(not(unix))]
    let _ = path;
}

pub const VERSION: &str = "1.2.0";
pub const DEFAULT_UA: &str = "opencode/latest/2.0.32/cli";

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub upstream_base_url: String,
    pub screen_url: String,
    pub upstream_api_key: String,
    pub relay_api_key: String,
    pub probe_model: String,
    /// Probe a lane with a real completion before promoting it.
    ///
    /// The provider's free tier allows roughly one completion per egress IP.
    /// A completion probe therefore *spends* the very allowance the lane is
    /// meant to serve, and the client's first real request then finds the IP
    /// already spent. With this off, a lane is promoted after the cheap
    /// screen check (the proxy can reach the upstream) and the client's own
    /// request is the first one through that IP.
    pub stage2_probe: bool,
    pub proxy_pool_target: u64,
    pub proxy_max_candidates: u64,
    pub proxy_test_concurrency: u64,
    pub proxy_probe_timeout: u64,
    pub relay_proxy_timeout: u64,
    pub relay_attempts: u64,
    pub lane_cooldown_sec: u64,
    pub lane_recover_sec: u64,
    pub direct_lane: bool,
    pub webshare_token: String,
    /// Add the Webshare *backbone* rotating entry: the gateway hands out a
    /// fresh residential exit IP on every new connection.
    pub webshare_rotating: bool,
    /// Optional country code for the rotating entry (e.g. "us").
    pub webshare_country: String,
    /// How long a failed rotating lane rests before it is tried again.
    pub rotating_cooldown_sec: u64,
    pub sources_enabled: bool,
    pub allow_socks: bool,
    pub lane_max_inflight: u64,
    pub max_lanes_per_subnet: u64,
    pub adaptive_concurrency: bool,
    pub persist_lanes: bool,
    pub upstream_user_agent: String,
    pub burn_memory: bool,
    pub burn_ttl_sec: u64,
    pub tor_enabled: bool,
    pub tor_lanes: u64,
    pub tor_socks_port: u64,
    pub lane_pin_count: u64,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            upstream_base_url: "https://opencode.ai/inference/openai/v1".into(),
            screen_url: "https://opencode.ai/zen/v1/models".into(),
            upstream_api_key: "public".into(),
            relay_api_key: String::new(),
            probe_model: "beam-free".into(),
            stage2_probe: true,
            proxy_pool_target: 30,
            proxy_max_candidates: 3000,
            proxy_test_concurrency: 60,
            proxy_probe_timeout: 25,
            relay_proxy_timeout: 40,
            relay_attempts: 6,
            lane_cooldown_sec: 3600,
            lane_recover_sec: 3600,
            direct_lane: true,
            webshare_token: String::new(),
            webshare_rotating: false,
            webshare_country: String::new(),
            rotating_cooldown_sec: 5,
            sources_enabled: true,
            allow_socks: true,
            lane_max_inflight: 2,
            max_lanes_per_subnet: 3,
            adaptive_concurrency: true,
            persist_lanes: true,
            upstream_user_agent: DEFAULT_UA.into(),
            burn_memory: true,
            burn_ttl_sec: 86400,
            tor_enabled: false,
            tor_lanes: 12,
            tor_socks_port: 9150,
            lane_pin_count: 3,
        }
    }
}

/// Clamped, hot-path view of the settings.
#[derive(Clone, Debug)]
pub struct Runtime {
    pub upstream_base: String,
    pub screen_url: String,
    pub api_keys: Vec<String>,
    pub relay_key: String,
    pub probe_model: String,
    pub stage2_probe: bool,
    pub ua: String,
    pub pool_target: usize,
    pub max_candidates: usize,
    pub test_concurrency: usize,
    pub probe_timeout: u64,
    pub relay_proxy_timeout: u64,
    pub attempts: u32,
    pub cooldown: f64,
    pub recover: f64,
    pub max_inflight: u32,
    pub max_per_subnet: usize,
    pub pin_count: usize,
    pub adaptive: bool,
    pub persist: bool,
    pub burn_memory: bool,
    pub ttl: f64,
    pub allow_socks: bool,
    pub direct: bool,
    pub webshare: String,
    pub webshare_rotating: bool,
    pub webshare_country: String,
    /// Rest period for a failed rotating lane (seconds).
    pub rotating_cooldown: f64,
    pub sources: bool,
}

impl Settings {
    pub fn rt(&self) -> Runtime {
        let base = self.upstream_base_url.trim().trim_end_matches('/').to_string();
        let raw_keys: Vec<String> = self
            .upstream_api_key
            .replace('\n', ",")
            .split(',')
            .map(|k| k.trim().to_string())
            .filter(|k| !k.is_empty())
            .collect();
        let mut ua = self.upstream_user_agent.trim().to_string();
        if !ua.to_lowercase().contains("opencode") {
            ua = DEFAULT_UA.to_string();
        }
        let mut screen = self.screen_url.trim().to_string();
        if !(screen.starts_with("http://") || screen.starts_with("https://")) {
            screen = "https://opencode.ai/zen/v1/models".into();
        }
        Runtime {
            upstream_base: base,
            screen_url: screen,
            api_keys: if raw_keys.is_empty() { vec!["public".into()] } else { raw_keys },
            relay_key: self.relay_api_key.trim().to_string(),
            probe_model: self.probe_model.clone(),
            stage2_probe: self.stage2_probe,
            ua,
            pool_target: (self.proxy_pool_target.max(1)) as usize,
            max_candidates: (self.proxy_max_candidates.max(10)) as usize,
            test_concurrency: (self.proxy_test_concurrency.max(1)) as usize,
            probe_timeout: self.proxy_probe_timeout.max(5),
            relay_proxy_timeout: self.relay_proxy_timeout.max(10),
            attempts: (self.relay_attempts.max(1).min(20)) as u32,
            cooldown: self.lane_cooldown_sec.max(30) as f64,
            recover: self.lane_recover_sec.max(60) as f64,
            max_inflight: self.lane_max_inflight.max(1) as u32,
            max_per_subnet: self.max_lanes_per_subnet as usize,
            pin_count: self.lane_pin_count.max(1) as usize,
            adaptive: self.adaptive_concurrency,
            persist: self.persist_lanes,
            burn_memory: self.burn_memory,
            ttl: self.burn_ttl_sec.max(60) as f64,
            allow_socks: self.allow_socks,
            direct: self.direct_lane,
            webshare: self.webshare_token.trim().to_string(),
            webshare_rotating: self.webshare_rotating,
            webshare_country: self.webshare_country.trim().to_lowercase(),
            rotating_cooldown: self.rotating_cooldown_sec.max(0) as f64,
            sources: self.sources_enabled,
        }
    }

    /// Set one key from raw JSON, coercing types; unknown keys are ignored.
    /// This mirrors the Python settings dict overlay (no bounds here).
    pub fn set_raw(&mut self, k: &str, v: &Value) {
        macro_rules! num {
            ($f:ident) => {
                if let Some(n) = v.as_u64() {
                    self.$f = n;
                } else if let Some(s) = v.as_str() {
                    if let Ok(n) = s.trim().parse::<u64>() {
                        self.$f = n;
                    }
                } else if let Some(n) = v.as_f64() {
                    if n >= 0.0 {
                        self.$f = n as u64;
                    }
                }
            };
        }
        macro_rules! boolean {
            ($f:ident) => {
                if let Some(b) = v.as_bool() {
                    self.$f = b;
                } else if let Some(s) = v.as_str() {
                    self.$f = matches!(s.to_lowercase().as_str(), "1" | "true" | "yes" | "on");
                }
            };
        }
        macro_rules! text {
            ($f:ident) => {
                if let Some(s) = v.as_str() {
                    self.$f = s.to_string();
                } else if !v.is_null() {
                    self.$f = v.to_string();
                }
            };
        }
        match k {
            "upstream_base_url" => text!(upstream_base_url),
            "screen_url" => text!(screen_url),
            "upstream_api_key" => text!(upstream_api_key),
            "relay_api_key" => text!(relay_api_key),
            "probe_model" => text!(probe_model),
            "stage2_probe" => boolean!(stage2_probe),
            "webshare_rotating" => boolean!(webshare_rotating),
            "webshare_country" => text!(webshare_country),
            "rotating_cooldown_sec" => num!(rotating_cooldown_sec),
            "proxy_pool_target" => num!(proxy_pool_target),
            "proxy_max_candidates" => num!(proxy_max_candidates),
            "proxy_test_concurrency" => num!(proxy_test_concurrency),
            "proxy_probe_timeout" => num!(proxy_probe_timeout),
            "relay_proxy_timeout" => num!(relay_proxy_timeout),
            "relay_attempts" => num!(relay_attempts),
            "lane_cooldown_sec" => num!(lane_cooldown_sec),
            "lane_recover_sec" => num!(lane_recover_sec),
            "direct_lane" => boolean!(direct_lane),
            "webshare_token" => text!(webshare_token),
            "sources_enabled" => boolean!(sources_enabled),
            "allow_socks" => boolean!(allow_socks),
            "lane_max_inflight" => num!(lane_max_inflight),
            "max_lanes_per_subnet" => num!(max_lanes_per_subnet),
            "adaptive_concurrency" => boolean!(adaptive_concurrency),
            "persist_lanes" => boolean!(persist_lanes),
            "upstream_user_agent" => text!(upstream_user_agent),
            "burn_memory" => boolean!(burn_memory),
            "burn_ttl_sec" => num!(burn_ttl_sec),
            "tor_enabled" => boolean!(tor_enabled),
            "tor_lanes" => num!(tor_lanes),
            "tor_socks_port" => num!(tor_socks_port),
            "lane_pin_count" => num!(lane_pin_count),
            _ => {}
        }
    }

    /// API-boundary validation: bounds + special rules, then set.
    pub fn validate_and_set(&mut self, k: &str, v: &Value) -> Result<(), String> {
        // An empty relay key means `check_auth` accepts every request, which
        // silently turns the relay into an open door to the upstream account.
        // Anonymous mode is an explicit startup choice (RELAY_ALLOW_ANONYMOUS),
        // never something the HTTP API can switch on.
        if k == "relay_api_key" {
            if let Some(s) = v.as_str() {
                if s.trim().is_empty() {
                    return Err(
                        "relay_api_key: cannot be empty — that disables authentication for every client (run with RELAY_ALLOW_ANONYMOUS=1 if that is what you want)".into(),
                    );
                }
            }
        }
        let bounds: Option<(u64, u64)> = match k {
            "proxy_pool_target" => Some((1, 500)),
            "proxy_max_candidates" => Some((100, 100000)),
            "proxy_test_concurrency" => Some((1, 500)),
            "proxy_probe_timeout" => Some((5, 120)),
            "relay_proxy_timeout" => Some((10, 300)),
            "relay_attempts" => Some((1, 20)),
            "lane_cooldown_sec" => Some((10, 86400)),
            "lane_recover_sec" => Some((30, 86400)),
            "lane_max_inflight" => Some((1, 32)),
            "max_lanes_per_subnet" => Some((0, 100)),
            "burn_ttl_sec" => Some((60, 2592000)),
            "tor_lanes" => Some((0, 200)),
            "tor_socks_port" => Some((1, 65535)),
            "lane_pin_count" => Some((1, 50)),
            _ => None,
        };
        if let Some((lo, hi)) = bounds {
            let iv = v
                .as_u64()
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<u64>().ok()))
                .or_else(|| v.as_f64().map(|f| f as u64));
            let iv = iv.ok_or_else(|| format!("{k}: must be a number"))?;
            if iv < lo || iv > hi {
                return Err(format!("{k}: must be between {lo} and {hi} (got {iv})"));
            }
            self.set_raw(k, v);
            return Ok(());
        }
        if k == "upstream_base_url" {
            if let Some(s) = v.as_str() {
                let t = s.trim();
                if !t.is_empty() && !t.to_lowercase().starts_with("http://") && !t.to_lowercase().starts_with("https://") {
                    return Err("upstream_base_url: must start with http:// or https://".into());
                }
            }
        }
        if k == "upstream_user_agent" {
            if let Some(s) = v.as_str() {
                if s.trim().is_empty() {
                    return Err("upstream_user_agent: cannot be empty".into());
                }
                if !s.to_lowercase().contains("opencode") {
                    return Err(
                        "upstream_user_agent: must contain 'opencode' — the free tier answers 429 to every other User-Agent".into(),
                    );
                }
            }
        }
        self.set_raw(k, v);
        Ok(())
    }

    /// Keys the API may update; masked secrets are skipped by the caller.
    pub const KEYS: &'static [&'static str] = &[
        "upstream_base_url", "screen_url", "upstream_api_key", "relay_api_key", "probe_model",
        "stage2_probe", "webshare_rotating", "webshare_country", "rotating_cooldown_sec",
        "proxy_pool_target", "proxy_max_candidates", "proxy_test_concurrency",
        "proxy_probe_timeout", "relay_proxy_timeout", "relay_attempts",
        "lane_cooldown_sec", "lane_recover_sec", "direct_lane", "webshare_token",
        "sources_enabled",
        "allow_socks", "lane_max_inflight", "max_lanes_per_subnet",
        "adaptive_concurrency", "persist_lanes", "upstream_user_agent",
        "burn_memory", "burn_ttl_sec", "tor_enabled", "tor_lanes",
        "tor_socks_port", "lane_pin_count",
    ];

    pub fn env_overlay(&mut self) {
        let pairs: &[(&str, &str)] = &[
            ("UPSTREAM_BASE_URL", "upstream_base_url"),
            ("SCREEN_URL", "screen_url"),
            ("UPSTREAM_API_KEY", "upstream_api_key"),
            ("RELAY_API_KEY", "relay_api_key"),
            ("PROBE_MODEL", "probe_model"),
            ("STAGE2_PROBE", "stage2_probe"),
            ("PROXY_POOL_TARGET", "proxy_pool_target"),
            ("PROXY_MAX_CANDIDATES", "proxy_max_candidates"),
            ("PROXY_TEST_CONCURRENCY", "proxy_test_concurrency"),
            ("PROXY_PROBE_TIMEOUT", "proxy_probe_timeout"),
            ("RELAY_PROXY_TIMEOUT", "relay_proxy_timeout"),
            ("RELAY_ATTEMPTS", "relay_attempts"),
            ("LANE_COOLDOWN_SEC", "lane_cooldown_sec"),
            ("LANE_RECOVER_SEC", "lane_recover_sec"),
            ("DIRECT_LANE", "direct_lane"),
            ("WEBSHARE_TOKEN", "webshare_token"),
            ("WEBSHARE_ROTATING", "webshare_rotating"),
            ("WEBSHARE_COUNTRY", "webshare_country"),
            ("ROTATING_COOLDOWN_SEC", "rotating_cooldown_sec"),
            ("SOURCES_ENABLED", "sources_enabled"),
            ("ALLOW_SOCKS", "allow_socks"),
            ("LANE_MAX_INFLIGHT", "lane_max_inflight"),
            ("MAX_LANES_PER_SUBNET", "max_lanes_per_subnet"),
            ("ADAPTIVE_CONCURRENCY", "adaptive_concurrency"),
            ("PERSIST_LANES", "persist_lanes"),
            ("UPSTREAM_USER_AGENT", "upstream_user_agent"),
            ("BURN_MEMORY", "burn_memory"),
            ("BURN_TTL_SEC", "burn_ttl_sec"),
            ("TOR_ENABLED", "tor_enabled"),
            ("TOR_LANES", "tor_lanes"),
            ("TOR_SOCKS_PORT", "tor_socks_port"),
            ("LANE_PIN_COUNT", "lane_pin_count"),
        ];
        for (env, key) in pairs {
            if let Ok(v) = std::env::var(env) {
                if v.is_empty() {
                    continue;
                }
                if matches!(
                    *key,
                    "direct_lane" | "allow_socks" | "adaptive_concurrency" | "persist_lanes"
                        | "burn_memory" | "tor_enabled"
                ) {
                    let b = matches!(v.to_lowercase().as_str(), "1" | "true" | "yes" | "on");
                    self.set_raw(key, &Value::Bool(b));
                } else {
                    self.set_raw(key, &Value::String(v));
                }
            }
        }
    }

    pub fn load(path: &Path) -> Settings {
        let mut s = Settings::default();
        // Base: environment (mirrors the Python defaults-vs-env layering).
        s.env_overlay();
        // Then the file overlays every key it carries.
        if let Ok(txt) = std::fs::read_to_string(path) {
            if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&txt) {
                for (k, v) in map {
                    if Settings::KEYS.contains(&k.as_str()) {
                        s.set_raw(&k, &v);
                    }
                }
            }
        }
        s
    }

    pub fn save(&self, path: &Path) -> Result<(), String> {
        let body = serde_json::to_string_pretty(self).map_err(|e| e.to_string())?;
        write_private(path, &body).map_err(|e| e.to_string())
    }

    pub fn public(&self) -> Value {
        let mask = |k: &str| -> Value {
            if k.is_empty() {
                Value::String("(none)".into())
            } else if k.len() > 6 {
                Value::String(format!("{}...", &k[..6]))
            } else {
                Value::String(format!("{k}..."))
            }
        };
        let keys = self
            .upstream_api_key
            .split(',')
            .map(|s| mask(s.trim()))
            .collect::<Vec<_>>();
        json!({
            "upstream_base_url": self.upstream_base_url,
            "upstream_api_key": Value::Array(keys),
            "relay_api_key": mask(&self.relay_api_key),
            "probe_model": self.probe_model,
            "stage2_probe": self.stage2_probe,
            "webshare_rotating": self.webshare_rotating,
            "webshare_country": self.webshare_country,
            "proxy_pool_target": self.proxy_pool_target,
            "proxy_max_candidates": self.proxy_max_candidates,
            "proxy_test_concurrency": self.proxy_test_concurrency,
            "proxy_probe_timeout": self.proxy_probe_timeout,
            "relay_proxy_timeout": self.relay_proxy_timeout,
            "relay_attempts": self.relay_attempts,
            "lane_cooldown_sec": self.lane_cooldown_sec,
            "lane_recover_sec": self.lane_recover_sec,
            "direct_lane": self.direct_lane,
            "webshare_token": mask(&self.webshare_token),
            "allow_socks": self.allow_socks,
            "lane_max_inflight": self.lane_max_inflight,
            "max_lanes_per_subnet": self.max_lanes_per_subnet,
            "adaptive_concurrency": self.adaptive_concurrency,
            "persist_lanes": self.persist_lanes,
            "upstream_user_agent": self.upstream_user_agent,
            "burn_memory": self.burn_memory,
            "burn_ttl_sec": self.burn_ttl_sec,
            "tor_enabled": self.tor_enabled,
            "tor_lanes": self.tor_lanes,
            "tor_socks_port": self.tor_socks_port,
            "lane_pin_count": self.lane_pin_count,
        })
    }
}

/// Files this process uses (env-configurable, like the Python relay).
pub struct Paths {
    pub settings: PathBuf,
    pub lanes: PathBuf,
    pub burned: PathBuf,
}

impl Paths {
    pub fn from_env() -> Paths {
        Paths {
            settings: PathBuf::from(std::env::var("SETTINGS_FILE").unwrap_or_else(|_| "settings.json".into())),
            lanes: PathBuf::from(std::env::var("LANES_FILE").unwrap_or_else(|_| "lanes.json".into())),
            burned: PathBuf::from(std::env::var("BURNED_FILE").unwrap_or_else(|_| "burned.json".into())),
        }
    }
}

pub type SettingsLock = RwLock<Settings>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_empty_relay_key() {        let mut s = Settings::default();
        let err = s
            .validate_and_set("relay_api_key", &json!(""))
            .unwrap_err();
        assert!(err.contains("cannot be empty"), "unexpected error: {err}");
        assert!(s.relay_api_key.is_empty(), "empty key must not be stored");
        assert!(s.validate_and_set("relay_api_key", &json!("   ")).is_err());
        assert!(s.validate_and_set("relay_api_key", &json!("rly_abc")).is_ok());
        assert_eq!(s.relay_api_key, "rly_abc");
    }

    #[test]
    fn stage2_probe_is_off_by_default_in_the_example_but_on_in_the_binary() {
        // The binary keeps the upstream behaviour (a lane is proven with a
        // real completion) unless the operator turns it off: the provider's
        // free tier allows roughly one completion per egress IP, so a probe
        // spends the allowance the lane exists to serve.
        let mut s = Settings::default();
        assert!(s.stage2_probe, "the default keeps the original probing");
        s.validate_and_set("stage2_probe", &json!(false)).unwrap();
        assert!(!s.stage2_probe);
        s.validate_and_set("stage2_probe", &json!("false")).unwrap();
        assert!(!s.stage2_probe, "a string flag must parse like a boolean");
        s.validate_and_set("stage2_probe", &json!(true)).unwrap();
        assert!(s.stage2_probe);
    }

    #[test]
    fn secret_files_are_owner_only() {
        let dir = std::env::temp_dir().join(format!(
            "zen-relay-perm-{}-{}",
            std::process::id(),
            now() as u64
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("settings.json");
        let s = Settings::default();
        s.save(&path).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600, "settings.json must not be group/world readable");
        }
        // an existing 0644 file is fixed in place
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
            restrict_permissions(&path);
            let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
        }
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn public_view_masks_every_secret() {
        let mut s = Settings::default();
        s.relay_api_key = "rly_0123456789abcdef0123456789abcdef".into();
        s.upstream_api_key = "sk-upstream-secret-value".into();
        s.webshare_token = "webshare-secret-token".into();
        let out = s.public().to_string();
        assert!(!out.contains("0123456789abcdef0123456789abcdef"), "relay key leaked");
        assert!(!out.contains("sk-upstream-secret-value"), "upstream key leaked");
        assert!(!out.contains("webshare-secret-token"), "webshare token leaked");
    }
}

pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}
