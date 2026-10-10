//! Exit the process when realtime block ingestion stops advancing.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use tokio_util::sync::CancellationToken;
use tracing::error;

static LAST_BLOCK_PROGRESS_MS: AtomicU64 = AtomicU64::new(0);

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

/// Call after a block range is successfully fetched and logged.
pub fn touch_block_progress() {
    LAST_BLOCK_PROGRESS_MS.store(now_ms(), Ordering::Relaxed);
}

pub fn spawn_stall_watchdog(
    stale_secs: u64,
    grace_secs: u64,
    shutdown_token: CancellationToken,
) {
    let started_ms = now_ms();
    tokio::spawn(async move {
        let interval = std::time::Duration::from_secs(30);
        loop {
            tokio::select! {
                _ = shutdown_token.cancelled() => break,
                _ = tokio::time::sleep(interval) => {}
            }

            let now = now_ms();
            let last = LAST_BLOCK_PROGRESS_MS.load(Ordering::Relaxed);
            if last == 0 {
                if now.saturating_sub(started_ms) > grace_secs.saturating_mul(1000) {
                    error!(
                        grace_secs,
                        "ingestor stall: no block progress within startup grace — exiting for docker restart"
                    );
                    std::process::exit(1);
                }
                continue;
            }

            let idle_ms = now.saturating_sub(last);
            if idle_ms > stale_secs.saturating_mul(1000) {
                error!(
                    stale_secs,
                    idle_secs = idle_ms / 1000,
                    "ingestor stall: no successful eth_getLogs block fetch — exiting for docker restart"
                );
                std::process::exit(1);
            }
        }
    });
}
