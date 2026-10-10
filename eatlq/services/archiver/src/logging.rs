use crate::error::AppError;
use tracing_subscriber::{fmt, EnvFilter};

pub fn init() -> Result<(), AppError> {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));

    fmt()
        .with_env_filter(filter)
        .with_target(true)
        .compact()
        .try_init()
        .map_err(|error| AppError::InvalidPayload(error.to_string()))
}
