"""Check 2 (read-only): apply blacklist/mintable filter to the 131-trade base set.

Filter rule mirrors the live gate semantics I enabled: DROP a trade only when
GoPlus POSITIVELY flags is_blacklisted or is_mintable. Missing data is NOT a drop
(live config = canary_on_missing, still trades, just small) -> kept here.

Compares OFF vs ON: trade count, fatal(<=-50%) rate, $1000-cap monthly, MDD, and
whether dropped trades are disproportionately fatal (good cut) vs indiscriminate.

CAVEAT: GoPlus is current-state, not block-state at the ~10min entry moment.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import risk_rebacktest as R  # noqa: E402

CHAIN = 8453


def goplus_flags(token):
    qs = urllib.parse.urlencode({"contract_addresses": token})
    url = f"https://api.gopluslabs.io/api/v1/token_security/{CHAIN}?{qs}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "EATLQ-probe/1.0"})
            with urllib.request.urlopen(req, timeout=6.0) as r:
                d = json.loads(r.read(512 * 1024).decode("utf-8"))
        except Exception as e:
            return {"state": "error:" + type(e).__name__}
        if "too many" in str(d.get("message", "")).lower():
            time.sleep(4.0 * (attempt + 1))
            continue
        res = d.get("result") or {}
        info = res.get(token.lower()) or (next(iter(res.values())) if res else {})
        if not info:
            return {"state": "no_data"}
        return {
            "state": "data",
            "blacklist": str(info.get("is_blacklisted")) in ("1", "true", "True"),
            "mintable": str(info.get("is_mintable")) in ("1", "true", "True"),
            "bl_judg": info.get("is_blacklisted") not in (None, ""),
            "mint_judg": info.get("is_mintable") not in (None, ""),
        }
    return {"state": "rate_limited"}


def metrics(trades):
    sim = R.simulate(trades, start=10000, cap_usd=1000, frac=0.20)
    rets = [t.ret for t in trades]
    fatal = sum(1 for r in rets if r <= -0.5)
    return {
        "n": len(trades),
        "fatal": fatal,
        "fatal_rate": fatal / len(trades) if trades else 0.0,
        "mean_ret": sum(rets) / len(rets) if rets else 0.0,
        "monthly": sim["monthly_return"],
        "mdd": sim["max_drawdown"],
        "end": sim["end"],
    }


def main():
    _, test, dynamic, risk, _ = R.load_inputs()
    base = R.build_trades(test, dynamic, risk, "none", cap_usd=1000, start_equity=10000, frac=0.20, haircut=0.02)
    print(f"probing GoPlus for {len(base)} base-trade tokens (clean backoff)...", flush=True)

    flags = {}
    states = {}
    for i, t in enumerate(base, 1):
        if t.token not in flags:
            flags[t.token] = goplus_flags(t.token)
            time.sleep(2.5)
        st = flags[t.token].get("state")
        states[st] = states.get(st, 0) + 1
        if i % 20 == 0:
            print(f"  {i}/{len(base)} states={states}", flush=True)

    kept, dropped = [], []
    for t in base:
        f = flags[t.token]
        if f.get("state") == "data" and (f.get("blacklist") or f.get("mintable")):
            dropped.append(t)
        else:
            kept.append(t)

    judg = sum(1 for t in base if flags[t.token].get("bl_judg"))
    print(f"\n=== Check 2: blacklist/mintable filter on base set ===")
    print(f"token states (unique tokens): {states}")
    print(f"blacklist/mintable judgable trades: {judg}/{len(base)} ({judg/len(base)*100:.1f}%)")
    off = metrics(base)
    on = metrics(kept)
    print(f"\nOFF (no filter): n={off['n']} fatal={off['fatal']}({off['fatal_rate']*100:.1f}%) "
          f"mean={off['mean_ret']:+.3f} monthly={off['monthly']*100:+.1f}% mdd={off['mdd']*100:.1f}% end=${off['end']:,.0f}")
    print(f"ON  (filtered) : n={on['n']} fatal={on['fatal']}({on['fatal_rate']*100:.1f}%) "
          f"mean={on['mean_ret']:+.3f} monthly={on['monthly']*100:+.1f}% mdd={on['mdd']*100:.1f}% end=${on['end']:,.0f}")
    print(f"\ndropped n={len(dropped)}")
    if dropped:
        df_fatal = sum(1 for t in dropped if t.ret <= -0.5)
        print(f"  dropped fatal(<=-50%)={df_fatal}/{len(dropped)} ({df_fatal/len(dropped)*100:.1f}%)")
        for t in dropped:
            tag = "FATAL" if t.ret <= -0.5 else "ok"
            print(f"   {t.token} ret={t.ret:+.3f} [{tag}] {flags[t.token]}")
    else:
        print("  -> filter removed ZERO trades on this sample (no positive blacklist/mintable among judgable tokens)")


if __name__ == "__main__":
    main()
