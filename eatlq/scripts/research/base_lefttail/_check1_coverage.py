"""Check 1 (read-only): API coverage probe over cached external-risk scans.

Population = tokens in _external_risk_current.json (holdout-scope historical
signals). Tally GoPlus + honeypot.is: data / no-data / timeout, and the share
where blacklist/mintable can be positively judged.

CAVEAT: cache is CURRENT-state scanner data, not block-state at the ~10min entry
moment. Coverage now is a proxy; for dead/aged tokens it may understate the
coverage that existed live at entry time.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
c = json.load(open(os.path.join(HERE, "_external_risk_current.json")))
n = len(c)

gp_data = gp_none = hp_data = hp_none = 0
timeout = err_other = 0
bl_judg = mint_judg = 0
both_data = 0
positive_bl = positive_mint = positive_hp = 0


def has_val(v):
    return v is not None and str(v).strip() != ""


for tok, rec in c.items():
    raw = rec.get("raw", {}) or {}
    errs = rec.get("errors", []) or []
    if any("timeout" in e.lower() or "timed out" in e.lower() for e in errs):
        timeout += 1
    if errs:
        err_other += 1

    gp = raw.get("goplus")
    info = {}
    if isinstance(gp, dict):
        res = gp.get("result") or {}
        info = res.get(tok.lower()) or (next(iter(res.values())) if res else {}) or {}
    if isinstance(info, dict) and info:
        gp_data += 1
        if has_val(info.get("is_blacklisted")):
            bl_judg += 1
            if str(info.get("is_blacklisted")).strip() in ("1", "true", "True"):
                positive_bl += 1
        if has_val(info.get("is_mintable")):
            mint_judg += 1
            if str(info.get("is_mintable")).strip() in ("1", "true", "True"):
                positive_mint += 1
    else:
        gp_none += 1

    hp = raw.get("honeypot")
    hp_ok = False
    if isinstance(hp, dict):
        sim = hp.get("simulationResult") or {}
        hpr = hp.get("honeypotResult") or {}
        if hp.get("simulationSuccess") or sim or hpr:
            hp_ok = True
        if str((hpr.get("isHoneypot") if isinstance(hpr, dict) else None)) in ("1", "true", "True"):
            positive_hp += 1
    if hp_ok:
        hp_data += 1
    else:
        hp_none += 1

    if isinstance(info, dict) and info and hp_ok:
        both_data += 1


def pct(x):
    return f"{x} ({x/n*100:.1f}%)"


print(f"=== Check 1: API coverage probe  n={n} (scope=holdout cached tokens) ===")
print(f"GoPlus  : data={pct(gp_data)}  no_data={pct(gp_none)}")
print(f"Honeypot: data={pct(hp_data)}  no_data={pct(hp_none)}")
print(f"both providers have data: {pct(both_data)}")
print(f"timeout(any provider)={pct(timeout)}  any_error={pct(err_other)}")
print()
print(f"blacklist judgable (is_blacklisted present): {pct(bl_judg)}")
print(f"mintable  judgable (is_mintable  present): {pct(mint_judg)}")
print(f"  -> positively FLAGGED blacklist: {pct(positive_bl)}")
print(f"  -> positively FLAGGED mintable : {pct(positive_mint)}")
print(f"  -> positively FLAGGED honeypot : {pct(positive_hp)}")
