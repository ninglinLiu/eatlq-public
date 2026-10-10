"""Transaction submission layer — separates READ vs SEND RPC lanes.

SEND lane: MEV-protected endpoint (QuickNode MEV / Blink Base RPC). Only
  send_raw_transaction goes here — keeps swap intent off public routing.
READ lane: archive + simulation (eth_call, estimateGas, slot0, QuoterV2).

Flashblocks: poll receipt every 200ms using standard wait; Base preconfirms
  in ~200ms so short poll interval catches inclusion faster than 2s block wait.

Refs:
  QuickNode MEV Protection (Base): https://www.quicknode.com/docs/base/endpoints
  Blink Base integration: https://docs.blinklabs.xyz/blink/base/api-reference
  Base Flashblocks: https://docs.base.org/base-chain/flashblocks/overview
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional
from urllib.parse import urlparse

from web3 import Web3
from web3.types import TxReceipt

try:
    from notify import send_telegram
except Exception:  # pragma: no cover - notify is always present in-container
    def send_telegram(_message: str) -> bool:
        return False

log = logging.getLogger(__name__)

# Module-level send Web3 — set once at executor startup via init_send_lane().
_send_w3: Optional[Web3] = None
_send_w3_fallback: Optional[Web3] = None
_send_rpc_label: str = "default"

# Soft degrade (primary died, fallback served the call) stays in logs only.
# Hard outage (both lanes down / tx not broadcast) pages Telegram once per
# incident, then at most every HARD interval until the lane recovers.
RPC_ALERT_MIN_INTERVAL_SEC = float(os.environ.get("EXECUTOR_RPC_ALERT_INTERVAL_SEC", "300"))
RPC_HARD_REPEAT_SEC = float(os.environ.get("EXECUTOR_RPC_HARD_ALERT_INTERVAL_SEC", "900"))
_rpc_alert_last: dict = {}
_rpc_hard_active: dict = {}


def _rpc_mark_recovered(*kinds: str) -> None:
    for kind in kinds:
        _rpc_hard_active.pop(kind, None)


def _rpc_alert(kind: str, message: str, *, escalate: bool = False) -> bool:
    """Telegram only for real outages.

    Soft (`escalate=False`): fallback/retry saved the call — log, no Telegram.
    Hard (`escalate=True`): the call failed. First incident always pages;
    repeats of the same ongoing outage are held to RPC_HARD_REPEAT_SEC so a
    429 storm cannot flood the chat. After `_rpc_mark_recovered`, the next
    hard failure pages immediately again.
    """
    if not escalate:
        log.warning("RPC soft degrade [%s] (telegram suppressed): %s", kind, message)
        return False
    now = time.monotonic()
    last = float(_rpc_alert_last.get(kind, 0.0) or 0.0)
    already = bool(_rpc_hard_active.get(kind))
    if already and (now - last) < RPC_HARD_REPEAT_SEC:
        log.warning(
            "RPC hard [%s] still down; telegram held for %.0fs: %s",
            kind,
            RPC_HARD_REPEAT_SEC - (now - last),
            message,
        )
        return False
    _rpc_alert_last[kind] = now
    _rpc_hard_active[kind] = True
    send_telegram(f"🚨 ESCALATED Keystone RPC down [{kind}]\n{message}")
    return True


class _ReadFallbackHTTPProvider(Web3.HTTPProvider):
    """HTTPProvider that retries transient disconnects, then walks public fallbacks.

    Public Base RPCs drop idle keep-alives. The first recovery is always: close the
    dead session and retry the same URL. Only after those retries fail do we rotate.
    The fallbacks are intentionally read-only: raw transaction submission still goes
    through the dedicated SEND lane so a public read backup never leaks swap intent.
    """

    def __init__(
        self,
        primary_url: str,
        fallback_url: str = "",
        request_kwargs: Optional[dict] = None,
        extra_urls: Optional[list] = None,
        retries: int = 3,
    ):
        super().__init__(primary_url, request_kwargs=request_kwargs)
        self._retries = max(1, int(retries))
        self._lanes: list[Web3.HTTPProvider] = []
        extras = extra_urls or []
        urls = []
        for url in [fallback_url, *extras]:
            url = (url or "").strip()
            if url and url != primary_url and url not in urls:
                urls.append(url)
        for url in urls:
            self._lanes.append(Web3.HTTPProvider(url, request_kwargs=request_kwargs))
        self._fallback = self._lanes[0] if self._lanes else None
        self._fallback_label = _rpc_label(urls[0]) if urls else ""

    def _call_lane(self, provider: Web3.HTTPProvider, method, params):
        if provider is self:
            return super().make_request(method, params)
        return provider.make_request(method, params)

    def make_request(self, method, params):
        providers = [self, *self._lanes]
        last_err: Optional[Exception] = None
        primary_err: Optional[Exception] = None
        for i, provider in enumerate(providers):
            label = _rpc_label(getattr(provider, "endpoint_uri", "") or "")
            for attempt in range(1, self._retries + 1):
                try:
                    out = self._call_lane(provider, method, params)
                    _rpc_mark_recovered("read_both_down")
                    return out
                except Exception as e:
                    last_err = e
                    if i == 0:
                        primary_err = e
                    transient = _is_transient_rpc_error(e)
                    log.warning(
                        "READ RPC %s failed for %s (attempt %d/%d, transient=%s): %s",
                        label,
                        method,
                        attempt,
                        self._retries,
                        transient,
                        e,
                    )
                    if transient and attempt < self._retries:
                        _reset_provider_session(provider)
                        time.sleep(0.15 * attempt)
                        continue
                    break
            if i == 0 and self._lanes:
                _rpc_alert(
                    "read_primary",
                    f"primary READ RPC failed ({primary_err}); using fallback {self._fallback_label}",
                )
        _rpc_alert(
            "read_both_down",
            f"BOTH read lanes down — primary({primary_err}) + fallback {self._fallback_label}({last_err})",
            escalate=True,
        )
        raise last_err if last_err is not None else RuntimeError("READ RPC failed")


def _rpc_label(url: str) -> str:
    host = (urlparse(url).netloc or url).split("@")[-1]
    low = host.lower()
    if "quiknode.pro" in low:
        provider = "quicknode"
    elif "drpc.org" in low:
        provider = "drpc"
    elif "publicnode" in low:
        provider = "publicnode"
    elif "1rpc.io" in low:
        provider = "1rpc"
    elif "tenderly" in low:
        provider = "tenderly-public"
    elif "base.org" in low:
        provider = "base-public"
    else:
        provider = "custom"
    return f"{provider}:{host}"


def _is_transient_rpc_error(exc: BaseException) -> bool:
    """Public nodes drop keep-alives; those should retry the same lane."""
    text = str(exc).lower()
    needles = (
        "remotely disconnected",
        "remote end closed",
        "connection aborted",
        "connection reset",
        "broken pipe",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "503",
        "502",
        "429",
        "eof occurred",
        "reset by peer",
        "connection refused",
        "protocolerror",
        "cannot connect",
    )
    return any(n in text for n in needles)


def _reset_provider_session(provider) -> None:
    """Drop a dead keep-alive session so the next attempt opens a new TCP/TLS socket."""
    for attr in ("_request_session", "session"):
        session = getattr(provider, attr, None)
        if session is None:
            continue
        try:
            session.close()
        except Exception:
            pass
        try:
            import requests

            setattr(provider, attr, requests.Session())
        except Exception:
            pass


def catchup_windows(
    last_processed: Optional[int],
    head: int,
    chunk: int = 20,
    max_blocks: int = 300,
) -> list:
    """Split a reconnect gap into inclusive [from, to] eth_getLogs windows.

    last_processed=None means first tick: only the current head (no historical sweep).
    """
    if head < 0 or chunk <= 0 or max_blocks <= 0:
        return []
    if last_processed is None:
        return [(head, head)]
    if head == last_processed:
        return []
    if head < last_processed:
        return [(head, head)]
    start = last_processed + 1
    end = min(head, start + max_blocks - 1)
    windows = []
    cur = start
    while cur <= end:
        nxt = min(cur + chunk - 1, end)
        windows.append((cur, nxt))
        cur = nxt + 1
    return windows


def _split_rpc_urls(*raw: str) -> list[str]:
    out: list[str] = []
    seen = set()
    for item in raw:
        if not item:
            continue
        for part in str(item).replace(";", ",").split(","):
            url = part.strip()
            if url and url not in seen:
                seen.add(url)
                out.append(url)
    return out


def _rpc_request_kwargs(timeout: int = 30) -> dict:
    return {
        "timeout": timeout,
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; EATLQ/1.0)",
            "Content-Type": "application/json",
        },
    }


def init_send_lane(read_rpc_url: str) -> tuple[Web3, Web3]:
    """Create READ and SEND Web3 instances. SEND defaults to EXECUTOR_SEND_RPC_URL
    or EXECUTOR_RPC_URL; falls back to read_rpc_url if unset."""
    global _send_w3, _send_w3_fallback, _send_rpc_label
    read_url = read_rpc_url.strip()
    send_url = (
        os.environ.get("EXECUTOR_SEND_RPC_URL", "").strip()
        or os.environ.get("EXECUTOR_RPC_URL", "").strip()
        or read_url
    )
    fallback_url = os.environ.get("EXECUTOR_SEND_RPC_URL_FALLBACK", "").strip()
    read_fallback_url = os.environ.get("EXECUTOR_READ_RPC_URL_FALLBACK", "").strip()
    extra_read = _split_rpc_urls(os.environ.get("EXECUTOR_READ_RPC_EXTRAS", ""))
    retries = int(os.environ.get("EXECUTOR_RPC_RETRY", "3") or "3")
    request_kwargs = _rpc_request_kwargs(30)
    if (read_fallback_url and read_fallback_url != read_url) or extra_read:
        read_w3 = Web3(
            _ReadFallbackHTTPProvider(
                read_url,
                read_fallback_url,
                request_kwargs=request_kwargs,
                extra_urls=extra_read,
                retries=retries,
            )
        )
    else:
        read_w3 = Web3(
            _ReadFallbackHTTPProvider(
                read_url,
                "",
                request_kwargs=request_kwargs,
                retries=retries,
            )
        )
    _send_w3 = Web3(Web3.HTTPProvider(send_url, request_kwargs=request_kwargs))
    _send_w3_fallback = None
    if fallback_url and fallback_url != send_url:
        _send_w3_fallback = Web3(Web3.HTTPProvider(fallback_url, request_kwargs=request_kwargs))
    mev_flag = os.environ.get("EXECUTOR_MEV_PROTECTED", "auto").lower()
    send_is_quicknode = "quiknode.pro" in send_url.lower()
    send_is_blink = "blink" in send_url.lower()
    send_is_protected = send_is_quicknode or send_is_blink
    if send_is_protected and mev_flag in ("1", "true", "required", "auto"):
        _send_rpc_label = "MEV-protected-quicknode" if send_is_quicknode else "MEV-protected"
    elif send_url != read_url:
        _send_rpc_label = "dedicated"
    else:
        _send_rpc_label = "shared"
    log.info(
        "tx lanes: READ=%s SEND=%s (%s, mev_flag=%s)",
        _rpc_label(read_url),
        _rpc_label(send_url),
        _send_rpc_label,
        mev_flag,
    )
    if read_fallback_url and read_fallback_url != read_url:
        log.info("READ fallback lane: %s", _rpc_label(read_fallback_url))
    if mev_flag in ("1", "true", "required") and not send_is_protected:
        log.warning(
            "EXECUTOR_MEV_PROTECTED=%s but SEND RPC is not a recognized protected endpoint — "
            "point EXECUTOR_SEND_RPC_URL at QuickNode (MEV on) or Blink Base RPC"
            % mev_flag
        )
    if _send_w3_fallback is not None:
        log.info("SEND fallback lane: %s", _rpc_label(fallback_url))
    return read_w3, _send_w3


def get_send_w3(fallback: Web3) -> Web3:
    return _send_w3 if _send_w3 is not None else fallback


def _send_with_retries(w3: Web3, signed_raw: bytes, retries: int):
    last_err: Optional[Exception] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            return w3.eth.send_raw_transaction(signed_raw)
        except Exception as e:
            last_err = e
            if _is_transient_rpc_error(e) and attempt < retries:
                log.warning("SEND RPC transient fail attempt %d/%d: %s", attempt, retries, e)
                _reset_provider_session(getattr(w3, "provider", w3))
                time.sleep(0.15 * attempt)
                continue
            raise
    raise last_err if last_err is not None else RuntimeError("SEND RPC failed")


def send_raw_transaction(signed_raw: bytes, read_w3: Web3):
    """Broadcast via SEND lane; retry transient drops, then try fallback URL."""
    primary = _send_w3 if _send_w3 is not None else read_w3
    retries = int(os.environ.get("EXECUTOR_RPC_RETRY", "3") or "3")
    try:
        out = _send_with_retries(primary, signed_raw, retries)
        _rpc_mark_recovered("send_both_down", "send_down_no_fallback")
        return out
    except Exception as e:
        if _send_w3_fallback is None:
            _rpc_alert(
                "send_down_no_fallback",
                f"SEND RPC failed and no fallback configured ({e}) — tx not broadcast",
                escalate=True,
            )
            raise
        log.warning("primary SEND RPC failed (%s), trying fallback", e)
        _rpc_alert("send_primary", f"primary SEND RPC failed ({e}); trying fallback")
        try:
            out = _send_with_retries(_send_w3_fallback, signed_raw, retries)
            _rpc_mark_recovered("send_both_down", "send_down_no_fallback")
            return out
        except Exception as e2:
            _rpc_alert(
                "send_both_down",
                f"BOTH send lanes down — primary({e}) + fallback({e2}) — tx not broadcast",
                escalate=True,
            )
            raise


def wait_for_receipt(
    read_w3: Web3,
    tx_hash,
    timeout_sec: float = 60.0,
    poll_ms: int = 200,
) -> TxReceipt:
    """Poll for receipt. 200ms poll aligns with Base Flashblocks cadence."""
    deadline = time.monotonic() + timeout_sec
    h = tx_hash.hex() if hasattr(tx_hash, "hex") else tx_hash
    while time.monotonic() < deadline:
        try:
            receipt = read_w3.eth.get_transaction_receipt(tx_hash)
            if receipt is not None:
                return receipt
        except Exception:
            pass
        time.sleep(poll_ms / 1000.0)
    raise TimeoutError(f"receipt not found for {h} after {timeout_sec}s")
