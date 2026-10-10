"""Sprint044 — Schema probe for sender_* aggregates.

Read-only.  Lists columns + tiny stats so we can decide whether to leverage
existing aggregates instead of re-running FIFO across 1M raw swaps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

CHAIN_ID = 8453
REPO_ROOT = Path(__file__).resolve().parent.parent
DIAG_DIR = REPO_ROOT / "mvp" / "diagnostics"

CH_URL = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")
CH_SETTINGS = "max_execution_time=15"


def ch(sql: str) -> list[dict[str, str]]:
    url = CH_URL.rstrip("/") + f"/?{CH_SETTINGS}"
    req = Request(
        url,
        data=(sql + "\nFORMAT TSVWithNames").encode(),
        headers={
            "X-ClickHouse-User": CH_USER,
            "X-ClickHouse-Key": CH_PASS,
            "Content-Type": "text/plain",
        },
        method="POST",
    )
    with urlopen(req, timeout=20) as r:
        text = r.read().decode()
    rows: list[dict[str, str]] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return rows
    headers = lines[0].split("\t")
    for line in lines[1:]:
        rows.append(dict(zip(headers, line.split("\t"))))
    return rows


def main() -> None:
    out: dict[str, Any] = {"sprint": "044_schema_probe"}

    targets = [
        "sender_swap_profile",
        "sender_pnl_summary_v1",
        "sender_token_pnl_v1",
        "sender_trade_day_rollup_v1",
        "sender_venue_trade_profile",
        "sender_token_trade_profile",
        "sender_preference_profile_v1",
        "sender_behavior_labels_v1",
        "sender_inference_candidates_v1",
        "sender_inference_cohort_summary_v1",
        "swap_trade_events_all",
        "uniswap_swaps_clean_v1",
        "aerodrome_vamm_swaps_mv",
        "uniswap_v3_swaps_trade_semantic",
        "token_registry",
    ]

    schemas: dict[str, list[dict[str, str]]] = {}
    for table in targets:
        try:
            schemas[table] = ch(
                "SELECT name, type FROM system.columns "
                f"WHERE database='eatlq' AND table='{table}' ORDER BY position"
            )
        except Exception as exc:  # pragma: no cover
            schemas[table] = [{"error": str(exc)}]
    out["schemas"] = schemas

    # Use system.parts for fast row counts (no full scan)
    parts_counts = ch(
        "SELECT table, sum(rows) AS n_rows "
        "FROM system.parts "
        "WHERE database='eatlq' AND active=1 "
        f"AND table IN ({','.join(repr(t) for t in targets)}) "
        "GROUP BY table ORDER BY table"
    )
    out["row_counts_via_parts"] = parts_counts

    sample_aero = ch(
        "SELECT name, type FROM system.columns "
        "WHERE database='eatlq' AND table='aerodrome_vamm_swaps_mv' ORDER BY position"
    )
    out["aerodrome_vamm_swaps_mv_schema"] = sample_aero

    out_path = DIAG_DIR / "sprint044_schema_probe.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
