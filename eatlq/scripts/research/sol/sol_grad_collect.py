#!/usr/bin/env python3
"""Direction A — post-graduation mid-term behavior of pump.fun graduates (now on
PumpSwap AMM). Speed-INSENSITIVE: we enter minutes after graduation via the AMM
(reliable Jupiter buy/sell), which fits a 30-300s poller.

Mostly FREE: graduate feed (advanced-api-v2) gives graduationDate + poolAddress +
birth/graduation-fixed ex-ante features; GeckoTerminal gives the post-grad minute
OHLCV on the PumpSwap pool (verified to cover from graduation for <16h-old pools).

We sample graduates aged [--min-age-h, --max-age-h] so (a) the post-grad path is
settled and (b) GT minute history still reaches graduation. Output one row/coin:
  mvp/diagnostics/sol/grad_paths.jsonl
"""
from __future__ import annotations
import argparse
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

FEED = "https://advanced-api-v2.pump.fun/coins/graduated?limit=50"
GT = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{}/ohlcv/minute?aggregate=1&limit=1000"
OUT = "/opt/EATLQ/mvp/diagnostics/sol/grad_paths.jsonl"


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            return json.loads(urllib.request.urlopen(req, timeout=20).read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (i + 1)); continue
            if i == tries - 1:
                return None
            time.sleep(1.0 * (i + 1))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.0 * (i + 1))


def enumerate_grads(min_age_h, max_age_h, max_pages):
    now = time.time() * 1000
    lo = now - max_age_h * 3.6e6
    hi = now - min_age_h * 3.6e6
    out = []
    ls = None
    for _ in range(max_pages):
        url = FEED + (f"&lastScore={ls}" if ls is not None else "")
        d = get(url)
        if not d or not d.get("coins"):
            break
        for c in d["coins"]:
            gd = c.get("graduationDate")
            if gd and lo <= gd <= hi:
                out.append(c)
        ls = d.get("pagination", {}).get("lastScore")
        oldest = d["coins"][-1].get("graduationDate") or now
        if oldest < lo or not d.get("pagination", {}).get("hasMore") or ls is None:
            break
        time.sleep(0.15)
    return out


def grid_from_ohlcv(cl, grad_s):
    """cl = ohlcv_list desc [ts,o,h,l,c,v]. Return path dict {minutes_after_grad: close}
    plus forward MFE and volume features."""
    rows = sorted(cl, key=lambda x: x[0])  # asc by ts
    rows = [r for r in rows if r[0] >= grad_s - 90]
    if not rows:
        return None
    path = {}
    for r in rows:
        m = round((r[0] - grad_s) / 60)
        if m >= 0:
            path[m] = r[4]  # close
    if not path:
        return None
    # MFE over available window from grad
    p0 = rows[0][4]
    highs = [r[2] for r in rows]
    mfe = max(highs) / p0 - 1 if p0 else None
    vol10 = sum(r[5] for r in rows if r[0] <= grad_s + 600)
    n_candles = len(rows)
    return {"path": {str(k): v for k, v in path.items()}, "p_grad": p0, "mfe_avail": mfe,
            "vol10m": vol10, "n_candles": n_candles, "last_min": max(path.keys())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-h", type=float, default=2.5)
    ap.add_argument("--max-age-h", type=float, default=16.0)
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--gt-sleep", type=float, default=2.2)
    args = ap.parse_args()

    grads = enumerate_grads(args.min_age_h, args.max_age_h, args.max_pages)
    print(f"[gradA] enumerated {len(grads)} graduates aged [{args.min_age_h},{args.max_age_h}]h")
    n = 0
    miss = 0
    with open(OUT, "w") as f:
        for i, c in enumerate(grads):
            pool = c.get("poolAddress")
            mint = c.get("coinMint")
            gd = c.get("graduationDate")
            if not pool or not gd:
                continue
            o = get(GT.format(pool))
            time.sleep(args.gt_sleep)
            cl = (((o or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") if o else None
            if not cl:
                miss += 1
                continue
            g = grid_from_ohlcv(cl, gd / 1000.0)
            if not g:
                miss += 1
                continue
            row = {
                "mint": mint, "pool": pool,
                "grad_ms": gd, "creation_ms": c.get("creationTime"),
                "grad_day": datetime.fromtimestamp(gd / 1000, tz=timezone.utc).strftime("%m-%d %H"),
                "grad_speed_h": (round((gd - c.get("creationTime")) / 3.6e6, 2) if c.get("creationTime") else None),
                # === FIELD SAFETY (lookahead policy) ===
                # [EX-ANTE SAFE] fixed at creation/launch, knowable at our entry time:
                #   grad_speed_h (grad-creation), sniperCount (# snipers in launch block),
                #   hasTwitter/hasWebsite/hasTelegram, twitterReuse, and the OHLCV-derived
                #   early-post-grad features in `g` (observable by grad+entry_delay).
                "sniperCount": c.get("sniperCount"),
                "hasTwitter": bool(c.get("hasTwitter")), "hasWebsite": bool(c.get("hasWebsite")),
                "hasTelegram": bool(c.get("hasTelegram")), "twitterReuse": c.get("twitterReuseCount"),
                # [CURRENT-STATE / LOOKAHEAD -> DO NOT USE AS FEATURE; stored for audit only]
                #   sniperOwnedPct (snipers' CURRENT holdings; they sell -> changes),
                #   numKols (CUMULATIVE KOLs traded to date), cur_mc (current mcap),
                #   ath_mc (the OUTCOME / label).  Forbidden as ex-ante features.
                "_la_sniperOwnedPct": c.get("sniperOwnedPercentage"),
                "_la_numKols": c.get("numKolsTraded"),
                "ath_mc": c.get("allTimeHighMarketCap"), "cur_mc": c.get("marketCap"),
                **g,
            }
            f.write(json.dumps(row) + "\n")
            n += 1
            if (i + 1) % 50 == 0:
                f.flush()
                print(f"  [gradA] {i+1}/{len(grads)} ok={n} miss={miss}", flush=True)
    print(f"[done] wrote {n} (miss={miss}) -> {OUT}")


if __name__ == "__main__":
    main()
