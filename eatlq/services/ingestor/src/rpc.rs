use std::collections::HashMap;
use std::time::Duration;
use crate::{
    backfill_state::BackfillStateStore,
    codec,
    config::RpcConfig,
    error::AppError,
    stall_watchdog::touch_block_progress,
};
use eatlq_common::{IngestRecordV1, RawFrame};
use reqwest::Client;
use reqwest::StatusCode;
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use tracing::{debug, info, warn};

pub async fn run_logs_fetcher(
    config: RpcConfig,
    mut raw_rx: mpsc::Receiver<RawFrame>,
    record_tx: mpsc::Sender<IngestRecordV1>,
    source: String,
    chain_id: u64,
    state_postgres_url: Option<String>,
    shutdown_token: CancellationToken,
) -> Result<(), AppError> {
    let client = Client::builder()
        .timeout(config.request_timeout())
        .build()?;
    let state_store = match BackfillStateStore::connect_url_if_enabled(state_postgres_url.as_deref()).await {
        Ok(store) => store,
        Err(error) => {
            warn!(error = %error, "realtime head state disabled because postgres connection failed");
            None
        }
    };
    let mut last_processed_block: Option<u64> = None;

    loop {
        tokio::select! {
            _ = shutdown_token.cancelled() => {
                break;
            }
            maybe_frame = raw_rx.recv() => {
                let Some(frame) = maybe_frame else {
                    break;
                };

                let head_number = match codec::decode_new_head_frame(&frame) {
                    Ok(number) => number,
                    Err(error) => {
                        match &error {
                            AppError::InvalidPayload(message) if message == "not a subscription event" => {
                                debug!("ignoring non-subscription websocket payload before newHeads decode");
                            }
                            _ => warn!(error = %error, "dropping invalid newHeads payload"),
                        }
                        continue;
                    }
                };

                if let Some(store) = state_store.as_ref() {
                    if let Err(error) = store.record_realtime_head_seen(chain_id, &source, head_number).await {
                        warn!(error = %error, head_number, "failed to persist realtime head seen state");
                        let _ = store
                            .record_realtime_state_error(chain_id, &source, &error.to_string())
                            .await;
                    }
                }

                let Some((from_block, to_block)) =
                    catchup_window(last_processed_block, head_number, config.catchup_max_blocks)
                else {
                    debug!(block_number = head_number, "skipping duplicate head");
                    continue;
                };
                if matches!(last_processed_block, Some(previous) if head_number < previous) {
                    warn!(
                        previous_block = last_processed_block,
                        head_block = head_number,
                        "head moved backwards, resetting fetch window"
                    );
                }

                let (logs, completed_to) =
                    fetch_logs_catchup(&client, &config, from_block, to_block).await;
                let Some(completed_to) = completed_to else {
                    warn!(
                        from_block,
                        to_block,
                        "fetch_logs failed after retries, holding cursor for catch-up"
                    );
                    continue;
                };
                let log_count = logs.len();
                let mut tx_from_cache: HashMap<String, String> = HashMap::new();

                for log in logs {
                    let payload_text = synthesize_subscription_payload(head_number, &log).to_string();
                    let mut record = match codec::decode_raw_frame(
                        RawFrame {
                            received_at_ms: now_millis(),
                            payload_text,
                        },
                        &source,
                        chain_id,
                    ) {
                        Ok(record) => record,
                        Err(error) => {
                            warn!(error = %error, "dropping undecodable log entry");
                            continue;
                        }
                    };

                    if record.tx_from.is_none() {
                        if let Some(tx_hash) = record.tx_hash.as_deref() {
                            if let Some(from) = tx_from_cache.get(tx_hash) {
                                record.tx_from = Some(from.clone());
                            } else if let Some(from) =
                                fetch_transaction_from(&client, &config, tx_hash).await
                            {
                                tx_from_cache.insert(tx_hash.to_owned(), from.clone());
                                record.tx_from = Some(from);
                            }
                        }
                    }

                    record_tx
                        .send(record)
                        .await
                        .map_err(|_| AppError::ChannelClosed("decoded record channel"))?;
                }

                info!(
                    from_block,
                    to_block = completed_to,
                    fetched_logs = log_count,
                    "fetched block logs via eth_getLogs"
                );
                touch_block_progress();

                if let Some(store) = state_store.as_ref() {
                    if let Err(error) = store
                        .record_realtime_fetch_completed(chain_id, &source, from_block, completed_to)
                        .await
                    {
                        warn!(
                            error = %error,
                        from_block,
                        to_block = completed_to,
                        "failed to persist realtime fetch completion state"
                        );
                        let _ = store
                            .record_realtime_state_error(chain_id, &source, &error.to_string())
                            .await;
                    }
                }

                last_processed_block = Some(completed_to);
            }
        }
    }

    Ok(())
}

pub async fn fetch_logs(
    client: &Client,
    config: &RpcConfig,
    from_block: u64,
    to_block: u64,
) -> Result<Vec<Value>, AppError> {
    let mut filter = config.log_filter.clone();
    let filter_obj = filter
        .as_object_mut()
        .ok_or_else(|| AppError::InvalidPayload("LOG_FILTER_JSON must be a JSON object".to_owned()))?;
    filter_obj.insert("fromBlock".to_owned(), Value::String(hex_quantity(from_block)));
    filter_obj.insert("toBlock".to_owned(), Value::String(hex_quantity(to_block)));

    let body = json!({
        "jsonrpc": "2.0",
        "id": format!("logs-{from_block}-{to_block}"),
        "method": "eth_getLogs",
        "params": [filter],
    });

    let response_value = post_json_with_pool(client, config, &body).await?;

    if let Some(error) = response_value.get("error") {
        return Err(AppError::Rpc(error.to_string()));
    }

    let result = response_value
        .get("result")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::InvalidPayload("eth_getLogs result was not an array".to_owned()))?;

    Ok(result.to_vec())
}

const FETCH_LOGS_RETRIES: u32 = 5;

pub fn catchup_window(
    last_processed: Option<u64>,
    head: u64,
    max_blocks: u64,
) -> Option<(u64, u64)> {
    match last_processed {
        None => Some((head, head)),
        Some(previous) if head > previous => {
            let from = previous.saturating_add(1);
            let span = max_blocks.max(1).saturating_sub(1);
            Some((from, from.saturating_add(span).min(head)))
        }
        Some(previous) if head == previous => None,
        Some(_) => Some((head, head)),
    }
}

pub fn chunk_range(from: u64, to: u64, chunk: u64) -> Vec<(u64, u64)> {
    if from > to || chunk == 0 {
        return Vec::new();
    }
    let mut out = Vec::new();
    let mut cur = from;
    while cur <= to {
        let end = cur.saturating_add(chunk.saturating_sub(1)).min(to);
        out.push((cur, end));
        if end == u64::MAX {
            break;
        }
        cur = end.saturating_add(1);
    }
    out
}

async fn fetch_logs_catchup(
    client: &Client,
    config: &RpcConfig,
    from_block: u64,
    to_block: u64,
) -> (Vec<Value>, Option<u64>) {
    let mut all = Vec::new();
    let mut completed_to = None;
    for (chunk_from, chunk_to) in chunk_range(from_block, to_block, config.catchup_chunk.max(1)) {
        match fetch_logs_with_retry(client, config, chunk_from, chunk_to).await {
            Some(logs) => {
                all.extend(logs);
                completed_to = Some(chunk_to);
            }
            None => break,
        }
        if config.catchup_pause_ms > 0 && chunk_to < to_block {
            tokio::time::sleep(Duration::from_millis(config.catchup_pause_ms)).await;
        }
    }
    (all, completed_to)
}

pub async fn run_http_head_poller(
    config: RpcConfig,
    tx: mpsc::Sender<RawFrame>,
    shutdown_token: CancellationToken,
) -> Result<(), AppError> {
    if config.head_poll_ms == 0 {
        shutdown_token.cancelled().await;
        return Ok(());
    }
    let client = Client::builder()
        .timeout(config.request_timeout())
        .build()?;
    let interval = Duration::from_millis(config.head_poll_ms.max(250));
    let mut last: Option<u64> = None;
    info!(
        interval_ms = interval.as_millis() as u64,
        "http head poller enabled (public-rpc catch-up path)"
    );

    loop {
        tokio::select! {
            _ = shutdown_token.cancelled() => break,
            _ = tokio::time::sleep(interval) => {
                match fetch_latest_block_number(&client, &config).await {
                    Ok(head) => {
                        if last == Some(head) {
                            continue;
                        }
                        last = Some(head);
                        let frame = RawFrame {
                            received_at_ms: now_millis(),
                            payload_text: json!({
                                "jsonrpc": "2.0",
                                "method": "eth_subscription",
                                "params": {
                                    "subscription": "http_head_poll",
                                    "result": {"number": format!("0x{head:x}")}
                                }
                            })
                            .to_string(),
                        };
                        match tx.try_send(frame) {
                            Ok(()) => debug!(head, "http head poll emitted"),
                            Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
                                warn!(head, "raw channel full, dropping http head");
                            }
                            Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                                return Err(AppError::ChannelClosed("raw frame channel"));
                            }
                        }
                    }
                    Err(error) => {
                        warn!(error = %error, "http head poll failed; will retry");
                    }
                }
            }
        }
    }
    Ok(())
}

async fn fetch_logs_with_retry(
    client: &Client,
    config: &RpcConfig,
    from_block: u64,
    to_block: u64,
) -> Option<Vec<Value>> {
    for attempt in 1..=FETCH_LOGS_RETRIES {
        match fetch_logs(client, config, from_block, to_block).await {
            Ok(logs) => return Some(logs),
            Err(error) => {
                warn!(
                    attempt,
                    max_attempts = FETCH_LOGS_RETRIES,
                    from_block,
                    to_block,
                    error = %error,
                    "fetch_logs failed, retrying"
                );
                if attempt < FETCH_LOGS_RETRIES {
                    tokio::time::sleep(Duration::from_millis(500 * u64::from(attempt))).await;
                }
            }
        }
    }
    None
}

pub async fn fetch_transaction_from(
    client: &Client,
    config: &RpcConfig,
    tx_hash: &str,
) -> Option<String> {
    let body = json!({
        "jsonrpc": "2.0",
        "id": format!("tx-{tx_hash}"),
        "method": "eth_getTransactionByHash",
        "params": [tx_hash],
    });

    let response_value = match post_json_with_pool(client, config, &body).await {
        Ok(v) => v,
        Err(_) => return None,
    };

    if response_value.get("error").is_some() {
        return None;
    }

    response_value
        .pointer("/result/from")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .filter(|v| v.starts_with("0x") && v.len() == 42)
}

pub async fn fetch_latest_block_number(
    client: &Client,
    config: &RpcConfig,
) -> Result<u64, AppError> {
    let body = json!({
        "jsonrpc": "2.0",
        "id": "block-number",
        "method": "eth_blockNumber",
        "params": [],
    });

    let response_value = post_json_with_pool(client, config, &body).await?;

    if let Some(error) = response_value.get("error") {
        return Err(AppError::Rpc(error.to_string()));
    }

    let block_number = response_value
        .get("result")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::InvalidPayload("eth_blockNumber result was not a hex string".to_owned()))?;

    parse_hex_quantity(block_number).ok_or_else(|| {
        AppError::InvalidPayload("eth_blockNumber result was not a valid hex quantity".to_owned())
    })
}

async fn post_json_with_pool(
    client: &Client,
    config: &RpcConfig,
    body: &Value,
) -> Result<Value, AppError> {
    let attempts = config.http_pool.len().max(1);
    let mut last_error: Option<AppError> = None;

    for _ in 0..attempts {
        let Some((http_url, provider)) = config.http_pool.select() else {
            return Err(AppError::Rpc("http pool has no endpoints".to_owned()));
        };

        let response = match client.post(http_url.clone()).json(body).send().await {
            Ok(resp) => resp,
            Err(error) => {
                let rate_limited = error.status() == Some(StatusCode::TOO_MANY_REQUESTS);
                warn!(error = %error, provider = %provider, "http rpc transport error");
                config.http_pool.mark_failure("http_transport_error", rate_limited, false);
                last_error = Some(AppError::Http(error));
                continue;
            }
        };

        let status = response.status();
        if status == StatusCode::TOO_MANY_REQUESTS {
            warn!(provider = %provider, "http rpc rate limited (429)");
            config.http_pool.mark_failure("http_429", true, false);
            last_error = Some(AppError::Rpc("429 Too Many Requests".to_owned()));
            continue;
        }
        if status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN {
            warn!(provider = %provider, status = %status, "http rpc auth failure");
            config.http_pool.mark_failure("http_auth_failed", false, true);
            last_error = Some(AppError::Rpc(format!("auth failure: {status}")));
            continue;
        }

        let response_value: Value = match response.error_for_status() {
            Ok(resp) => match resp.json().await {
                Ok(v) => v,
                Err(error) => {
                    config.http_pool.mark_failure("http_json_decode", false, false);
                    last_error = Some(AppError::Http(error));
                    continue;
                }
            },
            Err(error) => {
                config.http_pool.mark_failure("http_status_error", false, false);
                last_error = Some(AppError::Http(error));
                continue;
            }
        };

        if let Some(error) = response_value.get("error") {
            let err_text = error.to_string();
            let rate_limited = err_text.contains("429") || err_text.to_ascii_lowercase().contains("rate");
            let auth_expired = err_text.contains("401")
                || err_text.to_ascii_lowercase().contains("auth")
                || err_text.to_ascii_lowercase().contains("unauthorized");
            config.http_pool.mark_failure("rpc_json_error", rate_limited, auth_expired);
            last_error = Some(AppError::Rpc(err_text));
            continue;
        }

        config.http_pool.mark_success();
        return Ok(response_value);
    }

    Err(last_error.unwrap_or_else(|| AppError::Rpc("all http pool endpoints failed".to_owned())))
}

fn synthesize_subscription_payload(block_number: u64, log: &Value) -> Value {
    json!({
        "jsonrpc": "2.0",
        "method": "eth_subscription",
        "params": {
            "subscription": format!("eth_getLogs:{block_number:#x}"),
            "result": log,
        }
    })
}

fn hex_quantity(value: u64) -> String {
    format!("0x{value:x}")
}

fn parse_hex_quantity(value: &str) -> Option<u64> {
    let trimmed = value.trim_start_matches("0x");
    if trimmed.is_empty() {
        return None;
    }

    u64::from_str_radix(trimmed, 16).ok()
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
    use crate::endpoint_pool::{EndpointEntry, EndpointPool, EndpointTier, PoolPolicy};
    use std::time::Duration;

    fn test_rpc_config() -> RpcConfig {
        let url: url::Url = "https://example.com".parse().expect("http url");
        let entries = vec![EndpointEntry::new(
            url.clone(),
            EndpointTier::Day,
            "test".to_owned(),
        )];
        let pool = EndpointPool::new(
            entries,
            PoolPolicy {
                cooldown: Duration::from_secs(30),
                ban: Duration::from_secs(3600),
                max_consecutive_failures: 3,
            },
            "http",
        );
        RpcConfig {
            http_url: url,
            http_pool: pool,
            log_filter: json!({
                "topics": [["0xtopic0"]],
            }),
            request_timeout_secs: 30,
            head_poll_ms: 0,
            catchup_chunk: 20,
            catchup_max_blocks: 300,
            catchup_pause_ms: 0,
        }
    }

    #[test]
    fn injects_block_range_into_filter() {
        let config = test_rpc_config();

        let mut filter = config.log_filter.clone();
        let filter_obj = filter.as_object_mut().expect("object filter");
        filter_obj.insert("fromBlock".to_owned(), Value::String(hex_quantity(16)));
        filter_obj.insert("toBlock".to_owned(), Value::String(hex_quantity(17)));

        assert_eq!(filter["fromBlock"], "0x10");
        assert_eq!(filter["toBlock"], "0x11");
        assert_eq!(filter["topics"][0][0], "0xtopic0");
    }

    #[test]
    fn builds_synthetic_subscription_payload() {
        let payload = synthesize_subscription_payload(
            4660,
            &json!({
                "address": "0x1111111111111111111111111111111111111111",
                "topics": ["0xtopic0"],
                "data": "0xdeadbeef",
            }),
        );

        assert_eq!(payload["method"], "eth_subscription");
        assert_eq!(payload["params"]["subscription"], "eth_getLogs:0x1234");
        assert_eq!(
            payload["params"]["result"]["address"],
            "0x1111111111111111111111111111111111111111"
        );
    }

    #[test]
    fn parses_hex_quantity() {
        assert_eq!(parse_hex_quantity("0x10"), Some(16));
        assert_eq!(parse_hex_quantity("0x0"), Some(0));
        assert_eq!(parse_hex_quantity(""), None);
        assert_eq!(parse_hex_quantity("0xzz"), None);
    }

    #[test]
    fn catchup_window_first_tick_is_head_only() {
        assert_eq!(catchup_window(None, 100, 300), Some((100, 100)));
    }

    #[test]
    fn catchup_window_skips_duplicate_head() {
        assert_eq!(catchup_window(Some(100), 100, 300), None);
    }

    #[test]
    fn catchup_window_fills_gap_and_caps() {
        assert_eq!(catchup_window(Some(100), 140, 20), Some((101, 120)));
        assert_eq!(catchup_window(Some(100), 110, 300), Some((101, 110)));
    }

    #[test]
    fn catchup_window_reorg_resets_to_head() {
        assert_eq!(catchup_window(Some(200), 150, 300), Some((150, 150)));
    }

    #[test]
    fn chunk_range_covers_gap() {
        assert_eq!(chunk_range(101, 130, 10), vec![(101, 110), (111, 120), (121, 130)]);
        assert_eq!(chunk_range(5, 5, 20), vec![(5, 5)]);
        assert!(chunk_range(9, 8, 20).is_empty());
    }
}
