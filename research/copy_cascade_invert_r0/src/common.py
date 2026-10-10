"""Shared helpers. No RPC. Writes only under the project root."""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/root/copy_cascade_invert_r0")
CONFIG = json.loads((ROOT / "configs/freeze.json").read_text())
PATHS = json.loads((ROOT / "configs/paths.json").read_text())

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def norm_addr(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s.startswith("0x") or s.startswith("0X"):
        return s.lower()
    return s


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def load_exclusions() -> set[str]:
    raw = read_json(Path(PATHS["actor_flow_exclusions"]))
    out = set()
    for row in raw.get("addresses", []):
        cat = row.get("category")
        if cat in {"router", "aggregator", "npm", "quoter", "permit", "account_infra", "system"}:
            out.add(norm_addr(row.get("address")))
    return out


def http_get(
    url: str,
    *,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    sleep_s: float = 1.6,
) -> tuple[int, bytes, dict[str, str]]:
    """Public HTTPS GET. Rate-limited. Never talks to our RPC."""
    time.sleep(sleep_s)
    hdrs = {"User-Agent": UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            meta = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), body, meta
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp else b""
        return int(exc.code), body, {"reason": str(exc.reason)}
    except Exception as exc:  # noqa: BLE001 — collectors must survive DNS/TLS/WAF
        return 0, b"", {"error": type(exc).__name__, "detail": str(exc)[:300]}


def snapshot_record(
    *,
    source: str,
    chain: str,
    url: str,
    status: int,
    body: bytes,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rec = {
        "source": source,
        "chain": chain,
        "url": url,
        "captured_at": utc_now_iso(),
        "http_status": status,
        "sha256": sha256_bytes(body) if body else None,
        "nbytes": len(body),
        "ok": 200 <= status < 300 and bool(body),
    }
    if extra:
        rec.update(extra)
    return rec


def parse_iso(ts: Any):
    if ts is None or ts == "":
        return None
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    s = str(ts).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def median(xs: list[float]) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    if n % 2:
        return ys[mid]
    return 0.5 * (ys[mid - 1] + ys[mid])


def top1_removed_mean(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    ys = list(xs)
    ys.remove(max(ys))
    return mean(ys)


def ensure_no_live() -> None:
    lock = ROOT / "NO_LIVE.lock"
    if not lock.exists():
        raise RuntimeError("NO_LIVE.lock missing")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
