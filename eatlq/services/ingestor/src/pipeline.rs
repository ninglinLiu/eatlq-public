use crate::{
    config::AppConfig,
    error::AppError,
    redis_streams,
    rpc,
    ws,
};
use eatlq_common::{IngestRecordV1, RawFrame};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

pub async fn run_pipeline(
    config: AppConfig,
    shutdown_token: CancellationToken,
) -> Result<(), AppError> {
    let (raw_tx, raw_rx) = mpsc::channel::<RawFrame>(config.pipeline.raw_channel_capacity);
    let (record_tx, record_rx) =
        mpsc::channel::<IngestRecordV1>(config.pipeline.record_channel_capacity);

    let ws_config = config.ws.clone();
    let rpc_config = config.rpc.clone();
    let rpc_poll_config = config.rpc.clone();
    let decode_source = config.ws.source.clone();
    let decode_chain_id = config.ws.chain_id;
    let redis_config = config.redis.clone();
    let raw_tx_http = raw_tx.clone();

    tokio::try_join!(
        ws::run_ws_reader(ws_config, raw_tx, shutdown_token.child_token()),
        rpc::run_http_head_poller(rpc_poll_config, raw_tx_http, shutdown_token.child_token()),
        rpc::run_logs_fetcher(
            rpc_config,
            raw_rx,
            record_tx,
            decode_source,
            decode_chain_id,
            config.backfill.state_postgres_url.clone(),
            shutdown_token.child_token()
        ),
        redis_streams::run_writer(redis_config, record_rx, shutdown_token.child_token()),
    )?;

    Ok(())
}
