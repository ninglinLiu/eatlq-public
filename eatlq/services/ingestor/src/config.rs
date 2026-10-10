use crate::endpoint_pool::{parse_endpoint_pool_json, EndpointPool, PoolPolicy};
use crate::error::AppError;
use serde_json::Value;
use std::{env, time::Duration};
use url::Url;

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub ws: WsConfig,
    pub rpc: RpcConfig,
    pub redis: RedisConfig,
    pub pipeline: PipelineConfig,
    pub backfill: BackfillConfig,
    pub stall_secs: u64,
    pub stall_grace_secs: u64,
}

#[derive(Debug, Clone)]
pub struct WsConfig {
    pub url: Url,
    pub ws_pool: EndpointPool,
    pub source: String,
    pub chain_id: u64,
    pub subscribe_method: String,
    pub subscribe_params: Value,
    pub reconnect_initial_backoff_ms: u64,
    pub reconnect_max_backoff_ms: u64,
    pub read_timeout_secs: u64,
    pub ws_idle_secs: u64,
    pub connect_timeout_secs: u64,
}

#[derive(Debug, Clone)]
pub struct RpcConfig {
    pub http_url: Url,
    pub http_pool: EndpointPool,
    pub log_filter: Value,
    pub request_timeout_secs: u64,
    pub head_poll_ms: u64,
    pub catchup_chunk: u64,
    pub catchup_max_blocks: u64,
    pub catchup_pause_ms: u64,
}

#[derive(Debug, Clone)]
pub struct RedisConfig {
    pub url: String,
    pub market_ticks_stream: String,
    pub market_ticks_maxlen: usize,
    pub batch_size: usize,
    pub batch_timeout_ms: u64,
    pub healthcheck_path: String,
}

#[derive(Debug, Clone)]
pub struct PipelineConfig {
    pub raw_channel_capacity: usize,
    pub record_channel_capacity: usize,
}

#[derive(Debug, Clone)]
pub struct BackfillConfig {
    pub from_block: Option<u64>,
    pub to_block: Option<u64>,
    pub chunk_size: u64,
    pub confirmations: u64,
    pub pause_ms: u64,
    pub source_suffix: String,
    pub state_postgres_url: Option<String>,
    pub job_name: String,
    pub auto_claim_pending_gap: bool,
    pub scheduler_poll_interval_ms: u64,
    pub gap_discovery: GapDiscoveryConfig,
}

#[derive(Debug, Clone)]
pub struct GapDiscoveryConfig {
    pub clickhouse_url: Option<String>,
    pub clickhouse_database: String,
    pub clickhouse_user: String,
    pub clickhouse_password: String,
    pub clickhouse_table: String,
    pub from_block: Option<u64>,
    pub to_block: Option<u64>,
    pub min_gap_size: u64,
    pub max_gap_size: u64,
    pub include_edge_gaps: bool,
}

impl AppConfig {
    pub fn from_env() -> Result<Self, AppError> {
        let ws_url = required("EVM_WS_URL")?;
        let subscribe_params_raw = optional("WS_SUBSCRIBE_PARAMS_JSON", r#"["newHeads"]"#);
        let http_url = match env::var("EVM_HTTP_URL") {
            Ok(value) if !value.trim().is_empty() => value,
            _ => default_http_url(&ws_url)?,
        };
        let log_filter_raw = match env::var("LOG_FILTER_JSON") {
            Ok(value) if !value.trim().is_empty() => value,
            _ => derive_log_filter_json(&subscribe_params_raw)?,
        };

        let ws_source = optional("WS_SOURCE", "quicknode");
        let pool_policy = pool_policy_from_env()?;
        let ws_pool_entries = parse_endpoint_pool_json(
            &optional("EVM_WS_URLS_POOL", ""),
            &ws_url,
            &ws_source,
        )
        .map_err(|message| AppError::InvalidPayload(format!("EVM_WS_URLS_POOL: {message}")))?;
        let ws_pool = EndpointPool::new(ws_pool_entries, pool_policy.clone(), "ws");

        let http_pool_entries = parse_endpoint_pool_json(
            &optional("EVM_HTTP_URLS_POOL", ""),
            &http_url,
            &ws_source,
        )
        .map_err(|message| AppError::InvalidPayload(format!("EVM_HTTP_URLS_POOL: {message}")))?;
        let http_pool = EndpointPool::new(http_pool_entries, pool_policy, "http");

        Ok(Self {
            ws: WsConfig {
                url: Url::parse(&ws_url)?,
                ws_pool,
                source: ws_source,
                chain_id: parse_u64("CHAIN_ID", &optional("CHAIN_ID", "1"))?,
                subscribe_method: optional("WS_SUBSCRIBE_METHOD", "eth_subscribe"),
                subscribe_params: serde_json::from_str(&subscribe_params_raw).map_err(|source| {
                    AppError::InvalidJsonEnv {
                        key: "WS_SUBSCRIBE_PARAMS_JSON",
                        source,
                    }
                })?,
                reconnect_initial_backoff_ms: parse_u64(
                    "WS_RECONNECT_INITIAL_BACKOFF_MS",
                    &optional("WS_RECONNECT_INITIAL_BACKOFF_MS", "1000"),
                )?,
                reconnect_max_backoff_ms: parse_u64(
                    "WS_RECONNECT_MAX_BACKOFF_MS",
                    &optional("WS_RECONNECT_MAX_BACKOFF_MS", "30000"),
                )?,
                read_timeout_secs: parse_u64(
                    "WS_READ_TIMEOUT_SECS",
                    &optional("WS_READ_TIMEOUT_SECS", "30"),
                )?,
                ws_idle_secs: parse_u64(
                    "INGESTOR_WS_IDLE_SECS",
                    &optional("INGESTOR_WS_IDLE_SECS", "120"),
                )?,
                connect_timeout_secs: parse_u64(
                    "WS_CONNECT_TIMEOUT_SECS",
                    &optional("WS_CONNECT_TIMEOUT_SECS", "10"),
                )?,
            },
            rpc: RpcConfig {
                http_url: Url::parse(&http_url)?,
                http_pool,
                log_filter: serde_json::from_str(&log_filter_raw).map_err(|source| {
                    AppError::InvalidJsonEnv {
                        key: "LOG_FILTER_JSON",
                        source,
                    }
                })?,
                request_timeout_secs: parse_u64(
                    "RPC_REQUEST_TIMEOUT_SECS",
                    &optional("RPC_REQUEST_TIMEOUT_SECS", "30"),
                )?,
                head_poll_ms: parse_u64(
                    "INGESTOR_HTTP_HEAD_POLL_MS",
                    &optional("INGESTOR_HTTP_HEAD_POLL_MS", "2000"),
                )?,
                catchup_chunk: parse_non_zero_u64(
                    "INGESTOR_CATCHUP_CHUNK",
                    &optional("INGESTOR_CATCHUP_CHUNK", "20"),
                )?,
                catchup_max_blocks: parse_non_zero_u64(
                    "INGESTOR_CATCHUP_MAX_BLOCKS",
                    &optional("INGESTOR_CATCHUP_MAX_BLOCKS", "300"),
                )?,
                catchup_pause_ms: parse_u64(
                    "INGESTOR_CATCHUP_PAUSE_MS",
                    &optional("INGESTOR_CATCHUP_PAUSE_MS", "50"),
                )?,
            },
            redis: RedisConfig {
                url: optional("REDIS_URL", "redis://redis:6379/"),
                market_ticks_stream: optional("MARKET_TICKS_STREAM", "market_ticks"),
                market_ticks_maxlen: parse_usize(
                    "MARKET_TICKS_MAXLEN",
                    &optional("MARKET_TICKS_MAXLEN", "200000"),
                )?,
                batch_size: parse_usize("REDIS_BATCH_SIZE", &optional("REDIS_BATCH_SIZE", "250"))?,
                batch_timeout_ms: parse_u64(
                    "REDIS_BATCH_TIMEOUT_MS",
                    &optional("REDIS_BATCH_TIMEOUT_MS", "1000"),
                )?,
                healthcheck_path: optional("INGESTOR_HEALTHCHECK_PATH", "/tmp/ingestor_healthy"),
            },
            pipeline: PipelineConfig {
                raw_channel_capacity: parse_usize(
                    "RAW_CHANNEL_CAPACITY",
                    &optional("RAW_CHANNEL_CAPACITY", "4096"),
                )?,
                record_channel_capacity: parse_usize(
                    "RECORD_CHANNEL_CAPACITY",
                    &optional("RECORD_CHANNEL_CAPACITY", "4096"),
                )?,
            },
            backfill: BackfillConfig {
                from_block: optional_u64("BACKFILL_FROM_BLOCK")?,
                to_block: optional_u64("BACKFILL_TO_BLOCK")?,
                chunk_size: parse_non_zero_u64(
                    "BACKFILL_CHUNK_SIZE",
                    &optional("BACKFILL_CHUNK_SIZE", "100"),
                )?,
                confirmations: parse_u64(
                    "BACKFILL_CONFIRMATIONS",
                    &optional("BACKFILL_CONFIRMATIONS", "6"),
                )?,
                pause_ms: parse_u64("BACKFILL_PAUSE_MS", &optional("BACKFILL_PAUSE_MS", "0"))?,
                source_suffix: optional("BACKFILL_SOURCE_SUFFIX", "backfill"),
                state_postgres_url: optional_non_empty("BACKFILL_STATE_POSTGRES_URL"),
                job_name: optional("BACKFILL_JOB_NAME", "default"),
                auto_claim_pending_gap: parse_bool(
                    "BACKFILL_AUTO_CLAIM_PENDING_GAP",
                    &optional("BACKFILL_AUTO_CLAIM_PENDING_GAP", "true"),
                )?,
                scheduler_poll_interval_ms: parse_u64(
                    "BACKFILL_SCHEDULER_POLL_INTERVAL_MS",
                    &optional("BACKFILL_SCHEDULER_POLL_INTERVAL_MS", "30000"),
                )?,
                gap_discovery: GapDiscoveryConfig {
                    clickhouse_url: optional_non_empty("BACKFILL_DISCOVERY_CLICKHOUSE_URL")
                        .or_else(|| optional_non_empty("CLICKHOUSE_URL")),
                    clickhouse_database: optional("BACKFILL_DISCOVERY_CLICKHOUSE_DB", "eatlq"),
                    clickhouse_user: optional("BACKFILL_DISCOVERY_CLICKHOUSE_USER", "default"),
                    clickhouse_password: optional("BACKFILL_DISCOVERY_CLICKHOUSE_PASSWORD", ""),
                    clickhouse_table: optional("BACKFILL_DISCOVERY_CLICKHOUSE_TABLE", "raw_evm_logs"),
                    from_block: optional_u64("BACKFILL_DISCOVERY_FROM_BLOCK")?,
                    to_block: optional_u64("BACKFILL_DISCOVERY_TO_BLOCK")?,
                    min_gap_size: parse_non_zero_u64(
                        "BACKFILL_DISCOVERY_MIN_GAP_SIZE",
                        &optional("BACKFILL_DISCOVERY_MIN_GAP_SIZE", "1"),
                    )?,
                    max_gap_size: parse_non_zero_u64(
                        "BACKFILL_DISCOVERY_MAX_GAP_SIZE",
                        &optional("BACKFILL_DISCOVERY_MAX_GAP_SIZE", "20"),
                    )?,
                    include_edge_gaps: parse_bool(
                        "BACKFILL_DISCOVERY_INCLUDE_EDGE_GAPS",
                        &optional("BACKFILL_DISCOVERY_INCLUDE_EDGE_GAPS", "false"),
                    )?,
                },
            },
            stall_secs: parse_u64(
                "INGESTOR_STALL_SECS",
                &optional("INGESTOR_STALL_SECS", "300"),
            )?,
            stall_grace_secs: parse_u64(
                "INGESTOR_STALL_GRACE_SECS",
                &optional("INGESTOR_STALL_GRACE_SECS", "120"),
            )?,
        })
    }
}

impl WsConfig {
    pub fn subscribe_payload(&self) -> Value {
        serde_json::json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": self.subscribe_method,
            "params": self.subscribe_params,
        })
    }

    pub fn reconnect_initial_backoff(&self) -> Duration {
        Duration::from_millis(self.reconnect_initial_backoff_ms)
    }

    pub fn reconnect_max_backoff(&self) -> Duration {
        Duration::from_millis(self.reconnect_max_backoff_ms)
    }

    pub fn read_timeout(&self) -> Duration {
        Duration::from_secs(self.read_timeout_secs)
    }

    pub fn ws_idle(&self) -> Duration {
        Duration::from_secs(self.ws_idle_secs)
    }

    pub fn connect_timeout(&self) -> Duration {
        Duration::from_secs(self.connect_timeout_secs)
    }
}

impl RpcConfig {
    pub fn request_timeout(&self) -> Duration {
        Duration::from_secs(self.request_timeout_secs)
    }
}

impl RedisConfig {
    pub fn batch_timeout(&self) -> Duration {
        Duration::from_millis(self.batch_timeout_ms)
    }
}

impl BackfillConfig {
    pub fn effective_source(&self, base_source: &str) -> String {
        let suffix = self.source_suffix.trim();
        if suffix.is_empty() {
            return base_source.to_owned();
        }

        format!("{base_source}-{suffix}")
    }

    pub fn pause(&self) -> Duration {
        Duration::from_millis(self.pause_ms)
    }

    pub fn state_enabled(&self) -> bool {
        self.state_postgres_url.is_some()
    }

    pub fn scheduler_poll_interval(&self) -> Duration {
        Duration::from_millis(self.scheduler_poll_interval_ms)
    }
}

impl GapDiscoveryConfig {
    pub fn require_clickhouse_url(&self) -> Result<&str, AppError> {
        self.clickhouse_url
            .as_deref()
            .ok_or(AppError::MissingEnv("BACKFILL_DISCOVERY_CLICKHOUSE_URL"))
    }
}

fn pool_policy_from_env() -> Result<PoolPolicy, AppError> {
    Ok(PoolPolicy {
        cooldown: Duration::from_secs(parse_u64(
            "INGESTOR_POOL_COOLDOWN_SECS",
            &optional("INGESTOR_POOL_COOLDOWN_SECS", "60"),
        )?),
        ban: Duration::from_secs(parse_u64(
            "INGESTOR_POOL_BAN_SECS",
            &optional("INGESTOR_POOL_BAN_SECS", "604800"),
        )?),
        max_consecutive_failures: parse_u64(
            "INGESTOR_POOL_MAX_CONSECUTIVE_FAILS",
            &optional("INGESTOR_POOL_MAX_CONSECUTIVE_FAILS", "5"),
        )? as u32,
    })
}

fn required(key: &'static str) -> Result<String, AppError> {
    env::var(key).map_err(|_| AppError::MissingEnv(key))
}

fn optional(key: &'static str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_owned())
}

fn parse_u64(key: &'static str, value: &str) -> Result<u64, AppError> {
    value.parse::<u64>().map_err(|_| AppError::InvalidNumber {
        key,
        value: value.to_owned(),
    })
}

fn parse_non_zero_u64(key: &'static str, value: &str) -> Result<u64, AppError> {
    let parsed = parse_u64(key, value)?;
    if parsed == 0 {
        return Err(AppError::InvalidNumber {
            key,
            value: value.to_owned(),
        });
    }

    Ok(parsed)
}

fn parse_bool(key: &'static str, value: &str) -> Result<bool, AppError> {
    match value.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" => Ok(false),
        _ => Err(AppError::InvalidNumber {
            key,
            value: value.to_owned(),
        }),
    }
}

fn parse_usize(key: &'static str, value: &str) -> Result<usize, AppError> {
    value.parse::<usize>().map_err(|_| AppError::InvalidNumber {
        key,
        value: value.to_owned(),
    })
}

fn optional_u64(key: &'static str) -> Result<Option<u64>, AppError> {
    match env::var(key) {
        Ok(value) => {
            let trimmed = value.trim();
            if trimmed.is_empty() {
                Ok(None)
            } else {
                parse_u64(key, trimmed).map(Some)
            }
        }
        Err(_) => Ok(None),
    }
}

fn optional_non_empty(key: &'static str) -> Option<String> {
    env::var(key).ok().and_then(|value| {
        let trimmed = value.trim();
        if trimmed.is_empty() {
            None
        } else {
            Some(trimmed.to_owned())
        }
    })
}

fn default_http_url(ws_url: &str) -> Result<String, AppError> {
    let mut url = Url::parse(ws_url)?;
    let next_scheme = match url.scheme() {
        "wss" => "https",
        "ws" => "http",
        other => {
            return Err(AppError::InvalidPayload(format!(
                "unsupported websocket url scheme for EVM_WS_URL: {other}"
            )))
        }
    };
    url.set_scheme(next_scheme)
        .map_err(|_| AppError::InvalidPayload("failed to derive EVM_HTTP_URL".to_owned()))?;
    Ok(url.to_string())
}

fn derive_log_filter_json(subscribe_params_raw: &str) -> Result<String, AppError> {
    let subscribe_params: Value =
        serde_json::from_str(subscribe_params_raw).map_err(|source| AppError::InvalidJsonEnv {
            key: "WS_SUBSCRIBE_PARAMS_JSON",
            source,
        })?;

    let Some(params) = subscribe_params.as_array() else {
        return Ok("{}".to_owned());
    };

    if params.first().and_then(Value::as_str) != Some("logs") {
        return Ok("{}".to_owned());
    }

    Ok(params
        .get(1)
        .cloned()
        .unwrap_or_else(|| serde_json::json!({}))
        .to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn derives_http_url_from_websocket_url() {
        let http_url = default_http_url("wss://example.com/path").expect("http url");
        assert_eq!(http_url, "https://example.com/path");
    }

    #[test]
    fn derives_log_filter_from_legacy_logs_subscription() {
        let filter = derive_log_filter_json(r#"["logs", {"topics": [["0xtopic0"]]}]"#)
            .expect("filter should derive");

        assert_eq!(filter, r#"{"topics":[["0xtopic0"]]}"#);
    }

    #[test]
    fn backfill_effective_source_appends_suffix() {
        let config = BackfillConfig {
            from_block: Some(1),
            to_block: None,
            chunk_size: 100,
            confirmations: 6,
            pause_ms: 0,
            source_suffix: "backfill".to_owned(),
            state_postgres_url: None,
            job_name: "default".to_owned(),
            auto_claim_pending_gap: true,
            scheduler_poll_interval_ms: 30_000,
            gap_discovery: GapDiscoveryConfig {
                clickhouse_url: Some("http://clickhouse:8123".to_owned()),
                clickhouse_database: "eatlq".to_owned(),
                clickhouse_user: "default".to_owned(),
                clickhouse_password: String::new(),
                clickhouse_table: "raw_evm_logs".to_owned(),
                from_block: None,
                to_block: None,
                min_gap_size: 1,
                max_gap_size: 20,
                include_edge_gaps: false,
            },
        };

        assert_eq!(config.effective_source("quicknode"), "quicknode-backfill");
    }

    #[test]
    fn backfill_effective_source_can_skip_suffix() {
        let config = BackfillConfig {
            from_block: Some(1),
            to_block: None,
            chunk_size: 100,
            confirmations: 6,
            pause_ms: 0,
            source_suffix: "   ".to_owned(),
            state_postgres_url: None,
            job_name: "default".to_owned(),
            auto_claim_pending_gap: true,
            scheduler_poll_interval_ms: 30_000,
            gap_discovery: GapDiscoveryConfig {
                clickhouse_url: Some("http://clickhouse:8123".to_owned()),
                clickhouse_database: "eatlq".to_owned(),
                clickhouse_user: "default".to_owned(),
                clickhouse_password: String::new(),
                clickhouse_table: "raw_evm_logs".to_owned(),
                from_block: None,
                to_block: None,
                min_gap_size: 1,
                max_gap_size: 20,
                include_edge_gaps: false,
            },
        };

        assert_eq!(config.effective_source("quicknode"), "quicknode");
    }

    #[test]
    fn parses_bool_variants() {
        assert!(parse_bool("FLAG", "true").expect("true"));
        assert!(!parse_bool("FLAG", "off").expect("off"));
    }
}
