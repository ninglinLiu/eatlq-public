use ingestor::{
    backfill_state::BackfillStateStore, config::AppConfig, logging, AppError,
};
use tracing::info;

#[tokio::main]
async fn main() -> Result<(), AppError> {
    logging::init()?;

    let config = AppConfig::from_env()?;
    let backfill_config = config.backfill.clone();
    let source = backfill_config.effective_source(&config.ws.source);
    let from_block = backfill_config
        .from_block
        .ok_or(AppError::MissingEnv("BACKFILL_FROM_BLOCK"))?;
    let to_block = backfill_config
        .to_block
        .ok_or(AppError::MissingEnv("BACKFILL_TO_BLOCK"))?;

    let store = BackfillStateStore::connect_if_enabled(&backfill_config)
        .await?
        .ok_or(AppError::MissingEnv("BACKFILL_STATE_POSTGRES_URL"))?;

    let plan = store
        .enqueue_gap(
            &backfill_config,
            &config.rpc,
            config.ws.chain_id,
            &source,
            from_block,
            to_block,
        )
        .await?;

    info!(
        gap_id = plan.gap_id,
        job_key = plan.job_key,
        from_block = plan.from_block,
        to_block = plan.to_block,
        "enqueued backfill gap range"
    );

    Ok(())
}
