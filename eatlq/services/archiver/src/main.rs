use std::time::Duration;

use archiver::{config::AppConfig, logging, shutdown, AppError};
use tokio_util::sync::CancellationToken;
use tracing::{error, warn};

#[tokio::main]
async fn main() -> Result<(), AppError> {
    logging::init()?;

    let config = AppConfig::from_env()?;
    let shutdown_token = CancellationToken::new();
    let signal_token = shutdown_token.clone();

    let signal_task = tokio::spawn(async move {
        shutdown::wait_for_shutdown_signal(signal_token).await;
    });

    let result = archiver::run(config, shutdown_token.clone()).await;

    if let Err(error) = &result {
        // Most commonly the flush watchdog tripping. Log loudly; the external
        // infra watchdog turns this into a Telegram alert + restart.
        error!(error = %error, "archiver pipeline exited with error — process will terminate for docker restart");
    }

    // Wake the signal task and join it, but NEVER block forever: a hung join
    // must not turn a clean error-exit into a zombie. The OS reaps the task
    // when the process exits regardless.
    shutdown_token.cancel();
    if tokio::time::timeout(Duration::from_secs(5), signal_task)
        .await
        .is_err()
    {
        warn!("signal task did not join within 5s — exiting anyway");
    }

    result
}
