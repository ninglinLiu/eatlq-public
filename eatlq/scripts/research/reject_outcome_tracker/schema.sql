-- Isolated, additive table. Does not touch any existing table/schema.
-- Safe to re-run (idempotent).
CREATE TABLE IF NOT EXISTS app.reject_outcome_tracking (
    id                BIGSERIAL PRIMARY KEY,
    pool_address      TEXT NOT NULL,
    token_address     TEXT,
    token_symbol      TEXT,
    reject_reason     TEXT NOT NULL,
    reject_category   TEXT NOT NULL,
    reject_ts         TIMESTAMPTZ NOT NULL,
    entry_tick        INTEGER,
    token0_is_meme    BOOLEAN,
    p_fatal           DOUBLE PRECISION,
    confidence        DOUBLE PRECISION,
    tick_1h           INTEGER,
    tick_6h           INTEGER,
    tick_24h          INTEGER,
    tick_72h          INTEGER,
    ret_1h_pct        DOUBLE PRECISION,
    ret_6h_pct        DOUBLE PRECISION,
    ret_24h_pct       DOUBLE PRECISION,
    ret_72h_pct       DOUBLE PRECISION,
    peak_ret_pct      DOUBLE PRECISION,
    trough_ret_pct    DOUBLE PRECISION,
    last_swap_at      TIMESTAMPTZ,
    still_trading_24h BOOLEAN,
    status            TEXT NOT NULL DEFAULT 'pending',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pool_address, reject_ts)
);

CREATE INDEX IF NOT EXISTS reject_outcome_tracking_status_idx
    ON app.reject_outcome_tracking (status);
CREATE INDEX IF NOT EXISTS reject_outcome_tracking_category_idx
    ON app.reject_outcome_tracking (reject_category);
