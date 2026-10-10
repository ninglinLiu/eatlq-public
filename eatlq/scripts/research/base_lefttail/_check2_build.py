"""Check 2 helper: dump the base (mode=none) trade set with token + return.

Reuses risk_rebacktest.load_inputs + build_trades so the trade set matches the
published rebacktest exactly. Writes _check2_base_trades.tsv: pool token ret size.
Read-only.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import risk_rebacktest as R  # noqa: E402

_, test, dynamic, risk, p_cut = R.load_inputs()
trades = R.build_trades(test, dynamic, risk, "none", cap_usd=1000, start_equity=10000, frac=0.20, haircut=0.02)
out = os.path.join(HERE, "_check2_base_trades.tsv")
with open(out, "w") as f:
    f.write("pool\ttoken\tret\tsize\n")
    for t in trades:
        f.write(f"{t.pool}\t{t.token}\t{t.ret:.6f}\t{t.size:.0f}\n")
print(f"base trades n={len(trades)} p_cut={p_cut:.4f} -> {out}")
fatal = sum(1 for t in trades if t.ret <= -0.5)
print(f"base fatal(<=-50%)={fatal} ({fatal/len(trades)*100:.1f}%)")
