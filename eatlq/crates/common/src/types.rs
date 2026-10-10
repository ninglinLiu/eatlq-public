use serde::{Deserialize, Serialize};

#[derive(Debug, Clone)]
pub struct RawFrame {
    pub received_at_ms: u64,
    pub payload_text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IngestRecordV1 {
    pub version: u16,
    pub chain_id: u64,
    pub source: String,
    pub subscription_id: Option<String>,
    pub method: Option<String>,
    // Local ingest timestamp in epoch milliseconds, used as the Phase 1 archival clock.
    pub received_at_ms: u64,
    pub block_number: Option<u64>,
    pub block_hash: Option<String>,
    pub tx_hash: Option<String>,
    pub tx_from: Option<String>,
    pub tx_index: Option<u64>,
    pub log_index: Option<u64>,
    pub address: Option<String>,
    pub topic0: Option<String>,
    pub topic1: Option<String>,
    pub topic2: Option<String>,
    pub topic3: Option<String>,
    pub data: Option<String>,
    pub removed: Option<bool>,
    pub payload_json: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SignalV1 {
    pub version: u16,
    pub strategy_id: String,
    pub signal_type: String,
    pub score: f64,
    pub emitted_at_ms: u64,
    pub payload_json: String,
}
