#!/usr/bin/env python3
"""TRAIL × max-hold coarse frontier with train/holdout + Bootstrap CIs. Offline, RPC-free.

Grid (coarse — step >= 0.05, NOT finer, by design): TRAIL in {0.10..0.35 step .05} x maxhold {None,30min}.
Split: reuse smartexit t0-median split (rug 84/83, clean 48/47).
Lens: rug = real-gas (stress167 base50 + exit_p*); clean = QuoterV2 single-shot (optimistic).
Bootstrap: resample holdout pools w/ replacement >=1000x -> 90% CI on fatal@+300, clean median, clean total.
Pre-registered rule R (written BEFORE looking at holdout): pick the cell with MAX clean-retain
subject to holdout fatal@+300 90%-CI-UPPER <= T. T is left to the principal; we report the
selection for a range of T.
"""
from __future__ import annotations
import json, math, os, sys
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import keystone_live as K
import live_config_rebacktest as L

SIZE_USD = 50.0
LN1P = K.LN1P; HARD_STOP = K.HARD_STOP
STRESS = "/opt/EATLQ/mvp/audits/stress167_20260624/_stress167_cache.json"
OUT_DIR = "/opt/EATLQ/mvp/audits/exit_logic_lookahead_20260624"
TRAILS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35]
MAXHOLDS = [None, 30]            # minutes
DELAYS = [0, 60, 300]
N_BOOT = 2000
T_CANDIDATES = [30, 40, 50, 60, 70]   # %; principal fills the real T
RNG = np.random.default_rng(20260624)


def lp_at(ps, tb, orient):
    ch = None
    for _, lt, lb in ps:
        if lb <= tb: ch = (lt, lb)
        else: break
    if ch is None: ch = (ps[0][1], ps[0][2])
    return ch[0] * LN1P * orient


def exit_block_param(ps, entry_lp, orient, eb, trail, maxhold_blk):
    if not ps: return eb
    peak = entry_lp; thr = math.log(1 - trail)
    for _, lt, lb in ps:
        lp = lt * LN1P * orient; peak = max(peak, lp)
        if maxhold_blk is not None and (lb - eb) >= maxhold_blk: return lb
        if (lp - entry_lp) <= HARD_STOP: return lb
        if (lp - peak) <= thr: return lb
    return ps[-1][2]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    _, path = K.load()
    priced = json.load(open(K.PCACHE)); labels = L._load_labels()
    stress = json.load(open(STRESS))
    df = L.run_lens(L.load_base_df(), "qv2")
    dep, skel, creators, token_skel = L._load_blacklists()
    m = L.gate_masks(df, dep, skel, creators, token_skel)
    rug = df[m["model_depth"] & ~m["live_full"] & df.gate_fric & df.tradeable
             & (df.label == "delayed_rug") & (df.ret > 0)].copy()
    clean = df[m["live_full"] & df.tradeable & (df.label == "clean") & (df.ret > 0)].copy()

    rug_base = {}
    for _, r in rug.iterrows():
        rec = stress.get(f"{r.pool}:base50")
        if rec and rec.get("status") == "ok" and rec.get("real_net") is not None:
            rug_base[r.pool] = float(rec["real_net"])
    clean_base = {r.pool: float(r.ret) for _, r in clean.iterrows()}

    def rows(frame, base_map):
        out = []
        for _, r in frame.iterrows():
            if r.pool not in base_map: continue
            sh = labels[r.pool]
            out.append(dict(pool=r.pool, orient=int(r.orient), eb=int(r.entry_block),
                            exb0=int(priced[r.pool]["exb"]),
                            flip=int(sh["flip_block"]) if sh.get("flip_block") else 10**12,
                            entry_lp=r.last_tick_pre * LN1P * int(r.orient),
                            t0=r.t0, base=base_map[r.pool]))
        return out
    R = rows(rug, rug_base); C = rows(clean, clean_base)

    def split(rs):
        ts = sorted(x["t0"] for x in rs); med = ts[len(ts)//2]
        return [x for x in rs if x["t0"] <= med], [x for x in rs if x["t0"] > med]
    Rtr, Rho = split(R); Ctr, Cho = split(C)

    def pnl_at(x, b_exit, delay, is_rug):
        ps = path.get(x["pool"], [])
        lp_exit0 = lp_at(ps, x["exb0"], x["orient"])
        eff = b_exit + delay
        if eff <= x["exb0"] or not is_rug:
            return (1.0 + x["base"]) * math.exp(lp_at(ps, eff, x["orient"]) - lp_exit0) - 1.0
        over = eff - x["exb0"]
        cand = min((60, 300, 600), key=lambda v: abs(v - over))
        rec = stress.get(f"{x['pool']}:exit_p{cand}")
        if rec and rec.get("status") == "ok" and rec.get("real_net") is not None:
            return float(rec["real_net"])
        return -1.0 if eff >= x["flip"] else x["base"]

    def cell_nets(rs, trail, mh_min, is_rug):
        """Return dict delay -> np.array of per-pool nets for this cell."""
        mh = None if mh_min is None else int(mh_min * 60 / 2)
        bex = [exit_block_param(path.get(x["pool"], []), x["entry_lp"], x["orient"], x["eb"], trail, mh)
               for x in rs]
        return {d: np.array([pnl_at(x, b, d, is_rug) for x, b in zip(rs, bex)]) for d in DELAYS}

    def agg(nets):
        a = nets[0]
        return dict(total=float(a.sum() * SIZE_USD), median=float(np.median(a)),
                    maxret=float(a.max()),
                    fatal={d: float((nets[d] <= -0.5).mean()) for d in DELAYS})

    # ---- compute every cell (train + holdout) ----
    cells = {}
    for trail in TRAILS:
        for mh in MAXHOLDS:
            key = f"{trail}/{mh or 'none'}"
            r_tr = cell_nets(Rtr, trail, mh, True); c_tr = cell_nets(Ctr, trail, mh, False)
            r_ho = cell_nets(Rho, trail, mh, True); c_ho = cell_nets(Cho, trail, mh, False)
            cells[key] = dict(trail=trail, maxhold=mh,
                              train=dict(rug=agg(r_tr), clean=agg(c_tr)),
                              holdout=dict(rug=agg(r_ho), clean=agg(c_ho)),
                              _rug_ho_nets=r_ho, _clean_ho_nets=c_ho)

    # ---- bootstrap on holdout ----
    def boot_ci(arr_fn, n_items, iters=N_BOOT, lo=5, hi=95):
        vals = np.empty(iters)
        for i in range(iters):
            idx = RNG.integers(0, n_items, n_items)
            vals[i] = arr_fn(idx)
        return float(np.percentile(vals, lo)), float(np.percentile(vals, hi))
    nR = len(Rho); nC = len(Cho)
    for key, c in cells.items():
        rnet300 = c["_rug_ho_nets"][300]
        cnet0 = c["_clean_ho_nets"][0]
        f300_ci = boot_ci(lambda idx: (rnet300[idx] <= -0.5).mean(), nR)
        cmed_ci = boot_ci(lambda idx: np.median(cnet0[idx]), nC)
        ctot_ci = boot_ci(lambda idx: cnet0[idx].sum() * SIZE_USD, nC)
        c["ci"] = dict(fatal300=[round(f300_ci[0], 4), round(f300_ci[1], 4)],
                       clean_median=[round(cmed_ci[0], 4), round(cmed_ci[1], 4)],
                       clean_total=[round(ctot_ci[0], 1), round(ctot_ci[1], 1)])

    base_clean_ho = cells["0.35/none"]["holdout"]["clean"]["total"]
    for c in cells.values():
        c["clean_retain"] = (c["holdout"]["clean"]["total"] / base_clean_ho) if base_clean_ho else None

    # ---- pre-registered rule R for each candidate T ----
    rule_R = {}
    for T in T_CANDIDATES:
        feas = [(k, c) for k, c in cells.items() if c["ci"]["fatal300"][1] * 100 <= T]
        if feas:
            bk, bc = max(feas, key=lambda kc: kc[1]["clean_retain"])
            rule_R[T] = dict(cell=bk, clean_retain=round(bc["clean_retain"], 3),
                             fatal300_pt=round(bc["holdout"]["rug"]["fatal"][300], 4),
                             fatal300_ci=bc["ci"]["fatal300"])
        else:
            rule_R[T] = dict(cell=None, note="no cell meets CI-upper<=T")

    # ---- adjacent-TRAIL CI overlap (maxhold=None lane) ----
    def overlap(a, b):
        return not (a[1] < b[0] or b[1] < a[0])
    lane = [f"{t}/none" for t in TRAILS]
    overlaps = []
    for i in range(len(lane) - 1):
        a = cells[lane[i]]["ci"]["fatal300"]; b = cells[lane[i+1]]["ci"]["fatal300"]
        overlaps.append(dict(pair=f"{TRAILS[i]} vs {TRAILS[i+1]}",
                             ci_a=a, ci_b=b, overlap=overlap(a, b)))

    out = dict(meta=dict(rug_n=len(R), clean_n=len(C), rug_train=len(Rtr), rug_holdout=len(Rho),
                         clean_train=len(Ctr), clean_holdout=len(Cho), n_boot=N_BOOT,
                         grid_trails=TRAILS, grid_maxholds=MAXHOLDS,
                         rug_lens="real-gas", clean_lens="QuoterV2 optimistic",
                         T_candidates=T_CANDIDATES),
               cells={k: {kk: vv for kk, vv in c.items() if not kk.startswith("_")}
                      for k, c in cells.items()},
               rule_R_by_T=rule_R, adjacent_overlap_maxhold_none=overlaps)
    json.dump(out, open(os.path.join(OUT_DIR, "trail_frontier.json"), "w"), indent=2)

    # ---- console ----
    print(f"split: rug tr/ho={len(Rtr)}/{len(Rho)}  clean tr/ho={len(Ctr)}/{len(Cho)}  n_boot={N_BOOT}")
    print("\n=== HOLDOUT grid (point estimates) ===")
    print(f"{'cell':>11}{'f@0':>6}{'f@60':>7}{'f@300':>7}{'rugPnL':>9}{'clnPnL':>9}{'clnMed':>8}{'maxWin':>8}{'retain':>8}")
    for t in TRAILS:
        for mh in MAXHOLDS:
            k = f"{t}/{mh or 'none'}"; c = cells[k]; h = c["holdout"]
            print(f"{k:>11}{h['rug']['fatal'][0]*100:>5.0f}%{h['rug']['fatal'][60]*100:>6.0f}%{h['rug']['fatal'][300]*100:>6.0f}%"
                  f"{h['rug']['total']:>9.0f}{h['clean']['total']:>9.0f}{h['clean']['median']*100:>7.0f}%{h['clean']['maxret']*100:>7.0f}%{c['clean_retain']*100:>7.0f}%")
    print("\n=== HOLDOUT Bootstrap 90% CI (maxhold=none lane) ===")
    print(f"{'cell':>11}{'fatal@300 [CI]':>22}{'cleanMed [CI]':>26}{'cleanTotal [CI]':>26}")
    for t in TRAILS:
        c = cells[f"{t}/none"]; ci = c["ci"]; h = c["holdout"]
        print(f"{t:>11}{h['rug']['fatal'][300]*100:>6.0f}% [{ci['fatal300'][0]*100:.0f},{ci['fatal300'][1]*100:.0f}]"
              f"{h['clean']['median']*100:>10.0f}% [{ci['clean_median'][0]*100:.0f},{ci['clean_median'][1]*100:.0f}]"
              f"{h['clean']['total']:>12.0f} [{ci['clean_total'][0]:.0f},{ci['clean_total'][1]:.0f}]")
    print("\n=== adjacent-TRAIL fatal@300 CI overlap (maxhold none) ===")
    for o in overlaps:
        print(f"  {o['pair']:>14}: CI {[round(x*100) for x in o['ci_a']]} vs {[round(x*100) for x in o['ci_b']]} -> "
              f"{'OVERLAP (indistinguishable)' if o['overlap'] else 'separated'}")
    print("\n=== pre-registered rule R: pick max clean-retain s.t. fatal@300 CI-upper <= T ===")
    for T in T_CANDIDATES:
        r = rule_R[T]
        if r["cell"]:
            print(f"  T={T}%: -> cell {r['cell']}  clean_retain={r['clean_retain']*100:.0f}%  "
                  f"fatal@300={r['fatal300_pt']*100:.0f}% CI[{r['fatal300_ci'][0]*100:.0f},{r['fatal300_ci'][1]*100:.0f}]")
        else:
            print(f"  T={T}%: {r['note']}")
    print(f"\nwrote {OUT_DIR}/trail_frontier.json")


if __name__ == "__main__":
    main()
