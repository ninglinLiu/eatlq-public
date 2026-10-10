"""Snapshot Fomo leaderboard. No RPC. No key fabrication, no token forging.

Route manifest confirms `routes/leaderboard` exists and the client calls
`https://prod-api.fomo.family/v2/leaderboard{,/daily,/weekly,/following}`.
Every `prod-api` path answers HTTP 431 `{"error":"unauthorized"}` without a
session; even `/health` returns 401 "No authorization token provided". Auth is
Privy-issued per user, so there is no anonymous public board to scrape.
"""
from __future__ import annotations

import json
import os

from src.common import ROOT, http_get, snapshot_record, write_json

OUT = ROOT / "data/raw/fomo"
SOURCE = "fomo"

PUBLIC_PAGES = [
    ("base", "https://fomo.family/leaderboard"),
]
PROD_API = "https://prod-api.fomo.family"
API_PATHS = [
    "/v2/leaderboard",
    "/v2/leaderboard/daily",
    "/v2/leaderboard/weekly",
    "/v2/clans/leaderboard",
    "/health",
]


def collect() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    snapshots = []
    blocked = True

    for chain, url in PUBLIC_PAGES:
        status, body, meta = http_get(url, headers={"Referer": "https://fomo.family/"})
        rec = snapshot_record(
            source=SOURCE, chain=chain, url=url, status=status, body=body,
            extra={"kind": "spa_html", "auth": "none", "transport_meta": meta},
        )
        raw_path = OUT / f"public_{chain}_{rec['captured_at'].replace(':', '')}.bin"
        raw_path.write_bytes(body)
        rec["raw_path"] = str(raw_path)
        rec["row_hint"] = 0  # SPA shell renders rows client-side
        snapshots.append(rec)

    # Record the API refusal verbatim so the block is evidenced, not asserted.
    for path in API_PATHS:
        status, body, meta = http_get(
            PROD_API + path,
            headers={
                "Accept": "application/json",
                "Origin": "https://fomo.family",
                "Referer": "https://fomo.family/leaderboard",
            },
            sleep_s=1.0,
        )
        rec = snapshot_record(
            source=SOURCE, chain="multi", url=PROD_API + path, status=status, body=body,
            extra={"kind": "prod_api", "auth": "none", "transport_meta": meta},
        )
        raw_path = OUT / f"prodapi_{path.strip('/').replace('/', '_')}_{rec['captured_at'].replace(':', '')}.bin"
        raw_path.write_bytes(body)
        rec["raw_path"] = str(raw_path)
        rec["row_hint"] = _json_traders(body)
        rec["ok"] = rec["row_hint"] > 0
        if rec["row_hint"] > 0:
            blocked = False
        snapshots.append(rec)

    key = os.environ.get("FOMO_API_KEY", "").strip()
    if key:
        for window in ("daily", "weekly"):
            url = f"{PROD_API}/v2/leaderboard/{window}"
            status, body, meta = http_get(url, headers={"Authorization": f"Bearer {key}"}, sleep_s=1.0)
            rec = snapshot_record(
                source=SOURCE, chain="multi", url=url, status=status, body=body,
                extra={"kind": "prod_api", "auth": "FOMO_API_KEY", "window": window, "transport_meta": meta},
            )
            raw_path = OUT / f"api_{window}_{rec['captured_at'].replace(':', '')}.bin"
            raw_path.write_bytes(body)
            rec["raw_path"] = str(raw_path)
            rec["row_hint"] = _json_traders(body)
            if rec["row_hint"] > 0:
                blocked = False
            snapshots.append(rec)
    else:
        snapshots.append(
            {
                "source": SOURCE,
                "url": f"{PROD_API}/v2/leaderboard/{{window}}",
                "status_flag": "BLOCKED_AUTH",
                "note": "No FOMO_API_KEY in environment. Privy session not forged.",
                "row_hint": 0,
                "ok": False,
            }
        )

    summary = {
        "source": SOURCE,
        "status": "SOURCE_BLOCKED_AUTH" if blocked else "CAPTURED",
        "n_requests": len(snapshots),
        "n_ok_with_rows": sum(1 for s in snapshots if s.get("row_hint", 0) > 0),
        "api_base": PROD_API,
        "api_note": (
            "All /v2/* return HTTP 431 {'error':'unauthorized'}; /health returns 401 "
            "'No authorization token provided'. Auth is per-user Privy, so no "
            "anonymous board exists. Not substituted with any other source."
        ),
        "snapshots": snapshots,
    }
    write_json(OUT / "collect_summary.json", summary)
    return summary


def _json_traders(body: bytes) -> int:
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return 0
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        if payload.get("error") or payload.get("statusCode") not in (None, 200):
            return 0
        obj = payload.get("responseObject") or payload
        if isinstance(obj, dict):
            for key in ("users", "traders", "leaderboard", "data"):
                if isinstance(obj.get(key), list):
                    return len(obj[key])
        if isinstance(obj, list):
            return len(obj)
    return 0


if __name__ == "__main__":
    s = collect()
    print(json.dumps({k: s[k] for k in ("source", "status", "n_ok_with_rows")}, indent=2))
