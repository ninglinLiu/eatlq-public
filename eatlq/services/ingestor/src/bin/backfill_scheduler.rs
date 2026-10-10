use ingestor::{backfill_scheduler, config::AppConfig, logging, shutdown, AppError};
use tokio_util::sync::CancellationToken;
use tracing::warn;

#[tokio::main]
async fn main() -> Result<(), AppError> {
    logging::init()?;

    if let Err(error) = rustls::crypto::ring::default_provider().install_default() {
        warn!(error = ?error, "rustls crypto provider was already installed");
    }

    let config = AppConfig::from_env()?;
    let shutdown_token = CancellationToken::new();
    let signal_token = shutdown_token.clone();

    let signal_task = tokio::spawn(async move {
        shutdown::wait_for_shutdown_signal(signal_token).await;
    });

    let result = backfill_scheduler::run_scheduler(config, shutdown_token.clone()).await;

    shutdown_token.cancel();
    let _ = signal_task.await;

    result
}
