use crate::error::AppError;
use std::{env, time::Duration};

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub redis: RedisConfig,
    pub clickhouse: ClickHouseConfig,
    pub archiver: ArchiverConfig,
}

#[derive(Debug, Clone)]
pub struct RedisConfig {
    pub url: String,
    /// One or more Redis streams (comma-separated in ARCHIVER_STREAMS).
    pub streams: Vec<String>,
    pub group: String,
    pub consumer: String,
    pub read_count: usize,
    pub block_ms: u64,
    pub pending_min_idle_ms: u64,
    pub pending_reclaim_count: usize,
    pub pending_reclaim_interval_ms: u64,
}

#[derive(Debug, Clone)]
pub struct ClickHouseConfig {
    pub url: String,
    pub database: String,
    pub user: String,
    pub password: String,
    pub table: String,
    pub insert_timeout_secs: u64,
}

#[derive(Debug, Clone)]
pub struct ArchiverConfig {
    pub batch_size: usize,
    pub batch_timeout_ms: u64,
    pub healthcheck_path: String,
    /// Exit process if no successful flush for this many seconds (docker restart).
    pub max_idle_flush_secs: u64,
    /// Cap startup PEL recovery batches per stream before entering main loop.
    pub startup_recover_max_batches: usize,
}

impl AppConfig {
    pub fn from_env() -> Result<Self, AppError> {
        let primary_stream = optional("MARKET_TICKS_STREAM", "market_ticks");
        let streams = parse_streams(
            &optional("ARCHIVER_STREAMS", &primary_stream),
            &primary_stream,
        );

        Ok(Self {
            redis: RedisConfig {
                url: optional("REDIS_URL", "redis://redis:6379/0"),
                streams,
                group: optional("ARCHIVER_GROUP", "archiver_group"),
                consumer: optional("ARCHIVER_CONSUMER", "archiver-1"),
                read_count: parse_usize(
                    "ARCHIVER_READ_COUNT",
                    &optional("ARCHIVER_READ_COUNT", "250"),
                )?,
                block_ms: parse_u64("ARCHIVER_BLOCK_MS", &optional("ARCHIVER_BLOCK_MS", "1000"))?,
                pending_min_idle_ms: parse_u64(
                    "ARCHIVER_PENDING_MIN_IDLE_MS",
                    &optional("ARCHIVER_PENDING_MIN_IDLE_MS", "60000"),
                )?,
                pending_reclaim_count: parse_usize(
                    "ARCHIVER_PENDING_RECLAIM_COUNT",
                    &optional("ARCHIVER_PENDING_RECLAIM_COUNT", "250"),
                )?,
                pending_reclaim_interval_ms: parse_u64(
                    "ARCHIVER_PENDING_RECLAIM_INTERVAL_MS",
                    &optional("ARCHIVER_PENDING_RECLAIM_INTERVAL_MS", "30000"),
                )?,
            },
            clickhouse: ClickHouseConfig {
                url: optional("CLICKHOUSE_URL", "http://clickhouse:8123"),
                database: optional("CLICKHOUSE_DB", "eatlq"),
                user: optional("CLICKHOUSE_USER", "default"),
                password: optional("CLICKHOUSE_PASSWORD", ""),
                table: optional("ARCHIVER_CLICKHOUSE_TABLE", "raw_evm_logs"),
                insert_timeout_secs: parse_u64(
                    "ARCHIVER_CLICKHOUSE_INSERT_TIMEOUT_SECS",
                    &optional("ARCHIVER_CLICKHOUSE_INSERT_TIMEOUT_SECS", "120"),
                )?,
            },
            archiver: ArchiverConfig {
                batch_size: parse_usize(
                    "ARCHIVER_BATCH_SIZE",
                    &optional("ARCHIVER_BATCH_SIZE", "1000"),
                )?,
                batch_timeout_ms: parse_u64(
                    "ARCHIVER_BATCH_TIMEOUT_MS",
                    &optional("ARCHIVER_BATCH_TIMEOUT_MS", "1000"),
                )?,
                healthcheck_path: optional("ARCHIVER_HEALTHCHECK_PATH", "/tmp/archiver_healthy"),
                max_idle_flush_secs: parse_u64(
                    "ARCHIVER_MAX_IDLE_FLUSH_SECS",
                    &optional("ARCHIVER_MAX_IDLE_FLUSH_SECS", "300"),
                )?,
                startup_recover_max_batches: parse_usize(
                    "ARCHIVER_STARTUP_RECOVER_MAX_BATCHES",
                    &optional("ARCHIVER_STARTUP_RECOVER_MAX_BATCHES", "200"),
                )?,
            },
        })
    }
}

impl ArchiverConfig {
    pub fn batch_timeout(&self) -> Duration {
        Duration::from_millis(self.batch_timeout_ms)
    }
}

impl RedisConfig {
    pub fn pending_reclaim_interval(&self) -> Duration {
        Duration::from_millis(self.pending_reclaim_interval_ms)
    }
}

fn parse_streams(raw: &str, fallback: &str) -> Vec<String> {
    let mut out: Vec<String> = raw
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect();
    if out.is_empty() {
        out.push(fallback.to_owned());
    }
    out
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

fn parse_usize(key: &'static str, value: &str) -> Result<usize, AppError> {
    value.parse::<usize>().map_err(|_| AppError::InvalidNumber {
        key,
        value: value.to_owned(),
    })
}
