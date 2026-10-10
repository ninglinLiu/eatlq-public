use crate::{
    config::RedisConfig,
    endpoint_pool::EndpointPool,
};
use redis::Client;
use tracing::{debug, warn};

fn unix_ms_now() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

pub async fn publish_pool_health(
    redis_config: &RedisConfig,
    ws_pool: &EndpointPool,
    http_pool: &EndpointPool,
) {
    let client = match Client::open(redis_config.url.as_str()) {
        Ok(c) => c,
        Err(error) => {
            warn!(error = %error, "pool health redis client open failed");
            return;
        }
    };
    let mut connection = match client.get_multiplexed_async_connection().await {
        Ok(c) => c,
        Err(error) => {
            warn!(error = %error, "pool health redis connect failed");
            return;
        }
    };

    let mut fields: Vec<(String, String)> = Vec::new();
    fields.extend(ws_pool.redis_health_fields());
    fields.extend(http_pool.redis_health_fields());
    fields.push(("updated_at_ms".to_owned(), unix_ms_now().to_string()));

    if fields.is_empty() {
        return;
    }

    let flat: Vec<(&str, &str)> = fields
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();

    if let Err(error) = redis::cmd("HSET")
        .arg("ingestor:pool_health")
        .arg(flat)
        .query_async::<()>(&mut connection)
        .await
    {
        warn!(error = %error, "failed to publish ingestor:pool_health");
        return;
    }
    debug!("published ingestor:pool_health");
    touch_docker_health_file(&redis_config.healthcheck_path);
}

fn touch_docker_health_file(path: &str) {
    if let Err(error) = std::fs::write(path, unix_ms_now().to_string()) {
        warn!(error = %error, path, "failed to touch docker healthcheck file");
    } else {
        debug!(path, "updated ingestor healthcheck file");
    }
}

pub fn spawn_pool_health_reporter(
    redis_config: RedisConfig,
    ws_pool: EndpointPool,
    http_pool: EndpointPool,
    shutdown_token: tokio_util::sync::CancellationToken,
) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(30));
        loop {
            tokio::select! {
                _ = shutdown_token.cancelled() => break,
                _ = interval.tick() => {
                    publish_pool_health(&redis_config, &ws_pool, &http_pool).await;
                }
            }
        }
    });
}
