#!/usr/bin/env python3
"""C: anti-survivorship-bias labeled universe builder (READ-ONLY).

The 131/217 fork-replay sets only contained pools QuoterV2 could price AND that
passed the entry friction gate -> survivors. That hides honeypots that the SAME
QuoterV2/friction ruler silently rejected. To measure the TRUE honeypot
prevalence we must enumerate the strategy's full *candidate universe* (every pool
that passes MIN_PRE + the p_fatal model gate, regardless of whether QuoterV2 could
price it or whether we ever traded it) and ground-truth-label each one via the
multi-point fork-replay labeler (clean / instant_hp / delayed_rug / dead).

Outputs `_universe_blocks.json` = [{pool, token, entry_block, fee}] for the
p-gated candidate universe. The actual labeling is run by fork_replay_backtest.py
--universe (sharded). Survivorship structure (1275 -> 644 -> 131) is printed.
"""
from __future__ import annotations
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, "/opt/EATLQ/scripts/lib")

import keystone_live as K          # noqa: E402
import risk_rebacktest as R        # noqa: E402
from web3 import Web3              # noqa: E402

ARCHIVE_RPC = os.environ.get("FORK_ARCHIVE_RPC", "")
if not ARCHIVE_RPC:
    raise SystemExit("FORK_ARCHIVE_RPC is not set — export an archive RPC endpoint (the token must never be committed)")
FEE_SELECTOR = "0xddca3f43"  # UniswapV3Pool.fee()
OUT = os.path.join(HERE, "_universe_blocks.json")


def pool_fee(w3, pool: str, block: int):
    """Read the pool's fee tier at `block` from the archive (no override)."""
    try:
        r = w3.provider.make_request(
            "eth_call",
            [{"to": Web3.to_checksum_address(pool), "data": FEE_SELECTOR}, hex(int(block))],
        )
        res = r.get("result")
        if res and res != "0x":
            return int(res, 16)
    except Exception:
        pass
    return None


def main():
    df, _ = K.load()
    clf, X, tr, te = R.fit_model(df)
    df = df.copy()
    df["p"] = clf.predict_proba(X)[:, 1]
    priced = json.load(open(K.PCACHE))
    df["why"] = df.pool.map(lambda x: (priced.get(x) or {}).get("why"))
    df["fric"] = df.pool.map(lambda x: (priced.get(x) or {}).get("fric"))
    df["pfee"] = df.pool.map(lambda x: (priced.get(x) or {}).get("fee"))

    gtr = df[tr & (df.why == "ok") & df.fric.notna() & (df.fric >= R.FRIC_THR)]
    p_cut = gtr.p.quantile(0.6)

    # Candidate universe = whole window, passes p_fatal gate (what the strategy
    # would actually consider entering), regardless of QuoterV2/friction.
    cand = df[df.p <= p_cut].copy()
    # Traded survivor subset (the 131/217 ruler): priced + friction ok.
    traded = cand[(cand.why == "ok") & cand.fric.notna() & (cand.fric >= R.FRIC_THR)]

    print("#### SURVIVORSHIP STRUCTURE ####")
    print(f"  MIN_PRE population (>=5 swaps/600s) : {len(df)}")
    print(f"  p-gated candidate universe (p<=p_cut={p_cut:.3f}) : {len(cand)}")
    print(f"  traded survivor subset (priced+friction ok)       : {len(traded)}")
    print(f"  -> hidden by QuoterV2/friction ruler              : {len(cand) - len(traded)}")
    print(f"     why breakdown (candidate): {cand.why.fillna('NONE').value_counts().to_dict()}")

    # Build rows; fetch fee for pools missing a cached fee tier.
    w3 = Web3(Web3.HTTPProvider(ARCHIVE_RPC, request_kwargs={"timeout": 30}))
    rows = []
    need_fetch = 0
    t0 = time.time()
    for j, rec in enumerate(cand.itertuples(index=False)):
        fee = getattr(rec, "pfee", None)
        eb = int(rec.entry_block) if rec.entry_block == rec.entry_block else None
        if (fee is None or fee != fee) and eb:
            fee = pool_fee(w3, rec.pool, eb)
            need_fetch += 1
        rows.append({
            "pool": rec.pool, "token": str(rec.alt), "entry_block": eb,
            "fee": int(fee) if fee else None,
            "in_traded_131_217": bool(rec.why == "ok" and rec.fric == rec.fric and rec.fric >= R.FRIC_THR),
            "qv2_why": rec.why, "qv2_fric": (float(rec.fric) if rec.fric == rec.fric else None),
        })
        if (j + 1) % 100 == 0:
            print(f"  ...built {j+1}/{len(cand)} (fee fetches={need_fetch})", flush=True)
    have_fee = sum(1 for r in rows if r["fee"])
    print(f"\nrows={len(rows)}  with entry_block+fee+token={sum(1 for r in rows if r['fee'] and r['entry_block'] and r['token'])}"
          f"  fee_fetched={need_fetch}  ({time.time()-t0:.0f}s)")
    json.dump({
        "meta": {
            "p_cut": float(p_cut), "n_min_pre": len(df),
            "n_candidate_universe": len(cand), "n_traded_survivors": len(traded),
            "n_hidden": len(cand) - len(traded),
        },
        "rows": rows,
    }, open(OUT, "w"), indent=2)
    # also a flat list for the sharded labeler
    json.dump(rows, open(OUT.replace(".json", "_flat.json"), "w"))
    print(f"wrote {OUT} and *_flat.json")


if __name__ == "__main__":
    main()
