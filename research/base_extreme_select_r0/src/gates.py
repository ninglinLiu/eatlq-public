"""Apply the pre-registered concentration gate and episode diagnostics.

Does not retune. Hyperparameters are the frozen choice from the one OOS pass.
Leave-one-pool-out precision is required by GOAL.md §5.5 once a single pool
contributes more than 30% of OOS hits.
"""
import json
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from model import FEATS, IS_END, N_PICK_OOS, OUT, TARGETS, prep


def boot_prec(y, n=4000, alpha=0.05, seed=11):
    rng = np.random.default_rng(seed)
    m = len(y)
    if m == 0:
        return float("nan"), float("nan")
    draws = y[rng.integers(0, m, (n, m))].mean(axis=1)
    return tuple(float(x) for x in np.quantile(draws, [alpha / 2, 1 - alpha / 2]))


def episodes(pool, bar, y):
    """Split positives into contiguous-in-time runs per pool (gap > 2h starts a new one)."""
    order = np.argsort(bar)
    last = {}
    eid = np.full(len(y), -1, dtype=np.int64)
    nxt = 0
    for i in order:
        if y[i] == 0:
            continue
        p = pool[i]
        t = bar[i]
        if p in last and (t - last[p][0]) <= np.timedelta64(2, "h"):
            eid[i] = last[p][1]
        else:
            eid[i] = nxt
            nxt += 1
        last[p] = (t, eid[i])
    return eid


def main():
    df = prep()
    y200 = (df["net_up_bps"].to_numpy() >= TARGETS["+200%"]).astype(np.int8)
    is_mask = (df["bar"].to_numpy() < IS_END)
    Xis = df.loc[is_mask, FEATS].to_numpy()
    yis = y200[is_mask]
    Xoos = df.loc[~is_mask, FEATS].to_numpy()
    oos = df.loc[~is_mask].reset_index(drop=True)
    yo = y200[~is_mask]
    print(f"IS {is_mask.sum():,} / {yis.sum():,} pos;  OOS {len(oos):,} / {yo.sum():,} pos",
          flush=True)

    spw = float((len(yis) - yis.sum()) / max(int(yis.sum()), 1))
    gbm = XGBClassifier(
        tree_method="hist", n_jobs=-1, eval_metric="logloss",
        scale_pos_weight=spw, subsample=0.8, colsample_bytree=0.8,
        reg_lambda=1.0, random_state=0,
        max_depth=4, n_estimators=600, min_child_weight=10, learning_rate=0.05,
    )
    gbm.fit(Xis, yis)
    print("gbm fitted", flush=True)

    med = np.nanmedian(Xis, axis=0)
    sc = StandardScaler().fit(np.where(np.isnan(Xis), med, Xis))
    lr = LogisticRegression(C=0.1, max_iter=2000, class_weight="balanced")
    lr.fit(sc.transform(np.where(np.isnan(Xis), med, Xis)), yis)
    print("logit fitted", flush=True)

    scores = {
        "gbm": gbm.predict_proba(Xoos)[:, 1],
        "logit": lr.predict_proba(sc.transform(np.where(np.isnan(Xoos), med, Xoos)))[:, 1],
    }
    eid = episodes(oos["pool"].to_numpy(), oos["bar"].to_numpy(), yo)

    report = {}
    for name, sco in scores.items():
        top = np.argsort(-sco)[:N_PICK_OOS]
        hit = yo[top]
        pools = oos["pool"].to_numpy()[top]
        bars = oos["bar"].to_numpy()[top]
        feats = {c: oos[c].to_numpy()[top] for c in ("r1", "r4", "r24", "pos_hi", "rng")}
        net = oos["net_up_bps"].to_numpy()[top]
        pc = Counter(pools[hit == 1])
        top_pool, top_n = (pc.most_common(1)[0] if pc else ("", 0))
        conc = top_n / max(int(hit.sum()), 1)

        # leave-one-pool-out: drop the heaviest pool, keep the same K from the rest
        loo_mask = oos["pool"].to_numpy() != top_pool
        loo_top = np.argsort(np.where(loo_mask, sco, -1.0))[::-1][:N_PICK_OOS]
        loo_hit = yo[loo_top]
        loo_p = float(loo_hit.mean())
        loo_lo, loo_hi = boot_prec(loo_hit)

        # unique episodes among the original top-K hits
        hit_eids = eid[top][hit == 1]
        n_ep = len(set(int(x) for x in hit_eids if x >= 0))

        # one pick per pool per calendar day (what a real book can take)
        key = list(zip(pools, bars.astype("datetime64[D]")))
        seen, keep = set(), []
        for i, k in enumerate(key):
            if k in seen:
                continue
            seen.add(k)
            keep.append(i)
        dedup = np.array(keep)
        # still take at most 180 unique (pool, day)
        dedup = dedup[:N_PICK_OOS]
        dhit = hit[dedup] if len(dedup) else np.array([], dtype=np.int8)
        # wait: hit is aligned to `top`, so index into top then yo
        dhit = yo[top[dedup]] if len(dedup) else np.array([], dtype=np.int8)

        # first hour of each positive episode only — can we enter BEFORE the move?
        first = {}
        for i in np.argsort(oos["bar"].to_numpy()):
            e = eid[i]
            if e >= 0 and e not in first:
                first[e] = i
        first_idx = np.array(sorted(first.values()))
        # among top-K, how many are the opening bar of their episode?
        top_e = eid[top]
        is_open = np.array([e >= 0 and first.get(int(e), -1) == top[j]
                            for j, e in enumerate(top_e)])

        rec = dict(
            n_pick=int(len(top)), n_hit=int(hit.sum()),
            prec=float(hit.mean()),
            prec_lo=boot_prec(hit)[0], prec_hi=boot_prec(hit)[1],
            n_pools=len(set(pools.tolist())),
            n_hit_pools=len(pc),
            top_pool=str(top_pool), top_pool_hits=int(top_n),
            top_pool_share=float(conc),
            loo_prec=loo_p, loo_lo=loo_lo, loo_hi=loo_hi,
            loo_hits=int(loo_hit.sum()),
            n_episodes=int(n_ep),
            unique_pool_days=int(len(dedup)),
            dedup_prec=float(dhit.mean()) if len(dhit) else float("nan"),
            n_opening_bar=int(is_open.sum()),
            opening_bar_hits=int(yo[top[is_open]].sum()) if is_open.any() else 0,
            feat_med={c: float(np.nanmedian(feats[c])) for c in feats},
            feat_hit_med={c: float(np.nanmedian(feats[c][hit == 1])) for c in feats},
            already_up_r24=float((feats["r24"] > np.log(1.5)).mean()),
            already_up_hit=float((feats["r24"][hit == 1] > np.log(1.5)).mean()) if hit.sum() else 0.0,
            half1=float(hit[bars < bars[len(bars) // 2]].mean()) if len(hit) else float("nan"),
            half2=float(hit[bars >= bars[len(bars) // 2]].mean()) if len(hit) else float("nan"),
        )
        report[name] = rec
        print(f"\n=== {name} ===", flush=True)
        print(f"  raw 精度 {100*rec['prec']:.1f}% [{100*rec['prec_lo']:.1f},{100*rec['prec_hi']:.1f}]  "
              f"命中 {rec['n_hit']}/{rec['n_pick']}", flush=True)
        print(f"  集中度 最重池 {rec['top_pool'][:12]}  {rec['top_pool_hits']}/{rec['n_hit']} = "
              f"{100*rec['top_pool_share']:.1f}%  (门=30%)", flush=True)
        print(f"  留一后 精度 {100*rec['loo_prec']:.1f}% [{100*rec['loo_lo']:.1f},{100*rec['loo_hi']:.1f}]  "
              f"命中 {rec['loo_hits']}/{N_PICK_OOS}", flush=True)
        print(f"  独立事件 {rec['n_episodes']} 个  /  独立(池,日) {rec['unique_pool_days']}  "
              f"去重精度 {100*rec['dedup_prec']:.1f}%", flush=True)
        print(f"  顶K里是事件第一根的 {rec['n_opening_bar']} 根（其中命中 {rec['opening_bar_hits']}）",
              flush=True)
        print(f"  进场时 r24 中位 {rec['feat_med']['r24']:.3f}  "
              f"已涨过 +50% 的占比 {100*rec['already_up_r24']:.1f}% "
              f"(命中子集 {100*rec['already_up_hit']:.1f}%)", flush=True)
        print(f"  pos_hi 中位 {rec['feat_med']['pos_hi']:.3f}  "
              f"命中子集 {rec['feat_hit_med']['pos_hi']:.3f}", flush=True)

    with open(f"{OUT}/gates.json", "w") as f:
        json.dump(report, f, indent=1)
    print(f"\nwrote {OUT}/gates.json", flush=True)


if __name__ == "__main__":
    main()
