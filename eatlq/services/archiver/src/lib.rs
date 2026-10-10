pub mod clickhouse_sink;
pub mod config;
pub mod error;
pub mod logging;
pub mod pipeline;
pub mod redis_streams;
pub mod shutdown;

pub use error::AppError;

use config::AppConfig;
use tokio_util::sync::CancellationToken;

pub async fn run(config: AppConfig, shutdown_token: CancellationToken) -> Result<(), AppError> {
    pipeline::run_pipeline(config, shutdown_token).await
}
