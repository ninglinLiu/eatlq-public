pub mod backfill;
pub mod backfill_discovery;
pub mod backfill_scheduler;
pub mod backfill_state;
pub mod codec;
pub mod config;
pub mod endpoint_pool;
pub mod error;
pub mod logging;
pub mod pipeline;
pub mod pool_health;
pub mod redis_streams;
pub mod rpc;
pub mod shutdown;
pub mod stall_watchdog;
pub mod ws;

pub use error::AppError;

use config::AppConfig;
use pool_health::spawn_pool_health_reporter;
use stall_watchdog::spawn_stall_watchdog;
use tokio_util::sync::CancellationToken;

pub async fn run(config: AppConfig, shutdown_token: CancellationToken) -> Result<(), AppError> {
    spawn_pool_health_reporter(
        config.redis.clone(),
        config.ws.ws_pool.clone(),
        config.rpc.http_pool.clone(),
        shutdown_token.child_token(),
    );
    spawn_stall_watchdog(
        config.stall_secs,
        config.stall_grace_secs,
        shutdown_token.child_token(),
    );
    pipeline::run_pipeline(config, shutdown_token).await
}
