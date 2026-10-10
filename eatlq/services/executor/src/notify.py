"""Lightweight Telegram notifications for executor events."""
from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


class TelegramConfigError(RuntimeError):
    """Raised when live mode is enabled but Telegram alerting is unusable."""


def telegram_configured() -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return bool(token and chat_id)


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    text = message[:3900]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        log.warning("telegram send failed: %s", e)
        return False


def validate_telegram_or_raise(live_mode: bool) -> None:
    """Startup gate: in live mode, alerting MUST work or we refuse to trade.

    Trading live without a working alert channel means failures (honeypots,
    stuck sells, RPC outages) would be silent. We require the bot token + chat
    id to be present AND a real test message to send successfully. In non-live
    (paper/dry-run) mode this is a no-op so local/dev runs are unaffected.

    Raises TelegramConfigError on missing config or a failed test send.
    """
    if not live_mode:
        return
    if not telegram_configured():
        raise TelegramConfigError(
            "LIVE_TRADING_MODE=true requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "so failures are not silent. Refusing to start."
        )
    ok = send_telegram("✅ Keystone executor 启动自检：Telegram 告警通道可用。")
    if not ok:
        raise TelegramConfigError(
            "LIVE_TRADING_MODE=true but the Telegram startup test message failed to "
            "send (bad token/chat_id or network). Refusing to start."
        )
