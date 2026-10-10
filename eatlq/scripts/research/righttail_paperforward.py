#!/usr/bin/env python3
"""
Paper-forward tracker for the ONE validated +EV rule (meta_reexam_Q1_Q5_verdict).

LOCKED RULE (no tuning here — fidelity to the validated backtest):
  - venue/universe : Base Uniswap-V3, WETH-paired, non-routing alt (fetch() guarantees)
  - decision point : dp = birth + 6 min
  - entry filter   : active_min >= 6 over the 6 fully-completed minutes BEFORE dp (strict, no leak)
  - position       : $10, gas $0.05/tx
  - exit           : 50% @ 2x, 25% @ 5x, 25% moonbag with 50% trailing stop (armed >2x);
                     death gap > 90 min or horizon -> liquidate; honeypot -> total loss
  Backtest: ALL EV +0.22 / PF 2.02 / -100% 7.3% ; recent-OOS EV +0.30 / PF 2.33 (n=99).

This script is READ-ONLY against ClickHouse (zero RPC, zero spend). It is idempotent and
cron-safe. On first run it (a) stamps a forward_epoch = now and (b) seeds the ledger with
recent historical qualifiers as a SEED cohort (baseline only, NOT a forward test). Pools
born at/after forward_epoch form the FORWARD cohort = the true real-time OOS test.

Usage:
  python3 righttail_paperforward.py            # incremental update + print stats
  python3 righttail_paperforward.py --days 14  # widen the seed/scan lookback
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from righttail_label_substrate import fetch  # noqa: E402
from righttail_p2_execution_sim import simulate_pool, portfolio_stats  # noqa: E402

LEDGER = "/opt/EATLQ/mvp/diagnostics/paperforward_ledger.csv"
STATE = "/opt/EATLQ/mvp/diagnostics/paperforward_state.json"

# ---- LOCKED RULE CONSTANTS ----
OBS_MIN = 6
ACTIVE_THR = 6
STRICT = True
SIZE_USD = 10.0
GAS_USD = 0.05
ETH_USD = 1665.0
GAP_SEC = 90 * 60
TARGETS = [(2.0, 0.5), (5.0, 0.25)]
MOONBAG = 0.25
TRAIL = 0.5
FINALIZE_AGE_SEC = 72 * 3600   # match backtest horizon: keep re-eval (moonbag) until dp is 72h old

FIELDS = ["pool", "birth_ts", "birth_utc", "detected_at_utc", "cohort", "dp_ts",
          "weth_t1", "fee_bps", "active_min", "status", "exit_kind",
          "ret", "ret_ideal", "peak_mult", "hold_sec", "last_eval_utc"]


def now_ts() -> int:
    return int(time.time())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def load_ledger() -> dict:
    if not os.path.exists(LEDGER):
        return {}
    out = {}
    for r in csv.DictReader(open(LEDGER)):
        out[r["pool"]] = r
    return out


def save_ledger(rows: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in sorted(rows.values(), key=lambda x: int(x["birth_ts"])):
            w.writerow(r)


def load_state() -> dict:
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {}


def save_state(s: dict) -> None:
    json.dump(s, open(STATE, "w"), indent=2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="lookback window for scanning pools")
    args = ap.parse_args()

    t_now = now_ts()
    state = load_state()
    first_run = "forward_epoch_ts" not in state
    if first_run:
        state["forward_epoch_ts"] = t_now
        state["created_utc"] = iso(t_now)
        print(f"[pf] FIRST RUN — forward_epoch stamped at {iso(t_now)}", flush=True)
        print("[pf] pools born before this instant are SEED (baseline); born after = FORWARD test.", flush=True)
    fwd_epoch = int(state["forward_epoch_ts"])

    ledger = load_ledger()

    # scan recent pools (born up to now); fetch needs an upper bound -> now+1d
    born_before = iso(t_now + 86400)[:19].replace("T", " ")
    print(f"[pf] scanning pools born in last {args.days}d ...", flush=True)
    pools, meta = fetch(args.days, born_before, 72)
    print(f"[pf] fetched {len(pools):,} candidate pools", flush=True)

    obs = OBS_MIN * 60
    size_wei = (SIZE_USD / ETH_USD) * 1e18
    gas_wei = (GAS_USD / ETH_USD) * 1e18

    new_fwd = new_seed = updated = 0
    for p, bl in pools.items():
        m = meta[p]
        birth = m["birth"]
        dp = birth + obs
        # need 6 completed minutes before we can even decide
        if t_now < dp:
            continue
        # if already finalized, skip
        prev = ledger.get(p)
        if prev and prev["status"] == "closed":
            continue

        bl.sort(key=lambda x: x["m"])
        pre = [b for b in bl if b["m"] < dp]  # strict: fully-completed minutes only
        active_min = len(pre)
        if active_min < ACTIVE_THR:
            continue  # does not qualify for the rule -> not a paper trade

        res = simulate_pool(bl, birth, bool(m["weth_t1"]), m["fee_bps"], dp,
                            size_wei, gas_wei, GAP_SEC, TARGETS, MOONBAG, TRAIL, strict=STRICT)
        if res is None:
            continue
        ret, kind, hold_sec, peak, _capture, ret_ideal = res

        # closed if death/honeypot already realized, or dp old enough to finalize
        closed = (kind in ("honeypot_total_loss", "death_gap", "death_no_exit")
                  or (t_now - dp) >= FINALIZE_AGE_SEC)
        cohort = "forward" if birth >= fwd_epoch else "seed"

        row = {
            "pool": p, "birth_ts": birth, "birth_utc": iso(birth),
            "detected_at_utc": prev["detected_at_utc"] if prev else iso(t_now),
            "cohort": prev["cohort"] if prev else cohort,
            "dp_ts": dp, "weth_t1": m["weth_t1"], "fee_bps": m["fee_bps"],
            "active_min": active_min, "status": "closed" if closed else "open",
            "exit_kind": kind, "ret": round(ret, 4), "ret_ideal": round(ret_ideal, 4),
            "peak_mult": peak, "hold_sec": hold_sec, "last_eval_utc": iso(t_now),
        }
        if not prev:
            if row["cohort"] == "forward":
                new_fwd += 1
            else:
                new_seed += 1
        else:
            updated += 1
        ledger[p] = row

    save_ledger(ledger)
    save_state(state)

    # ---- report ----
    def stats_for(cohort, only_closed=True):
        recs = []
        for r in ledger.values():
            if r["cohort"] != cohort:
                continue
            if only_closed and r["status"] != "closed":
                continue
            recs.append((float(r["ret"]), r["exit_kind"], int(r["hold_sec"]),
                         float(r["peak_mult"]), 0.0, float(r["ret_ideal"])))
        return portfolio_stats(recs)

    print(f"\n[pf] new forward={new_fwd}  new seed={new_seed}  updated={updated}")
    print(f"[pf] forward_epoch={state['created_utc']}  (forward cohort born >= this)\n")
    for cohort in ("forward", "seed"):
        n_total = sum(1 for r in ledger.values() if r["cohort"] == cohort)
        n_open = sum(1 for r in ledger.values() if r["cohort"] == cohort and r["status"] == "open")
        s = stats_for(cohort)
        tag = "TRUE FORWARD TEST" if cohort == "forward" else "seed (baseline, not forward)"
        print(f"=== {cohort.upper()} [{tag}] total={n_total} open={n_open} closed={s.get('n')} ===")
        if s.get("n"):
            print(f"    EV/trade={s['EV_per_trade']}  EV_ideal={s['EV_per_trade_IDEAL_no_exit_slip']}  "
                  f"PF={s['pf']}  win={s['win_rate']}  -100%={s['total_loss_-100%_rate']}  "
                  f"ge2x={s['realized_ge2x_rate']}  maxReal={s['max_realized_mult']}  "
                  f"top5sh={s['top5_win_share']}  cum={s['cum_pnl_units']}u")
        print()
    print(f"[pf] ledger -> {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
