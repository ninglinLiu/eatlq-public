use crate::{
    backfill_state::{BackfillRunPlan, BackfillStateStore},
    codec,
    config::{AppConfig, BackfillConfig, RpcConfig},
    error::AppError,
    redis_streams, rpc,
};
use eatlq_common::RawFrame;
use reqwest::Client;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use tracing::{info, warn};

pub async fn run_backfill(
    config: AppConfig,
    shutdown_token: CancellationToken,
) -> Result<(), AppError> {
    let backfill_config = config.backfill.clone();
    let source = backfill_config.effective_source(&config.ws.source);
    let chain_id = config.ws.chain_id;
    let rpc_config = config.rpc.clone();
    let redis_config = config.redis.clone();

    let (record_tx, record_rx) = mpsc::channel(config.pipeline.record_channel_capacity);
    let writer_token = shutdown_token.child_token();
    let writer_handle = tokio::spawn(async move {
        redis_streams::run_writer(redis_config, record_rx, writer_token).await
    });

    let producer_result = produce_backfill_records(
        rpc_config,
        backfill_config,
        record_tx,
        source,
        chain_id,
        shutdown_token.clone(),
    )
    .await;

    let writer_result = writer_handle.await?;

    producer_result?;
    writer_result?;
    Ok(())
}

async fn produce_backfill_records(
    rpc_config: RpcConfig,
    backfill_config: BackfillConfig,
    record_tx: mpsc::Sender<eatlq_common::IngestRecordV1>,
    source: String,
    chain_id: u64,
    shutdown_token: CancellationToken,
) -> Result<(), AppError> {
    let client = Client::builder()
        .timeout(rpc_config.request_timeout())
        .build()?;
    let default_to_block = resolve_default_to_block(&client, &rpc_config, &backfill_config).await?;
    let state_store = BackfillStateStore::connect_if_enabled(&backfill_config).await?;
    let run_plan = resolve_run_plan(
        state_store.as_ref(),
        &client,
        &rpc_config,
        &backfill_config,
        chain_id,
        &source,
        default_to_block,
    )
    .await?;
    run_backfill_with_plan_and_client(
        rpc_config,
        backfill_config,
        record_tx,
        source,
        chain_id,
        shutdown_token,
        client,
        state_store,
        run_plan,
    )
    .await
}

pub async fn run_backfill_with_plan(
    config: AppConfig,
    run_plan: BackfillRunPlan,
    shutdown_token: CancellationToken,
) -> Result<(), AppError> {
    let backfill_config = config.backfill.clone();
    let source = backfill_config.effective_source(&config.ws.source);
    let chain_id = config.ws.chain_id;
    let rpc_config = config.rpc.clone();
    let redis_config = config.redis.clone();

    let (record_tx, record_rx) = mpsc::channel(config.pipeline.record_channel_capacity);
    let writer_token = shutdown_token.child_token();
    let writer_handle = tokio::spawn(async move {
        redis_streams::run_writer(redis_config, record_rx, writer_token).await
    });

    let client = Client::builder()
        .timeout(rpc_config.request_timeout())
        .build()?;
    let state_store = BackfillStateStore::connect_if_enabled(&backfill_config).await?;

    let producer_result = run_backfill_with_plan_and_client(
        rpc_config,
        backfill_config,
        record_tx,
        source,
        chain_id,
        shutdown_token.clone(),
        client,
        state_store,
        run_plan,
    )
    .await;

    let writer_result = writer_handle.await?;

    producer_result?;
    writer_result?;
    Ok(())
}

async fn run_backfill_with_plan_and_client(
    rpc_config: RpcConfig,
    backfill_config: BackfillConfig,
    record_tx: mpsc::Sender<eatlq_common::IngestRecordV1>,
    source: String,
    chain_id: u64,
    shutdown_token: CancellationToken,
    client: Client,
    state_store: Option<BackfillStateStore>,
    run_plan: BackfillRunPlan,
) -> Result<(), AppError> {
    let from_block = run_plan.from_block;
    let to_block = run_plan.to_block;

    if to_block < from_block {
        warn!(
            from_block,
            to_block,
            confirmations = backfill_config.confirmations,
            "backfill target is behind start block, exiting without writes"
        );
        if let Some(store) = state_store.as_ref() {
            store.mark_completed(&run_plan, None).await?;
        }
        return Ok(());
    }

    info!(
        from_block,
        to_block,
        chunk_size = backfill_config.chunk_size,
        confirmations = backfill_config.confirmations,
        source,
        "starting backfill range scan"
    );

    let mut chunk_start = from_block;
    let mut last_completed_to_block = None;
    while chunk_start <= to_block {
        if shutdown_token.is_cancelled() {
            warn!(next_block = chunk_start, "backfill interrupted by shutdown");
            break;
        }

        let chunk_end = chunk_end(chunk_start, to_block, backfill_config.chunk_size);
        let logs = match rpc::fetch_logs(&client, &rpc_config, chunk_start, chunk_end).await {
            Ok(logs) => logs,
            Err(error) => {
                if let Some(store) = state_store.as_ref() {
                    store
                        .mark_failed(&run_plan, &error.to_string(), last_completed_to_block)
                        .await?;
                }
                return Err(error);
            }
        };
        let log_count = logs.len();

        for log in logs {
            let payload_text = synthesize_backfill_payload(chunk_start, chunk_end, &log).to_string();
            let record = codec::decode_raw_frame(
                RawFrame {
                    received_at_ms: now_millis(),
                    payload_text,
                },
                &source,
                chain_id,
            )?;

            record_tx
                .send(record)
                .await
                .map_err(|_| AppError::ChannelClosed("backfill record channel"))?;
        }

        info!(
            from_block = chunk_start,
            to_block = chunk_end,
            fetched_logs = log_count,
            "backfill chunk fetched via eth_getLogs"
        );

        last_completed_to_block = Some(chunk_end);
        if let Some(store) = state_store.as_ref() {
            store
                .record_chunk(&run_plan, chunk_start, chunk_end, log_count)
                .await?;
        }

        chunk_start = chunk_end.saturating_add(1);
        if !shutdown_token.is_cancelled() && backfill_config.pause_ms > 0 && chunk_start <= to_block {
            tokio::time::sleep(backfill_config.pause()).await;
        }
    }

    if let Some(store) = state_store.as_ref() {
        store
            .mark_completed(&run_plan, last_completed_to_block)
            .await?;
    }

    Ok(())
}

async fn resolve_default_to_block(
    client: &Client,
    rpc_config: &RpcConfig,
    backfill_config: &BackfillConfig,
) -> Result<u64, AppError> {
    match backfill_config.to_block {
        Some(to_block) => Ok(to_block),
        None => {
            let latest_block = rpc::fetch_latest_block_number(client, rpc_config).await?;
            Ok(latest_block.saturating_sub(backfill_config.confirmations))
        }
    }
}

async fn resolve_run_plan(
    state_store: Option<&BackfillStateStore>,
    _client: &Client,
    rpc_config: &RpcConfig,
    backfill_config: &BackfillConfig,
    chain_id: u64,
    source: &str,
    default_to_block: u64,
) -> Result<BackfillRunPlan, AppError> {
    if let Some(store) = state_store {
        return store
            .resolve_run_plan(
                backfill_config,
                rpc_config,
                chain_id,
                source,
                default_to_block,
            )
            .await;
    }

    let from_block = backfill_config
        .from_block
        .ok_or(AppError::MissingEnv("BACKFILL_FROM_BLOCK"))?;
    let to_block = backfill_config.to_block.unwrap_or(default_to_block);
    Ok(BackfillRunPlan {
        gap_id: 0,
        job_key: "stateless".to_owned(),
        from_block,
        to_block,
    })
}

fn chunk_end(start: u64, inclusive_end: u64, chunk_size: u64) -> u64 {
    let max_end = start.saturating_add(chunk_size.saturating_sub(1));
    max_end.min(inclusive_end)
}

fn synthesize_backfill_payload(from_block: u64, to_block: u64, log: &serde_json::Value) -> serde_json::Value {
    serde_json::json!({
        "jsonrpc": "2.0",
        "method": "eth_subscription",
        "params": {
            "subscription": format!("eth_getLogs:backfill:{from_block:#x}-{to_block:#x}"),
            "result": log,
        }
    })
}

fn now_millis() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};

    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunk_end_respects_inclusive_upper_bound() {
        assert_eq!(chunk_end(100, 120, 10), 109);
        assert_eq!(chunk_end(115, 120, 10), 120);
    }

    #[test]
    fn backfill_payload_marks_range() {
        let payload = synthesize_backfill_payload(
            16,
            31,
            &serde_json::json!({
                "address": "0x1111111111111111111111111111111111111111",
            }),
        );

        assert_eq!(
            payload["params"]["subscription"],
            "eth_getLogs:backfill:0x10-0x1f"
        );
        assert_eq!(
            payload["params"]["result"]["address"],
            "0x1111111111111111111111111111111111111111"
        );
    }

    #[test]
    fn resolves_chunk_range_without_overflow() {
        assert_eq!(chunk_end(u64::MAX - 1, u64::MAX, 10), u64::MAX);
    }
}
