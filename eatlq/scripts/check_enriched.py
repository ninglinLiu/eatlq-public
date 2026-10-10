import csv
import sys
from collections import Counter

import numpy as np

fp = sys.argv[1] if len(sys.argv) > 1 else "logs/labeled_dataset_run003a_v2_only_enriched.csv"
with open(fp) as f:
    rdr = csv.DictReader(f)
    hdr = rdr.fieldnames
    rows = list(rdr)

print(f"file: {fp}")
print(f"columns: {len(hdr)}")
print(f"rows: {len(rows)}")


def stats(col):
    vals = []
    nulls = 0
    for r in rows:
        v = r.get(col)
        if v is None or v == "":
            nulls += 1
            continue
        try:
            vals.append(float(v))
        except Exception:
            nulls += 1
    if not vals:
        return None
    a = np.array(vals)
    zeros = int((a == 0).sum())
    return {
        "non_null": len(vals),
        "nulls": nulls,
        "zero_rate": zeros / len(vals),
        "mean": float(a.mean()),
        "std": float(a.std()),
        "min": float(a.min()),
        "max": float(a.max()),
        "p50": float(np.percentile(a, 50)),
        "p90": float(np.percentile(a, 90)),
        "p99": float(np.percentile(a, 99)),
    }


cols = [
    "pool_age_sec", "pool_age_min",
    "is_new_pool_5m", "is_new_pool_15m", "is_new_pool_60m",
    "sm_buy_count_5m", "sm_sell_count_5m", "sm_unique_buyers_5m",
    "sm_net_flow_5m", "sm_buy_pressure_ratio_5m",
    "sm_first_seen_in_pool", "sm_wallet_tier_max", "sm_wallet_tier_mean",
    "unique_buyers_5m", "buyer_gini_5m",
    "top1_buyer_share_5m", "top3_buyer_share_5m", "top5_buyer_share_5m",
    "new_buyer_ratio_5m", "repeat_buyer_ratio_5m",
    "unique_buyer_entropy_5m",
]

header = f"{'feature':32s} {'non_null':>8s} {'zero_rate':>9s} {'mean':>10s} {'std':>10s} {'p90':>12s} {'max':>12s}"
print("\n=== enriched feature diagnostics ===")
print(header)
print("-" * len(header))
broken = []
for c in cols:
    s = stats(c)
    if s is None:
        print(f"{c:32s}  ALL-NULL")
        broken.append(c)
        continue
    print(f"{c:32s} {s['non_null']:>8d} {s['zero_rate']:>9.3f} "
          f"{s['mean']:>10.4f} {s['std']:>10.4f} "
          f"{s['p90']:>12.4f} {s['max']:>12.4f}")
    if s['zero_rate'] > 0.95 or s['std'] == 0:
        broken.append(c + f"  (zero_rate={s['zero_rate']:.3f})")

print("\n=== pool_age_bucket histogram ===")
for k, v in Counter(r["pool_age_bucket"] for r in rows).most_common():
    print(f"  {k:10s} {v:>6d}")

print("\n=== broken/near-empty features ===")
for b in broken:
    print(" ", b)
