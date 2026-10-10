use ingestor::{backfill_discovery, config::AppConfig, logging, AppError};

#[tokio::main]
async fn main() -> Result<(), AppError> {
    logging::init()?;

    let config = AppConfig::from_env()?;
    backfill_discovery::run_gap_discovery(config).await
}
