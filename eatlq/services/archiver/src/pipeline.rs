use crate::{
    clickhouse_sink::{self, RawEvmLogRow},
    config::AppConfig,
    error::AppError,
    redis_streams::{self, StreamEntry},
};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;
use tokio::time::{interval, MissedTickBehavior};
use tokio_util::sync::CancellationToken;
use tracing::{error, info, warn};

static LAST_FLUSH_MS: AtomicU64 = AtomicU64::new(0);

fn mark_flush_success() {
    LAST_FLUSH_MS.store(now_millis(), Ordering::Relaxed);
}

fn seconds_since_last_flush() -> u64 {
    let last = LAST_FLUSH_MS.load(Ordering::Relaxed);
    if last == 0 {
        return 0;
    }
    now_millis().saturating_sub(last) / 1000
}

async fn check_flush_watchdog(config: &AppConfig) -> Result<(), AppError> {
    let idle = seconds_since_last_flush();
    if idle > config.archiver.max_idle_flush_secs {
        error!(
            idle_secs = idle,
            max_idle_secs = config.archiver.max_idle_flush_secs,
            "archiver flush watchdog triggered — exiting for container restart"
        );
        return Err(AppError::InvalidPayload(format!(
            "archiver flush watchdog: no flush for {idle}s (max {}s)",
            config.archiver.max_idle_flush_secs
        )));
    }
    Ok(())
}

struct PendingCursor {
    start_id: String,
}

pub async fn run_pipeline(
    config: AppConfig,
    shutdown_token: CancellationToken,
) -> Result<(), AppError> {
    let clickhouse_client = clickhouse_sink::build_client(&config.clickhouse);
    let mut redis_connection = redis_streams::connect(&config.redis).await?;
    redis_streams::ensure_consumer_group(&mut redis_connection, &config.redis).await?;

    let mut rows = Vec::<RawEvmLogRow>::with_capacity(config.archiver.batch_size);
    let mut ack_entries = Vec::<StreamEntry>::with_capacity(config.archiver.batch_size);
    let mut pending_cursors: HashMap<String, PendingCursor> = config
        .redis
        .streams
        .iter()
        .map(|s| {
            (
                s.clone(),
                PendingCursor {
                    start_id: "0-0".to_owned(),
                },
            )
        })
        .collect();

    recover_pending(
        &clickhouse_client,
        &mut redis_connection,
        &config,
        &mut rows,
        &mut ack_entries,
        &mut pending_cursors,
    )
    .await?;

    mark_flush_success();

    let mut flush_tick = interval(config.archiver.batch_timeout());
    flush_tick.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let mut reclaim_tick = interval(config.redis.pending_reclaim_interval());
    reclaim_tick.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let mut watchdog_tick = interval(Duration::from_secs(60));
    watchdog_tick.set_missed_tick_behavior(MissedTickBehavior::Delay);

    loop {
        tokio::select! {
            _ = shutdown_token.cancelled() => {
                flush_pending(
                    &clickhouse_client,
                    &mut redis_connection,
                    &config,
                    &mut rows,
                    &mut ack_entries,
                ).await?;
                break;
            }
            _ = watchdog_tick.tick() => {
                check_flush_watchdog(&config).await?;
            }
            _ = flush_tick.tick() => {
                flush_pending(
                    &clickhouse_client,
                    &mut redis_connection,
                    &config,
                    &mut rows,
                    &mut ack_entries,
                ).await?;
            }
            _ = reclaim_tick.tick() => {
                reclaim_pending_once(
                    &clickhouse_client,
                    &mut redis_connection,
                    &config,
                    &mut rows,
                    &mut ack_entries,
                    &mut pending_cursors,
                ).await?;
            }
            batch = redis_streams::read_batch(&mut redis_connection, &config.redis) => {
                let batch = batch?;

                // Liveness heartbeat: a successful XREADGROUP (even empty) proves
                // the main loop is advancing and Redis is reachable. This keeps the
                // docker freshness healthcheck green during genuinely idle periods
                // and makes the flush watchdog mean "the loop is stuck" rather than
                // "no data exists". A real stall (CH insert hang, Redis dead, or a
                // future deadlock) stops refreshing this and trips the watchdog.
                update_healthcheck_file(&config.archiver.healthcheck_path);
                mark_flush_success();

                if batch.entries.is_empty() {
                    continue;
                }

                ingest_stream_entries(&mut rows, batch.entries, &mut ack_entries);

                if rows.len() >= config.archiver.batch_size {
                    flush_pending(
                        &clickhouse_client,
                        &mut redis_connection,
                        &config,
                        &mut rows,
                        &mut ack_entries,
                    ).await?;
                }
            }
        }
    }

    Ok(())
}

fn ingest_stream_entries(
    rows: &mut Vec<RawEvmLogRow>,
    entries: Vec<StreamEntry>,
    ack_entries: &mut Vec<StreamEntry>,
) {
    for mut entry in entries {
        if let Some(record) = entry.record.take() {
            match clickhouse_sink::row_from_record(record) {
                Ok(row) => rows.push(row),
                Err(error) => warn!(error = %error, "dropping invalid record before clickhouse insert"),
            }
        }
        ack_entries.push(entry);
    }
}

async fn recover_pending(
    clickhouse_client: &clickhouse::Client,
    redis_connection: &mut redis::aio::MultiplexedConnection,
    config: &AppConfig,
    rows: &mut Vec<RawEvmLogRow>,
    ack_entries: &mut Vec<StreamEntry>,
    pending_cursors: &mut HashMap<String, PendingCursor>,
) -> Result<(), AppError> {
    let mut recovered_messages = 0usize;
    let mut deleted_pending_ids = 0usize;

    for stream in &config.redis.streams {
        let mut batches = 0usize;
        loop {
            if batches >= config.archiver.startup_recover_max_batches {
                warn!(
                    stream = %stream,
                    max_batches = config.archiver.startup_recover_max_batches,
                    "startup PEL recovery capped; remaining pending handled in main loop"
                );
                break;
            }
            let cursor = pending_cursors
                .get_mut(stream)
                .map(|c| c.start_id.clone())
                .unwrap_or_else(|| "0-0".to_owned());
            let claimed =
                redis_streams::claim_pending_batch(redis_connection, &config.redis, stream, &cursor)
                    .await?;
            if let Some(c) = pending_cursors.get_mut(stream) {
                c.start_id = claimed.next_start_id.clone();
            }
            deleted_pending_ids += claimed.deleted_ids.len();

            if claimed.batch.entries.is_empty() {
                break;
            }

            recovered_messages += claimed.batch.entries.len();
            ingest_stream_entries(rows, claimed.batch.entries, ack_entries);
            flush_pending(clickhouse_client, redis_connection, config, rows, ack_entries).await?;
            batches += 1;

            if claimed.next_start_id == "0-0" {
                break;
            }
        }
    }

    if recovered_messages > 0 || deleted_pending_ids > 0 {
        info!(
            recovered_messages,
            deleted_pending_ids,
            min_idle_ms = config.redis.pending_min_idle_ms,
            streams = ?config.redis.streams,
            "recovered pending redis stream messages on startup"
        );
    }

    Ok(())
}

async fn reclaim_pending_once(
    clickhouse_client: &clickhouse::Client,
    redis_connection: &mut redis::aio::MultiplexedConnection,
    config: &AppConfig,
    rows: &mut Vec<RawEvmLogRow>,
    ack_entries: &mut Vec<StreamEntry>,
    pending_cursors: &mut HashMap<String, PendingCursor>,
) -> Result<(), AppError> {
    for stream in &config.redis.streams {
        let cursor = pending_cursors
            .get(stream)
            .map(|c| c.start_id.clone())
            .unwrap_or_else(|| "0-0".to_owned());
        let claimed =
            redis_streams::claim_pending_batch(redis_connection, &config.redis, stream, &cursor)
                .await?;
        if let Some(c) = pending_cursors.get_mut(stream) {
            c.start_id = claimed.next_start_id.clone();
        }
        if claimed.batch.entries.is_empty() {
            continue;
        }
        info!(
            stream = %stream,
            reclaimed = claimed.batch.entries.len(),
            "reclaimed idle pending archiver messages"
        );
        ingest_stream_entries(rows, claimed.batch.entries, ack_entries);
        flush_pending(clickhouse_client, redis_connection, config, rows, ack_entries).await?;
    }
    Ok(())
}

async fn flush_pending(
    clickhouse_client: &clickhouse::Client,
    redis_connection: &mut redis::aio::MultiplexedConnection,
    config: &AppConfig,
    rows: &mut Vec<RawEvmLogRow>,
    ack_entries: &mut Vec<StreamEntry>,
) -> Result<(), AppError> {
    if rows.is_empty() && ack_entries.is_empty() {
        return Ok(());
    }

    let inserted = rows.len();
    let acked = ack_entries.len();

    if !rows.is_empty() {
        clickhouse_sink::insert_rows(clickhouse_client, &config.clickhouse, rows).await?;
    }

    redis_streams::ack_entries(redis_connection, &config.redis, ack_entries).await?;
    update_healthcheck_file(&config.archiver.healthcheck_path);
    mark_flush_success();

    info!(
        clickhouse_table = %config.clickhouse.table,
        inserted_rows = inserted,
        acked_messages = acked,
        "archiver flushed records to clickhouse"
    );

    rows.clear();
    ack_entries.clear();
    Ok(())
}

fn update_healthcheck_file(path: &str) {
    let timestamp = format!("{}", now_millis());
    if let Err(error) = std::fs::write(path, timestamp) {
        warn!(error = %error, path, "failed to update archiver healthcheck file");
    }
}

fn now_millis() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};

    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}
