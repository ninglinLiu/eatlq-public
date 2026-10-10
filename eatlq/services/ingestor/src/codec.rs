use crate::error::AppError;
use eatlq_common::{IngestRecordV1, RawFrame};
use serde_json::Value;

pub fn decode_new_head_frame(raw: &RawFrame) -> Result<u64, AppError> {
    let payload: Value = serde_json::from_str(&raw.payload_text)?;
    let method = payload.get("method").and_then(Value::as_str);
    if method != Some("eth_subscription") {
        return Err(AppError::InvalidPayload("not a subscription event".to_owned()));
    }

    payload
        .pointer("/params/result/number")
        .and_then(Value::as_str)
        .and_then(hex_to_u64)
        .ok_or_else(|| AppError::InvalidPayload("newHeads payload missing block number".to_owned()))
}

pub fn decode_raw_frame(
    raw: RawFrame,
    source: &str,
    chain_id: u64,
) -> Result<IngestRecordV1, AppError> {
    let payload: Value = serde_json::from_str(&raw.payload_text)?;

    let method = payload.get("method").and_then(Value::as_str).map(str::to_owned);
    if method.as_deref() != Some("eth_subscription") {
        return Err(AppError::InvalidPayload("not a subscription event".to_owned()));
    }

    let subscription_id = payload
        .pointer("/params/subscription")
        .and_then(Value::as_str)
        .map(str::to_owned);

    let result = payload.pointer("/params/result").unwrap_or(&payload);

    let topics = result
        .get("topics")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    Ok(IngestRecordV1 {
        version: 1,
        chain_id,
        source: source.to_owned(),
        subscription_id,
        method,
        received_at_ms: raw.received_at_ms,
        block_number: result
            .get("blockNumber")
            .and_then(Value::as_str)
            .and_then(hex_to_u64),
        block_hash: result
            .get("blockHash")
            .and_then(Value::as_str)
            .map(str::to_owned),
        tx_hash: result
            .get("transactionHash")
            .and_then(Value::as_str)
            .map(str::to_owned),
        tx_from: None,
        tx_index: result
            .get("transactionIndex")
            .and_then(Value::as_str)
            .and_then(hex_to_u64),
        log_index: result
            .get("logIndex")
            .and_then(Value::as_str)
            .and_then(hex_to_u64),
        address: result
            .get("address")
            .and_then(Value::as_str)
            .map(str::to_owned),
        topic0: topics.first().and_then(Value::as_str).map(str::to_owned),
        topic1: topics.get(1).and_then(Value::as_str).map(str::to_owned),
        topic2: topics.get(2).and_then(Value::as_str).map(str::to_owned),
        topic3: topics.get(3).and_then(Value::as_str).map(str::to_owned),
        data: result.get("data").and_then(Value::as_str).map(str::to_owned),
        removed: result.get("removed").and_then(Value::as_bool),
        payload_json: raw.payload_text,
    })
}

pub fn encode_msgpack(record: &IngestRecordV1) -> Result<Vec<u8>, AppError> {
    // Use named (map) encoding so downstream Python consumers receive a dict
    // without relying on a hard-coded field order. Matches LASZLO convention.
    rmp_serde::to_vec_named(record).map_err(AppError::from)
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
    use eatlq_common::RawFrame;

    #[test]
    fn decodes_eth_log_payload() {
        let raw = RawFrame {
            received_at_ms: 1_731_234_567_890,
            payload_text: r#"{
                "jsonrpc":"2.0",
                "method":"eth_subscription",
                "params":{
                    "subscription":"0xsub",
                    "result":{
                        "address":"0x1111111111111111111111111111111111111111",
                        "topics":["0xtopic0","0xtopic1"],
                        "data":"0xdeadbeef",
                        "blockNumber":"0x10",
                        "blockHash":"0xblock",
                        "transactionHash":"0xtx",
                        "transactionIndex":"0x1",
                        "logIndex":"0x2",
                        "removed":false
                    }
                }
            }"#
            .to_owned(),
        };

        let record = decode_raw_frame(raw, "quicknode", 1).expect("record should decode");

        assert_eq!(record.chain_id, 1);
        assert_eq!(record.subscription_id.as_deref(), Some("0xsub"));
        assert_eq!(record.block_number, Some(16));
        assert_eq!(record.log_index, Some(2));
        assert_eq!(record.address.as_deref(), Some("0x1111111111111111111111111111111111111111"));
        assert_eq!(record.topic1.as_deref(), Some("0xtopic1"));
    }

    #[test]
    fn rejects_subscription_confirmation_payload() {
        let raw = RawFrame {
            received_at_ms: 1_731_234_567_890,
            payload_text: r#"{
                "jsonrpc":"2.0",
                "id":1,
                "result":"0xsub"
            }"#
            .to_owned(),
        };

        let error = decode_raw_frame(raw, "quicknode", 1).expect_err("confirmation should drop");
        assert!(matches!(error, AppError::InvalidPayload(_)));
    }

    #[test]
    fn decodes_new_head_payload() {
        let raw = RawFrame {
            received_at_ms: 1_731_234_567_890,
            payload_text: r#"{
                "jsonrpc":"2.0",
                "method":"eth_subscription",
                "params":{
                    "subscription":"0xsub",
                    "result":{
                        "number":"0x10"
                    }
                }
            }"#
            .to_owned(),
        };

        let block_number = decode_new_head_frame(&raw).expect("head should decode");
        assert_eq!(block_number, 16);
    }

    #[test]
    fn msgpack_roundtrip_has_bytes() {
        let record = IngestRecordV1 {
            version: 1,
            chain_id: 1,
            source: "quicknode".to_owned(),
            subscription_id: None,
            method: Some("eth_subscription".to_owned()),
            received_at_ms: 42,
            block_number: None,
            block_hash: None,
            tx_hash: None,
            tx_from: None,
            tx_index: None,
            log_index: None,
            address: None,
            topic0: None,
            topic1: None,
            topic2: None,
            topic3: None,
            data: None,
            removed: None,
            payload_json: "{}".to_owned(),
        };

        let bytes = encode_msgpack(&record).expect("msgpack should encode");
        assert!(!bytes.is_empty());
    }
}
