#!/usr/bin/env python3
"""Task B — 巧刀 (smart conditional exit) with train/holdout validation. Offline, RPC-free.

Hypothesis: route each trade by a PRE-ENTRY risk score (p_fatal). High-risk -> tight
exit (max-hold cap) to escape before a rug; low-risk -> loose (current trailing) to
keep the clean convex right tail. If a rule found on TRAIN also works on HOLDOUT,
the rug tail and clean alpha are SEPARABLE (Conclusion A). Else welded (Conclusion B).

Lens: rug base = real-gas (stress167 base50); clean base = QuoterV2 single-shot (optimistic).
Earlier/longer-exit delta = realized forward tick-path ratio; post-exb delay = real-gas exit_p*.
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
CUR_TRAIL = 0.35
P_GRID = [0.05, 0.08, 0.10, 0.12]      # pre-entry p_fatal routing thresholds
T_GRID_MIN = [20, 30]                   # tight-bucket max-hold (minutes)
DELAYS = [0, 60, 300]


def lp_at(ps, tb, orient):
    ch = None
    for _, lt, lb in ps:
        if lb <= tb: ch = (lt, lb)
        else: break
    if ch is None: ch = (ps[0][1], ps[0][2])
    return ch[0] * LN1P * orient


def exit_block_param(ps, entry_lp, orient, eb, trail, maxhold_blk):
    if not ps: return eb, entry_lp
    peak = entry_lp; thr = math.log(1 - trail)
    for _, lt, lb in ps:
        lp = lt * LN1P * orient; peak = max(peak, lp)
        if maxhold_blk is not None and (lb - eb) >= maxhold_blk: return lb, lp
        if (lp - entry_lp) <= HARD_STOP: return lb, lp
        if (lp - peak) <= thr: return lb, lp
    return ps[-1][2], ps[-1][1] * LN1P * orient


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
            exb0 = int(priced[r.pool]["exb"])
            flip = int(sh["flip_block"]) if sh.get("flip_block") else 10**12
            entry_lp = r.last_tick_pre * LN1P * int(r.orient)
            out.append(dict(pool=r.pool, orient=int(r.orient), eb=int(r.entry_block),
                            exb0=exb0, flip=flip, entry_lp=entry_lp, p=float(r.p),
                            t0=r.t0, base=base_map[r.pool]))
        return out
    R = rows(rug, rug_base); C = rows(clean, clean_base)

    # time split per class (median t0) -> train/holdout
    def split(rs):
        ts = sorted(x["t0"] for x in rs); med = ts[len(ts)//2]
        tr = [x for x in rs if x["t0"] <= med]; ho = [x for x in rs if x["t0"] > med]
        return tr, ho
    Rtr, Rho = split(R); Ctr, Cho = split(C)

    def pnl_at(x, b_exit, delay, is_rug):
        """net at exit block b_exit + delay (blocks). tick pre-exb0, real-gas post-exb0 (rug)."""
        ps = path.get(x["pool"], [])
        lp_exit0 = lp_at(ps, x["exb0"], x["orient"])
        eff = b_exit + delay
        if eff <= x["exb0"] or not is_rug:
            lp_t = lp_at(ps, eff, x["orient"])
            return (1.0 + x["base"]) * math.exp(lp_t - lp_exit0) - 1.0
        over = eff - x["exb0"]
        cand = min((60, 300, 600), key=lambda v: abs(v - over))
        rec = stress.get(f"{x['pool']}:exit_p{cand}")
        if rec and rec.get("status") == "ok" and rec.get("real_net") is not None:
            return float(rec["real_net"])
        return -1.0 if eff >= x["flip"] else x["base"]

    def apply_rule(rs, is_rug, P, Tmin):
        """route: p>=P -> tight (maxhold Tmin, current trail); else loose (current trail, no cap)."""
        maxhold = int(Tmin * 60 / 2)
        per = []
        for x in rs:
            tight = x["p"] >= P
            mh = maxhold if tight else None
            b_exit, _ = exit_block_param(path.get(x["pool"], []), x["entry_lp"], x["orient"],
                                         x["eb"], CUR_TRAIL, mh)
            per.append((x, b_exit))
        return per

    def metrics(per, is_rug):
        out = {}
        for d in DELAYS:
            nets = [pnl_at(x, b, d, is_rug) for x, b in per]
            a = np.array(nets)
            out[d] = dict(total=float(a.sum() * SIZE_USD),
                          fatal_rate=float((a <= -0.5).mean()),
                          median=float(np.median(a)), wr=float((a > 0).mean()),
                          maxret=float(a.max()))
        return out

    def blunt(rs, is_rug, trail, maxhold_min):
        mh = None if maxhold_min is None else int(maxhold_min*60/2)
        per = []
        for x in rs:
            b,_=exit_block_param(path.get(x["pool"],[]),x["entry_lp"],x["orient"],x["eb"],trail,mh)
            per.append((x,b))
        return metrics(per, is_rug)

    # ---- TRAIN: pick best (P, T) ----
    # objective: minimize rug fatal@+300 subject to clean total PnL retain >= 80% (train)
    base_clean_tr = blunt(Ctr, False, CUR_TRAIL, None)[0]["total"]
    cands = []
    for P in P_GRID:
        for T in T_GRID_MIN:
            rm = metrics(apply_rule(Rtr, True, P, T), True)
            cm = metrics(apply_rule(Ctr, False, P, T), False)
            clean_ret = cm[0]["total"] / base_clean_tr if base_clean_tr else 0
            cands.append(dict(P=P, T=T, rug_fatal300=rm[300]["fatal_rate"],
                              clean_retain=clean_ret, rug_total0=rm[0]["total"],
                              clean_total0=cm[0]["total"]))
    feasible = [c for c in cands if c["clean_retain"] >= 0.80]
    pool_sel = feasible if feasible else cands
    best = min(pool_sel, key=lambda c: (c["rug_fatal300"], -c["clean_retain"]))

    # ---- HOLDOUT: freeze best, evaluate ----
    P, T = best["P"], best["T"]
    rm_ho = metrics(apply_rule(Rho, True, P, T), True)
    cm_ho = metrics(apply_rule(Cho, False, P, T), False)
    # train metrics of the frozen rule (for overfit self-check)
    rm_tr = metrics(apply_rule(Rtr, True, P, T), True)
    cm_tr = metrics(apply_rule(Ctr, False, P, T), False)

    # baselines on HOLDOUT
    b_cur_r = blunt(Rho, True, CUR_TRAIL, None); b_cur_c = blunt(Cho, False, CUR_TRAIL, None)
    b_t25_r = blunt(Rho, True, 0.25, None);      b_t25_c = blunt(Cho, False, 0.25, None)
    b_tight_r = blunt(Rho, True, CUR_TRAIL, T);  b_tight_c = blunt(Cho, False, CUR_TRAIL, T)

    def row(tag, rr, cc):
        return (f"| {tag} | {rr[0]['fatal_rate']*100:.0f}% / {rr[60]['fatal_rate']*100:.0f}% / {rr[300]['fatal_rate']*100:.0f}% "
                f"| ${rr[0]['total']:,.0f} | ${cc[0]['total']:,.0f} | {cc[0]['median']*100:.0f}% | {cc[0]['maxret']*100:.0f}% |")

    base_clean_ho = b_cur_c[0]["total"]
    smart_clean_retain_ho = cm_ho[0]["total"] / base_clean_ho if base_clean_ho else 0

    out = {
        "meta": dict(rug_n=len(R), clean_n=len(C),
                     rug_train=len(Rtr), rug_holdout=len(Rho),
                     clean_train=len(Ctr), clean_holdout=len(Cho),
                     routing_score="p_fatal (15 pre-entry feats)",
                     p_fatal_auc_rug_vs_clean=0.425),
        "train_selected_rule": best,
        "train_metrics_frozen_rule": dict(rug=rm_tr, clean=cm_tr,
                                          clean_retain=cm_tr[0]["total"]/ (blunt(Ctr,False,CUR_TRAIL,None)[0]["total"] or 1)),
        "holdout": dict(rug=rm_ho, clean=cm_ho, clean_retain=smart_clean_retain_ho),
        "holdout_baselines": dict(
            blunt_current=dict(rug=b_cur_r, clean=b_cur_c),
            blunt_trail025=dict(rug=b_t25_r, clean=b_t25_c),
            blunt_alltight=dict(rug=b_tight_r, clean=b_tight_c)),
    }
    json.dump(out, open(os.path.join(OUT_DIR, "smartexit_backtest.json"), "w"), indent=2)

    print(f"split: rug train/holdout={len(Rtr)}/{len(Rho)}  clean train/holdout={len(Ctr)}/{len(Cho)}")
    print(f"p_fatal AUC(rug-winner vs clean-winner)=0.425  best single feat |AUC-0.5|=0.153 (<router need 0.70)")
    print(f"\nTRAIN-selected巧刀 rule: p_fatal>= {P} -> max-hold {T}min (tight); else current trailing")
    print(f"  train: rug_fatal@+300={best['rug_fatal300']*100:.0f}%  clean_retain={best['clean_retain']*100:.0f}%")
    print(f"\n=== HOLDOUT comparison ===")
    print("| 规则 | rug fatal@+0/+60/+300 | rug总PnL | clean总PnL | clean中位 | 最大赢家 |")
    print("|---|---|---|---|---|---|")
    print(row("巧刀(train-frozen)", rm_ho, cm_ho))
    print(row("钝刀-现状(TRAIL0.35)", b_cur_r, b_cur_c))
    print(row("钝刀-TRAIL0.25", b_t25_r, b_t25_c))
    print(row(f"钝刀-全紧(maxhold{T}min)", b_tight_r, b_tight_c))
    print(f"\nholdout: 巧刀 clean_retain={smart_clean_retain_ho*100:.0f}%  rug_fatal@+300={rm_ho[300]['fatal_rate']*100:.0f}% "
          f"vs 钝刀现状 {b_cur_r[300]['fatal_rate']*100:.0f}%")
    print(f"\noverfit self-check: train rug_fatal@+300={rm_tr[300]['fatal_rate']*100:.0f}% -> holdout {rm_ho[300]['fatal_rate']*100:.0f}% ; "
          f"train clean_retain={best['clean_retain']*100:.0f}% -> holdout {smart_clean_retain_ho*100:.0f}%")
    print(f"wrote {OUT_DIR}/smartexit_backtest.json")


if __name__ == "__main__":
    main()
