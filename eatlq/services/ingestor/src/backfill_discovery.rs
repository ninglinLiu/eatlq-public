use crate::{
    backfill_state::{BackfillRunPlan, BackfillStateStore, RealtimeHeadState},
    config::{AppConfig, GapDiscoveryConfig},
    error::AppError,
    rpc,
};
use reqwest::Client;
use serde::Deserialize;
use tracing::info;

#[derive(Debug, Clone, PartialEq, Eq)]
struct CandidateGap {
    from_block: u64,
    to_block: u64,
}

#[derive(Debug, Deserialize)]
struct ClickHouseBlockRow {
    block_number: u64,
}

pub async fn run_gap_discovery(config: AppConfig) -> Result<(), AppError> {
    let backfill_config = config.backfill.clone();
    let discovery_config = backfill_config.gap_discovery.clone();
    let source = backfill_config.effective_source(&config.ws.source);
    let realtime_source = config.ws.source.clone();
    let chain_id = config.ws.chain_id;

    let state_store = BackfillStateStore::connect_if_enabled(&backfill_config)
        .await?
        .ok_or(AppError::MissingEnv("BACKFILL_STATE_POSTGRES_URL"))?;

    let client = Client::builder()
        .timeout(config.rpc.request_timeout())
        .build()?;
    let realtime_head_state = state_store
        .load_realtime_head_state(chain_id, &realtime_source)
        .await?;
    let to_block = resolve_discovery_to_block(&client, &config, realtime_head_state.as_ref()).await?;
    let from_block = resolve_discovery_from_block(&state_store, &config, chain_id).await?;

    if to_block < from_block {
        info!(
            from_block,
            to_block,
            "gap discovery range is empty, exiting without enqueues"
        );
        return Ok(());
    }

    let discovered_blocks = fetch_archived_blocks(
        &client,
        &discovery_config,
        chain_id,
        from_block,
        to_block,
    )
    .await?;
    let gaps = detect_candidate_gaps(&discovered_blocks, from_block, to_block, &discovery_config);

    let mut enqueued = 0usize;
    for gap in gaps {
        let BackfillRunPlan {
            gap_id,
            from_block,
            to_block,
            ..
        } = state_store
            .enqueue_gap(
                &backfill_config,
                &config.rpc,
                chain_id,
                &source,
                gap.from_block,
                gap.to_block,
            )
            .await?;
        enqueued += 1;
        info!(
            gap_id,
            from_block,
            to_block,
            "enqueued candidate gap discovered from ClickHouse"
        );
    }

    let realtime_last_seen_head = realtime_head_state.as_ref().and_then(|state| state.last_seen_head);
    let realtime_last_fetched_head = realtime_head_state
        .as_ref()
        .and_then(|state| state.last_fetched_head);

    info!(
        inspected_from_block = from_block,
        inspected_to_block = to_block,
        realtime_last_seen_head = ?realtime_last_seen_head,
        realtime_last_fetched_head = ?realtime_last_fetched_head,
        archived_block_rows = discovered_blocks.len(),
        enqueued_candidate_gaps = enqueued,
        "completed ClickHouse-backed candidate gap discovery"
    );

    Ok(())
}

async fn resolve_discovery_from_block(
    state_store: &BackfillStateStore,
    config: &AppConfig,
    chain_id: u64,
) -> Result<u64, AppError> {
    if let Some(from_block) = config.backfill.gap_discovery.from_block {
        return Ok(from_block);
    }

    if let Some(from_block) = config.backfill.from_block {
        return Ok(from_block);
    }

    let checkpoint = state_store
        .job_last_completed_to_block(chain_id, &config.backfill.job_name)
        .await?;
    checkpoint
        .map(|value| value.saturating_add(1))
        .ok_or(AppError::MissingEnv("BACKFILL_DISCOVERY_FROM_BLOCK"))
}

async fn resolve_discovery_to_block(
    client: &Client,
    config: &AppConfig,
    realtime_head_state: Option<&RealtimeHeadState>,
) -> Result<u64, AppError> {
    if let Some(to_block) = config.backfill.gap_discovery.to_block {
        return Ok(to_block);
    }

    if let Some(to_block) = config.backfill.to_block {
        return Ok(to_block);
    }

    if let Some(last_fetched_head) = realtime_head_state.and_then(|state| state.last_fetched_head) {
        return Ok(last_fetched_head);
    }

    let latest_block = rpc::fetch_latest_block_number(client, &config.rpc).await?;
    Ok(latest_block.saturating_sub(config.backfill.confirmations))
}

async fn fetch_archived_blocks(
    client: &Client,
    config: &GapDiscoveryConfig,
    chain_id: u64,
    from_block: u64,
    to_block: u64,
) -> Result<Vec<u64>, AppError> {
    let query = format!(
        "SELECT DISTINCT block_number \
         FROM {database}.{table} \
         WHERE chain_id = {chain_id} \
           AND block_number >= {from_block} \
           AND block_number <= {to_block} \
         ORDER BY block_number \
         FORMAT JSONEachRow",
        database = config.clickhouse_database,
        table = config.clickhouse_table,
    );

    let mut request = client
        .post(config.require_clickhouse_url()?)
        .body(query);

    if !config.clickhouse_user.is_empty() {
        request = request.basic_auth(
            config.clickhouse_user.as_str(),
            Some(config.clickhouse_password.as_str()),
        );
    }

    let response = request.send().await?.error_for_status()?.text().await?;
    let mut blocks = Vec::new();

    for line in response.lines().filter(|line| !line.trim().is_empty()) {
        let row: ClickHouseBlockRow = serde_json::from_str(line)?;
        blocks.push(row.block_number);
    }

    Ok(blocks)
}

fn detect_candidate_gaps(
    discovered_blocks: &[u64],
    from_block: u64,
    to_block: u64,
    config: &GapDiscoveryConfig,
) -> Vec<CandidateGap> {
    let mut gaps = Vec::new();

    if discovered_blocks.is_empty() {
        if config.include_edge_gaps {
            maybe_push_gap(&mut gaps, from_block, to_block, config);
        }
        return gaps;
    }

    if config.include_edge_gaps {
        let first = discovered_blocks[0];
        if first > from_block {
            maybe_push_gap(&mut gaps, from_block, first.saturating_sub(1), config);
        }
    }

    for window in discovered_blocks.windows(2) {
        let previous = window[0];
        let current = window[1];
        if current > previous.saturating_add(1) {
            maybe_push_gap(
                &mut gaps,
                previous.saturating_add(1),
                current.saturating_sub(1),
                config,
            );
        }
    }

    if config.include_edge_gaps {
        let last = discovered_blocks[discovered_blocks.len() - 1];
        if last < to_block {
            maybe_push_gap(&mut gaps, last.saturating_add(1), to_block, config);
        }
    }

    gaps
}

fn maybe_push_gap(
    gaps: &mut Vec<CandidateGap>,
    from_block: u64,
    to_block: u64,
    config: &GapDiscoveryConfig,
) {
    if to_block < from_block {
        return;
    }

    let gap_size = to_block.saturating_sub(from_block).saturating_add(1);
    if gap_size < config.min_gap_size || gap_size > config.max_gap_size {
        return;
    }

    gaps.push(CandidateGap {
        from_block,
        to_block,
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_config() -> GapDiscoveryConfig {
        GapDiscoveryConfig {
            clickhouse_url: Some("http://clickhouse:8123".to_owned()),
            clickhouse_database: "eatlq".to_owned(),
            clickhouse_user: "default".to_owned(),
            clickhouse_password: String::new(),
            clickhouse_table: "raw_evm_logs".to_owned(),
            from_block: None,
            to_block: None,
            min_gap_size: 1,
            max_gap_size: 10,
            include_edge_gaps: false,
        }
    }

    #[test]
    fn detects_internal_candidate_gaps() {
        let gaps = detect_candidate_gaps(&[100, 101, 105, 106], 100, 106, &sample_config());

        assert_eq!(
            gaps,
            vec![CandidateGap {
                from_block: 102,
                to_block: 104
            }]
        );
    }

    #[test]
    fn respects_gap_size_limits() {
        let mut config = sample_config();
        config.min_gap_size = 2;
        config.max_gap_size = 2;

        let gaps = detect_candidate_gaps(&[100, 103], 100, 103, &config);
        assert_eq!(gaps, vec![CandidateGap { from_block: 101, to_block: 102 }]);
    }

    #[test]
    fn can_include_edge_gaps() {
        let mut config = sample_config();
        config.include_edge_gaps = true;

        let gaps = detect_candidate_gaps(&[102, 103], 100, 105, &config);
        assert_eq!(
            gaps,
            vec![
                CandidateGap {
                    from_block: 100,
                    to_block: 101
                },
                CandidateGap {
                    from_block: 104,
                    to_block: 105
                }
            ]
        );
    }
}
