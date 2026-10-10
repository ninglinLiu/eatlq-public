use crate::{config::RedisConfig, error::AppError};
use eatlq_common::IngestRecordV1;
use redis::{
    streams::{StreamAutoClaimReply, StreamId, StreamReadReply},
    Client, FromRedisValue, Value,
};
use tracing::{debug, info, warn};

#[derive(Debug)]
pub struct StreamEntry {
    pub stream: String,
    pub record: Option<IngestRecordV1>,
    pub ack_id: String,
}

#[derive(Debug)]
pub struct StreamBatch {
    pub entries: Vec<StreamEntry>,
}

#[derive(Debug)]
pub struct PendingClaimBatch {
    pub batch: StreamBatch,
    pub next_start_id: String,
    pub deleted_ids: Vec<String>,
}

pub async fn connect(config: &RedisConfig) -> Result<redis::aio::MultiplexedConnection, AppError> {
    let client = Client::open(config.url.as_str())?;
    let connection = client.get_multiplexed_async_connection().await?;
    Ok(connection)
}

pub async fn ensure_consumer_group(
    connection: &mut redis::aio::MultiplexedConnection,
    config: &RedisConfig,
) -> Result<(), AppError> {
    for stream in &config.streams {
        let result: redis::RedisResult<String> = redis::cmd("XGROUP")
            .arg("CREATE")
            .arg(stream)
            .arg(&config.group)
            .arg("0")
            .arg("MKSTREAM")
            .query_async(connection)
            .await;

        match result {
            Ok(_) => {
                info!(stream = %stream, group = %config.group, "created redis consumer group");
            }
            Err(error) if error.code() == Some("BUSYGROUP") => {
                debug!(stream = %stream, group = %config.group, "redis consumer group already exists");
            }
            Err(error) => return Err(AppError::Redis(error)),
        }
    }
    Ok(())
}

pub async fn read_batch(
    connection: &mut redis::aio::MultiplexedConnection,
    config: &RedisConfig,
) -> Result<StreamBatch, AppError> {
    let mut cmd = redis::cmd("XREADGROUP");
    cmd.arg("GROUP")
        .arg(&config.group)
        .arg(&config.consumer)
        .arg("COUNT")
        .arg(config.read_count)
        .arg("BLOCK")
        .arg(config.block_ms)
        .arg("STREAMS");
    for stream in &config.streams {
        cmd.arg(stream);
    }
    for _ in &config.streams {
        cmd.arg(">");
    }

    let reply: Option<StreamReadReply> = cmd.query_async(connection).await?;

    let mut out = Vec::new();
    for key in reply.unwrap_or(StreamReadReply { keys: Vec::new() }).keys {
        let stream = key.key.clone();
        let batch = decode_entries(&stream, key.ids)?;
        out.extend(batch.entries);
    }

    Ok(StreamBatch { entries: out })
}

pub async fn claim_pending_batch(
    connection: &mut redis::aio::MultiplexedConnection,
    config: &RedisConfig,
    stream: &str,
    start_id: &str,
) -> Result<PendingClaimBatch, AppError> {
    let reply: StreamAutoClaimReply = redis::cmd("XAUTOCLAIM")
        .arg(stream)
        .arg(&config.group)
        .arg(&config.consumer)
        .arg(config.pending_min_idle_ms)
        .arg(start_id)
        .arg("COUNT")
        .arg(config.pending_reclaim_count)
        .query_async(connection)
        .await?;

    let batch = decode_entries(stream, reply.claimed)?;

    Ok(PendingClaimBatch {
        batch,
        next_start_id: reply.next_stream_id,
        deleted_ids: reply.deleted_ids,
    })
}

pub async fn ack_entries(
    connection: &mut redis::aio::MultiplexedConnection,
    config: &RedisConfig,
    entries: &[StreamEntry],
) -> Result<(), AppError> {
    use std::collections::HashMap;

    let mut by_stream: HashMap<&str, Vec<String>> = HashMap::new();
    for entry in entries {
        by_stream
            .entry(entry.stream.as_str())
            .or_default()
            .push(entry.ack_id.clone());
    }

    for (stream, ids) in by_stream {
        ack_ids(connection, config, stream, &ids).await?;
    }
    Ok(())
}

pub async fn ack_ids(
    connection: &mut redis::aio::MultiplexedConnection,
    config: &RedisConfig,
    stream: &str,
    ack_ids: &[String],
) -> Result<(), AppError> {
    if ack_ids.is_empty() {
        return Ok(());
    }

    let _: u64 = redis::cmd("XACK")
        .arg(stream)
        .arg(&config.group)
        .arg(ack_ids)
        .query_async(connection)
        .await?;

    Ok(())
}

fn bytes_from_value(value: &Value) -> Result<Vec<u8>, AppError> {
    match value {
        Value::BulkString(bytes) => Ok(bytes.clone()),
        _ => Vec::<u8>::from_redis_value(value).map_err(AppError::from),
    }
}

fn decode_entries(stream: &str, entries: Vec<StreamId>) -> Result<StreamBatch, AppError> {
    let mut out = Vec::with_capacity(entries.len());

    for entry in entries {
        let payload_value = match entry
            .map
            .get("data")
            .or_else(|| entry.map.get("payload_msgpack"))
            .or_else(|| entry.map.get("payload"))
            .or_else(|| entry.map.get("tick"))
        {
            Some(v) => v,
            None => {
                warn!(
                    stream,
                    entry_id = %entry.id,
                    "stream entry missing msgpack blob field; acking to avoid PEL pileup"
                );
                out.push(StreamEntry {
                    stream: stream.to_owned(),
                    record: None,
                    ack_id: entry.id,
                });
                continue;
            }
        };

        let payload_msgpack = match bytes_from_value(payload_value) {
            Ok(b) => b,
            Err(e) => {
                warn!(stream, entry_id = %entry.id, error = %e, "failed to read payload bytes; acking");
                out.push(StreamEntry {
                    stream: stream.to_owned(),
                    record: None,
                    ack_id: entry.id,
                });
                continue;
            }
        };

        match rmp_serde::from_slice::<IngestRecordV1>(&payload_msgpack) {
            Ok(record) => {
                out.push(StreamEntry {
                    stream: stream.to_owned(),
                    record: Some(record),
                    ack_id: entry.id,
                });
            }
            Err(e) => {
                warn!(stream, entry_id = %entry.id, error = %e, "failed to deserialize msgpack payload; acking");
                out.push(StreamEntry {
                    stream: stream.to_owned(),
                    record: None,
                    ack_id: entry.id,
                });
            }
        }
    }

    Ok(StreamBatch { entries: out })
}

#[cfg(test)]
mod tests {
    use super::*;
    use redis::streams::StreamId;
    use std::collections::HashMap;

    #[test]
    fn decodes_stream_entries_into_records_and_ack_ids() {
        let record = IngestRecordV1 {
            version: 1,
            chain_id: 1,
            source: "quicknode".to_owned(),
            subscription_id: None,
            method: Some("eth_subscription".to_owned()),
            received_at_ms: 42,
            block_number: Some(16),
            block_hash: Some("0xblock".to_owned()),
            tx_hash: Some("0xtx".to_owned()),
            tx_from: None,
            tx_index: Some(1),
            log_index: Some(2),
            address: Some("0xaddress".to_owned()),
            topic0: Some("0xtopic0".to_owned()),
            topic1: None,
            topic2: None,
            topic3: None,
            data: Some("0xdeadbeef".to_owned()),
            removed: Some(false),
            payload_json: "{}".to_owned(),
        };

        let mut map = HashMap::new();
        map.insert(
            "data".to_owned(),
            Value::BulkString(rmp_serde::to_vec_named(&record).expect("msgpack bytes")),
        );

        let batch = decode_entries(
            "market_ticks",
            vec![StreamId {
                id: "1-0".to_owned(),
                map,
            }],
        )
        .expect("stream entry should decode");

        assert_eq!(batch.entries.len(), 1);
        assert_eq!(batch.entries[0].ack_id, "1-0");
        assert_eq!(batch.entries[0].record.as_ref().unwrap().block_number, Some(16));
    }
}
