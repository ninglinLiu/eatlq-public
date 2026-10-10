use crate::{
    config::{BackfillConfig, RpcConfig},
    error::AppError,
};
use tokio_postgres::{Client, NoTls, Row};
use tracing::warn;

pub struct BackfillStateStore {
    client: Client,
}

#[derive(Debug, Clone)]
pub struct BackfillRunPlan {
    pub gap_id: i64,
    pub job_key: String,
    pub from_block: u64,
    pub to_block: u64,
}

#[derive(Debug, Clone, Default)]
pub struct RealtimeHeadState {
    pub last_seen_head: Option<u64>,
    pub last_fetched_head: Option<u64>,
}

impl BackfillStateStore {
    pub async fn connect_if_enabled(config: &BackfillConfig) -> Result<Option<Self>, AppError> {
        Self::connect_url_if_enabled(config.state_postgres_url.as_deref()).await
    }

    pub async fn connect_url_if_enabled(postgres_url: Option<&str>) -> Result<Option<Self>, AppError> {
        let Some(url) = postgres_url else {
            return Ok(None);
        };

        let (client, connection) = tokio_postgres::connect(url, NoTls).await?;
        tokio::spawn(async move {
            if let Err(error) = connection.await {
                warn!(error = %error, "backfill state postgres connection closed");
            }
        });

        let store = Self { client };
        store.bootstrap().await?;
        Ok(Some(store))
    }

    pub async fn resolve_run_plan(
        &self,
        backfill_config: &BackfillConfig,
        rpc_config: &RpcConfig,
        chain_id: u64,
        source: &str,
        resolved_to_block: u64,
    ) -> Result<BackfillRunPlan, AppError> {
        let job_key = job_key(chain_id, &backfill_config.job_name);
        self.upsert_job(&job_key, chain_id, source, rpc_config, backfill_config)
            .await?;

        if backfill_config.from_block.is_none() && backfill_config.auto_claim_pending_gap {
            if let Some(plan) = self.claim_next_gap(&job_key).await? {
                self.mark_job_running(&job_key, plan.from_block, plan.to_block)
                    .await?;
                return Ok(plan);
            }
        }

        let from_block = match backfill_config.from_block {
            Some(from_block) => from_block,
            None => match self.last_completed_to_block(&job_key).await? {
                Some(last_completed) => last_completed.saturating_add(1),
                None => return Err(AppError::MissingEnv("BACKFILL_FROM_BLOCK")),
            },
        };

        let to_block = backfill_config.to_block.unwrap_or(resolved_to_block);
        let gap_id = self
            .create_or_reopen_gap(&job_key, from_block, to_block)
            .await?;
        self.mark_job_running(&job_key, from_block, to_block).await?;

        Ok(BackfillRunPlan {
            gap_id,
            job_key,
            from_block,
            to_block,
        })
    }

    pub async fn claim_next_pending_gap(
        &self,
        backfill_config: &BackfillConfig,
        rpc_config: &RpcConfig,
        chain_id: u64,
        source: &str,
    ) -> Result<Option<BackfillRunPlan>, AppError> {
        let job_key = job_key(chain_id, &backfill_config.job_name);
        self.upsert_job(&job_key, chain_id, source, rpc_config, backfill_config)
            .await?;

        let Some(plan) = self.claim_next_gap(&job_key).await? else {
            return Ok(None);
        };

        self.mark_job_running(&job_key, plan.from_block, plan.to_block)
            .await?;

        Ok(Some(plan))
    }

    pub async fn enqueue_gap(
        &self,
        backfill_config: &BackfillConfig,
        rpc_config: &RpcConfig,
        chain_id: u64,
        source: &str,
        from_block: u64,
        to_block: u64,
    ) -> Result<BackfillRunPlan, AppError> {
        let job_key = job_key(chain_id, &backfill_config.job_name);
        self.upsert_job(&job_key, chain_id, source, rpc_config, backfill_config)
            .await?;

        let gap_id = self
            .client
            .query_one(
                "
                INSERT INTO app.backfill_gap_ranges (
                    job_key,
                    from_block,
                    to_block,
                    status,
                    claimed_at,
                    run_started_at,
                    run_finished_at,
                    last_error
                )
                VALUES ($1, $2, $3, 'pending', NULL, NULL, NULL, NULL)
                ON CONFLICT (job_key, from_block, to_block)
                DO UPDATE SET
                    status = CASE
                        WHEN app.backfill_gap_ranges.status = 'completed' THEN app.backfill_gap_ranges.status
                        ELSE 'pending'
                    END,
                    claimed_at = CASE
                        WHEN app.backfill_gap_ranges.status = 'completed' THEN app.backfill_gap_ranges.claimed_at
                        ELSE NULL
                    END,
                    run_started_at = CASE
                        WHEN app.backfill_gap_ranges.status = 'completed' THEN app.backfill_gap_ranges.run_started_at
                        ELSE NULL
                    END,
                    run_finished_at = CASE
                        WHEN app.backfill_gap_ranges.status = 'completed' THEN app.backfill_gap_ranges.run_finished_at
                        ELSE NULL
                    END,
                    last_error = CASE
                        WHEN app.backfill_gap_ranges.status = 'completed' THEN app.backfill_gap_ranges.last_error
                        ELSE NULL
                    END,
                    updated_at = NOW()
                RETURNING id
                ",
                &[
                    &job_key,
                    &to_i64(from_block, "from_block")?,
                    &to_i64(to_block, "to_block")?,
                ],
            )
            .await?
            .get(0);

        Ok(BackfillRunPlan {
            gap_id,
            job_key,
            from_block,
            to_block,
        })
    }

    pub async fn job_last_completed_to_block(
        &self,
        chain_id: u64,
        job_name: &str,
    ) -> Result<Option<u64>, AppError> {
        let job_key = job_key(chain_id, job_name);
        self.last_completed_to_block(&job_key).await
    }

    pub async fn record_chunk(
        &self,
        plan: &BackfillRunPlan,
        chunk_from_block: u64,
        chunk_to_block: u64,
        fetched_logs_count: usize,
    ) -> Result<(), AppError> {
        let fetched_logs_count = i64::try_from(fetched_logs_count).map_err(|_| {
            AppError::InvalidPayload("fetched log count exceeded postgres bigint".to_owned())
        })?;
        let chunk_from_block_i64 = to_i64(chunk_from_block, "chunk_from_block")?;
        let chunk_to_block_i64 = to_i64(chunk_to_block, "chunk_to_block")?;

        self.client
            .execute(
                "
                INSERT INTO app.backfill_chunk_progress (
                    gap_id,
                    chunk_from_block,
                    chunk_to_block,
                    fetched_logs_count
                )
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (gap_id, chunk_from_block, chunk_to_block)
                DO UPDATE SET
                    fetched_logs_count = EXCLUDED.fetched_logs_count,
                    created_at = NOW()
                ",
                &[&plan.gap_id, &chunk_from_block_i64, &chunk_to_block_i64, &fetched_logs_count],
            )
            .await?;

        self.client
            .execute(
                "
                UPDATE app.backfill_gap_ranges
                SET last_completed_to_block = $2,
                    updated_at = NOW()
                WHERE id = $1
                ",
                &[&plan.gap_id, &chunk_to_block_i64],
            )
            .await?;

        self.client
            .execute(
                "
                UPDATE app.backfill_jobs
                SET last_completed_to_block = $2,
                    updated_at = NOW()
                WHERE job_key = $1
                ",
                &[&plan.job_key, &chunk_to_block_i64],
            )
            .await?;

        Ok(())
    }

    pub async fn mark_completed(
        &self,
        plan: &BackfillRunPlan,
        final_completed_to_block: Option<u64>,
    ) -> Result<(), AppError> {
        let final_completed_to_i64 = final_completed_to_block
            .map(|value| to_i64(value, "final_completed_to_block"))
            .transpose()?;

        self.client
            .execute(
                "
                UPDATE app.backfill_gap_ranges
                SET status = 'completed',
                    run_finished_at = NOW(),
                    last_error = NULL,
                    last_completed_to_block = COALESCE($2, last_completed_to_block),
                    updated_at = NOW()
                WHERE id = $1
                ",
                &[&plan.gap_id, &final_completed_to_i64],
            )
            .await?;

        self.client
            .execute(
                "
                UPDATE app.backfill_jobs
                SET last_run_status = 'completed',
                    last_run_finished_at = NOW(),
                    last_error = NULL,
                    last_completed_to_block = COALESCE($2, last_completed_to_block),
                    updated_at = NOW()
                WHERE job_key = $1
                ",
                &[&plan.job_key, &final_completed_to_i64],
            )
            .await?;

        Ok(())
    }

    pub async fn mark_failed(
        &self,
        plan: &BackfillRunPlan,
        error_message: &str,
        last_completed_to_block: Option<u64>,
    ) -> Result<(), AppError> {
        let last_completed_to_i64 = last_completed_to_block
            .map(|value| to_i64(value, "last_completed_to_block"))
            .transpose()?;
        let error_text = truncate_error(error_message);

        self.client
            .execute(
                "
                UPDATE app.backfill_gap_ranges
                SET status = 'failed',
                    run_finished_at = NOW(),
                    last_error = $2,
                    last_completed_to_block = COALESCE($3, last_completed_to_block),
                    updated_at = NOW()
                WHERE id = $1
                ",
                &[&plan.gap_id, &error_text, &last_completed_to_i64],
            )
            .await?;

        self.client
            .execute(
                "
                UPDATE app.backfill_jobs
                SET last_run_status = 'failed',
                    last_run_finished_at = NOW(),
                    last_error = $2,
                    last_completed_to_block = COALESCE($3, last_completed_to_block),
                    updated_at = NOW()
                WHERE job_key = $1
                ",
                &[&plan.job_key, &error_text, &last_completed_to_i64],
            )
            .await?;

        Ok(())
    }

    async fn bootstrap(&self) -> Result<(), AppError> {
        self.client
            .batch_execute(
                "
                CREATE SCHEMA IF NOT EXISTS app;

                CREATE TABLE IF NOT EXISTS app.backfill_jobs (
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

                CREATE TABLE IF NOT EXISTS app.backfill_gap_ranges (
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
                    UNIQUE(job_key, from_block, to_block)
                );

                CREATE INDEX IF NOT EXISTS idx_backfill_gap_ranges_job_status
                    ON app.backfill_gap_ranges(job_key, status, from_block);

                CREATE TABLE IF NOT EXISTS app.backfill_chunk_progress (
                    id BIGSERIAL PRIMARY KEY,
                    gap_id BIGINT NOT NULL REFERENCES app.backfill_gap_ranges(id) ON DELETE CASCADE,
                    chunk_from_block BIGINT NOT NULL,
                    chunk_to_block BIGINT NOT NULL,
                    fetched_logs_count BIGINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(gap_id, chunk_from_block, chunk_to_block)
                );

                CREATE TABLE IF NOT EXISTS app.ingestor_head_state (
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
                ",
            )
            .await?;

        Ok(())
    }

    pub async fn record_realtime_head_seen(
        &self,
        chain_id: u64,
        source: &str,
        head_number: u64,
    ) -> Result<(), AppError> {
        let chain_id = to_i64(chain_id, "chain_id")?;
        let head_number = to_i64(head_number, "head_number")?;

        self.client
            .execute(
                "
                INSERT INTO app.ingestor_head_state (
                    chain_id,
                    source,
                    last_seen_head,
                    last_head_seen_at,
                    last_error
                )
                VALUES ($1, $2, $3, NOW(), NULL)
                ON CONFLICT (chain_id, source)
                DO UPDATE SET
                    last_seen_head = GREATEST(
                        COALESCE(app.ingestor_head_state.last_seen_head, EXCLUDED.last_seen_head),
                        EXCLUDED.last_seen_head
                    ),
                    last_head_seen_at = NOW(),
                    last_error = NULL,
                    updated_at = NOW()
                ",
                &[&chain_id, &source, &head_number],
            )
            .await?;

        Ok(())
    }

    pub async fn record_realtime_fetch_completed(
        &self,
        chain_id: u64,
        source: &str,
        from_block: u64,
        to_block: u64,
    ) -> Result<(), AppError> {
        let chain_id = to_i64(chain_id, "chain_id")?;
        let from_block = to_i64(from_block, "from_block")?;
        let to_block = to_i64(to_block, "to_block")?;

        self.client
            .execute(
                "
                INSERT INTO app.ingestor_head_state (
                    chain_id,
                    source,
                    last_seen_head,
                    last_fetched_head,
                    last_fetch_from_block,
                    last_head_seen_at,
                    last_fetch_completed_at,
                    last_error
                )
                VALUES ($1, $2, $3, $3, $4, NOW(), NOW(), NULL)
                ON CONFLICT (chain_id, source)
                DO UPDATE SET
                    last_seen_head = GREATEST(
                        COALESCE(app.ingestor_head_state.last_seen_head, EXCLUDED.last_seen_head),
                        EXCLUDED.last_seen_head
                    ),
                    last_fetched_head = GREATEST(
                        COALESCE(app.ingestor_head_state.last_fetched_head, EXCLUDED.last_fetched_head),
                        EXCLUDED.last_fetched_head
                    ),
                    last_fetch_from_block = EXCLUDED.last_fetch_from_block,
                    last_fetch_completed_at = NOW(),
                    last_error = NULL,
                    updated_at = NOW()
                ",
                &[&chain_id, &source, &to_block, &from_block],
            )
            .await?;

        Ok(())
    }

    pub async fn record_realtime_state_error(
        &self,
        chain_id: u64,
        source: &str,
        error_message: &str,
    ) -> Result<(), AppError> {
        let chain_id = to_i64(chain_id, "chain_id")?;
        let error_text = truncate_error(error_message);

        self.client
            .execute(
                "
                INSERT INTO app.ingestor_head_state (
                    chain_id,
                    source,
                    last_error
                )
                VALUES ($1, $2, $3)
                ON CONFLICT (chain_id, source)
                DO UPDATE SET
                    last_error = EXCLUDED.last_error,
                    updated_at = NOW()
                ",
                &[&chain_id, &source, &error_text],
            )
            .await?;

        Ok(())
    }

    pub async fn load_realtime_head_state(
        &self,
        chain_id: u64,
        source: &str,
    ) -> Result<Option<RealtimeHeadState>, AppError> {
        let chain_id = to_i64(chain_id, "chain_id")?;
        let row = self
            .client
            .query_opt(
                "
                SELECT last_seen_head, last_fetched_head
                FROM app.ingestor_head_state
                WHERE chain_id = $1 AND source = $2
                ",
                &[&chain_id, &source],
            )
            .await?;

        row.map(|row| {
            Ok(RealtimeHeadState {
                last_seen_head: row
                    .get::<_, Option<i64>>("last_seen_head")
                    .map(|value| to_u64(value, "last_seen_head"))
                    .transpose()?,
                last_fetched_head: row
                    .get::<_, Option<i64>>("last_fetched_head")
                    .map(|value| to_u64(value, "last_fetched_head"))
                    .transpose()?,
            })
        })
        .transpose()
    }

    async fn upsert_job(
        &self,
        job_key: &str,
        chain_id: u64,
        source: &str,
        rpc_config: &RpcConfig,
        backfill_config: &BackfillConfig,
    ) -> Result<(), AppError> {
        let chain_id = to_i64(chain_id, "chain_id")?;
        let confirmations = to_i64(backfill_config.confirmations, "confirmations")?;

        self.client
            .execute(
                "
                INSERT INTO app.backfill_jobs (
                    job_key,
                    chain_id,
                    source,
                    filter_json,
                    confirmations,
                    last_run_status
                )
                VALUES ($1, $2, $3, $4, $5, 'idle')
                ON CONFLICT (job_key)
                DO UPDATE SET
                    chain_id = EXCLUDED.chain_id,
                    source = EXCLUDED.source,
                    filter_json = EXCLUDED.filter_json,
                    confirmations = EXCLUDED.confirmations,
                    updated_at = NOW()
                ",
                &[&job_key, &chain_id, &source, &rpc_config.log_filter, &confirmations],
            )
            .await?;

        Ok(())
    }

    async fn claim_next_gap(&self, job_key: &str) -> Result<Option<BackfillRunPlan>, AppError> {
        let row = self
            .client
            .query_opt(
                "
                UPDATE app.backfill_gap_ranges
                SET status = 'running',
                    claimed_at = NOW(),
                    run_started_at = NOW(),
                    run_finished_at = NULL,
                    last_error = NULL,
                    updated_at = NOW()
                WHERE id = (
                    SELECT id
                    FROM app.backfill_gap_ranges
                    WHERE job_key = $1
                      AND status IN ('pending', 'failed')
                    ORDER BY from_block ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, from_block, to_block
                ",
                &[&job_key],
            )
            .await?;

        row.map(|row| self.plan_from_row(job_key, &row)).transpose()
    }

    async fn last_completed_to_block(&self, job_key: &str) -> Result<Option<u64>, AppError> {
        let row = self
            .client
            .query_opt(
                "SELECT last_completed_to_block FROM app.backfill_jobs WHERE job_key = $1",
                &[&job_key],
            )
            .await?;

        row.and_then(|row| row.get::<_, Option<i64>>(0))
            .map(|value| to_u64(value, "last_completed_to_block"))
            .transpose()
    }

    async fn create_or_reopen_gap(
        &self,
        job_key: &str,
        from_block: u64,
        to_block: u64,
    ) -> Result<i64, AppError> {
        let from_block = to_i64(from_block, "from_block")?;
        let to_block = to_i64(to_block, "to_block")?;
        let row = self
            .client
            .query_one(
                "
                INSERT INTO app.backfill_gap_ranges (
                    job_key,
                    from_block,
                    to_block,
                    status,
                    claimed_at,
                    run_started_at,
                    run_finished_at,
                    last_error
                )
                VALUES ($1, $2, $3, 'running', NOW(), NOW(), NULL, NULL)
                ON CONFLICT (job_key, from_block, to_block)
                DO UPDATE SET
                    status = 'running',
                    claimed_at = NOW(),
                    run_started_at = NOW(),
                    run_finished_at = NULL,
                    last_error = NULL,
                    updated_at = NOW()
                RETURNING id
                ",
                &[&job_key, &from_block, &to_block],
            )
            .await?;

        Ok(row.get(0))
    }

    async fn mark_job_running(
        &self,
        job_key: &str,
        from_block: u64,
        to_block: u64,
    ) -> Result<(), AppError> {
        let from_block = to_i64(from_block, "from_block")?;
        let to_block = to_i64(to_block, "to_block")?;

        self.client
            .execute(
                "
                UPDATE app.backfill_jobs
                SET last_requested_from_block = $2,
                    last_requested_to_block = $3,
                    last_run_started_at = NOW(),
                    last_run_finished_at = NULL,
                    last_run_status = 'running',
                    last_error = NULL,
                    updated_at = NOW()
                WHERE job_key = $1
                ",
                &[&job_key, &from_block, &to_block],
            )
            .await?;

        Ok(())
    }

    fn plan_from_row(&self, job_key: &str, row: &Row) -> Result<BackfillRunPlan, AppError> {
        let gap_id = row.get::<_, i64>("id");
        let from_block = to_u64(row.get::<_, i64>("from_block"), "from_block")?;
        let to_block = to_u64(row.get::<_, i64>("to_block"), "to_block")?;

        Ok(BackfillRunPlan {
            gap_id,
            job_key: job_key.to_owned(),
            from_block,
            to_block,
        })
    }
}

fn job_key(chain_id: u64, job_name: &str) -> String {
    format!("chain-{chain_id}:{}", job_name.trim())
}

fn to_i64(value: u64, field: &str) -> Result<i64, AppError> {
    i64::try_from(value).map_err(|_| {
        AppError::InvalidPayload(format!("{field} exceeded postgres bigint range"))
    })
}

fn to_u64(value: i64, field: &str) -> Result<u64, AppError> {
    u64::try_from(value)
        .map_err(|_| AppError::InvalidPayload(format!("{field} was negative in postgres state")))
}

fn truncate_error(error_message: &str) -> String {
    const MAX_ERROR_LEN: usize = 2000;
    let mut truncated = error_message.trim().to_owned();
    truncated.truncate(MAX_ERROR_LEN);
    truncated
}
