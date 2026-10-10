"""Snapshot public GMGN Discover / wallet rank pages. No RPC. No WAF bypass."""
from __future__ import annotations

import json
from pathlib import Path

from src.common import ROOT, http_get, snapshot_record, write_json

OUT = ROOT / "data/raw/gmgn"
SOURCE = "gmgn"

# Endpoints the public Discover UI itself loads (docs: https://docs.gmgn.ai/index/discover)
TAGS = ("smart_degen", "fresh_wallet", "kol")
WINDOWS = (("7d", "pnl_7d"), ("1d", "pnl_1d"), ("30d", "pnl_30d"))
CHAINS = ("base", "bsc")


def _urls() -> list[tuple[str, str, str, str]]:
    rows = []
    for chain in CHAINS:
        rows.append((chain, "html", "discover", f"https://gmgn.ai/discover?chain={chain}"))
        for tag in TAGS:
            for window, orderby in WINDOWS:
                api = (
                    f"https://gmgn.ai/defi/quotation/v1/rank/{chain}/wallets/{window}"
                    f"?tag={tag}&orderby={orderby}&direction=desc"
                )
                rows.append((chain, window, tag, api))
    return rows


def collect() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    snapshots = []
    blocked = True
    for chain, window, tag, url in _urls():
        status, body, meta = http_get(
            url,
            headers={"Referer": f"https://gmgn.ai/discover?chain={chain}"},
        )
        rec = snapshot_record(
            source=SOURCE, chain=chain, url=url, status=status, body=body,
            extra={"window": window, "tag": tag, "transport_meta": meta},
        )
        raw_name = f"{chain}_{window}_{tag}_{rec['captured_at'].replace(':', '')}.bin"
        raw_path = OUT / raw_name
        raw_path.write_bytes(body)
        rec["raw_path"] = str(raw_path)
        parsed = None
        if rec["ok"]:
            try:
                parsed = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                parsed = None
        rec["parsed_json"] = bool(isinstance(parsed, dict))
        rec["row_hint"] = _row_hint(parsed) if parsed is not None else 0
        if rec["ok"] and rec["row_hint"] > 0:
            blocked = False
        snapshots.append(rec)
    summary = {
        "source": SOURCE,
        "status": "SOURCE_BLOCKED" if blocked else "CAPTURED",
        "n_requests": len(snapshots),
        "n_ok_with_rows": sum(1 for s in snapshots if s.get("row_hint", 0) > 0),
        "snapshots": snapshots,
    }
    write_json(OUT / "collect_summary.json", summary)
    return summary


def _row_hint(payload) -> int:
    if not isinstance(payload, dict):
        return 0
    data = payload.get("data") or payload
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("rank", "list", "wallets", "rank_list"):
            if isinstance(data.get(key), list):
                return len(data[key])
    return 0


if __name__ == "__main__":
    print(json.dumps({k: collect()[k] for k in ("source", "status", "n_ok_with_rows")}, indent=2))
