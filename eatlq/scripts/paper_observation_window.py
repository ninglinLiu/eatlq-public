#!/usr/bin/env python3
"""
Long-window paper/dry-run observation tool (READ-ONLY).

Sprint013 S1-T3: answers the Sprint012 next-step question — "does the strategy
emit signals over a 30-60 min window?" — without adding any metrics backend
or mutating any Redis state.

Contract (mechanically enforced by tests/test_paper_observation_window.py):

- NO writes to Redis. Only XLEN / XRANGE / XINFO GROUPS / XPENDING are used.
- NO new allow_* flag. NO economic claim. NO live authorization.
- NO raw Redis URL or credential is ever written to the artifact or stdout.
- NO network read beyond the Redis host already configured by the compose stack.
- If Redis is unreachable, exit non-zero with a diagnostic — do not block forever.

Usage:
    python3 scripts/paper_observation_window.py --dry
        # print config and exit — no Redis contact, safe to run in CI.

    REDIS_URL=redis://127.0.0.1:6379/0 \\
    python3 scripts/paper_observation_window.py \\
        --window-sec 1800 --interval-sec 30

Artifacts land in ``mvp/observations/obs_<UTC timestamp>.{json,md}`` and
contain only aggregate counters (XLEN deltas, PEL depth, consumer lag), not
raw payloads.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "mvp" / "observations"

DEFAULT_STREAMS: Tuple[str, ...] = (
    "market_ticks",
    "signals",
    "signals_dead_letter",
)

DEFAULT_GROUPS: Tuple[Tuple[str, str], ...] = (
    ("market_ticks", "archiver_group"),
    ("market_ticks", "strategy_group"),
    ("signals", "execution_group"),
)

MIN_INTERVAL_SEC = 5
MAX_WINDOW_SEC = 4 * 60 * 60  # 4h upper bound; Sprint013 expects 30-60 min.

_CRED_RE = re.compile(r"(?P<scheme>\w+)://[^/@\s]+@", re.IGNORECASE)
_HEX_TOKEN_RE = re.compile(r"/[A-Fa-f0-9]{16,}")
_PRIVKEY_RE = re.compile(r"0x[A-Fa-f0-9]{64}")


def redact_url(s: str) -> str:
    """Strip credentials and long hex tokens from a URL-ish string.

    Safe for logging. Intentionally conservative: anything that looks like a
    hex token of >= 16 chars is replaced, covering QuickNode-style path
    tokens, API keys in paths, and chain private keys.
    """

    s = _CRED_RE.sub(r"\g<scheme>://[REDACTED_CREDENTIAL]@", s)
    s = _HEX_TOKEN_RE.sub("/[REDACTED_HEX_TOKEN]", s)
    s = _PRIVKEY_RE.sub("0x[REDACTED_PRIVATE_KEY]", s)
    return s


@dataclasses.dataclass
class ObservationConfig:
    redis_url: str
    window_sec: int
    interval_sec: int
    streams: Tuple[str, ...]
    groups: Tuple[Tuple[str, str], ...]
    output_dir: Path
    dry: bool

    def safe_redis_url(self) -> str:
        return redact_url(self.redis_url)


def parse_args(argv: Optional[List[str]] = None) -> ObservationConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-sec", type=int, default=1800, help="Total observation window seconds (default 1800 = 30 min; capped at 4h).")
    parser.add_argument("--interval-sec", type=int, default=30, help="Snapshot cadence seconds (default 30; minimum 5).")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Artifact directory (default mvp/observations/).",
    )
    parser.add_argument(
        "--dry",
        action="store_true",
        help="Print resolved config and exit; no Redis contact, no artifact written.",
    )
    args = parser.parse_args(argv)

    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

    interval = max(MIN_INTERVAL_SEC, int(args.interval_sec))
    window = max(interval, min(MAX_WINDOW_SEC, int(args.window_sec)))

    return ObservationConfig(
        redis_url=redis_url,
        window_sec=window,
        interval_sec=interval,
        streams=DEFAULT_STREAMS,
        groups=DEFAULT_GROUPS,
        output_dir=args.output_dir,
        dry=bool(args.dry),
    )


def _load_redis_client(url: str):
    """Import and open the Redis client lazily so the script can be AST-scanned
    without the runtime dependency being installed."""

    try:
        import redis  # noqa: WPS433 — lazy import is deliberate.
    except Exception as exc:  # pragma: no cover - import guard
        raise RuntimeError(f"redis-py is not available: {exc!r}") from exc
    return redis.Redis.from_url(url, socket_connect_timeout=5, decode_responses=False)


def _safe_xlen(client, stream: str) -> Optional[int]:
    try:
        return int(client.xlen(stream))
    except Exception as exc:  # pragma: no cover - network dependent
        return None if "no such key" in str(exc).lower() else None


def _safe_group_info(client, stream: str) -> List[Dict[str, Any]]:
    try:
        raw = client.xinfo_groups(stream)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for row in raw or []:
        item: Dict[str, Any] = {}
        for k, v in row.items() if isinstance(row, dict) else []:
            key = k.decode() if isinstance(k, bytes) else str(k)
            if isinstance(v, bytes):
                try:
                    val: Any = v.decode()
                except Exception:
                    val = str(v)
            else:
                val = v
            item[key] = val
        if item:
            out.append(item)
    return out


def _snapshot_once(client, cfg: ObservationConfig) -> Dict[str, Any]:
    snap: Dict[str, Any] = {
        "ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "streams": {},
        "groups": {},
    }
    for stream in cfg.streams:
        snap["streams"][stream] = {"xlen": _safe_xlen(client, stream)}
    for stream, group in cfg.groups:
        key = f"{stream}:{group}"
        info_rows = _safe_group_info(client, stream)
        match = next((r for r in info_rows if r.get("name") == group or r.get("name") == group.encode() or str(r.get("name")) == group), None)
        if match is None:
            snap["groups"][key] = {"present": False}
            continue
        snap["groups"][key] = {
            "present": True,
            "pending": match.get("pending"),
            "lag": match.get("lag"),
            "last_delivered_id": match.get("last-delivered-id"),
            "consumers": match.get("consumers"),
        }
    return snap


def run_dry(cfg: ObservationConfig) -> int:
    payload = {
        "mode": "dry",
        "redis_url": cfg.safe_redis_url(),
        "window_sec": cfg.window_sec,
        "interval_sec": cfg.interval_sec,
        "streams": list(cfg.streams),
        "groups": [list(pair) for pair in cfg.groups],
        "output_dir": str(cfg.output_dir.relative_to(REPO_ROOT)) if cfg.output_dir.is_relative_to(REPO_ROOT) else str(cfg.output_dir),
        "guard": "no_write_no_new_allow_flag_no_economic_claim",
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def run_observation(cfg: ObservationConfig) -> int:
    try:
        client = _load_redis_client(cfg.redis_url)
    except Exception as exc:
        print(
            f"paper_observation_window: FAIL — cannot open Redis ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    try:
        client.ping()
    except Exception as exc:
        print(
            "paper_observation_window: FAIL — Redis PING error on "
            f"{cfg.safe_redis_url()}: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 2

    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    samples: List[Dict[str, Any]] = []
    started = time.monotonic()
    t0_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(
        f"paper_observation_window: START window={cfg.window_sec}s interval={cfg.interval_sec}s "
        f"redis={cfg.safe_redis_url()}",
        flush=True,
    )
    while (time.monotonic() - started) < cfg.window_sec:
        snap = _snapshot_once(client, cfg)
        samples.append(snap)
        print(
            f"  · {snap['ts_utc']} xlen(signals)={snap['streams'].get('signals', {}).get('xlen')!r} "
            f"dead_letter={snap['streams'].get('signals_dead_letter', {}).get('xlen')!r}",
            flush=True,
        )
        sleep_for = min(cfg.interval_sec, max(0, cfg.window_sec - (time.monotonic() - started)))
        if sleep_for <= 0:
            break
        time.sleep(sleep_for)

    finished_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    deltas: Dict[str, Dict[str, Any]] = {}
    if len(samples) >= 2:
        first = samples[0]["streams"]
        last = samples[-1]["streams"]
        for stream in cfg.streams:
            a = first.get(stream, {}).get("xlen")
            b = last.get(stream, {}).get("xlen")
            deltas[stream] = {"first_xlen": a, "last_xlen": b, "delta": (b - a) if isinstance(a, int) and isinstance(b, int) else None}

    artifact: Dict[str, Any] = {
        "schema_version": "paper_observation_window.v1",
        "started_at_utc": t0_iso,
        "finished_at_utc": finished_iso,
        "redis_url": cfg.safe_redis_url(),
        "window_sec": cfg.window_sec,
        "interval_sec": cfg.interval_sec,
        "sample_count": len(samples),
        "streams_observed": list(cfg.streams),
        "groups_observed": [list(p) for p in cfg.groups],
        "xlen_deltas": deltas,
        "samples": samples,
        "guard": {
            "decision_flags": {
                "IMPLEMENTATION_ALLOWED": False,
                "TRAINING_ALLOWED": False,
                "LABELING_ALLOWED": False,
                "LIVE_ALLOWED": False,
                "PHASE6_ALLOWED": False,
                "WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED": False,
                "REAL_TRADING_ALLOWED": False,
            },
            "controlling_readout": "mvp/runs/run-004-v2-7d-alpha-readout.md",
            "controlling_verdict": "FAILED_NO_7D_ALPHA",
            "notes": "Observation-only artifact. Does not supersede the economic guard.",
        },
    }

    slug = re.sub(r"[^0-9A-Za-z]", "", t0_iso)[:15] or "obs"
    out_json = cfg.output_dir / f"obs_{slug}.json"
    out_md = cfg.output_dir / f"obs_{slug}.md"
    out_json.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    out_md.write_text(_render_markdown_summary(artifact), encoding="utf-8")

    print(
        f"paper_observation_window: OK samples={len(samples)} "
        f"artifact_json={out_json.relative_to(REPO_ROOT)} "
        f"artifact_md={out_md.relative_to(REPO_ROOT)}",
        flush=True,
    )
    return 0


def _render_markdown_summary(artifact: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Paper observation window")
    lines.append("")
    lines.append(f"- Generated: {artifact['finished_at_utc']}")
    lines.append(f"- Window: {artifact['window_sec']}s ({artifact['sample_count']} samples @ {artifact['interval_sec']}s)")
    lines.append(f"- Redis: `{artifact['redis_url']}`")
    lines.append("")
    lines.append("## XLEN deltas")
    lines.append("")
    lines.append("| stream | first_xlen | last_xlen | delta |")
    lines.append("| ------ | ---------- | --------- | ----- |")
    for stream, row in artifact.get("xlen_deltas", {}).items():
        lines.append(
            f"| {stream} | {row.get('first_xlen')!r} | {row.get('last_xlen')!r} | {row.get('delta')!r} |"
        )
    lines.append("")
    lines.append("## Guard")
    lines.append("")
    guard = artifact["guard"]
    for flag, val in guard["decision_flags"].items():
        lines.append(f"- `{flag}`: `{val}`")
    lines.append(f"- Controlling verdict: `{guard['controlling_verdict']}` from `{guard['controlling_readout']}`")
    lines.append("")
    lines.append("> Observation-only artifact. This document does not authorize training, labeling, live, or Phase 6; see `mvp/audits/failed_no_7d_alpha_guard.md`.")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    cfg = parse_args(argv)
    if cfg.dry:
        return run_dry(cfg)
    return run_observation(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
