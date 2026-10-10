#!/usr/bin/env python3
"""Full-day (43,200-block) liquidation scans on selected stress days + calm controls.

5 eth_getLogs calls per day (10,000-block hard span limit).
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/root/recon_out")
from probe_rpc import CALLS, get_logs  # noqa: E402

TOPICS = {
    "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41": "MORPHO",
    "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286": "AAVEFORK",
    "0x298637f684da70674f26509b10f07ec2fbc77a335ab1e7d6215a4b2484d8bb52": "MOONWELL",
    "0x9850ab1af75177e4a9201c65a2cf7976d5d28e40ef63494b44366f86b2f9412e": "COMET_C",
    "0x1547a878dc89ad3c367b6338b4be6a65a5dd74fb77ae044da1e8747ef1f4f62f": "COMET_D",
}

DAYS = [
    ("2026-02-05", 41730127, "stress -15.03%"),
    ("2025-10-10", 36632527, "stress -12.45%"),
    ("2026-06-05", 46914126, "stress -11.70%"),
    ("2025-11-04", 37712527, "stress -11.86%"),
    ("2026-08-15", 49981326, "calm   -0.41%"),
    ("2026-04-25", 45142926, "calm   -0.52%"),
]
DAY_BLOCKS = 43_200


def chunks(b0):
    out = []
    a = b0
    while a < b0 + DAY_BLOCKS:
        b = min(a + 9_999, b0 + DAY_BLOCKS - 1)
        out.append((a, b))
        a = b + 1
    return out


def fetch(c):
    a, b = c
    r = get_logs([list(TOPICS.keys())], a, b)
    return (a, b, None if "error" in r else r["result"],
            r.get("error", {}).get("message", "")[:60] if "error" in r else None)


if __name__ == "__main__":
    res = {}
    for dt, b0, tag in DAYS:
        cs = chunks(b0)
        logs = []
        errs = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            for a, b, lg, err in ex.map(fetch, cs):
                if err:
                    errs.append((a, b, err))
                else:
                    logs += lg
        c = Counter(TOPICS[l["topics"][0]] for l in logs)
        blocks = sorted({int(l["blockNumber"], 16) for l in logs})
        eps = 1 if blocks else 0
        for x, y in zip(blocks, blocks[1:]):
            if y - x > 300:
                eps += 1
        res[dt] = {"tag": tag, "b0": b0, "n": len(logs), "by_proto": dict(c),
                   "episodes": eps, "errs": errs, "logs": logs}
        print(f"{dt}  {tag:16s} total={len(logs):<6} episodes={eps:<4} {dict(c)}"
              + (f"  ERRS={errs}" if errs else ""), flush=True)

    json.dump({k: {kk: vv for kk, vv in v.items()} for k, v in res.items()},
              open("/root/recon_out/fullday.json", "w"))
    print("rpc_calls:", CALLS)
