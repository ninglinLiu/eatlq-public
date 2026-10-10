#!/usr/bin/env python3
"""B6 causal weather-gate falsification, entirely from frozen local evidence.

No network or RPC module is imported.  The optional universe snapshot is read
from local ClickHouse; subsequent runs use the immutable JSON snapshot.
"""
from __future__ import annotations

import csv
import os
import hashlib
import importlib.util
import json
import math
import random
import re
import subprocess
import itertools
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics"
SCRIPT = ROOT / "scripts/research/base_b6/b6_regime_gate.py"
PREREG = ROOT / "configs/research/base_b6_preregistration_20260714.json"
SNAPSHOT = Path(os.environ.get("B6_SNAPSHOT", DIAG / "base_b6_universe_snapshot_20260714.json"))
OUT = Path(os.environ.get("B6_OUT", DIAG / "base_b6_keystone_regime_gate_20260714.json"))
SVG = Path(os.environ.get("B6_SVG", DIAG / "base_b6_random_gate_distribution_20260714.svg"))
GOLDEN = ROOT / "tests/fixtures/base_b6_golden_20260714.json"
B4 = Path(os.environ.get("B6_B4", DIAG / "base_b4_arm_b_20260714.json"))
B5 = DIAG / "base_b5_keystone_cha_reverify_20260714.json"
ETH = DIAG / "base_2min_accel_b3_eth_daily_20260714.json"
SEED = 20260714


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iso_day(s: str) -> date:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()


def build_snapshot() -> dict:
    q = """SELECT lower(pool_address),pool_type,lower(target_token),created_block,
toUnixTimestamp64Milli(created_time),snapshot_120,window FROM eatlq.base_pool_universe_b3
ORDER BY created_block,pool_address FORMAT TabSeparated"""
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query", q],
                       text=True, capture_output=True, timeout=120, check=True)
    rows=[]
    for z in csv.reader(p.stdout.splitlines(), delimiter="\t"):
        pool,typ,tok,block,ms,snap,w=z
        rows.append({"pool":pool,"pool_type":typ,"token":tok,"created_block":int(block),
                     "created_ms":int(ms),"snapshot_120":int(snap),"window":w})
    if len(rows)!=3331:
        raise AssertionError(f"B3 universe drift: {len(rows)} != 3331")
    hq="""SELECT toUnixTimestamp64Milli(m.block_time) FROM eatlq.discovered_pools p FINAL
INNER JOIN eatlq.block_ts_map m FINAL ON p.chain_id=m.chain_id AND p.created_block=m.block_number
WHERE p.chain_id=8453 AND p.has_weth_leg=1
AND m.block_time>=toDateTime64('2026-01-23 00:00:00',3,'UTC')
AND m.block_time<toDateTime64('2026-06-09 00:00:00',3,'UTC') ORDER BY m.block_time FORMAT TSV"""
    hp=subprocess.run(["docker","exec","eatlq-clickhouse-1","clickhouse-client","--query",hq],
                      text=True,capture_output=True,timeout=120,check=True)
    history=[int(x) for x in hp.stdout.splitlines() if x]
    if not history: raise AssertionError("missing issuance history")
    payload={"schema_version":2,"research_only":True,
             "source":"local ClickHouse base_pool_universe_b3 plus discovered_pools/block_ts_map issuance history",
             "rpc_calls":0,"b3_rows":rows,"issuance_created_ms":history,
             "issuance_start":"2026-01-23T00:00:00Z"}
    SNAPSHOT.write_text(json.dumps(payload,separators=(",",":"),sort_keys=True)+"\n")
    return payload


def load_b5_module():
    p=ROOT/"scripts/research/base_b5/b5_offline_reverify.py"
    s=importlib.util.spec_from_file_location("b5_for_b6",p)
    m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


def summarize(vals):
    a=np.asarray(vals,dtype=float)
    if not len(a): return {"n":0,"mean":None,"top1_removed_mean":None,"win_rate":None}
    k=max(1,math.ceil(.01*len(a))); order=np.argsort(a)[::-1]; rem=np.delete(a,order[:k])
    return {"n":int(len(a)),"mean":float(a.mean()),"median":float(np.median(a)),
            "win_rate":float((a>0).mean()),"fatal_rate":float((a<=-.5).mean()),
            "top1_removed_mean":float(rem.mean()) if len(rem) else None}


def weather_by_day(snapshot, b4rows, closes, start, end):
    universe=snapshot["b3_rows"]
    created=sorted(datetime.fromtimestamp(x/1000,tz=timezone.utc) for x in snapshot["issuance_created_ms"])
    bypool={x["pool"]:x for x in universe}
    probes=[]
    for r in b4rows:
        u=bypool.get(r["pool_address"].lower())
        if not u: continue
        t=datetime.fromtimestamp(u["created_ms"]/1000,tz=timezone.utc)
        # Frozen proxy: missing/unknown is adverse; otherwise Quoter recovery <10%.
        proxy_bad=(r.get("status")!="measured" or r.get("qv2_amount_out") is None or
                   int(r["qv2_amount_out"]) < 10**14)
        probes.append((t,proxy_bad,bool(r.get("severe_entry_adverse")),r.get("status")=="measured"))
    out={}; d=start
    while d<=end:
        cut=datetime.combine(d,datetime.min.time(),tzinfo=timezone.utc)
        a=cut-timedelta(days=7); b=cut-timedelta(days=35)
        n7=sum(a<=x<cut for x in created); n28=sum(b<=x<a for x in created)
        issuance=(n7/7)/(n28/28) if n28 else None
        hp=[x for x in probes if a<=x[0] and x[0]+timedelta(minutes=2)<cut]
        hp_n=len(hp); hp_rate=sum(x[1] for x in hp)/hp_n if hp_n else None
        d1=d-timedelta(days=1); d8=d-timedelta(days=8)
        trend=(closes.get(str(d1))/closes.get(str(d8))-1
               if closes.get(str(d1)) is not None and closes.get(str(d8)) is not None else None)
        history=(cut-created[0]).total_seconds()>=35*86400
        eligible=history and issuance is not None and hp_n>=20 and trend is not None
        opened=eligible and issuance<=1.25 and hp_rate<=.20 and trend>=-.05
        out[str(d)]={"issuance_ratio":issuance,"honeypot_proxy_n":hp_n,
                     "honeypot_proxy_rate":hp_rate,"eth_trailing_7d_return":trend,
                     "eligible":eligible,"open":opened}
        d+=timedelta(days=1)
    # Probe-level association, not a favorable fill: all missing proxy rows are bad.
    measured=[x for x in probes if x[3]]
    tp=sum(p and t for _,p,t,_ in measured); fp=sum(p and not t for _,p,t,_ in measured)
    fn=sum((not p) and t for _,p,t,_ in measured); tn=sum((not p) and (not t) for _,p,t,_ in measured)
    den=math.sqrt((tp+fp)*(tp+fn)*(tn+fp)*(tn+fn))
    assoc={"n":len(measured),"unknown_excluded_from_association":len(probes)-len(measured),
           "unknown_policy_for_live_gate":"adverse proxy only; not treated as validation truth",
           "confusion_matrix":{"tp":tp,"fp":fp,"fn":fn,"tn":tn},
           "precision":tp/(tp+fp) if tp+fp else None,"recall":tp/(tp+fn) if tp+fn else None,
           "phi":(tp*tn-fp*fn)/den if den else None,
           "proxy":"QuoterV2 recovery <10%; unknown counts adverse",
           "truth":"B4 severe_entry_adverse from AtomicRoundTrip"}
    return out,assoc


def mc(vals):
    if len(vals)<20: return {"status":"NOT_RUN_UNPOWERED","n":len(vals)}
    rng=random.Random(SEED); terminal=[]; draw=[]
    for _ in range(10000):
        eq=peak=100.; md=0.
        for __ in vals:
            eq+=rng.choice(vals); peak=max(peak,eq); md=max(md,peak-eq)
        terminal.append(eq); draw.append(md)
    return {"status":"RUN","paths":10000,"seed":SEED,
            "terminal_p5":float(np.quantile(terminal,.05,method="lower")),
            "max_drawdown_p95":float(np.quantile(draw,.95,method="higher"))}


def svg_hist(vals,true_mean,p95):
    w,h=900,480; left,right,top,bottom=70,25,35,65
    if vals:
        counts,edges=np.histogram(vals,bins=40); ymax=max(counts); lo,hi=float(edges[0]),float(edges[-1])
    else: counts=np.array([0]); edges=np.array([0.,1.]); ymax=1; lo,hi=0.,1.
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
           '<rect width="100%" height="100%" fill="white"/>',
           '<text x="450" y="22" text-anchor="middle" font-family="sans-serif" font-size="16">B6 same-open-share random gate distribution</text>']
    if not vals:
        parts.append('<text x="450" y="230" text-anchor="middle" font-family="sans-serif" font-size="18" fill="#991b1b">0 true open days: all 10,000 matched draws have no return; no finite distribution</text>')
    pw=w-left-right; ph=h-top-bottom
    for i,c in enumerate(counts):
        x=left+i*pw/len(counts); bw=pw/len(counts)-1; bh=ph*c/ymax
        parts.append(f'<rect x="{x:.2f}" y="{top+ph-bh:.2f}" width="{bw:.2f}" height="{bh:.2f}" fill="#7aa6d8"/>')
    def xpos(v): return left+(v-lo)/(hi-lo)*pw if hi>lo else left
    if p95 is not None: parts.append(f'<line x1="{xpos(p95):.2f}" x2="{xpos(p95):.2f}" y1="{top}" y2="{top+ph}" stroke="#d97706" stroke-width="3"/><text x="{xpos(p95):.2f}" y="{top+15}" font-size="12" fill="#92400e"> random p95</text>')
    if true_mean is not None: parts.append(f'<line x1="{xpos(true_mean):.2f}" x2="{xpos(true_mean):.2f}" y1="{top}" y2="{top+ph}" stroke="#b91c1c" stroke-width="3"/><text x="{xpos(true_mean):.2f}" y="{top+32}" font-size="12" fill="#991b1b"> true gate</text>')
    parts += [f'<line x1="{left}" x2="{left+pw}" y1="{top+ph}" y2="{top+ph}" stroke="black"/>',
              f'<text x="450" y="455" text-anchor="middle" font-family="sans-serif">mean net return per selected opportunity</text>',
              '</svg>']
    SVG.write_text("\n".join(parts)+"\n")


def main():
    prereg=json.loads(PREREG.read_text())
    snapshot=json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else build_snapshot()
    if snapshot.get("schema_version")!=2:
        snapshot=build_snapshot()
    b4=json.loads(B4.read_text())
    closes={k:float(v) for k,v in json.loads(ETH.read_text())["closes"].items()}
    start=iso_day("2026-02-27T05:41:15Z"); end=iso_day("2026-06-08T18:29:40Z")
    weather,assoc=weather_by_day(snapshot,b4["rows"],closes,start,end)

    b5=load_b5_module(); df,path,_=b5.load_base(); df["regime"]=df.t0.map(b5.regime)
    X,y=b5.features(df); p,cut,train=b5.fit_forward(df,X,y,list(X.columns))
    df["selected"]=(p<=cut)&df.fric.notna()&(df.fric>=-.05)&df.regime.notna()
    # Stream the 647k-row TSV one pool at a time.  B5's all-in-memory loader is
    # convenient but unnecessary here and can exceed a constrained audit host.
    scoped={r.pool:r for r in df[df.regime.notna()].itertuples(index=False)}; sims={}
    def parsed(z):
        pool,block,ms,tx,li,a0,a1,tick=z
        return {"block":int(block),"ms":int(ms),"tx":tx,"log_index":int(li),
                "a0":int(a0),"a1":int(a1),"tick":int(tick)}
    with open(b5.SWAPS) as fh:
        reader=csv.reader(fh,delimiter="\t")
        for pool,grp in itertools.groupby(reader,key=lambda z:z[0]):
            r=scoped.get(pool)
            if r is None: continue
            ps=[parsed(z) for z in grp]
            eth=float(closes.get(str(r.t0.date())) or min(closes.values()))
            sims[pool]=b5.simulate(r,ps,path,eth)
    for pool,r in scoped.items():
        if pool not in sims:
            eth=float(closes.get(str(r.t0.date())) or min(closes.values()))
            sims[pool]=b5.simulate(r,[],path,eth)
    rows=[]
    for r in df[df.regime.notna()].itertuples(index=False):
        rows.append({"pool":r.pool,"signal_time":r.t0.isoformat(),"day":str(r.t0.date()),
                     "regime":r.regime,"selected":bool(r.selected),"ret":float(sims[r.pool]["ret"]),
                     "reason":sims[r.pool]["reason"]})
    selected=[r for r in rows if r["selected"]]
    gated=[r for r in selected if weather.get(r["day"],{}).get("open")]
    eligible_days=sorted(d for d,x in weather.items() if x["eligible"])
    open_days=sorted(d for d,x in weather.items() if x["open"])
    rng=random.Random(SEED); random_means=[]; random_ns=[]; random_sets=[]
    if not open_days:
        # Exact zero-open-share comparator: every admissible random gate is also
        # closed.  Preserve all 10k draws without doing redundant row scans.
        random_means=[-math.inf]*prereg["random_gate"]["draws"]
        random_ns=[0]*prereg["random_gate"]["draws"]
        random_sets=[set()]*prereg["random_gate"]["draws"]
    else:
      for _ in range(prereg["random_gate"]["draws"]):
        ds=set(rng.sample(eligible_days,len(open_days)))
        vv=[r["ret"] for r in selected if r["day"] in ds]
        random_means.append(float(np.mean(vv)) if vv else -math.inf); random_ns.append(len(vv))
        random_sets.append(ds)
    finite=[x for x in random_means if math.isfinite(x)]
    rp95=float(np.quantile(finite,.95,method="higher")) if finite else None

    def vals(which,lo,hi,draw=None):
        rr=[r for r in rows if lo<=pd.Timestamp(r["signal_time"])<hi]
        if which=="unconditional": return [r["ret"] for r in rr]
        rr=[r for r in rr if r["selected"]]
        if which=="always_on": return [r["ret"] for r in rr]
        days=set(open_days) if which=="true_gate" else random_sets[draw]
        return [r["ret"] for r in rr if r["day"] in days]

    anchor=pd.Timestamp(prereg["chronology"]["rolling_anchor"]); final=pd.Timestamp("2026-06-08T18:29:40Z")
    rolling=[]; i=0; lo=anchor
    while lo<final:
        hi=min(lo+pd.Timedelta(days=14),final)
        tabs={k:summarize(vals(k,lo,hi)) for k in ("unconditional","always_on","true_gate")}
        rv=[] if not open_days else [summarize(vals("random_gate",lo,hi,j))["mean"] for j in range(len(random_sets))]
        rv=[x for x in rv if x is not None]
        tabs["random_gate"]={"draws_with_opportunity":len(rv),"mean_of_means":float(np.mean(rv)) if rv else None,
                             "p95_mean":float(np.quantile(rv,.95,method="higher")) if rv else None}
        base=tabs["unconditional"]["mean"]
        tabs["true_gate"]["alpha_vs_unconditional"]=(tabs["true_gate"]["mean"]-base
            if tabs["true_gate"]["mean"] is not None and base is not None else None)
        rolling.append({"id":f"R{i:02d}","start":lo.isoformat(),"end_exclusive":hi.isoformat(),**tabs})
        lo+=pd.Timedelta(days=7); i+=1

    overall={k:summarize(vals(k,anchor,final)) for k in ("unconditional","always_on","true_gate")}
    regimes=[]
    for wid,(a,b,desc) in b5.REGIMES.items():
        lo=pd.Timestamp(a,tz="UTC"); hi=pd.Timestamp(b,tz="UTC")
        z={k:summarize(vals(k,lo,hi)) for k in ("unconditional","always_on","true_gate")}
        base=z["unconditional"]["mean"]
        z["true_gate"]["alpha_vs_unconditional"]=(z["true_gate"]["mean"]-base
            if z["true_gate"]["mean"] is not None and base is not None else None)
        regimes.append({"regime_id":wid,"description":desc,
                        "used_by_gate":False,"w3_partial" : wid=="W3",**z})
    cal_end=pd.Timestamp(re.search(r"\d{4}-\d\d-\d\dT[^ ]+Z",prereg["chronology"]["held_out_oos"]).group(0))
    segments={"calibration":{k:summarize(vals(k,anchor,cal_end)) for k in ("unconditional","always_on","true_gate")},
              "held_out_oos":{k:summarize(vals(k,cal_end,final)) for k in ("unconditional","always_on","true_gate")}}
    gt=[r["ret"] for r in gated]; mcr=mc(gt)
    powered=[r for r in rolling if r["true_gate"]["n"]>=20]
    powered_regimes=[r for r in regimes if r["true_gate"]["n"]>=20]
    gates={
      "pooled_mean_gt_0":overall["true_gate"]["mean"] is not None and overall["true_gate"]["mean"]>0,
      "top1_removed_mean_gt_0":overall["true_gate"]["top1_removed_mean"] is not None and overall["true_gate"]["top1_removed_mean"]>0,
      "every_powered_rolling_window_mean_ge_minus_10pct":bool(powered) and all(r["true_gate"]["mean"]>=-.10 for r in powered),
      "at_least_2_powered_regimes_positive_mean_and_alpha":sum(r["true_gate"]["mean"]>0 and r["true_gate"]["alpha_vs_unconditional"]>0 for r in powered_regimes)>=2,
      "true_mean_gt_random_p95":overall["true_gate"]["mean"] is not None and rp95 is not None and overall["true_gate"]["mean"]>rp95,
      "mc_terminal_p5_gt_100R":mcr.get("terminal_p5",-math.inf)>100,
      "mc_mdd_p95_le_20R":mcr.get("max_drawdown_p95",math.inf)<=20,
      "heldout_oos_mean_gt_0":segments["held_out_oos"]["true_gate"]["mean"] is not None and segments["held_out_oos"]["true_gate"]["mean"]>0}
    days=Counter(r["day"] for r in gated)
    capacity={"selected_opportunities":len(gated),"open_calendar_days":len(open_days),
              "open_days_with_opportunity":len(days),"opportunities_per_open_day":len(gated)/len(open_days) if open_days else 0,
              "total_notional_weth":.001*len(gated),"maximum_one_day_notional_weth":.001*max(days.values(),default=0),
              "opportunities_by_open_day":dict(sorted(days.items()))}
    source_paths=[SCRIPT,PREREG,SNAPSHOT,B4,B5,ETH,ROOT/"mvp/diagnostics/base_b5_real_swaps_20260714.tsv",
                  ROOT/"scripts/research/base_lefttail/_pools.tsv",ROOT/"scripts/research/base_lefttail/_fwdpath.tsv",
                  ROOT/"scripts/research/base_lefttail/_priced_live.json",GOLDEN,
                  ROOT/"tests/test_base_b6_regime_gate.py"]
    result={"schema_version":1,"research_only":True,"rpc_calls":0,"go_decision":"NOT_MADE",
      "causality":{"weather_timestamp":"UTC day start strictly before every signal on that day",
                    "gate_uses_W_labels":False,"weather_inputs":"only observations before signal"},
      "frozen_preregistration_sha256":sha(PREREG),"weather_by_day":weather,"proxy_validation":assoc,
      "performance":{"overall":overall,"segments":segments,"regime_acceptance_strata_not_gate_inputs":regimes,
                     "rolling_14d_step_7d":rolling},
      "random_gate":{"seed":SEED,"draws":len(random_means),"eligible_days":len(eligible_days),
                     "true_open_days":len(open_days),"finite_draws":len(finite),
                     "mean_quantiles":({str(q):float(np.quantile(finite,q,method="higher")) for q in (0,.05,.5,.95,1)} if finite else {}),
                     "p95":rp95,"true_gate_mean":overall["true_gate"]["mean"]},
      "capacity":capacity,"monte_carlo":mcr,
      "data_coverage":{"requested_start":"2026-02-27T05:41:15Z","requested_end":"2026-06-08T18:29:40Z",
                       "b5_max_signal_time":max(r["signal_time"] for r in rows),"w3_partial":True},
      "preregistered_right_tail":{"powered_n_min":20,"powered_rolling_windows":len(powered),
           "powered_regimes":len(powered_regimes),"gates":gates,
           "evidence_status":"PASS" if all(gates.values()) else "FAIL","go_decision":"NOT_MADE"},
      "golden":[{"pool":r["pool"],"signal_time":r["signal_time"],"ret":r["ret"],
                  "gate_open":weather[r["day"]]["open"],"weather":weather[r["day"]]} for r in selected[:5]],
      "rpc_ledger":{"logical_calls":0,"http_attempts":0,"note":"offline B3/B4/B5 evidence only"},
      "limitations":["KEYSTONE source coverage ends 2026-06-01 22:23 UTC; W3 is partial and is not extrapolated",
                     "random-gate zero-open case has no finite return distribution; comparison fails conservatively",
                     "actual historical third-party swaps are counterfactual fill evidence, not proof of our own fill"],
      "source_hashes":{str(p.relative_to(ROOT)):sha(p) for p in source_paths}}
    OUT.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    svg_hist(finite,overall["true_gate"]["mean"],rp95)
    print(json.dumps({"out":str(OUT),"svg":str(SVG),"gate":overall["true_gate"],
                      "open_days":len(open_days),"gates":gates},indent=2))


if __name__=="__main__": main()
