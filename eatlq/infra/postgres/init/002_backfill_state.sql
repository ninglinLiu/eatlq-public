CREATE TABLE IF NOT EXISTS app.backfill_jobs
(
    job_key TEXT PRIMARY KEY,
    chain_id BIGINT NOT NULL,
    source TEXT NOT NULL,
    filter_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    confirmations BIGINT NOT NULL DEFAULT 0,
    last_requested_from_block BIGINT,
    last_requested_to_block BIGINT,
    last_completed_to_block BIGINT,
    last_run_started_at TIMESTAMPTZ,
    last_run_finished_at TIMESTAMPTZ,
    last_run_status TEXT NOT NULL DEFAULT 'idle',
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app.backfill_gap_ranges
(
    id BIGSERIAL PRIMARY KEY,
    job_key TEXT NOT NULL REFERENCES app.backfill_jobs(job_key) ON DELETE CASCADE,
    from_block BIGINT NOT NULL,
    to_block BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    claimed_at TIMESTAMPTZ,
    run_started_at TIMESTAMPTZ,
    run_finished_at TIMESTAMPTZ,
    last_completed_to_block BIGINT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (job_key, from_block, to_block)
);

CREATE INDEX IF NOT EXISTS idx_backfill_gap_ranges_job_status
    ON app.backfill_gap_ranges(job_key, status, from_block);

CREATE TABLE IF NOT EXISTS app.backfill_chunk_progress
(
    id BIGSERIAL PRIMARY KEY,
    gap_id BIGINT NOT NULL REFERENCES app.backfill_gap_ranges(id) ON DELETE CASCADE,
    chunk_from_block BIGINT NOT NULL,
    chunk_to_block BIGINT NOT NULL,
    fetched_logs_count BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (gap_id, chunk_from_block, chunk_to_block)
);

CREATE TABLE IF NOT EXISTS app.ingestor_head_state
(
    chain_id BIGINT NOT NULL,
    source TEXT NOT NULL,
    last_seen_head BIGINT,
    last_fetched_head BIGINT,
    last_fetch_from_block BIGINT,
    last_head_seen_at TIMESTAMPTZ,
    last_fetch_completed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chain_id, source)
);
