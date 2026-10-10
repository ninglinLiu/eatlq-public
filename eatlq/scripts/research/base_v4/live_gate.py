"""Live executor yield gate — research must pause when V3 has open positions."""
from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

REDIS_KEY = "positions:open_pools"
DEFAULT_REDIS_URL = os.environ.get("RESEARCH_REDIS_URL", "redis://127.0.0.1:6379/0")


def _redis_llen(key: str) -> Optional[int]:
    try:
        import redis  # type: ignore
        r = redis.from_url(DEFAULT_REDIS_URL, socket_connect_timeout=2)
        return r.llen(key)
    except Exception:
        pass
    # fallback: local redis-cli
    try:
        out = subprocess.check_output(
            ["redis-cli", "-u", DEFAULT_REDIS_URL, "LLEN", key],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
        return int(out)
    except Exception:
        pass
    # fallback: docker redis (read-only; does not touch live executor config)
    try:
        out = subprocess.check_output(
            ["docker", "exec", "eatlq-redis-1", "redis-cli", "LLEN", key],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
        return int(out)
    except Exception:
        return None


def wait_for_live_clear(poll_s: int = 60, max_wait_s: Optional[int] = None) -> bool:
    """Block until positions:open_pools is empty. Returns False if max_wait exceeded."""
    t0 = time.time()
    while True:
        n = _redis_llen(REDIS_KEY)
        if n is None:
            # cannot read redis — conservative: assume live may be active
            print("[live_gate] WARN: cannot read Redis; sleeping 60s", flush=True)
            time.sleep(poll_s)
        elif n == 0:
            return True
        else:
            print(f"[live_gate] {n} open pools — research paused {poll_s}s", flush=True)
            time.sleep(poll_s)
        if max_wait_s and time.time() - t0 > max_wait_s:
            return False


def executor_healthy(container: str = "eatlq-executor-1") -> bool:
    """Best-effort docker health check."""
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
        return out in ("healthy", "")  # no healthcheck => empty
    except Exception:
        return True  # cannot inspect — don't block collection scripts
