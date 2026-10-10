use crate::{codec, config::RedisConfig, error::AppError};
use eatlq_common::IngestRecordV1;
use redis::Client;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use tracing::{debug, info};

pub async fn run_writer(
    config: RedisConfig,
    mut record_rx: mpsc::Receiver<IngestRecordV1>,
    shutdown_token: CancellationToken,
) -> Result<(), AppError> {
    let client = Client::open(config.url.as_str())?;
    let mut connection = client.get_multiplexed_async_connection().await?;
    let mut flush_tick = tokio::time::interval(config.batch_timeout());
    let mut batch = Vec::with_capacity(config.batch_size);

    loop {
        tokio::select! {
            _ = shutdown_token.cancelled() => {
                flush_records(&config, &mut connection, &mut batch).await?;
                break;
            }
            _ = flush_tick.tick() => {
                flush_records(&config, &mut connection, &mut batch).await?;
            }
            maybe_record = record_rx.recv() => {
                let Some(record) = maybe_record else {
                    flush_records(&config, &mut connection, &mut batch).await?;
                    break;
                };

                batch.push(record);
                if batch.len() >= config.batch_size {
                    flush_records(&config, &mut connection, &mut batch).await?;
                }
            }
        }
    }

    Ok(())
}

async fn flush_records(
    config: &RedisConfig,
    connection: &mut redis::aio::MultiplexedConnection,
    batch: &mut Vec<IngestRecordV1>,
) -> Result<(), AppError> {
    if batch.is_empty() {
        // Periodic tick with no matching logs: still prove the writer loop is alive.
        let _ = update_healthcheck_file(&config.healthcheck_path);
        return Ok(());
    }

    let flushed = batch.len();
    let mut encoded_records = Vec::with_capacity(batch.len());

    for record in batch.iter() {
        encoded_records.push((codec::encode_msgpack(record)?, record));
    }

    for (payload_msgpack, record) in encoded_records {
        // LASZLO contract: single field `data` carries the msgpack blob.
        // The previous extra top-level metadata fields (version/chain_id/source/
        // received_at_ms/address/tx_hash) are redundant with the msgpack payload
        // and are omitted to match the upstream contract.
        // Python consumer field order: docs/market-ticks-stream-contract.md
        let _: String = redis::cmd("XADD")
            .arg(&config.market_ticks_stream)
            .arg("MAXLEN")
            .arg("~")
            .arg(config.market_ticks_maxlen)
            .arg("*")
            .arg("data")
            .arg(payload_msgpack)
            .arg("tx_from")
            .arg(record.tx_from.as_deref().unwrap_or(""))
            .query_async(connection)
            .await?;
    }

    update_healthcheck_file(&config.healthcheck_path)?;
    batch.clear();

    info!(
        stream = %config.market_ticks_stream,
        flushed_records = flushed,
        "flushed records to redis stream"
    );

    Ok(())
}

fn update_healthcheck_file(path: &str) -> Result<(), AppError> {
    let timestamp = format!("{}", now_millis());
    std::fs::write(path, timestamp)?;
    debug!(path, "updated ingestor healthcheck file");
    Ok(())
}

fn now_millis() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};

    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}
