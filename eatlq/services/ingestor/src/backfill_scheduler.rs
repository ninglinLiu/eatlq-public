use crate::{
    backfill,
    backfill_state::BackfillStateStore,
    config::AppConfig,
    error::AppError,
};
use tokio_util::sync::CancellationToken;
use tracing::{info, warn};

pub async fn run_scheduler(
    config: AppConfig,
    shutdown_token: CancellationToken,
) -> Result<(), AppError> {
    let state_store = BackfillStateStore::connect_if_enabled(&config.backfill)
        .await?
        .ok_or(AppError::MissingEnv("BACKFILL_STATE_POSTGRES_URL"))?;
    let source = config.backfill.effective_source(&config.ws.source);
    let chain_id = config.ws.chain_id;

    info!(
        poll_interval_ms = config.backfill.scheduler_poll_interval_ms,
        job_name = config.backfill.job_name,
        source,
        "backfill scheduler started"
    );

    loop {
        if shutdown_token.is_cancelled() {
            break;
        }

        let maybe_plan = state_store
            .claim_next_pending_gap(
                &config.backfill,
                &config.rpc,
                chain_id,
                &source,
            )
            .await?;

        match maybe_plan {
            Some(plan) => {
                info!(
                    gap_id = plan.gap_id,
                    from_block = plan.from_block,
                    to_block = plan.to_block,
                    "scheduler claimed pending gap"
                );

                if let Err(error) = backfill::run_backfill_with_plan(
                    config.clone(),
                    plan,
                    shutdown_token.child_token(),
                )
                .await
                {
                    warn!(error = %error, "scheduler gap run failed");
                }
            }
            None => {
                tokio::select! {
                    _ = shutdown_token.cancelled() => {
                        break;
                    }
                    _ = tokio::time::sleep(config.backfill.scheduler_poll_interval()) => {}
                }
            }
        }
    }

    Ok(())
}
