use thiserror::Error;

#[derive(Debug, Error)]
pub enum AppError {
    #[error("missing required environment variable: {0}")]
    MissingEnv(&'static str),

    #[error("invalid numeric environment variable {key}={value}")]
    InvalidNumber { key: &'static str, value: String },

    #[error("invalid json in environment variable {key}: {source}")]
    InvalidJsonEnv {
        key: &'static str,
        #[source]
        source: serde_json::Error,
    },

    #[error("websocket url parse error: {0}")]
    Url(#[from] url::ParseError),

    #[error("websocket error: {0}")]
    WebSocket(#[from] tokio_tungstenite::tungstenite::Error),

    #[error("redis error: {0}")]
    Redis(#[from] redis::RedisError),

    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("http error: {0}")]
    Http(#[from] reqwest::Error),

    #[error("postgres error: {0}")]
    Postgres(#[from] tokio_postgres::Error),

    #[error("messagepack encode error: {0}")]
    MsgPackEncode(#[from] rmp_serde::encode::Error),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("channel closed: {0}")]
    ChannelClosed(&'static str),

    #[error("invalid websocket payload: {0}")]
    InvalidPayload(String),

    #[error("rpc error: {0}")]
    Rpc(String),

    #[error("task join error: {0}")]
    Join(#[from] tokio::task::JoinError),
}
