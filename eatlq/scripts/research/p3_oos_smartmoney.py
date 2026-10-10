#!/usr/bin/env python3
"""
P3 out-of-sample test: does a TRAIN-built smart-money set predict NEW right tails?

  1) Time split T (birth quantile). TRAIN = born < T, OOS = born >= T.
  2) Smart set S (TRAIN only): wallets appearing early in >= MIN_WIN train winners
     AND in 0 train deaths (exclude indiscriminate snipers/bots).
  3) OOS test (tradeable universe active_min>=MIN_ACTIVE, born>=T): backfill early
     buyers, then measure P(winner | smart-touched) vs base P(winner) -> lift.

Winner = mfe_72h>=5 & sellable. Lookahead-safe: S built only from pools born before
each OOS pool's birth window; early buyers observed in first 30min only.
tx->from cached to disk so reruns cost no RPC.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import time
import urllib.request
from collections import defaultdict
from datetime import datetime

CH = "eatlq-clickhouse-1"
URL = os.environ.get("RPC_URL", "https://mainnet.base.org")
CSV = "/opt/EATLQ/mvp/diagnostics/righttail_labels.csv"
CACHE = "/opt/EATLQ/mvp/diagnostics/p3_txfrom_cache.json"
POOLTX_CACHE = "/opt/EATLQ/mvp/diagnostics/p3_pooltxs_cache.json"
MAX_EARLY_TX = 15
OBS_SEC = 1800
MIN_WIN = 2          # smart wallet must appear in >= this many train winners
MIN_ACTIVE = 3       # OOS tradeable universe
TRAIN_Q = 0.60       # birth quantile split


def ch(sql: str) -> str:
    r = subprocess.run(["docker", "exec", CH, "clickhouse-client", "--query", sql],
                       capture_output=True, text=True, timeout=120, check=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    return r.stdout


def load_cache():
    if os.path.exists(CACHE):
        return json.load(open(CACHE))
    return {}


def save_cache(c):
    json.dump(c, open(CACHE, "w"))


def rpc_batch(calls):
    body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, m, p in calls]
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            resp = json.load(urllib.request.urlopen(req, timeout=60))
            return {it["id"]: it.get("result") for it in resp}
        except Exception:  # noqa
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    return {}


def early_buy_txs_bulk(pool_rows):
    """One combined query: first MAX_EARLY_TX distinct buy-tx per pool. -> {pool: [tx,...]}"""
    vals = ",".join(
        f"('{r['pool'].lower()}',{r['birth_ts']},{1 if r['weth_t1']=='1' else 0})"
        for r in pool_rows
    )
    sql = f"""
    WITH plist AS (
        SELECT * FROM values('pool String, birth UInt32, weth_t1 UInt8', {vals})
    ),
    buys AS (
        SELECT lower(s.pool_address) AS pool, s.tx_hash AS h, min(s.block_time) AS bt
        FROM eatlq.uniswap_v3_swaps_decoded s
        INNER JOIN plist p ON lower(s.pool_address)=p.pool
        WHERE s.chain_id=8453 AND s.block_time IS NOT NULL
          AND toUnixTimestamp(s.block_time) >= p.birth
          AND toUnixTimestamp(s.block_time) < p.birth + {OBS_SEC}
          AND ((p.weth_t1=1 AND s.amount1>0) OR (p.weth_t1=0 AND s.amount0>0))
        GROUP BY pool, h
    ),
    ranked AS (
        SELECT pool, h, row_number() OVER (PARTITION BY pool ORDER BY bt ASC, h ASC) AS rn FROM buys
    )
    SELECT pool, h FROM ranked WHERE rn <= {MAX_EARLY_TX} FORMAT TabSeparated
    """
    res = defaultdict(list)
    for ln in ch(sql).splitlines():
        if not ln.strip():
            continue
        pool, h = ln.split("\t")
        res[pool].append(h)
    return res


def f(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def main():
    rows = list(csv.DictReader(open(CSV)))
    for r in rows:
        r["birth_ts"] = int(datetime.fromisoformat(r["birth_utc"]).timestamp())
        r["is_winner"] = 1 if (f(r["mfe_72h"]) >= 5 and r["sellable_after_entry"] == "1") else 0
        r["is_death"] = 1 if (r["dead_24h"] == "1" and r["sellable_after_entry"] == "0") else 0
        r["active"] = int(r["early_active_min"])
    rows.sort(key=lambda r: r["birth_ts"])
    T = rows[int(len(rows) * TRAIN_Q)]["birth_ts"]
    print(f"split T={datetime.utcfromtimestamp(T).isoformat()}  total={len(rows)}")

    train = [r for r in rows if r["birth_ts"] < T]
    oos = [r for r in rows if r["birth_ts"] >= T and r["active"] >= MIN_ACTIVE]
    tr_win = [r for r in train if r["is_winner"]]
    tr_die = [r for r in train if r["is_death"]]
    print(f"train winners={len(tr_win)} train deaths={len(tr_die)} | OOS tradeable pools={len(oos)} "
          f"(OOS winners={sum(r['is_winner'] for r in oos)})")

    cache = load_cache()
    pool_txs = json.load(open(POOLTX_CACHE)) if os.path.exists(POOLTX_CACHE) else {}

    all_rows = {r["pool"].lower(): r for r in (tr_win + tr_die + oos)}
    need = [r for p, r in all_rows.items() if p not in pool_txs]
    if need:
        print(f"gathering early tx (CH bulk) for {len(need)} pools ...", flush=True)
        bulk = early_buy_txs_bulk(need)
        for r in need:
            p = r["pool"].lower()
            pool_txs[p] = bulk.get(p, [])
        json.dump(pool_txs, open(POOLTX_CACHE, "w"))
    pending = list(dict.fromkeys(h for p in all_rows for h in pool_txs.get(p, []) if h not in cache))
    print(f"resolving {len(pending)} new tx via QuickNode (cached {len(cache)}) ...", flush=True)
    B = 40
    for i in range(0, len(pending), B):
        chunk = pending[i:i + B]
        res = rpc_batch([(j, "eth_getTransactionByHash", [chunk[j]]) for j in range(len(chunk))])
        for j, h in enumerate(chunk):
            tx = res.get(j)
            cache[h] = tx["from"].lower() if tx and tx.get("from") else None
        if (i // B) % 10 == 0:
            save_cache(cache); print(f"  resolved {i+len(chunk)}/{len(pending)}", flush=True)
        time.sleep(0.15)
    save_cache(cache)

    def wallets(pool):
        return {cache.get(h) for h in pool_txs.get(pool, []) if cache.get(h)}

    # build smart set from TRAIN
    w_in_win = defaultdict(int); w_in_die = defaultdict(int)
    for r in tr_win:
        for w in wallets(r["pool"].lower()):
            w_in_win[w] += 1
    for r in tr_die:
        for w in wallets(r["pool"].lower()):
            w_in_die[w] += 1
    S = {w for w, c in w_in_win.items() if c >= MIN_WIN and w_in_die.get(w, 0) == 0}
    print(f"\nsmart set S size={len(S)} (>= {MIN_WIN} train winners, 0 train deaths)")

    # OOS test
    base = sum(r["is_winner"] for r in oos) / len(oos) if oos else 0
    touched = [r for r in oos if wallets(r["pool"].lower()) & S]
    untouched = [r for r in oos if not (wallets(r["pool"].lower()) & S)]
    p_touch = (sum(r["is_winner"] for r in touched) / len(touched)) if touched else 0
    p_untouch = (sum(r["is_winner"] for r in untouched) / len(untouched)) if untouched else 0
    recall = (sum(r["is_winner"] for r in touched) / sum(r["is_winner"] for r in oos)) if sum(r["is_winner"] for r in oos) else 0

    out = {
        "split_T_utc": datetime.utcfromtimestamp(T).isoformat(),
        "params": {"MIN_WIN": MIN_WIN, "MIN_ACTIVE": MIN_ACTIVE, "MAX_EARLY_TX": MAX_EARLY_TX, "TRAIN_Q": TRAIN_Q},
        "train_winners": len(tr_win), "train_deaths": len(tr_die),
        "smart_set_size": len(S),
        "oos_pools": len(oos), "oos_winners": sum(r["is_winner"] for r in oos),
        "oos_base_winner_rate": round(base, 4),
        "oos_smart_touched_pools": len(touched),
        "P(winner|smart_touched)": round(p_touch, 4),
        "P(winner|not_touched)": round(p_untouch, 4),
        "LIFT_vs_base": round(p_touch / base, 2) if base > 0 else None,
        "winner_recall_via_smart": round(recall, 4),
    }
    print("\n==== P3 OOS RESULT ====")
    print(json.dumps(out, indent=2, ensure_ascii=False))

    # sensitivity grid (free: cached). a priori S definitions, report ALL (no cherry-pick).
    print("\n==== SENSITIVITY GRID (OOS, active>=3) ====")
    print(f"{'min_win':>7} {'death_tol':>9} {'S':>4} {'touched':>7} {'winners':>7} "
          f"{'P(win|S)':>9} {'lift':>5} {'recall':>6}")
    oos_w_total = sum(r["is_winner"] for r in oos)
    for mw in (1, 2, 3):
        for dt in (0, 1):
            S2 = {w for w, c in w_in_win.items() if c >= mw and w_in_die.get(w, 0) <= dt}
            tch = [r for r in oos if wallets(r["pool"].lower()) & S2]
            wn = sum(r["is_winner"] for r in tch)
            pw = wn / len(tch) if tch else 0
            lift = pw / base if base > 0 else 0
            rec = wn / oos_w_total if oos_w_total else 0
            print(f"{mw:>7} {dt:>9} {len(S2):>4} {len(tch):>7} {wn:>7} "
                  f"{pw:>9.3f} {lift:>5.1f} {rec:>6.2f}")


if __name__ == "__main__":
    main()
