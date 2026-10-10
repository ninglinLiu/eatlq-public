"""Sprint044 Phase 5 — multi-tx gas + controller fingerprint for top candidates.

For the top 12 author-style candidates from Phase 4 (excluding any obvious
bots), pull 8 sample tx_hashes per recipient from CH and RPC them to build
a richer gas/controller fingerprint:

  - share of tx with maxPriorityFeePerGas == 0
  - share with priority <= 5 wei
  - share with priority <= 1e5 wei
  - share with priority > 1e6 wei (MEV-like)
  - distinct controllers (tx.from) seen
  - whether all controllers share top-1 dominant address
"""

from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAG_DIR = REPO_ROOT / "mvp" / "diagnostics"

CH_URL = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")
CH_SETTINGS = "max_execution_time=60"

RPC_URL = os.environ.get(
    "EVM_HTTP_URL",
    "https://orbital-purple-sea.base-mainnet.quiknode.pro/"
    "0123456789abcdef0123456789abcdef01234567/",
)

SAMPLES_PER_CAND = 8
TOP_N = 12


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
    with urlopen(req, timeout=120) as r:
        text = r.read().decode()
    rows: list[dict[str, str]] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return rows
    headers = lines[0].split("\t")
    for line in lines[1:]:
        rows.append(dict(zip(headers, line.split("\t"))))
    return rows


_rpc_id = 0


def rpc(method: str, params: list[Any]) -> Any:
    global _rpc_id
    _rpc_id += 1
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": _rpc_id, "method": method, "params": params}
    ).encode()
    req = Request(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    if "error" in d:
        return None
    return d.get("result")


def gas_bucket(wei: int | None) -> str:
    if wei is None:
        return "missing"
    if wei == 0:
        return "0"
    if wei == 1:
        return "1"
    if wei <= 5:
        return "2_to_5"
    if wei <= 100_000:
        return "6_to_1e5"
    if wei <= 1_000_000:
        return "1e5_to_1e6"
    return "gt_1e6"


def main() -> None:
    cand_csv = DIAG_DIR / "sprint044_global_signer_candidates.csv"
    if not cand_csv.exists():
        raise SystemExit(f"missing {cand_csv}")
    with cand_csv.open() as f:
        cands = list(csv.DictReader(f))

    cands.sort(key=lambda r: float(r["fifo_net_realized_weth"]), reverse=True)
    top = cands[:TOP_N]
    addr_in = ",".join(repr(c["recipient"]) for c in top)

    sql = f"""
WITH meme_pools AS (
  SELECT lower(pool_address) AS pa
  FROM eatlq.discovered_pools
  WHERE chain_id={CHAIN_ID}
    AND lower(pool_type)='v3' AND has_weth_leg=1
    AND fee_bps IN (500,3000,10000)
),
ranked AS (
  SELECT lower(s.recipient) AS recipient, lower(s.tx_hash) AS tx_hash,
         row_number() OVER (PARTITION BY lower(s.recipient) ORDER BY s.block_time) AS rn
  FROM eatlq.uniswap_v3_swaps s
  INNER JOIN meme_pools mp ON lower(s.pool_address)=mp.pa
  WHERE s.chain_id={CHAIN_ID} AND s.removed=0
    AND s.block_time >= toDateTime('2026-01-01 00:00:00')
    AND s.block_time <  toDateTime('2026-05-01 00:00:00')
    AND lower(s.recipient) IN ({addr_in})
)
SELECT recipient, tx_hash FROM ranked WHERE rn IN (1, 5, 13, 25, 50, 100, 250, 500)
ORDER BY recipient, rn
"""
    rows = ch(sql)
    by_recip: dict[str, list[str]] = {}
    for r in rows:
        by_recip.setdefault(r["recipient"], []).append(r["tx_hash"])

    fingerprints: dict[str, dict[str, Any]] = {}
    all_controllers: dict[str, set[str]] = {}

    t0 = time.time()
    for recip, txs in by_recip.items():
        gas_dist: dict[str, int] = {
            "0": 0, "1": 0, "2_to_5": 0, "6_to_1e5": 0,
            "1e5_to_1e6": 0, "gt_1e6": 0, "missing": 0,
        }
        controllers: dict[str, int] = {}
        tx_to_dist: dict[str, int] = {}
        n_sampled = 0
        for txh in txs[:SAMPLES_PER_CAND]:
            tx = rpc("eth_getTransactionByHash", [txh])
            if not tx:
                gas_dist["missing"] += 1
                continue
            mp = tx.get("maxPriorityFeePerGas")
            wei = None
            if mp is not None:
                try:
                    wei = int(mp, 16)
                except Exception:
                    wei = None
            gas_dist[gas_bucket(wei)] += 1
            tx_from = (tx.get("from") or "").lower()
            tx_to = (tx.get("to") or "").lower()
            if tx_from:
                controllers[tx_from] = controllers.get(tx_from, 0) + 1
            if tx_to:
                tx_to_dist[tx_to] = tx_to_dist.get(tx_to, 0) + 1
            n_sampled += 1
        share_zero = (gas_dist["0"] / n_sampled) if n_sampled else 0.0
        share_le5 = (
            (gas_dist["0"] + gas_dist["1"] + gas_dist["2_to_5"]) / n_sampled
            if n_sampled
            else 0.0
        )
        share_mev = (
            (gas_dist["1e5_to_1e6"] + gas_dist["gt_1e6"]) / n_sampled
            if n_sampled
            else 0.0
        )
        top_controller = (
            sorted(controllers.items(), key=lambda kv: kv[1], reverse=True)[0]
            if controllers
            else (None, 0)
        )
        all_controllers.setdefault(recip, set()).update(controllers.keys())
        fingerprints[recip] = {
            "n_sampled": n_sampled,
            "gas_dist": gas_dist,
            "share_priority_zero": round(share_zero, 4),
            "share_priority_le_5_wei": round(share_le5, 4),
            "share_priority_gt_1e5": round(share_mev, 4),
            "n_distinct_controllers": len(controllers),
            "top_controller": top_controller[0],
            "top_controller_share": (
                round(top_controller[1] / n_sampled, 4) if n_sampled else 0.0
            ),
            "tx_to_dist_top": dict(
                sorted(tx_to_dist.items(), key=lambda kv: kv[1], reverse=True)[:3]
            ),
        }
    elapsed = time.time() - t0

    controller_overlap: dict[str, list[str]] = {}
    for recip, ctrls in all_controllers.items():
        for c in ctrls:
            controller_overlap.setdefault(c, []).append(recip)
    shared_controllers = {
        c: recipients
        for c, recipients in controller_overlap.items()
        if len(recipients) >= 2
    }

    enriched_rows: list[dict[str, Any]] = []
    for r in cands:
        addr = r["recipient"]
        fp = fingerprints.get(addr, {})
        r2 = dict(r)
        r2["multi_tx_n_sampled"] = fp.get("n_sampled", 0)
        r2["multi_tx_share_priority_zero"] = fp.get("share_priority_zero", 0.0)
        r2["multi_tx_share_priority_le5"] = fp.get("share_priority_le_5_wei", 0.0)
        r2["multi_tx_share_priority_gt_1e5"] = fp.get("share_priority_gt_1e5", 0.0)
        r2["multi_tx_n_controllers"] = fp.get("n_distinct_controllers", 0)
        r2["multi_tx_top_controller"] = fp.get("top_controller", "")
        r2["multi_tx_top_controller_share"] = fp.get("top_controller_share", 0.0)
        r2["multi_tx_top_to_dist"] = json.dumps(fp.get("tx_to_dist_top", {}))
        enriched_rows.append(r2)

    enriched_rows.sort(key=lambda r: float(r["fifo_net_realized_weth"]), reverse=True)
    out_csv = DIAG_DIR / "sprint044_global_signer_candidates.csv"
    cols = list(enriched_rows[0].keys()) if enriched_rows else []
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in enriched_rows:
            w.writerow(r)

    summary = {
        "sprint": "044_phase5_gasprofile",
        "rpc_calls_used_phase5": _rpc_id,
        "rpc_elapsed_seconds": round(elapsed, 2),
        "samples_per_candidate": SAMPLES_PER_CAND,
        "top_evaluated": TOP_N,
        "fingerprints_top12": fingerprints,
        "shared_controllers": shared_controllers,
    }
    (DIAG_DIR / "sprint044_phase5_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )
    print(
        f"top={len(top)} fp={len(fingerprints)} rpc_calls={_rpc_id} "
        f"shared_controllers={len(shared_controllers)} elapsed={elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
