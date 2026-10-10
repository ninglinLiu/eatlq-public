#!/usr/bin/env python3
"""
P3 minimal validation: do RIGHT-TAIL winner pools share early-buyer wallets?

Single-use archive backfill (QuickNode) of the earliest buyer EOAs for:
  - winners: mfe_72h >= 5x AND sellable        (real, capturable movers)
  - deaths : dead_24h AND not sellable          (honeypot/death), time-matched

Then test whether winners are launched/sniped by an OVERLAPPING wallet cluster
(= predictive "smart money") vs deaths/chance. Decisive for whether full tx_from
backfill (P3) is worth the RPC investment.

Lookahead-safe framing: early buyers are observed in [birth, birth+30min] only;
the question is whether that early-wallet set predicts the eventual right tail.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import time
import urllib.request
from collections import defaultdict
from itertools import combinations

CH = "eatlq-clickhouse-1"
URL = os.environ.get("RPC_URL", "https://mainnet.base.org")
CSV = "/opt/EATLQ/mvp/diagnostics/righttail_labels.csv"
MAX_EARLY_TX = 20
OBS_SEC = 1800


def ch(sql: str) -> str:
    r = subprocess.run(["docker", "exec", CH, "clickhouse-client", "--query", sql],
                       capture_output=True, text=True, timeout=120, check=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    return r.stdout


def rpc_batch(calls):
    """calls: list of (id, method, params). Returns {id: result}."""
    body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, m, p in calls]
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            resp = json.load(urllib.request.urlopen(req, timeout=60))
            out = {}
            for item in resp:
                out[item["id"]] = item.get("result")
            return out
        except Exception as e:  # noqa
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    return {}


def early_buy_txs(pool: str, birth: int, weth_t1: bool):
    cond = "s.amount1 > 0" if weth_t1 else "s.amount0 > 0"  # WETH into pool = a buy
    sql = f"""
    SELECT DISTINCT s.tx_hash AS h, toUnixTimestamp(s.block_time) AS t
    FROM eatlq.uniswap_v3_swaps_decoded s
    WHERE s.chain_id=8453 AND lower(s.pool_address)='{pool}'
      AND s.block_time IS NOT NULL
      AND toUnixTimestamp(s.block_time) >= {birth}
      AND toUnixTimestamp(s.block_time) < {birth + OBS_SEC}
      AND {cond}
    ORDER BY t ASC, h ASC
    LIMIT {MAX_EARLY_TX}
    FORMAT TabSeparated
    """
    out = []
    for line in ch(sql).splitlines():
        if line.strip():
            out.append(line.split("\t")[0])
    return out


def load_pools():
    rows = list(csv.DictReader(open(CSV)))
    def f(v):
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0
    winners = [r for r in rows if f(r["mfe_72h"]) >= 5 and r["sellable_after_entry"] == "1"]
    deaths_all = sorted([r for r in rows if r["dead_24h"] == "1" and r["sellable_after_entry"] == "0"],
                        key=lambda r: r["birth_utc"])
    # time-match: sample len(winners) deaths evenly across birth order
    k = max(1, len(deaths_all) // len(winners))
    deaths = deaths_all[::k][:len(winners)]
    return winners, deaths


def collect_wallets(pool_rows, label):
    pool_wallets = {}
    all_calls = []
    pool_of_tx = {}
    for r in pool_rows:
        pool = r["pool"].lower()
        birth = int(__import__("datetime").datetime.fromisoformat(r["birth_utc"]).timestamp())
        weth_t1 = r["weth_t1"] == "1"
        try:
            txs = early_buy_txs(pool, birth, weth_t1)
        except Exception as e:  # noqa
            print(f"  CH err {pool[:10]}: {e}")
            txs = []
        pool_wallets[pool] = set()
        for h in txs:
            pool_of_tx.setdefault(h, []).append(pool)
        print(f"  [{label}] {pool[:12]} early_buy_tx={len(txs)}", flush=True)
    # batch resolve all tx -> from
    uniq = list(pool_of_tx.keys())
    print(f"  [{label}] resolving {len(uniq)} unique tx via QuickNode ...", flush=True)
    frm = {}
    B = 40
    for i in range(0, len(uniq), B):
        chunk = uniq[i:i + B]
        res = rpc_batch([(j, "eth_getTransactionByHash", [chunk[j]]) for j in range(len(chunk))])
        for j, h in enumerate(chunk):
            tx = res.get(j)
            if tx and tx.get("from"):
                frm[h] = tx["from"].lower()
        time.sleep(0.2)
    for h, pools in pool_of_tx.items():
        w = frm.get(h)
        if not w:
            continue
        for pool in pools:
            pool_wallets[pool].add(w)
    return pool_wallets


def main():
    winners, deaths = load_pools()
    print(f"winners={len(winners)} deaths={len(deaths)}\n")
    print("== collecting WINNER early wallets ==")
    win_w = collect_wallets(winners, "W")
    print("\n== collecting DEATH early wallets ==")
    die_w = collect_wallets(deaths, "D")

    # wallet -> appearances
    w_app = defaultdict(int); d_app = defaultdict(int)
    for s in win_w.values():
        for x in s:
            w_app[x] += 1
    for s in die_w.values():
        for x in s:
            d_app[x] += 1

    def mean_pairwise_shared(pw):
        ps = [s for s in pw.values() if s]
        if len(ps) < 2:
            return 0.0
        tot = sh = 0
        for a, b in combinations(ps, 2):
            tot += 1
            if a & b:
                sh += 1
        return round(sh / tot, 4) if tot else 0.0

    # recurring wallets
    w_recur = {x for x, c in w_app.items() if c >= 2}
    d_recur = {x for x, c in d_app.items() if c >= 2}

    # leave-one-out: does a winner contain a wallet seen in >=1 OTHER winner?
    loo_hit = 0
    for pool, s in win_w.items():
        others = set()
        for p2, s2 in win_w.items():
            if p2 != pool:
                others |= s2
        if s & others:
            loo_hit += 1
    winners_with_wallets = sum(1 for s in win_w.values() if s)

    # death false-positive: does a death contain a wallet from the winner-recurring set?
    death_fp = sum(1 for s in die_w.values() if s & w_recur)
    deaths_with_wallets = sum(1 for s in die_w.values() if s)

    # cross overlap: wallets in BOTH a winner and a death
    cross = set(w_app) & set(d_app)

    out = {
        "winners": len(winners), "deaths": len(deaths),
        "winner_pools_with_wallets": winners_with_wallets,
        "death_pools_with_wallets": deaths_with_wallets,
        "unique_winner_wallets": len(w_app), "unique_death_wallets": len(d_app),
        "winner_recurring_wallets_>=2pools": len(w_recur),
        "death_recurring_wallets_>=2pools": len(d_recur),
        "winner_top_wallet_pool_count": max(w_app.values()) if w_app else 0,
        "death_top_wallet_pool_count": max(d_app.values()) if d_app else 0,
        "mean_pairwise_shared_WINNERS": mean_pairwise_shared(win_w),
        "mean_pairwise_shared_DEATHS": mean_pairwise_shared(die_w),
        "winner_LOO_recall(share a wallet with another winner)":
            round(loo_hit / winners_with_wallets, 4) if winners_with_wallets else 0,
        "death_FP(contain a winner-recurring wallet)":
            round(death_fp / deaths_with_wallets, 4) if deaths_with_wallets else 0,
        "cross_wallets_in_both_W_and_D": len(cross),
    }
    print("\n==== P3 MINIMAL VALIDATION RESULT ====")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    # top recurring winner wallets
    top = sorted(w_app.items(), key=lambda kv: -kv[1])[:10]
    print("\ntop winner-recurring wallets (wallet: #winner pools):")
    for w, c in top:
        print(f"  {w}  W{c} D{d_app.get(w,0)}")


if __name__ == "__main__":
    main()
