#!/usr/bin/env python3
"""Stage 2 (ZERO RPC): fetch each deployer's FULL create history from the free
pump API (coins?creator=X). Cached + resumable. Used to test deployer LIFETIME
reputation as a creation-block-knowable fingerprint, strictly causally.
"""
import json, os, sys, time, urllib.request

LAB = "/opt/EATLQ/mvp/diagnostics/sol/creates_labeled.jsonl"
SHARD = int(sys.argv[1]) if len(sys.argv) > 1 else 0
OF = int(sys.argv[2]) if len(sys.argv) > 2 else 1
SLEEP = float(sys.argv[3]) if len(sys.argv) > 3 else 0.12
OUT = f"/opt/EATLQ/mvp/diagnostics/sol/deployer_history_s{SHARD}of{OF}.jsonl" if OF > 1 else "/opt/EATLQ/mvp/diagnostics/sol/deployer_history.jsonl"
API = "https://frontend-api-v3.pump.fun/coins?creator={c}&offset={o}&limit=50"


def get(url, tries=4):
    for k in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}), timeout=20).read())
        except Exception as e:
            if k == tries - 1:
                return {"__err__": str(e)[:100]}
            time.sleep(1.0 + k)


def fetch_creator(c, cap=600):
    coins, off = [], 0
    while off < cap:
        r = get(API.format(c=c, o=off))
        if not isinstance(r, list) or not r:
            break
        for x in r:
            coins.append({"mint": x.get("mint"), "ct": x.get("created_timestamp"),
                          "ath": x.get("ath_market_cap"), "ath_ts": x.get("ath_market_cap_timestamp"),
                          "complete": x.get("complete")})
        off += 50
        time.sleep(SLEEP)
        if len(r) < 50:
            break
    return coins


def main():
    creators = []
    seen = set()
    for l in open(LAB):
        c = json.loads(l)["creator"]
        if c not in seen:
            seen.add(c); creators.append(c)
    creators = [c for i, c in enumerate(creators) if i % OF == SHARD]
    done = set()
    if os.path.exists(OUT):
        for l in open(OUT):
            try:
                done.add(json.loads(l)["creator"])
            except Exception:
                pass
    todo = [c for c in creators if c not in done]
    print(f"creators total={len(creators)} done={len(done)} todo={len(todo)}", flush=True)
    f = open(OUT, "a")
    for i, c in enumerate(todo):
        coins = fetch_creator(c)
        f.write(json.dumps({"creator": c, "n": len(coins), "coins": coins}) + "\n")
        f.flush()
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(todo)} fetched", flush=True)
    f.close()
    print("done", flush=True)


if __name__ == "__main__":
    main()
