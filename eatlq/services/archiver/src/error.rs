use thiserror::Error;

#[derive(Debug, Error)]
pub enum AppError {
    #[error("missing required environment variable: {0}")]
    MissingEnv(&'static str),

    #[error("invalid numeric environment variable {key}={value}")]
    InvalidNumber { key: &'static str, value: String },

    #[error("redis error: {0}")]
    Redis(#[from] redis::RedisError),

    #[error("clickhouse error: {0}")]
    ClickHouse(#[from] clickhouse::error::Error),

    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("messagepack decode error: {0}")]
    MsgPackDecode(#[from] rmp_serde::decode::Error),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("invalid stream payload: {0}")]
    InvalidPayload(String),

    #[error("task join error: {0}")]
    Join(#[from] tokio::task::JoinError),

    #[error("clickhouse insert timed out after {timeout_secs}s")]
    ClickHouseInsertTimeout { timeout_secs: u64 },
}
