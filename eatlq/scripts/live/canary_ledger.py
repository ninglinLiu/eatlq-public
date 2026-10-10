#!/usr/bin/env python3
"""Single source of truth for the canary candidates ledger schema + safe IO.

Why this module exists
----------------------
The detector (writer) and the executor/publisher (readers) used to keep their
own private notion of the ledger columns. The detector's column set grew over
time (added `rule`, `onset_lag_min`, `onset_age_min`, `non_0x6ff5_buyers`) but
the on-disk header is only written once at file creation, so a stale 17-column
header sat on top of new 20-column rows. `csv.DictReader` then mapped columns by
the stale header, so `verdict` resolved to a numeric column and NO reader ever
saw `verdict == "CLEAN"`. Result: the canary was structurally unable to trade.

This module fixes the root cause by making BOTH sides share one schema:
  * `append_rows()` self-heals: if the on-disk header has drifted from the
    canonical schema, the stale file is rotated to a timestamped `.bak` and a
    fresh file with the correct header is started. The header can never again
    disagree with the rows beneath it.
  * `read_rows()` is drift-robust: it parses each data row POSITIONALLY against
    the canonical schema whenever the row width matches, so even a pre-migration
    file (stale header, canonical-width rows) is read correctly.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone

# Canonical ledger schema. ORDER MATTERS — rows are written and read positionally
# against this exact list. If you add/remove a column, do it here only.
LEDGER_COLUMNS = [
    "ts", "pool", "alt", "fee", "birth_block", "age_min", "early_swaps",
    "onset_lag_min", "onset_age_min",
    "share_0x6ff5", "non_0x6ff5_buyers", "distinct_recipients", "top_recipient_share",
    "roundtrip_quoter", "gas_pct", "roundtrip_net_after_cost", "sellable",
    "rule", "verdict", "reason",
]

DEFAULT_LEDGER = "/opt/EATLQ/mvp/diagnostics/live/candidates_ledger.csv"


def _read_header(path: str) -> list[str] | None:
    try:
        with open(path, newline="") as f:
            first = f.readline()
        if not first:
            return None
        return next(csv.reader([first]))
    except Exception:
        return None


def append_rows(rows: list[dict], path: str = DEFAULT_LEDGER) -> None:
    """Append rows under a header guaranteed to match the canonical schema.

    If the existing file's header has drifted, rotate it to `<path>.<ts>.bak`
    and start a fresh canonical file (raw history is preserved in the .bak for
    manual audit). No-op for an empty `rows` list other than schema healing.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)

    write_header = True
    if os.path.exists(path):
        existing = _read_header(path)
        if existing == LEDGER_COLUMNS:
            write_header = False
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            bak = f"{path}.{ts}.bak"
            try:
                os.rename(path, bak)
                print(f"[canary_ledger] schema drift detected; rotated stale ledger -> {bak}",
                      flush=True)
            except Exception as e:  # pragma: no cover - defensive
                print(f"[canary_ledger] WARN could not rotate stale ledger: {e}", flush=True)

    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def read_rows(path: str = DEFAULT_LEDGER) -> list[dict]:
    """Read all ledger rows as dicts keyed by the canonical schema.

    Drift-robust: a data row whose width equals the canonical schema is mapped
    positionally against the canonical columns (this is what rescues a file with
    a stale header but canonical-width rows). Otherwise we fall back to the
    file's own header, then to a best-effort positional zip.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="") as f:
            raw = list(csv.reader(f))
    except Exception:
        return []
    if not raw:
        return []

    header = raw[0]
    out: list[dict] = []
    for row in raw[1:]:
        if not row:
            continue
        if len(row) == len(LEDGER_COLUMNS):
            out.append(dict(zip(LEDGER_COLUMNS, row)))
        elif len(row) == len(header):
            out.append(dict(zip(header, row)))
        else:
            d = dict(zip(LEDGER_COLUMNS, row))
            # last resort: if a verdict token is hiding past the mapped columns,
            # surface it so CLEAN/REJECT filtering still works.
            if d.get("verdict") not in ("CLEAN", "REJECT"):
                for cell in row:
                    if cell in ("CLEAN", "REJECT"):
                        d["verdict"] = cell
                        break
            out.append(d)
    return out


def read_clean_candidates(path: str = DEFAULT_LEDGER) -> list[dict]:
    """Return only rows with verdict == CLEAN, parsed schema-safely."""
    return [r for r in read_rows(path) if r.get("verdict") == "CLEAN"]
