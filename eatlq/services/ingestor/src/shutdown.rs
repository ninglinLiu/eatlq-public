use tokio_util::sync::CancellationToken;
use tracing::{info, warn};

pub async fn wait_for_shutdown_signal(token: CancellationToken) {
    match tokio::signal::ctrl_c().await {
        Ok(()) => {
            info!("received ctrl-c, shutting down");
            token.cancel();
        }
        Err(error) => {
            warn!(error = %error, "failed to register ctrl-c handler");
            token.cancel();
        }
    }
}
