#!/usr/bin/env python3
"""How large can a position get on these pools before friction eats the edge?

The stop-fill run measured one size, $50, and found a 0.6% round trip that does
not widen during the crash. That makes position size the open question rather
than a settled input: with only 64 qualifying events a month, $50 clips can
deploy 2.1% of a $5,000 account per day, and no hit rate fixes an account that
cannot get its money to work.

So the same atomic round trip is repeated at five sizes on the same blocks. The
calm block answers how big an entry can be; the crash block answers how big an
exit can be, which is the side that actually failed for a third of episodes.
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path("/opt/EATLQ/scripts/lib")))

from fork_stop import atomic_at, compile_creation_bytecode  # noqa: E402

ETH_USD = 1865.0                       # panel median, same constant as GOAL.md
SIZES_USD = [50, 250, 500, 2500, 5000]
N_EPISODES = 100


def wei(usd: float) -> int:
    return int(round(usd / ETH_USD * 1e18))


def main() -> int:
    url = os.environ.get("FORK_ARCHIVE_RPC")
    if not url:
        raise SystemExit("FORK_ARCHIVE_RPC is not set")
    tr = json.loads((OUT / "stop_results.json").read_text())["trades"]
    eps = [t for t in tr if t.get("label") in ("filled", "cannot_sell")]
    if len(eps) > N_EPISODES:
        step = len(eps) / N_EPISODES
        eps = [eps[int(i * step)] for i in range(N_EPISODES)]
    bytecode = compile_creation_bytecode()
    print(f"episodes={len(eps)} sizes={SIZES_USD} calls={len(eps)*len(SIZES_USD)*2}")

    res = []
    for i, e in enumerate(eps, 1):
        row = dict(pool=e["pool"], symbol=e["symbol"], fee=e["fee"],
                   drop=e["drop"], peak_block=e["peak_block"],
                   stop_block=e["stop_block"], died=e["label"] == "cannot_sell",
                   calm={}, crash={})
        for usd in SIZES_USD:
            sp = wei(usd)
            for tag, blk in (("calm", e["peak_block"]), ("crash", e["stop_block"])):
                try:
                    a = atomic_at_size(url, bytecode, e["token"], e["fee"], blk, sp)
                    row[tag][str(usd)] = a
                except Exception as exc:
                    row[tag][str(usd)] = dict(ok=False,
                                              err=f"{type(exc).__name__}:{str(exc)[:80]}")
        res.append(row)
        c = row["calm"]
        print(f"  [{i}/{len(eps)}] {e['symbol']:<12} calm friction " +
              "  ".join(f"${s}:{_f(c.get(str(s)))}" for s in SIZES_USD), flush=True)
        if i % 10 == 0 or i == len(eps):
            (OUT / "size_sweep.json").write_text(json.dumps({"rows": res}, indent=1),
                                                 encoding="utf-8")

    print("\n=== round-trip friction by size (median over episodes) ===")
    print(f"{'size':>8}{'calm n_ok':>11}{'calm med':>10}{'calm q90':>10}"
          f"{'crash n_ok':>12}{'crash med':>11}{'crash q90':>11}")
    summary = {}
    for s in SIZES_USD:
        row = {}
        for tag in ("calm", "crash"):
            f = [r[tag][str(s)]["friction"] for r in res
                 if r[tag].get(str(s), {}).get("friction") is not None]
            row[tag] = dict(n_ok=len(f), n=len(res),
                            med=st.median(f) if f else None,
                            q90=(sorted(f)[len(f) // 10] if len(f) >= 10 else None))
        summary[str(s)] = row
        print(f"{s:>8}{row['calm']['n_ok']:>11}{_p(row['calm']['med']):>10}"
              f"{_p(row['calm']['q90']):>10}{row['crash']['n_ok']:>12}"
              f"{_p(row['crash']['med']):>11}{_p(row['crash']['q90']):>11}")

    (OUT / "size_sweep.json").write_text(json.dumps(
        {"updated_at": datetime.now(timezone.utc).isoformat(),
         "eth_usd": ETH_USD, "sizes_usd": SIZES_USD,
         "summary": summary, "rows": res}, indent=1), encoding="utf-8")
    return 0


def atomic_at_size(url, bytecode, token, fee, block, spend_wei):
    """Same call as fork_stop.atomic_at but with the size as an argument."""
    import fork_stop as fs
    keep = fs.SPEND_WEI
    fs.SPEND_WEI = spend_wei
    try:
        a = atomic_at(url, bytecode, token, fee, block)
    finally:
        fs.SPEND_WEI = keep
    return dict(ok=bool(a["sell_ok"]), friction=a["friction"], received=a["received"])


def _f(a) -> str:
    if not a or a.get("friction") is None:
        return "  x  "
    return f"{100*a['friction']:.1f}%"


def _p(x) -> str:
    return "n/a" if x is None else f"{100*x:.2f}%"


if __name__ == "__main__":
    raise SystemExit(main())
