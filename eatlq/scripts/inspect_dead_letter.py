#!/usr/bin/env python3
"""
READ-ONLY CLI for the ``signals_dead_letter`` Redis stream (Sprint013 S1-T4).

Answers the operator question "why did BUYs get rejected recently?" without
reaching for the raw executor log. Groups entries by the ``error`` taxonomy
declared in ``docs/executor-language-contract.md`` and reports counts + the
newest ``entry_id`` per bucket.

Contract (mechanically enforced by tests/test_inspect_dead_letter.py):

- NO writes. Only XRANGE is used. No XADD / XDEL / XTRIM / XCLAIM / SET / DEL.
- NO new allow_* flag. NO economic claim. NO live authorization.
- Payload content is NOT printed by default. ``--show-payloads N`` prints
  at most ``N`` chars per entry and always runs the redaction filter.

Usage:
    python3 scripts/inspect_dead_letter.py --from-json samples.json
    python3 scripts/inspect_dead_letter.py --from-json -   # read JSON list from stdin

    REDIS_URL=redis://127.0.0.1:6379/0 \\
    python3 scripts/inspect_dead_letter.py --count 500
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_STREAM = "signals_dead_letter"
DEFAULT_COUNT = 1000
REASON_TRUNCATE = 80

_CRED_RE = re.compile(r"(?P<scheme>\w+)://[^/@\s]+@", re.IGNORECASE)
_HEX_TOKEN_RE = re.compile(r"/[A-Fa-f0-9]{16,}")
_PRIVKEY_RE = re.compile(r"0x[A-Fa-f0-9]{64}")


def redact(s: str) -> str:
    s = _CRED_RE.sub(r"\g<scheme>://[REDACTED_CREDENTIAL]@", s)
    s = _HEX_TOKEN_RE.sub("/[REDACTED_HEX_TOKEN]", s)
    s = _PRIVKEY_RE.sub("0x[REDACTED_PRIVATE_KEY]", s)
    return s


def _as_text(v: Any) -> str:
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", errors="replace")
        except Exception:
            return repr(v)
    if v is None:
        return ""
    return str(v)


def normalize_entry(entry: Any) -> Dict[str, str]:
    """Accept either XRANGE-style tuples or already-decoded dicts.

    The executor writes dead-letter rows via an `XADD` call on the
    ``signals_dead_letter`` stream (see
    ``services/executor/src/main.py::_write_dead_letter``) with the field
    dict ``{entry_id: <original signal id>, error: ..., payload: ...}``.
    So XRANGE returns ``(redis_message_id, {entry_id: <signal id>, ...})``.

    - ``stream_message_id`` — the outer, auto-generated Redis stream id. Used
      for sorting / recency ordering (monotonic).
    - ``signal_entry_id`` — the ``entry_id`` field inside the dict; the id of
      the ORIGINAL ``signals`` entry that the executor failed to process.
    - ``error``, ``payload`` — as written by the executor.
    """

    stream_id = ""
    fields: Dict[str, Any] = {}

    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        stream_id_raw, raw_fields = entry
        stream_id = _as_text(stream_id_raw)
        if isinstance(raw_fields, dict):
            fields = dict(raw_fields)
    elif isinstance(entry, dict):
        stream_id = _as_text(entry.get("stream_message_id") or entry.get("id") or "")
        maybe_fields = entry.get("fields") or entry.get("f") or entry
        if isinstance(maybe_fields, dict):
            fields = dict(maybe_fields)

    decoded: Dict[str, str] = {"stream_message_id": stream_id}
    for k, v in fields.items():
        key = _as_text(k)
        if key == "entry_id":
            decoded["signal_entry_id"] = _as_text(v)
            continue
        if key in ("error", "payload"):
            decoded[key] = _as_text(v)
    decoded.setdefault("signal_entry_id", "")
    decoded.setdefault("error", "")
    decoded.setdefault("payload", "")
    return decoded


def bucket_key(error: str) -> str:
    """Truncate raw ``error`` strings so similar handler exceptions collapse
    into a single bucket; taxonomy tokens from executor-language-contract.md
    (``parse_failed``, ``buy_invalid_pool_or_token_address``, ``handle_failed:...``)
    all remain distinguishable."""

    if not error:
        return "<empty>"
    first = error.split(":", 1)[0].strip()
    if first and len(first) <= REASON_TRUNCATE:
        return first
    return error[:REASON_TRUNCATE].strip() or "<empty>"


def group_entries(entries: List[Any]) -> Dict[str, Any]:
    """Pure function used by tests: group a list of XRANGE-shaped entries.

    ``latest_stream_message_id`` is the max outer Redis stream id in the
    bucket and is the authoritative recency marker.
    ``latest_signal_entry_id`` is the original signal id tied to that latest
    row, useful for human debugging.
    """

    buckets: Dict[str, Dict[str, Any]] = {}
    total = 0
    for raw in entries:
        norm = normalize_entry(raw)
        total += 1
        key = bucket_key(norm["error"])
        b = buckets.setdefault(
            key,
            {
                "count": 0,
                "latest_stream_message_id": "",
                "latest_signal_entry_id": "",
                "latest_error_detail": "",
            },
        )
        b["count"] += 1
        if norm["stream_message_id"] >= b["latest_stream_message_id"]:
            b["latest_stream_message_id"] = norm["stream_message_id"]
            b["latest_signal_entry_id"] = norm["signal_entry_id"]
            b["latest_error_detail"] = norm["error"]
    return {
        "total": total,
        "buckets_sorted": sorted(
            ({"reason": k, **v} for k, v in buckets.items()),
            key=lambda row: (-row["count"], row["reason"]),
        ),
    }


def _render_text(summary: Dict[str, Any], stream: str, show_payloads: int, sample_payloads: List[Tuple[str, str, str]]) -> str:
    lines: List[str] = []
    lines.append(f"signals_dead_letter_stream: {stream}")
    lines.append(f"total_entries: {summary['total']}")
    if summary["total"] == 0:
        lines.append("(no entries)")
        return "\n".join(lines) + "\n"
    lines.append("")
    lines.append(f"{'count':>8}  {'pct':>6}  reason")
    lines.append(f"{'-'*8}  {'-'*6}  {'-'*40}")
    for row in summary["buckets_sorted"]:
        pct = 100.0 * row["count"] / max(1, summary["total"])
        lines.append(f"{row['count']:>8}  {pct:>5.1f}%  {row['reason']}")
    lines.append("")
    lines.append("latest per bucket (stream_id :: signal_entry_id :: redacted error):")
    for row in summary["buckets_sorted"]:
        lines.append(
            f"  - [{row['reason']}] {row['latest_stream_message_id']} :: {row['latest_signal_entry_id']} "
            f":: {redact(row['latest_error_detail'])[:180]}"
        )
    if show_payloads > 0 and sample_payloads:
        lines.append("")
        lines.append(f"payload previews (first {len(sample_payloads)}, max {show_payloads} chars each, redacted):")
        for stream_id, signal_id, err, payload in sample_payloads:
            preview = redact(payload)[:show_payloads]
            lines.append(f"  - {stream_id} / {signal_id} [{bucket_key(err)}] :: {preview}")
    return "\n".join(lines) + "\n"


def _load_json(source: str) -> List[Any]:
    if source == "-":
        data = sys.stdin.read()
    else:
        data = Path(source).read_text(encoding="utf-8")
    parsed = json.loads(data)
    if not isinstance(parsed, list):
        raise ValueError("--from-json expects a JSON list")
    return parsed


def _load_redis_client(url: str):
    try:
        import redis  # noqa: WPS433 — lazy import keeps the script AST-scannable without the runtime dep.
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"redis-py is not available: {exc!r}") from exc
    return redis.Redis.from_url(url, socket_connect_timeout=5, decode_responses=False)


def _read_from_redis(url: str, stream: str, count: int) -> List[Any]:
    client = _load_redis_client(url)
    client.ping()
    entries = client.xrange(stream, min="-", max="+", count=count)
    return list(entries or [])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", default=os.environ.get("SIGNALS_DEAD_LETTER_STREAM", DEFAULT_STREAM))
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help="Max entries to fetch (XRANGE COUNT).")
    parser.add_argument(
        "--from-json",
        default=None,
        help="Path to a JSON list of dead-letter entries; '-' reads from stdin. Offline mode.",
    )
    parser.add_argument(
        "--show-payloads",
        type=int,
        default=0,
        help="If > 0, print up to N chars of each sampled payload (redacted). Default 0 (off).",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    if args.from_json is not None:
        entries = _load_json(args.from_json)
    else:
        url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        try:
            entries = _read_from_redis(url, args.stream, max(1, args.count))
        except Exception as exc:
            print(
                f"inspect_dead_letter: FAIL — cannot read {args.stream} from Redis ({type(exc).__name__})",
                file=sys.stderr,
            )
            return 2

    summary = group_entries(entries)

    sample_payloads: List[Tuple[str, str, str, str]] = []
    if args.show_payloads > 0:
        for raw in entries[: max(0, args.show_payloads)]:
            norm = normalize_entry(raw)
            sample_payloads.append(
                (norm["stream_message_id"], norm["signal_entry_id"], norm["error"], norm["payload"])
            )

    if args.format == "json":
        out = {
            "stream": args.stream,
            "summary": summary,
            "guard": {
                "decision_flags": {
                    "LIVE_ALLOWED": False,
                    "TRAINING_ALLOWED": False,
                    "LABELING_ALLOWED": False,
                    "PHASE6_ALLOWED": False,
                    "WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED": False,
                },
                "controlling_readout": "mvp/runs/run-004-v2-7d-alpha-readout.md",
                "controlling_verdict": "FAILED_NO_7D_ALPHA",
            },
        }
        print(json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(summary, args.stream, args.show_payloads, sample_payloads))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
