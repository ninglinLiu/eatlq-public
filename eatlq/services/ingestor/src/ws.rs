use crate::{config::WsConfig, error::AppError};
use eatlq_common::RawFrame;
use futures_util::{SinkExt, StreamExt};
use std::time::Duration;
use tokio::{sync::mpsc, time::sleep};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tokio_util::sync::CancellationToken;
use tracing::{debug, info, warn};

pub async fn run_ws_reader(
    config: WsConfig,
    tx: mpsc::Sender<RawFrame>,
    shutdown_token: CancellationToken,
) -> Result<(), AppError> {
    let mut backoff = config.reconnect_initial_backoff();

    while !shutdown_token.is_cancelled() {
        let Some((ws_url, provider)) = config.ws_pool.select() else {
            warn!("ws pool has no endpoints configured");
            if !sleep_or_shutdown(backoff, &shutdown_token).await {
                break;
            }
            backoff = (backoff * 2).min(config.reconnect_max_backoff());
            continue;
        };

        let connect_result =
            tokio::time::timeout(config.connect_timeout(), connect_async(ws_url.as_str())).await;

        match connect_result {
            Ok(Ok((ws_stream, _response))) => {
                info!(url = %ws_url, provider = %provider, "websocket connected");
                backoff = config.reconnect_initial_backoff();
                config.ws_pool.mark_success();

                let (mut sink, mut stream) = ws_stream.split();
                let subscribe_payload = config.subscribe_payload().to_string();
                if let Err(error) = sink.send(Message::Text(subscribe_payload.into())).await {
                    warn!(error = %error, provider = %provider, "subscribe send failed, rotating endpoint");
                    config.ws_pool.mark_failure("subscribe_send_failed", false, false);
                    config.ws_pool.rotate("subscribe_send_failed");
                } else {
                    info!(provider = %provider, "subscription message sent");
                    let mut last_frame_at = tokio::time::Instant::now();
                    let idle_limit = config.ws_idle();

                    loop {
                        if shutdown_token.is_cancelled() {
                            let _ = sink.send(Message::Close(None)).await;
                            return Ok(());
                        }

                        if last_frame_at.elapsed() >= idle_limit {
                            warn!(
                                provider = %provider,
                                idle_secs = idle_limit.as_secs(),
                                "websocket idle timeout (no frames), rotating endpoint"
                            );
                            config.ws_pool.mark_failure("ws_idle_timeout", false, false);
                            config.ws_pool.rotate("ws_idle_timeout");
                            info!(
                                event = "ws_endpoint_rotated",
                                reason = "ws_idle_timeout",
                                provider = %provider,
                            );
                            break;
                        }

                        let remaining_idle = idle_limit.saturating_sub(last_frame_at.elapsed());
                        let read_timeout = config.read_timeout().min(remaining_idle);

                        match tokio::time::timeout(read_timeout, stream.next()).await {
                            Ok(Some(Ok(message))) => {
                                if let Some(frame) = message_to_frame(message)? {
                                    last_frame_at = tokio::time::Instant::now();
                                    match tx.try_send(frame) {
                                        Ok(()) => {}
                                        Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
                                            warn!("raw channel full, dropping frame");
                                        }
                                        Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                                            return Err(AppError::ChannelClosed("raw frame channel"));
                                        }
                                    }
                                }
                            }
                            Ok(Some(Err(error))) => {
                                warn!(error = %error, provider = %provider, "websocket stream error, rotating");
                                config.ws_pool.mark_failure("ws_stream_error", false, false);
                                config.ws_pool.rotate("ws_stream_error");
                                info!(
                                    event = "ws_endpoint_rotated",
                                    reason = "ws_stream_error",
                                    provider = %provider,
                                );
                                break;
                            }
                            Ok(None) => {
                                warn!(provider = %provider, "websocket stream ended, rotating");
                                config.ws_pool.mark_failure("ws_stream_ended", false, false);
                                config.ws_pool.rotate("ws_stream_ended");
                                info!(
                                    event = "ws_endpoint_rotated",
                                    reason = "ws_stream_ended",
                                    provider = %provider,
                                );
                                break;
                            }
                            Err(_) => {
                                debug!("read timeout reached, sending ping");
                                if let Err(error) =
                                    sink.send(Message::Ping(Vec::new().into())).await
                                {
                                    warn!(error = %error, provider = %provider, "ping failed, rotating");
                                    config.ws_pool.mark_failure("ws_ping_failed", false, false);
                                    config.ws_pool.rotate("ws_ping_failed");
                                    info!(
                                        event = "ws_endpoint_rotated",
                                        reason = "ws_ping_failed",
                                        provider = %provider,
                                    );
                                    break;
                                }
                            }
                        }
                    }
                }
            }
            Ok(Err(error)) => {
                let err_text = error.to_string();
                let rate_limited = err_text.contains("429");
                let auth_expired = err_text.contains("401")
                    || err_text.to_ascii_lowercase().contains("auth")
                    || err_text.to_ascii_lowercase().contains("unauthorized");
                warn!(
                    error = %error,
                    provider = %provider,
                    rate_limited,
                    auth_expired,
                    "websocket connect failed, rotating"
                );
                config.ws_pool.mark_failure("ws_connect_failed", rate_limited, auth_expired);
                config.ws_pool.rotate("ws_connect_failed");
                info!(
                    event = "ws_endpoint_rotated",
                    reason = "ws_connect_failed",
                    provider = %provider,
                );
            }
            Err(_) => {
                warn!(
                    provider = %provider,
                    timeout_secs = config.connect_timeout_secs,
                    "websocket connect timed out, rotating"
                );
                config.ws_pool.mark_failure("ws_connect_timeout", false, false);
                config.ws_pool.rotate("ws_connect_timeout");
                info!(
                    event = "ws_endpoint_rotated",
                    reason = "ws_connect_timeout",
                    provider = %provider,
                );
            }
        }

        if shutdown_token.is_cancelled() {
            break;
        }

        warn!(backoff_ms = backoff.as_millis(), "backing off before reconnect");
        if !sleep_or_shutdown(backoff, &shutdown_token).await {
            break;
        }
        backoff = (backoff * 2).min(config.reconnect_max_backoff());
    }

    Ok(())
}

async fn sleep_or_shutdown(duration: Duration, shutdown_token: &CancellationToken) -> bool {
    tokio::select! {
        _ = shutdown_token.cancelled() => false,
        _ = sleep(duration) => true,
    }
}

fn message_to_frame(message: Message) -> Result<Option<RawFrame>, AppError> {
    match message {
        Message::Text(text) => Ok(Some(RawFrame {
            received_at_ms: now_millis(),
            payload_text: text.to_string(),
        })),
        Message::Binary(bytes) => {
            let payload_text = String::from_utf8(bytes.to_vec())
                .map_err(|error| AppError::InvalidPayload(error.to_string()))?;

            Ok(Some(RawFrame {
                received_at_ms: now_millis(),
                payload_text,
            }))
        }
        Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => Ok(None),
        Message::Close(frame) => {
            debug!(close_frame = ?frame, "received websocket close frame");
            Ok(None)
        }
    }
}

fn now_millis() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};

    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}
