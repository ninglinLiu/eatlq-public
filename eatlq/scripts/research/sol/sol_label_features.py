#!/usr/bin/env python3
"""Stage 2 — label the sampled creates with settled outcomes + birth-fixed
ex-ante features, using ONLY free pump.fun per-mint endpoints (zero RPC).

Per mint we read frontend-api-v3/coins/{mint}:
  outcome:  ath_market_cap (USD peak), ath_market_cap_timestamp, complete
            (graduated), current usd_market_cap, last_trade_timestamp
  timing:   created_timestamp, time_to_ath_s  <-- CAPTURABILITY: if the right
            tail peaks within seconds, a 30-300s poller cannot enter in time.
  ex-ante (birth-fixed, no lookahead): has_description, has_twitter,
            has_website, has_telegram, nsfw, name/symbol length, creator.

We deliberately do NOT use cumulative/current fields (reply_count, holders,
buy/sell counts) as features — they are observed AFTER creation (lookahead).

Output: mvp/diagnostics/sol/creates_labeled.jsonl
"""
from __future__ import annotations
import json
import os
import time
import urllib.request

IN = "/opt/EATLQ/mvp/diagnostics/sol/creates_sample.jsonl"
OUT = "/opt/EATLQ/mvp/diagnostics/sol/creates_labeled.jsonl"
V3 = "https://frontend-api-v3.pump.fun/coins/{}"
ADV = "https://advanced-api-v2.pump.fun/coins/metadata/{}"


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            return json.loads(urllib.request.urlopen(req, timeout=15).read())
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(0.5 * (i + 1))


def main():
    rows = [json.loads(l) for l in open(IN)]
    print(f"[label] {len(rows)} sampled mints")
    done = set()
    mode = "w"
    if os.path.exists(OUT):
        for l in open(OUT):
            try:
                done.add(json.loads(l)["mint"])
            except Exception:
                pass
        mode = "a"
    n = 0
    miss = 0
    with open(OUT, mode) as f:
        for i, r in enumerate(rows):
            mint = r["mint"]
            if mint in done:
                continue
            d = get(V3.format(mint))
            time.sleep(0.12)
            if not d or "created_timestamp" not in d:
                miss += 1
                continue
            created = d.get("created_timestamp")  # ms
            ath = d.get("ath_market_cap")
            ath_ts = d.get("ath_market_cap_timestamp")
            t2ath = ((ath_ts - created) / 1000.0) if (ath_ts and created) else None
            desc = (d.get("description") or "").strip()
            out = {
                "mint": mint,
                "created_ms": created,
                "block_time": r.get("block_time"),
                "bonding_curve": d.get("bonding_curve"),
                "pool_address": d.get("pool_address"),
                # outcome / label
                "ath_mc": ath,
                "ath_ts": ath_ts,
                "time_to_ath_s": round(t2ath, 1) if t2ath is not None else None,
                "graduated": bool(d.get("complete")),
                "cur_mc_usd": d.get("usd_market_cap"),
                "last_trade_ms": d.get("last_trade_timestamp"),
                # birth-fixed ex-ante features (no lookahead)
                "has_desc": bool(desc),
                "desc_len": len(desc),
                "has_twitter": bool(d.get("twitter")),
                "has_website": bool(d.get("website")),
                "has_telegram": bool(d.get("telegram")),
                "nsfw": bool(d.get("nsfw")),
                "name_len": len(d.get("name") or ""),
                "sym_len": len(d.get("symbol") or ""),
                "creator": d.get("creator"),
                "mayhem": d.get("mayhem_state"),
                "is_banned": bool(d.get("is_banned")),
            }
            f.write(json.dumps(out) + "\n")
            n += 1
            if (i + 1) % 50 == 0:
                f.flush()
                print(f"  [label] {i+1}/{len(rows)} ok={n} miss={miss}", flush=True)
    print(f"[done] labeled {n} (miss={miss}) -> {OUT}")


if __name__ == "__main__":
    main()
