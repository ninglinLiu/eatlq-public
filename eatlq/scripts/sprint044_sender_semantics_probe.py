"""Sprint044 — Sender-field semantic probe (read-only).

We confirm:
  1. row count of each sender_* aggregate via system.parts (fast)
  2. case sensitivity / address layout of sender_address (one row sample)
  3. whether known signers (0x7e49 / 0x91228 / 0xe392) and known router
     (0x59c7) appear in sender_swap_profile / sender_pnl_summary_v1
  4. presence of swap_trade_events_all coverage with sender / recipient
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAG_DIR = REPO_ROOT / "mvp" / "diagnostics"

CH_URL = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")
CH_SETTINGS = "max_execution_time=20"


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
    with urlopen(req, timeout=30) as r:
        text = r.read().decode()
    rows: list[dict[str, str]] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return rows
    headers = lines[0].split("\t")
    for line in lines[1:]:
        rows.append(dict(zip(headers, line.split("\t"))))
    return rows


KNOWN = {
    "router_velora": "0x59c7c832e96d2568bea6db468c1aadcbbda08a52",
    "router_alt":    "0x9e0a538749a486bbf127f8848a6f2cf4e1e92dbd",
    "router_uni":    "0x2626664c2603336e57b271c5c0b26f421741e481",
    "signer_7e49":   "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a",
    "signer_91228":  "0x91228ca305e1094fa42720bd519801185297e719",
    "signer_e392":   "0xe3924d07fad8e504274f188df1d67ad257f5284f",
}


def main() -> None:
    out: dict[str, Any] = {"sprint": "044_sender_semantics_probe"}

    out["row_counts_via_parts"] = ch(
        "SELECT table, sum(rows) AS n_rows, sum(bytes_on_disk) AS bytes "
        "FROM system.parts WHERE database='eatlq' AND active=1 "
        "AND table IN ("
        "  'sender_swap_profile','sender_pnl_summary_v1',"
        "  'sender_token_pnl_v1','sender_trade_day_rollup_v1',"
        "  'sender_venue_trade_profile','sender_token_trade_profile',"
        "  'sender_preference_profile_v1','sender_behavior_labels_v1',"
        "  'sender_inference_candidates_v1','swap_trade_events_all',"
        "  'aerodrome_vamm_swaps_mv','uniswap_v3_swaps_trade_semantic'"
        ") GROUP BY table ORDER BY n_rows DESC"
    )

    out["sender_swap_profile_one_row"] = ch(
        "SELECT * FROM eatlq.sender_swap_profile WHERE chain_id=8453 LIMIT 1"
    )
    out["swap_trade_events_one_row"] = ch(
        "SELECT * FROM eatlq.swap_trade_events_all WHERE chain_id=8453 LIMIT 1"
    )

    for name, addr in KNOWN.items():
        # Try both lowercase and mixed-case literal equality, no lower()
        out[f"sender_swap_profile__{name}"] = ch(
            "SELECT sender_address, trade_count, v3_trade_count, "
            "       distinct_venues, first_trade_at, latest_trade_at "
            f"FROM eatlq.sender_swap_profile WHERE chain_id=8453 "
            f"AND sender_address = '{addr}' LIMIT 1"
        )

    for name, addr in KNOWN.items():
        out[f"swap_trade_events_as_sender__{name}"] = ch(
            "SELECT count() AS n_as_sender, "
            "       uniqExact(recipient_address) AS distinct_recipients, "
            "       uniqExact(venue_address) AS distinct_venues, "
            "       min(block_time) AS first_seen, "
            "       max(block_time) AS last_seen "
            f"FROM eatlq.swap_trade_events_all WHERE chain_id=8453 "
            f"AND sender_address = '{addr}'"
        )
        out[f"swap_trade_events_as_recipient__{name}"] = ch(
            "SELECT count() AS n_as_recipient, "
            "       uniqExact(sender_address) AS distinct_senders, "
            "       uniqExact(venue_address) AS distinct_venues, "
            "       min(block_time) AS first_seen, "
            "       max(block_time) AS last_seen "
            f"FROM eatlq.swap_trade_events_all WHERE chain_id=8453 "
            f"AND recipient_address = '{addr}'"
        )

    out["sender_swap_profile_top25"] = ch(
        "SELECT sender_address, trade_count, v3_trade_count, distinct_venues, "
        "       first_trade_at, latest_trade_at "
        "FROM eatlq.sender_swap_profile WHERE chain_id=8453 "
        "ORDER BY trade_count DESC LIMIT 25"
    )

    out_path = DIAG_DIR / "sprint044_sender_semantics_probe.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
