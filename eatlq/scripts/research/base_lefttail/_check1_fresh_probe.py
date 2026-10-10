"""Check 1 (read-only, FRESH): rate-limit-respecting coverage probe.

The cached _external_risk_current.json GoPlus column is corrupted by HTTP 429
("too many requests") because it was fetched at 0.15s spacing. This re-probes a
sample of N tokens live with proper backoff, classifying each provider as:
  data / no_data / rate_limited / timeout-or-error.

Read-only: only outbound GET to GoPlus + honeypot.is. No writes to system state.
CAVEAT: still CURRENT-state, not block-state at the ~10min entry moment.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CHAIN = 8453
N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
GP_SLEEP = 2.5
GP_RETRIES = 3


def get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "EATLQ-probe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(512 * 1024).decode("utf-8"))


def goplus(token):
    qs = urllib.parse.urlencode({"contract_addresses": token})
    url = f"https://api.gopluslabs.io/api/v1/token_security/{CHAIN}?{qs}"
    for attempt in range(GP_RETRIES):
        try:
            d = get(url, 6.0)
        except Exception as e:
            return ("error", type(e).__name__)
        msg = str(d.get("message", "")).lower()
        if "too many" in msg or str(d.get("code")) in ("4029", "429"):
            time.sleep(4.0 * (attempt + 1))
            continue
        res = d.get("result") or {}
        info = res.get(token.lower()) or (next(iter(res.values())) if res else {})
        if info:
            return ("data", info)
        return ("no_data", None)
    return ("rate_limited", None)


def honeypot(token):
    qs = urllib.parse.urlencode({"address": token, "chainID": str(CHAIN)})
    url = f"https://api.honeypot.is/v2/IsHoneypot?{qs}"
    try:
        d = get(url, 6.0)
    except Exception as e:
        return ("error", type(e).__name__)
    sim = d.get("simulationResult") or {}
    hpr = d.get("honeypotResult") or {}
    if d.get("simulationSuccess") or sim or hpr:
        return ("data", d)
    return ("no_data", None)


def main():
    cache = json.load(open(os.path.join(HERE, "_external_risk_current.json")))
    tokens = sorted(cache.keys())[:N]
    g = {"data": 0, "no_data": 0, "rate_limited": 0, "error": 0}
    h = {"data": 0, "no_data": 0, "error": 0}
    bl_judg = mint_judg = pos_bl = pos_mint = pos_hp = both = 0
    for i, t in enumerate(tokens, 1):
        gs, gi = goplus(t)
        g[gs] = g.get(gs, 0) + 1
        if gs == "data":
            if gi.get("is_blacklisted") not in (None, ""):
                bl_judg += 1
                if str(gi.get("is_blacklisted")) in ("1", "true", "True"):
                    pos_bl += 1
            if gi.get("is_mintable") not in (None, ""):
                mint_judg += 1
                if str(gi.get("is_mintable")) in ("1", "true", "True"):
                    pos_mint += 1
        hs, hd = honeypot(t)
        h[hs] = h.get(hs, 0) + 1
        if hs == "data":
            hpr = hd.get("honeypotResult") or {}
            if str(hpr.get("isHoneypot")) in ("1", "true", "True"):
                pos_hp += 1
        if gs == "data" and hs == "data":
            both += 1
        if i % 10 == 0:
            print(f"  probed {i}/{len(tokens)} goplus={g} hp={h}", flush=True)
        time.sleep(GP_SLEEP)

    n = len(tokens)
    def p(x):
        return f"{x} ({x/n*100:.1f}%)"
    print(f"\n=== Check 1 FRESH coverage  n={n} ===")
    print(f"GoPlus  : data={p(g['data'])} no_data={p(g['no_data'])} rate_limited={p(g['rate_limited'])} error={p(g['error'])}")
    print(f"Honeypot: data={p(h['data'])} no_data={p(h['no_data'])} error={p(h['error'])}")
    print(f"both providers data: {p(both)}")
    print(f"blacklist judgable: {p(bl_judg)}  mintable judgable: {p(mint_judg)}")
    print(f"positive blacklist: {p(pos_bl)}  positive mintable: {p(pos_mint)}  positive honeypot: {p(pos_hp)}")


if __name__ == "__main__":
    main()
