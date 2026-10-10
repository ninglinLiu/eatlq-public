use tokio_util::sync::CancellationToken;
use tracing::{info, warn};

/// Wait for either an external OS signal (ctrl-c / SIGTERM) OR the pipeline
/// cancelling the shared token (e.g. the flush watchdog tripping).
///
/// CRITICAL: this MUST also resolve when `token` is cancelled. If it only
/// awaited `ctrl_c()`, then when the pipeline returns an error and `main`
/// calls `token.cancel()`, the spawned signal task would stay parked on
/// `ctrl_c()` forever and `signal_task.await` in `main` would deadlock — the
/// process never exits, Docker never restarts, and the archiver becomes a
/// "fake alive" zombie (root cause of the 2026-06-25 38h stall).
pub async fn wait_for_shutdown_signal(token: CancellationToken) {
    tokio::select! {
        _ = token.cancelled() => {
            info!("shutdown token cancelled (pipeline exited), signal handler returning");
        }
        result = wait_for_os_signal() => {
            match result {
                Ok(()) => info!("received shutdown signal, shutting down"),
                Err(error) => warn!(error = %error, "failed to register signal handler"),
            }
            // Propagate to the pipeline so it can flush + break cleanly.
            token.cancel();
        }
    }
}

/// Resolve on SIGINT (ctrl-c) or SIGTERM (docker stop / orchestrator).
async fn wait_for_os_signal() -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut sigterm = signal(SignalKind::terminate())?;
        tokio::select! {
            r = tokio::signal::ctrl_c() => r,
            _ = sigterm.recv() => Ok(()),
        }
    }
    #[cfg(not(unix))]
    {
        tokio::signal::ctrl_c().await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    /// Regression guard for the 2026-06-25 deadlock: when the pipeline cancels
    /// the shared token (e.g. flush watchdog tripping), the signal handler MUST
    /// return promptly instead of staying parked on ctrl_c() forever.
    #[tokio::test]
    async fn returns_promptly_when_token_cancelled() {
        let token = CancellationToken::new();
        token.cancel();
        let res = tokio::time::timeout(Duration::from_secs(2), wait_for_shutdown_signal(token)).await;
        assert!(res.is_ok(), "wait_for_shutdown_signal hung after token cancel (deadlock regression)");
    }

    /// Cancelling the token after the handler has started must also wake it.
    #[tokio::test]
    async fn returns_when_token_cancelled_after_start() {
        let token = CancellationToken::new();
        let child = token.clone();
        let handle = tokio::spawn(async move { wait_for_shutdown_signal(child).await });
        tokio::time::sleep(Duration::from_millis(50)).await;
        token.cancel();
        let res = tokio::time::timeout(Duration::from_secs(2), handle).await;
        assert!(res.is_ok(), "handler did not wake on post-start token cancel");
    }
}
