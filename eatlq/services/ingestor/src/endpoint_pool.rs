use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use serde_json::{json, Value};
use url::Url;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum EndpointTier {
    Day = 0,
    Month = 1,
}

impl EndpointTier {
    fn parse(raw: &str) -> Self {
        match raw.trim().to_ascii_lowercase().as_str() {
            "month" | "monthly" => Self::Month,
            _ => Self::Day,
        }
    }
}

#[derive(Debug, Clone)]
pub struct EndpointEntry {
    pub url: Url,
    pub tier: EndpointTier,
    pub provider: String,
    cooldown_until: Option<Instant>,
    ban_until: Option<Instant>,
    consecutive_failures: u32,
    last_success_at: Option<Instant>,
    last_failure_reason: Option<String>,
}

impl EndpointEntry {
    pub(crate) fn new(url: Url, tier: EndpointTier, provider: String) -> Self {
        Self {
            url,
            tier,
            provider,
            cooldown_until: None,
            ban_until: None,
            consecutive_failures: 0,
            last_success_at: None,
            last_failure_reason: None,
        }
    }

    fn is_available(&self, now: Instant) -> bool {
        if let Some(until) = self.ban_until {
            if now < until {
                return false;
            }
        }
        if let Some(until) = self.cooldown_until {
            if now < until {
                return false;
            }
        }
        true
    }

    fn mark_success(&mut self, now: Instant) {
        self.consecutive_failures = 0;
        self.cooldown_until = None;
        self.last_success_at = Some(now);
        self.last_failure_reason = None;
    }

    fn mark_failure(
        &mut self,
        now: Instant,
        reason: &str,
        cooldown: Duration,
        max_consecutive: u32,
        ban: Duration,
        auth_expired: bool,
    ) {
        self.consecutive_failures = self.consecutive_failures.saturating_add(1);
        self.last_failure_reason = Some(reason.to_owned());
        if auth_expired {
            self.ban_until = Some(now + ban);
            self.cooldown_until = Some(now + ban);
        } else {
            self.cooldown_until = Some(now + cooldown);
            if self.consecutive_failures >= max_consecutive {
                self.ban_until = Some(now + ban);
            }
        }
    }
}

#[derive(Debug, Clone)]
pub struct PoolPolicy {
    pub cooldown: Duration,
    pub ban: Duration,
    pub max_consecutive_failures: u32,
}

struct PoolState {
    entries: Vec<EndpointEntry>,
    active_index: Option<usize>,
    last_rotation_at: Option<Instant>,
    last_rotation_reason: Option<String>,
}

#[derive(Clone)]
pub struct EndpointPool {
    inner: Arc<Mutex<PoolState>>,
    policy: PoolPolicy,
    pool_kind: &'static str,
}

impl std::fmt::Debug for EndpointPool {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("EndpointPool")
            .field("pool_kind", &self.pool_kind)
            .field("len", &self.len())
            .finish()
    }
}

impl EndpointPool {
    pub fn new(entries: Vec<EndpointEntry>, policy: PoolPolicy, pool_kind: &'static str) -> Self {
        let active_index = if entries.is_empty() { None } else { Some(0) };
        Self {
            inner: Arc::new(Mutex::new(PoolState {
                entries,
                active_index,
                last_rotation_at: None,
                last_rotation_reason: None,
            })),
            policy,
            pool_kind,
        }
    }

    pub fn len(&self) -> usize {
        self.inner.lock().expect("pool mutex").entries.len()
    }

    pub fn select(&self) -> Option<(Url, String)> {
        let mut state = self.inner.lock().expect("pool mutex");
        if state.entries.is_empty() {
            return None;
        }
        let now = Instant::now();
        let mut best: Option<(usize, EndpointTier)> = None;
        for (idx, entry) in state.entries.iter().enumerate() {
            if !entry.is_available(now) {
                continue;
            }
            match best {
                None => best = Some((idx, entry.tier)),
                Some((_, tier)) if entry.tier < tier => best = Some((idx, entry.tier)),
                _ => {}
            }
        }
        let (idx, _) = best.or_else(|| {
            // All cooled down — pick least-bad (soonest cooldown expiry) as last resort.
            state
                .entries
                .iter()
                .enumerate()
                .min_by_key(|(_, e)| e.cooldown_until.unwrap_or(now))
                .map(|(i, e)| (i, e.tier))
        })?;
        state.active_index = Some(idx);
        let entry = &state.entries[idx];
        Some((entry.url.clone(), entry.provider.clone()))
    }

    pub fn mark_success(&self) {
        let mut state = self.inner.lock().expect("pool mutex");
        let Some(idx) = state.active_index else {
            return;
        };
        state.entries[idx].mark_success(Instant::now());
    }

    pub fn mark_failure(&self, reason: &str, rate_limited: bool, auth_expired: bool) {
        let mut state = self.inner.lock().expect("pool mutex");
        let Some(idx) = state.active_index else {
            return;
        };
        let cooldown = if rate_limited {
            self.policy.cooldown * 2
        } else {
            self.policy.cooldown
        };
        state.entries[idx].mark_failure(
            Instant::now(),
            reason,
            cooldown,
            self.policy.max_consecutive_failures,
            self.policy.ban,
            auth_expired,
        );
        state.last_rotation_at = Some(Instant::now());
        state.last_rotation_reason = Some(reason.to_owned());
    }

    pub fn rotate(&self, reason: &str) {
        let mut state = self.inner.lock().expect("pool mutex");
        state.last_rotation_at = Some(Instant::now());
        state.last_rotation_reason = Some(reason.to_owned());
        state.active_index = None;
    }

    pub fn health_snapshot(&self) -> Value {
        let state = self.inner.lock().expect("pool mutex");
        let now = Instant::now();
        let active = state.active_index.and_then(|idx| state.entries.get(idx));
        let cooled = state
            .entries
            .iter()
            .filter(|e| !e.is_available(now))
            .count();
        json!({
            "pool_kind": self.pool_kind,
            "total": state.entries.len(),
            "cooled_down": cooled,
            "active_url": active.map(|e| e.url.to_string()),
            "active_provider": active.map(|e| e.provider.clone()),
            "last_rotation_reason": state.last_rotation_reason,
            "last_rotation_ts_ms": state.last_rotation_at.map(|t| {
                unix_ms_now().saturating_sub(t.elapsed().as_millis() as u64)
            }),
        })
    }

    pub fn redis_health_fields(&self) -> Vec<(String, String)> {
        let snap = self.health_snapshot();
        vec![
            (format!("{}:total", self.pool_kind), snap["total"].to_string()),
            (
                format!("{}:cooled_down", self.pool_kind),
                snap["cooled_down"].to_string(),
            ),
            (
                format!("{}:active_provider", self.pool_kind),
                snap["active_provider"]
                    .as_str()
                    .unwrap_or("")
                    .to_owned(),
            ),
            (
                format!("{}:active_url", self.pool_kind),
                snap["active_url"].as_str().unwrap_or("").to_owned(),
            ),
            (
                format!("{}:last_rotation_reason", self.pool_kind),
                snap["last_rotation_reason"]
                    .as_str()
                    .unwrap_or("")
                    .to_owned(),
            ),
        ]
    }
}

pub fn parse_endpoint_pool_json(
    raw: &str,
    fallback_url: &str,
    fallback_provider: &str,
) -> Result<Vec<EndpointEntry>, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Ok(vec![EndpointEntry::new(
            Url::parse(fallback_url).map_err(|e| e.to_string())?,
            EndpointTier::Day,
            fallback_provider.to_owned(),
        )]);
    }
    let value: Value = serde_json::from_str(trimmed).map_err(|e| e.to_string())?;
    let arr = value
        .as_array()
        .ok_or_else(|| "pool JSON must be an array".to_owned())?;
    if arr.is_empty() {
        return Ok(vec![EndpointEntry::new(
            Url::parse(fallback_url).map_err(|e| e.to_string())?,
            EndpointTier::Day,
            fallback_provider.to_owned(),
        )]);
    }
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        let obj = item
            .as_object()
            .ok_or_else(|| "pool entry must be an object".to_owned())?;
        let url_str = obj
            .get("url")
            .and_then(Value::as_str)
            .ok_or_else(|| "pool entry missing url".to_owned())?;
        let tier = obj
            .get("tier")
            .and_then(Value::as_str)
            .map(EndpointTier::parse)
            .unwrap_or(EndpointTier::Day);
        let provider = obj
            .get("provider")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_owned();
        out.push(EndpointEntry::new(
            Url::parse(url_str).map_err(|e| e.to_string())?,
            tier,
            provider,
        ));
    }
    Ok(out)
}

fn unix_ms_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pool_with_two() -> EndpointPool {
        let entries = vec![
            EndpointEntry::new(
                Url::parse("wss://day.example/ws").unwrap(),
                EndpointTier::Day,
                "infura".to_owned(),
            ),
            EndpointEntry::new(
                Url::parse("wss://month.example/ws").unwrap(),
                EndpointTier::Month,
                "alchemy".to_owned(),
            ),
        ];
        EndpointPool::new(
            entries,
            PoolPolicy {
                cooldown: Duration::from_secs(30),
                ban: Duration::from_secs(3600),
                max_consecutive_failures: 3,
            },
            "ws",
        )
    }

    #[test]
    fn prefers_day_tier() {
        let pool = pool_with_two();
        let (_url, provider) = pool.select().expect("select");
        assert_eq!(provider, "infura");
    }

    #[test]
    fn cooldown_skips_endpoint() {
        let pool = pool_with_two();
        pool.mark_failure("429", true, false);
        let (_url, provider) = pool.select().expect("select");
        assert_eq!(provider, "alchemy");
    }

    #[test]
    fn parses_pool_json() {
        let raw = r#"[{"url":"wss://a/ws","tier":"day","provider":"drpc"}]"#;
        let entries = parse_endpoint_pool_json(raw, "wss://fallback/ws", "fb").expect("parse");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].provider, "drpc");
    }

    #[test]
    fn empty_pool_json_falls_back() {
        let entries =
            parse_endpoint_pool_json("", "wss://fallback/ws", "fb").expect("parse");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].provider, "fb");
    }
}
