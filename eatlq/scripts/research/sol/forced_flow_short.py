#!/usr/bin/env python3
"""#5 FORCED-FLOW SHORT — crowded leveraged longs get liquidated -> cascade down.

Thesis: short an overheated alt that is ROLLING OVER, *conditioned on* crowded
leveraged longs (extreme positive funding longs are paying + OI stacked). A
liquidation cascade should follow; meanwhile the short COLLECTS the positive
funding. Real data: Coinglass 8h funding + OI + liquidation (24 alts, ~1y).

Protocol: TRAIN-only thresholds -> lock -> judge on untouched TEST holdout.
Anti-overfit crowding = extreme-quantile COUNT (no fitted weights).
Funding clipped (data has -200%/8h glitches). Short collects funding in hold.
"""
from __future__ import annotations
import json, glob, os, bisect, random, statistics as st

OKX = "/opt/EATLQ/scripts/research/sol/_cache_okx"
FUND = "/opt/EATLQ/scripts/research/sol/_cache_fund_cg"
OIC = "/opt/EATLQ/scripts/research/sol/_cache_oi_cg"
LIQ = "/opt/EATLQ/scripts/research/sol/_cache_liq_cg"

MIN_HOURS = 2000
TAKER = 0.0005; RT_COST = 2 * TAKER
RUNUP_MIN = 0.20; PULLBACK_MIN = 0.03
HARD_STOP = 0.15; TRAIL_ARM = 0.12; TRAIL_ATR = 1.5; TIME_BARS = 48
CLIP = float(os.environ.get("FUND_CLIP", "0.02"))
SPLIT = 0.6


def load_okx():
    data = {}
    for fn in glob.glob(f"{OKX}/*.json"):
        if "manifest" in fn:
            continue
        sym = os.path.basename(fn).split("_")[0]
        d = json.load(open(fn))
        if len(d) >= MIN_HOURS:
            data[sym] = d
    return data


def load_series(path):
    out = {}
    for fn in glob.glob(f"{path}/*.json"):
        sym = os.path.basename(fn)[:-5]
        out[sym] = json.load(open(fn))["rows"]
    return out


def ema(s, span):
    k = 2/(span+1); o=[s[0]]
    for x in s[1:]: o.append(o[-1]+k*(x-o[-1]))
    return o


def atr(series, n=14):
    tr=[0.0]
    for i in range(1,len(series)):
        h,l,pc=series[i][2],series[i][3],series[i-1][4]
        tr.append(max(h-l,abs(h-pc),abs(l-pc)))
    o=[tr[0]]*len(series); a=sum(tr[1:n+1])/n if len(tr)>n else tr[0]
    for i in range(len(series)):
        if i>=n: a=(a*(n-1)+tr[i])/n
        o[i]=a
    return o


def features(series):
    c=[r[4] for r in series]; h=[r[2] for r in series]
    feats=[]
    for t in range(len(series)):
        if t<60: feats.append(None); continue
        ret24=c[t]/c[t-24]-1
        hi24=max(h[t-24:t+1]); pullback=(hi24-c[t])/hi24
        feats.append({"t":t,"ret24":ret24,"pullback":pullback})
    return feats


def is_candidate(f):
    return f and f["ret24"]>=RUNUP_MIN and f["pullback"]>=PULLBACK_MIN


# --- 8h-series helpers (ts in seconds) ---
def fund_sum(rows, t0, t1):
    """clipped funding settled in (t0, t1]."""
    return sum(max(-CLIP,min(CLIP,r)) for ts,r in rows if t0<ts<=t1)


def val_at(rows, t, idxcol=1):
    """last value at or before t (rows sorted by ts)."""
    ts=[r[0] for r in rows]
    i=bisect.bisect_right(ts,t)-1
    return rows[i][idxcol] if i>=0 else None


def sim_short(series, at_series, entry_i, fund_rows):
    n=len(series); entry=series[entry_i][1]
    if entry<=0: return None
    armed=False; best_low=entry; end=min(entry_i+TIME_BARS,n)
    for j in range(entry_i,end):
        hi,lo,cl=series[j][2],series[j][3],series[j][4]
        adverse=(hi-entry)/entry
        if adverse>=HARD_STOP:
            ex_ts=series[j][0]
            fund=fund_sum(fund_rows,series[entry_i][0],ex_ts)  # short COLLECTS +funding
            return (-HARD_STOP-RT_COST+fund,adverse,1,j-entry_i+1)
        best_low=min(best_low,lo); fav=(entry-lo)/entry
        if not armed and fav>=TRAIL_ARM: armed=True
        if armed:
            trail=best_low+TRAIL_ATR*at_series[j]
            if hi>=trail:
                ex_ts=series[j][0]
                fund=fund_sum(fund_rows,series[entry_i][0],ex_ts)
                ret=(entry-trail)/entry-RT_COST+fund
                mae=max((series[k][2]-entry)/entry for k in range(entry_i,j+1))
                return (ret,mae,0,j-entry_i+1)
    ex=end-1; ex_ts=series[ex][0]
    fund=fund_sum(fund_rows,series[entry_i][0],ex_ts)
    ret=(entry-series[ex][4])/entry-RT_COST+fund
    mae=max((series[j][2]-entry)/entry for j in range(entry_i,ex+1))
    return (ret,mae,0,ex-entry_i+1)


def collect(okx, fund, oi, liq):
    sigs=[]; per={}
    cov={s:fund[s][0][0] for s in fund}
    for sym,series in okx.items():
        if sym not in fund: continue
        feats=features(series); at_series=atr(series)
        per[sym]=(series,feats,at_series)
        tmax,tmin=series[-1][0],series[0][0]; cut=tmin+SPLIT*(tmax-tmin)
        for f in feats:
            if not is_candidate(f): continue
            entry_i=f["t"]+1
            if entry_i>=len(series): continue
            t=series[f["t"]][0]
            if t<cov[sym]: continue                    # funding coverage
            # crowding features (only data <= t)
            cumf=fund_sum(fund[sym],t-48*3600,t)        # 48h funding longs paid
            fnow=val_at(fund[sym],t)
            oi_now=val_at(oi.get(sym,[]),t); oi_24=val_at(oi.get(sym,[]),t-24*3600)
            oiramp=(oi_now/oi_24-1) if (oi_now and oi_24) else 0.0
            seg="train" if t<=cut else "test"
            sigs.append({"sym":sym,"f":f,"entry_i":entry_i,"seg":seg,"t":t,
                         "cumf":cumf,"fnow":fnow or 0.0,"oiramp":oiramp})
    FK=["cumf","fnow","oiramp"]
    tr=[s for s in sigs if s["seg"]=="train"]
    thr={k:(sorted(s[k] for s in tr)[int(2/3*len(tr))] if tr else 0) for k in FK}
    out=[]; last_exit={}
    for s in sorted(sigs,key=lambda x:(x["sym"],x["entry_i"])):
        sym=s["sym"]
        if s["entry_i"]<=last_exit.get(sym,-1): continue
        series,feats,at_series=per[sym]
        r=sim_short(series,at_series,s["entry_i"],fund[sym])
        if not r: continue
        last_exit[sym]=s["entry_i"]+r[3]
        s["score"]=sum(1 for k in FK if s[k]>=thr[k]); s["sim"]=r
        # liquidation confirmation: long_liq during hold
        ex_ts=s["t"]+r[3]*3600
        s["longliq"]=sum(a for ts,a,b in liq.get(sym,[]) if s["t"]<ts<=ex_ts)
        out.append(s)
    return out, thr


def bin_of(sc): return "low(0-1)" if sc<=1 else "high(2-3)"


def stats(g):
    rets=[s["sim"][0] for s in g]; maes=sorted(s["sim"][1] for s in g)
    cat=sum(s["sim"][2] for s in g)/len(g); ev=sum(rets)/len(rets)
    win=sum(1 for r in rets if r>0)/len(rets)
    m95=maes[int(0.95*len(maes))-1] if maes else 0
    liq=sum(s["longliq"] for s in g)/len(g)
    return len(g),ev,win,cat,m95,liq


def report(sigs,name):
    print(f"\n--- {name}: short EV by crowding (n|EV|win%|catastrophe%|mae_p95|avg_longliq$) ---")
    b={}
    for s in sigs: b.setdefault(bin_of(s["score"]),[]).append(s)
    for k in ["low(0-1)","high(2-3)"]:
        g=b.get(k)
        if not g: print(f"  {k}: (none)"); continue
        n,ev,win,cat,m95,liq=stats(g)
        print(f"  {k}: n={n:4d} EV={100*ev:+6.2f}% win={100*win:3.0f}% "
              f"catastrophe={100*cat:5.1f}% mae_p95={100*m95:5.1f}% longliq=${liq/1e6:.2f}M")


def robust(sigs,name):
    rets=sorted((s["sim"][0] for s in sigs),reverse=True)
    if not rets: print(f"  [{name}] none"); return
    pos=sum(r for r in rets if r>0); neg=-sum(r for r in rets if r<0)
    pf=pos/neg if neg else 999; pf1=(pos-rets[0])/neg if neg else 999
    rng=random.Random(7); m=[]
    for _ in range(2000): m.append(sum(rets[rng.randrange(len(rets))] for _ in range(len(rets)))/len(rets))
    m.sort()
    print(f"  [{name}] n={len(rets)} EV={100*sum(rets)/len(rets):+.2f}% "
          f"PF={pf:.2f} PF_top1rm={pf1:.2f} boot_p5={100*m[int(0.05*len(m))]:+.2f}%")


def main():
    okx=load_okx(); fund=load_series(FUND); oi=load_series(OIC); liq=load_series(LIQ)
    print(f"#### #5 FORCED-FLOW SHORT (crowded longs -> cascade). clip={CLIP} ####")
    print(f"coins funded={len(fund)} | short collects funding | TRAIN thresholds locked, judge on TEST")
    sigs,thr=collect(okx,fund,oi,liq)
    print(f"covered signals={len(sigs)}  crowding thr(top1/3,train)={ {k:round(v,4) for k,v in thr.items()} }")
    allr=[s["sim"][0] for s in sigs]
    print(f"ALL short EV={100*sum(allr)/len(allr):+.2f}% catastrophe={100*sum(s['sim'][2] for s in sigs)/len(sigs):.1f}%")
    report(sigs,"ALL")
    tr=[s for s in sigs if s["seg"]=="train"]; te=[s for s in sigs if s["seg"]=="test"]
    report(tr,"TRAIN (in-sample threshold)")
    report(te,"TEST (OOS holdout)")
    print("\n  robustness on HIGH-crowding (score>=2):")
    robust([s for s in sigs if s["score"]>=2],"ALL high-crowd")
    robust([s for s in te if s["score"]>=2],"TEST high-crowd")
    # gates on TEST
    b={}
    for s in te: b.setdefault(bin_of(s["score"]),[]).append(s)
    seq=[k for k in ["low(0-1)","high(2-3)"] if b.get(k)]
    evs=[sum(x["sim"][0] for x in b[k])/len(b[k]) for k in seq]
    cats=[sum(x["sim"][2] for x in b[k])/len(b[k]) for k in seq]
    h1=len(evs)==2 and evs[1]>evs[0] and evs[1]>0
    h2=len(cats)==2 and cats[1]<cats[0]
    print(f"\n=== TEST GATES ===")
    print(f"  EV low->high = {[round(100*x,2) for x in evs]} -> H1(crowd raises short EV & >0) {'PASS' if h1 else 'FAIL'}")
    print(f"  catastrophe low->high = {[round(100*x,1) for x in cats]} -> H2(crowd lowers catastrophe) {'PASS' if h2 else 'FAIL'}")
    print(f"  >>> {'SURVIVES holdout' if (h1 and h2) else 'FALSIFIED on holdout'}")


if __name__=="__main__":
    main()
