use crate::{config::ClickHouseConfig, error::AppError};
use clickhouse::{Client, Row};
use eatlq_common::IngestRecordV1;
use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Clone, Row, Serialize)]
pub struct RawEvmLogRow {
    pub chain_id: u64,
    pub source: String,
    pub subscription_id: String,
    pub block_time: Option<i64>,
    pub received_at_ms: i64,
    pub block_number: u64,
    pub tx_hash: String,
    pub log_index: u32,
    pub address: String,
    pub topic0: String,
    pub topic1: String,
    pub topic2: String,
    pub topic3: String,
    pub data: String,
    pub removed: u8,
    pub value_usd: Option<f64>,
    pub payload_json: String,
}

pub fn build_client(config: &ClickHouseConfig) -> Client {
    Client::default()
        .with_url(&config.url)
        .with_database(&config.database)
        .with_user(&config.user)
        .with_password(&config.password)
}

pub async fn insert_rows(
    client: &Client,
    config: &ClickHouseConfig,
    rows: &[RawEvmLogRow],
) -> Result<(), AppError> {
    if rows.is_empty() {
        return Ok(());
    }

    let timeout = std::time::Duration::from_secs(config.insert_timeout_secs);
    let rows_owned = rows.to_vec();
    let table = config.table.clone();
    let client = client.clone();

    tokio::time::timeout(timeout, async move {
        let mut insert = client.insert::<RawEvmLogRow>(&table)?;
        for row in &rows_owned {
            insert.write(row).await?;
        }
        insert.end().await?;
        Ok::<(), clickhouse::error::Error>(())
    })
    .await
    .map_err(|_| AppError::ClickHouseInsertTimeout {
        timeout_secs: config.insert_timeout_secs,
    })??;

    Ok(())
}

pub fn row_from_record(record: IngestRecordV1) -> Result<RawEvmLogRow, AppError> {
    let block_time = extract_block_time_ms(&record.payload_json)?;
    let received_at_ms = i64::try_from(record.received_at_ms).map_err(|_| {
        AppError::InvalidPayload("received_at_ms overflowed i64".to_owned())
    })?;

    Ok(RawEvmLogRow {
        chain_id: record.chain_id,
        source: record.source,
        subscription_id: record.subscription_id.unwrap_or_default(),
        block_time,
        received_at_ms,
        block_number: record.block_number.unwrap_or_default(),
        tx_hash: record.tx_hash.unwrap_or_default(),
        log_index: record
            .log_index
            .and_then(|value| u32::try_from(value).ok())
            .unwrap_or_default(),
        address: record.address.unwrap_or_default(),
        topic0: record.topic0.unwrap_or_default(),
        topic1: record.topic1.unwrap_or_default(),
        topic2: record.topic2.unwrap_or_default(),
        topic3: record.topic3.unwrap_or_default(),
        data: record.data.unwrap_or_default(),
        removed: u8::from(record.removed.unwrap_or(false)),
        value_usd: None,
        payload_json: record.payload_json,
    })
}

fn extract_block_time_ms(payload_json: &str) -> Result<Option<i64>, AppError> {
    let payload: Value = serde_json::from_str(payload_json)?;
    let block_timestamp = payload
        .pointer("/params/result/blockTimestamp")
        .and_then(Value::as_str)
        .and_then(hex_to_u64)
        .map(|seconds| seconds.saturating_mul(1000));

    Ok(block_timestamp.and_then(|value| i64::try_from(value).ok()))
}

fn hex_to_u64(value: &str) -> Option<u64> {
    let trimmed = value.trim_start_matches("0x");

    if trimmed.is_empty() {
        return None;
    }

    u64::from_str_radix(trimmed, 16).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_block_timestamp_from_payload() {
        let payload = r#"{
            "jsonrpc":"2.0",
            "method":"eth_subscription",
            "params":{
                "result":{
                    "blockTimestamp":"0x65f55b00"
                }
            }
        }"#;

        let block_time = extract_block_time_ms(payload).expect("timestamp should parse");
        assert_eq!(block_time, Some(1_710_578_432_000));
    }
}
