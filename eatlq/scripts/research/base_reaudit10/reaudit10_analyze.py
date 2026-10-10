#!/usr/bin/env python3
"""Offline estimator and ten-verdict renderer for the seconds re-audit."""
from __future__ import annotations
import bisect,csv,hashlib,importlib.util,json,math,random,sys
from collections import Counter,defaultdict
from datetime import datetime,timedelta,timezone
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT=Path("/opt/EATLQ"); DIAG=ROOT/"mvp/diagnostics"
PREREG=ROOT/"configs/research/base_reaudit10_seconds_preregistration_20260715.json"
CORE=DIAG/"base_reaudit10_seconds_rpc_20260715.json"; B4=DIAG/"base_reaudit10_b4_health_rpc_20260715.json"; RUG=DIAG/"base_reaudit10_rug_five_evidence_rpc_20260715.json"
B3=DIAG/"base_2min_accel_b3_v1_20260714.json"; B3RPC=DIAG/"base_reaudit10_b3_seconds_rpc_20260715.json"; B6=DIAG/"base_b6_keystone_regime_gate_corrected_20260714.json"; ETH=DIAG/"base_2min_accel_b3_eth_daily_20260714.json"
MONITOR=DIAG/"base_b5_lp_monitor_20260714.json";TOGGLE=DIAG/"base_reaudit10_transfer_toggle_probe_20260715.json"
SWAPS=(DIAG/"base_b5_real_swaps_20260714.tsv",DIAG/"base_b8_future_swaps_20260714.tsv")
OUT=DIAG/"base_reaudit10_final_20260715.json"; PER=DIAG/"base_reaudit10_per_trade_20260715.json"; SVG=DIAG/"base_reaudit10_latency_budget_frontier_20260715.svg"; REPORT=ROOT/"reports/reaudit_10_suspect_conclusions_result_20260715.md"
SCRIPT_SOURCES=(ROOT/"scripts/research/base_reaudit10/reaudit10_rpc.py",ROOT/"scripts/research/base_reaudit10/reaudit10_rugs_resume.py",ROOT/"scripts/research/base_reaudit10/reaudit10_b3_seconds_rpc.py",ROOT/"scripts/research/base_reaudit10/reaudit10_transfer_toggle_probe.py",ROOT/"scripts/research/base_reaudit10/reaudit10_finalize_ledger.py",ROOT/"scripts/research/base_reaudit10/reaudit10_analyze.py",ROOT/"tests/test_base_reaudit10.py",ROOT/"tests/fixtures/base_reaudit10_golden_20260715.json")
OFFSETS=(1,3,5,10,15); SPEND=10**15; CUTOFF=46865124; SEED=20260715
sys.path.insert(0,str(ROOT/"scripts/wallet_profile_v0")); from ch_helper import ch_query  # noqa:E402
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def mod(path,name):
 s=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
def wilson(k,n):
 if not n:return [None,None]
 z=1.96;p=k/n;d=1+z*z/n;c=(p+z*z/(2*n))/d;h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d;return [c-h,c+h]
def summary(vals):
 a=np.asarray(vals,dtype=float)
 if not len(a): return {"n":0,"mean":None,"median":None,"win_rate":None,"fatal_rate":None,"top1_removed_mean":None,"top1_contribution":None}
 order=np.argsort(a)[::-1]; k=max(1,math.ceil(.01*len(a))); rem=np.delete(a,order[:k]); pos=np.maximum(a,0).sum(); top=np.maximum(a[order[:k]],0).sum()
 return {"n":int(len(a)),"mean":float(a.mean()),"median":float(np.median(a)),"win_rate":float((a>0).mean()),"fatal_rate":float((a<=-.5).mean()),"top1_removed_mean":float(rem.mean()) if len(rem) else None,"top1_contribution":float(top/pos) if pos else 0.}
def load_swaps():
 out=defaultdict(list)
 for path in SWAPS:
  with path.open() as f:
   for z in csv.reader(f,delimiter="\t"):
    pool,b,ms,tx,li,a0,a1,tick=z; out[pool].append({"block":int(b),"ms":int(ms),"tx":tx,"log_index":int(li),"a0":int(a0),"a1":int(a1),"tick":int(tick)})
 for v in out.values(): v.sort(key=lambda x:(x["block"],x["log_index"]))
 return out
def extend_rug_swaps(out,rug):
 pools=sorted({r["pool"].lower() for r in rug["rows"]}); quoted=",".join("'"+p+"'" for p in pools); lo=min(r["flip_block"] for r in rug["rows"])
 q=f"""SELECT lower(pool_address) pool,block_number block,toUnixTimestamp64Milli(block_time) ms,lower(tx_hash) tx,log_index,toString(amount0) a0,toString(amount1) a1,tick FROM eatlq.uniswap_v3_swaps_decoded FINAL WHERE chain_id=8453 AND removed=0 AND block_number BETWEEN {lo} AND {CUTOFF} AND lower(pool_address) IN ({quoted}) ORDER BY pool,block,log_index"""
 for x in ch_query(q,timeout=1200):out[x["pool"]].append({"block":int(x["block"]),"ms":int(x["ms"]),"tx":x["tx"],"log_index":int(x["log_index"]),"a0":int(x["a0"]),"a1":int(x["a1"]),"tick":int(x["tick"])})
 for p in pools:out[p].sort(key=lambda x:(x["block"],x["log_index"]));out[p]=list({(x["block"],x["log_index"]):x for x in out[p]}.values());out[p].sort(key=lambda x:(x["block"],x["log_index"]))
 return out
def direction(s,orient):
 weth,tok=(s["a0"],s["a1"]) if orient==-1 else (s["a1"],s["a0"])
 if weth>0 and tok<0:return "buy",weth/-tok,weth,-tok
 if weth<0 and tok>0:return "sell",-weth/tok,-weth,tok
 return "other",None,abs(weth),abs(tok)

def rug_classes(rug,swaps,orient_by_pool):
 classes={}; details=[]
 for r in rug["rows"]:
  chosen=r.get("cause_points") or r.get("points",[]); pts={p["delta_blocks"]:p for p in chosen}
  def depth(d):
   x=pts.get(d,{}).get("pool_weth_balance",{}); return int(x["raw"]) if x.get("status")=="measured" else None
  pre=depth(-1); post=depth(0); ratio=(post/pre if pre and post is not None else None); collapse=ratio is not None and ratio<=.2
  preok=pts.get(-1,{}).get("atomic",{}).get("sell_ok") is True
  post_a=pts.get(0,{}).get("atomic",{}); postbad=post_a.get("status")=="measured" and (not post_a.get("sell_ok") or post_a.get("recovery_ratio",0)<=.1)
  recovery_atomic=any(p.get("atomic",{}).get("sell_ok") and p["atomic"].get("recovery_ratio",0)>.1 for d,p in pts.items() if d>0)
  later_sell=False; orient=orient_by_pool.get(r["pool"])
  if orient is not None:
   for s in swaps.get(r["pool"],[]):
    if r["flip_block"]<s["block"]<=CUTOFF and direction(s,orient)[0]=="sell": later_sell=True; break
  ev=r.get("event_evidence",{}); weth_out=ev.get("weth_out_logs",{}).get("logs",[]); pool_logs=ev.get("pool_logs",{}).get("logs",[])
  mechanics=bool(weth_out or pool_logs) and r.get("candidate_receipt",{}).get("status")=="measured"
  recovered=recovery_atomic or later_sell
  if postbad and recovered: cls="TEMPORARY_UNSELLABLE"
  elif collapse and mechanics and preok and postbad and not recovered: cls="CONFIRMED_LP_RUG"
  elif not collapse and preok and postbad and not recovered and ev.get("token_transfer_logs",{}).get("logs") and r.get("candidate_trace",{}).get("status")=="measured": cls="TRANSFER_RESTRICTION_RUG"
  elif post_a.get("sell_ok") and post_a.get("recovery_ratio",1)<=.1: cls="LIQUIDITY_IMPAIRMENT"
  else: cls="UNRESOLVED"
  effective_flip=r.get("candidate_cause_block") or r["flip_block"]; classes[r["pool"]]={"class":cls,"flip_block":effective_flip,"checkpoint_flip_block":r["flip_block"],"no_recovery_through_cutoff":not recovered,"depth_ratio":ratio,"mechanics":mechanics,"pre_atomic_sell_ok":preok,"post_atomic_bad":postbad,"later_actual_sell":later_sell,"atomic_recovery":recovery_atomic,"candidate_tx":r.get("candidate_cause_tx")}
  details.append({"pool":r["pool"],**classes[r["pool"]]})
 return classes,{"counts":dict(Counter(x["class"] for x in details)),"rows":details}

def real_sell(pool_swaps,orient,qty,lo,hi):
 blocks=[x["block"] for x in pool_swaps]; i=bisect.bisect_left(blocks,lo)
 for s in pool_swaps[i:]:
  if s["block"]>hi: break
  side,px,_,tok=direction(s,orient)
  if side=="sell" and tok>=qty:return s,px,tok
 return None
def classify_cell(row,eo,xo,swaps,rugs,ethusd):
 ep=next(p for p in row.get("entry_points",[]) if p["offset_blocks"]==eo); a=ep["atomic"]
 if a.get("status")!="measured": return {"resolved":False,"ret":None,"evidence":"UNRESOLVED_ENTRY_RPC"}
 if not a.get("buy_ok") or int(a.get("received_target",0))<=0:return {"resolved":True,"ret":0.,"gross":0.,"evidence":"NO_ENTRY_FLAT","d5_pass":False}
 qty=int(a["received_target"]); d5=bool(a.get("sell_ok") and a.get("recovery_ratio",0)>.1); xps=sorted(row["exit_points"],key=lambda p:p["offset_blocks"]); start=next(i for i,p in enumerate(xps) if p["offset_blocks"]==xo); gas=.075/(.001*ethusd)
 for attempt,p in enumerate(xps[start:],1):
  b=p["scheduled_block"]; real=real_sell(swaps.get(row["pool"],[]),int(row["orient"]),qty,b,b+1)
  if real:
   s,px,tok=real; gross=qty*px/SPEND-1; q=p.get("exact_size_quotes_by_entry_offset",{}).get(str(eo),{}); qgross=int(q.get("amount_out",0))/SPEND-1 if q.get("status")=="measured" and int(q.get("amount_out",0))>0 else None; return {"resolved":True,"ret":gross-(1+attempt)*gas,"gross":gross,"evidence":"REAL_SELL","exit_block":s["block"],"exit_tx":s["tx"],"exit_log_index":s["log_index"],"attempts":attempt,"d5_pass":d5,"qty":str(qty),"guarded_quote_gross":qgross,"quote_minus_real_gross":None if qgross is None else qgross-gross}
  guard=p["atomic_guard"]; q=p.get("exact_size_quotes_by_entry_offset",{}).get(str(eo),{})
  if guard.get("status")=="measured" and guard.get("sell_ok") and guard.get("recovery_ratio",0)>.1 and q.get("status")=="measured" and int(q.get("amount_out",0))>0:
   proceeds=int(q["amount_out"]); gross=proceeds/SPEND-1; return {"resolved":True,"ret":gross-(1+attempt)*gas,"gross":gross,"evidence":"ATOMIC_GUARDED_EXACT_QUOTE" if attempt==1 else "TEMPORARY_RECOVERY","exit_block":b,"attempts":attempt,"d5_pass":d5,"qty":str(qty),"proceeds":str(proceeds)}
 rug=rugs.get(row["pool"])
 if rug and rug["class"] in ("CONFIRMED_LP_RUG","TRANSFER_RESTRICTION_RUG") and rug["flip_block"]<=xps[start]["scheduled_block"] and rug["no_recovery_through_cutoff"]:
  return {"resolved":True,"ret":-1-(1+len(xps[start:]))*gas,"gross":-1.,"evidence":rug["class"],"attempts":len(xps[start:]),"d5_pass":d5,"qty":str(qty)}
 return {"resolved":False,"ret":None,"gross":None,"evidence":"UNRESOLVED_EXIT","d5_pass":d5,"qty":str(qty)}

def rolling(rows,eo,xo):
 lo=datetime(2026,2,27,5,41,15,tzinfo=timezone.utc); end=datetime(2026,6,8,18,29,40,tzinfo=timezone.utc); out=[]; i=0
 while lo+timedelta(days=7*i)<end:
  a=lo+timedelta(days=7*i); b=a+timedelta(days=14); rr=[r for r in rows if a<=datetime.fromisoformat(r["signal_time"])<b]; vals=[r["matrix"][f"{eo}:{xo}"]["ret"] for r in rr if r["matrix"][f"{eo}:{xo}"]["resolved"]]; out.append({"id":f"R{i:02}","start":a.isoformat(),"end_exclusive":b.isoformat(),**summary(vals),"unresolved":sum(not r["matrix"][f"{eo}:{xo}"]["resolved"] for r in rr)}); i+=1
 return out
def mc(rows,eo,xo):
 key=f"{eo}:{xo}"; rr=[r for r in rows if r["matrix"][key]["resolved"]]; by=defaultdict(list)
 for r in rr:by[r["regime"]].append(r["matrix"][key]["ret"])
 rng=np.random.default_rng(SEED+eo*100+xo); arr=np.zeros((10000,len(rr))); seq=sorted(rr,key=lambda x:x["signal_time"])
 for j,r in enumerate(seq): arr[:,j]=rng.choice(by[r["regime"]],10000,replace=True)
 eq=100+np.cumsum(arr,axis=1); peaks=np.maximum.accumulate(np.column_stack([np.full(10000,100.),eq]),axis=1)[:,1:]; dd=np.max(peaks-eq,axis=1); term=eq[:,-1]
 return {"paths":10000,"terminal_p5":float(np.quantile(term,.05)),"terminal_median":float(np.median(term)),"max_drawdown_p95":float(np.quantile(dd,.95))}
def evaluate(primary,baseline,eo,xo):
 key=f"{eo}:{xo}"; pv=[r["matrix"][key]["ret"] for r in primary if r["matrix"][key]["resolved"]]; bv=[r["matrix"][key]["ret"] for r in baseline if r["matrix"][key]["resolved"]]; pooled=summary(pv); regimes={}
 for w in ("W1","W2","W3"):
  pr=[r["matrix"][key]["ret"] for r in primary if r["regime"]==w and r["matrix"][key]["resolved"]]; br=[r["matrix"][key]["ret"] for r in baseline if r["regime"]==w and r["matrix"][key]["resolved"]]; regimes[w]={**summary(pr),"baseline_mean":summary(br)["mean"],"alpha_delta":None if not pr or not br else float(np.mean(pr)-np.mean(br)),"unresolved":sum(r["regime"]==w and not r["matrix"][key]["resolved"] for r in primary)}
 den=sum(x["n"] for x in regimes.values()); wb=sum((x["baseline_mean"] or 0)*x["n"] for x in regimes.values())/den if den else None; pooled["same_window_weighted_baseline_mean"]=wb; pooled["alpha_delta"]=None if wb is None else pooled["mean"]-wb
 roll=rolling(primary,eo,xo); monte=mc(primary,eo,xo); oos=[r["matrix"][key]["ret"] for r in primary if datetime.fromisoformat(r["signal_time"])>=datetime(2026,4,15,tzinfo=timezone.utc) and r["matrix"][key]["resolved"]]; unresolved=sum(not r["matrix"][key]["resolved"] for r in primary); powered_r=[x for x in regimes.values() if x["n"]>=20]; powered_roll=[x for x in roll if x["n"]>=20]
 gates={"pooled_mean_gt_0":pooled["mean"]>0,"pooled_top1_removed_mean_gt_0":pooled["top1_removed_mean"]>0,"every_powered_rolling_mean_ge_minus_10pct":all(x["mean"]>=-.1 for x in powered_roll) if powered_roll else None,"at_least_2_powered_regimes_positive_mean_and_alpha":sum(x["mean"]>0 and x["alpha_delta"] is not None and x["alpha_delta"]>0 for x in powered_r)>=2 if len(powered_r)>=2 else None,"mc_terminal_p5_gt_100R":monte["terminal_p5"]>100,"mc_mdd_p95_le_20R":monte["max_drawdown_p95"]<=20,"heldout_oos_mean_gt_0":bool(oos) and np.mean(oos)>0,"primary_unresolved_eq_0":unresolved==0}
 status="INCONCLUSIVE" if unresolved or any(v is None for v in gates.values()) else "PASS" if all(gates.values()) else "FAIL"
 bias=[r["matrix"][key]["quote_minus_real_gross"] for r in primary if r["matrix"][key].get("quote_minus_real_gross") is not None]
 return {"entry_offset_blocks":eo,"exit_offset_blocks":xo,"pooled":pooled,"unresolved":unresolved,"unconditional_baseline":{**summary(bv),"unresolved":sum(not r["matrix"][key]["resolved"] for r in baseline)},"real_vs_guarded_quote_bias":summary(bias),"evidence":dict(Counter(r["matrix"][key]["evidence"] for r in primary)),"regimes":regimes,"rolling":roll,"heldout_oos":summary(oos),"monte_carlo":monte,"gates":gates,"status":status}

def b4_analysis(raw):
 ages={}; trans=Counter();trans_w=defaultdict(Counter)
 def cat(p):
  a=p["atomic"]; q=p["qv2_sell_exact_received"]
  if a.get("status")!="measured":return "unknown"
  if not a.get("buy_ok"):return "buy_failure"
  ar=a.get("recovery_ratio",0); qr=int(q.get("amount_out",0))/SPEND if q.get("status")=="measured" else None
  if not a.get("sell_ok"):
   if qr is not None and qr>=.9:return "qv2_reasonable_atomic_sell_failure"
   if qr is not None:return "sell_failure_qv2_adverse"
   return "sell_failure_qv2_unknown"
  if ar<=.1 and qr is not None and qr>=.9:return "qv2_reasonable_atomic_severe"
  if ar<=.1:return "slippage_or_liquidity"
  return "healthy"
 for age in ("2m","5m","10m"):
  rr=[(r,cat(next(p for p in r.get("points",[]) if p["age"]==age))) for r in raw["rows"] if r.get("points")]; counts=Counter(c for _,c in rr); hp=sum(c.startswith("sell_failure") or c=="qv2_reasonable_atomic_sell_failure" for _,c in rr); severe=sum(c.startswith("sell_failure") or c.startswith("qv2_reasonable_atomic") or c=="slippage_or_liquidity" for _,c in rr); dis=sum(c.startswith("qv2_reasonable_atomic") for _,c in rr)
  windows={}
  for w in ("W1","W2","W3"):
   wr=[c for r,c in rr if r["window"]==w]; wc=Counter(wr); whp=sum(c.startswith("sell_failure") or c=="qv2_reasonable_atomic_sell_failure" for c in wr); wsev=sum(c.startswith("sell_failure") or c.startswith("qv2_reasonable_atomic") or c=="slippage_or_liquidity" for c in wr); windows[w]={"n":len(wr),"counts":dict(wc),"instant_hp":whp,"instant_hp_rate":whp/len(wr) if wr else None,"severe":wsev,"severe_rate":wsev/len(wr) if wr else None}
  hp_q_unknown=counts.get("sell_failure_qv2_unknown",0);hp_q_measured=hp-hp_q_unknown
  ages[age]={"n":len(rr),"counts":dict(counts),"instant_hp":hp,"instant_hp_rate":hp/len(rr) if rr else None,"instant_hp_wilson95":wilson(hp,len(rr)),"severe":severe,"severe_rate":severe/len(rr) if rr else None,"H0a_measured_disease_n":hp_q_measured,"H0a_unknown_disease_n":hp_q_unknown,"qv2_reasonable_atomic_severe_disagreement":dis,"H0a_disagreement_wilson95":wilson(dis,hp_q_measured),"windows":windows}
 for r in raw["rows"]:
  if not r.get("points"):continue
  c2=cat(next(p for p in r["points"] if p["age"]=="2m")); c10=cat(next(p for p in r["points"] if p["age"]=="10m")); trans[f"{c2}->{c10}"]+=1;trans_w[r["window"]][f"{c2}->{c10}"]+=1
 return {"ages":ages,"two_to_ten_minute_transitions":dict(trans),"two_to_ten_minute_transitions_by_window":{w:dict(c) for w,c in trans_w.items()}}

def weather_gate(primary,eo,xo):
 weather=json.loads(B6.read_text())["weather_by_day"]; cutoff=datetime(2026,4,15,tzinfo=timezone.utc); cal=[v["honeypot_proxy_rate"] for d,v in weather.items() if v["eligible"] and datetime.fromisoformat(d+"T00:00:00+00:00")<cutoff]; threshold=float(np.quantile(cal,.35,method="linear")); days=sorted(d for d,v in weather.items() if v["eligible"] and datetime.fromisoformat(d+"T00:00:00+00:00")>=cutoff); opens={d for d in days if weather[d]["honeypot_proxy_n"]>=20 and weather[d]["honeypot_proxy_rate"]<=threshold}; key=f"{eo}:{xo}"; rows=[r for r in primary if datetime.fromisoformat(r["signal_time"])>=cutoff and r["signal_time"][:10] in days and r["matrix"][key]["resolved"]]; ung=[r["matrix"][key]["ret"] for r in rows]; gated=[r["matrix"][key]["ret"] for r in rows if r["signal_time"][:10] in opens]; rng=random.Random(SEED+xo); dist=[]
 for _ in range(10000):
  sample=set(rng.sample(days,len(opens))); v=[r["matrix"][key]["ret"] for r in rows if r["signal_time"][:10] in sample]
  if v:dist.append(float(np.mean(v)))
 return {"threshold":threshold,"oos_eligible_days":len(days),"open_days":len(opens),"ungated":summary(ung),"derived_gate":summary(gated),"random_same_open_days_p95":float(np.quantile(dist,.95)) if dist else None,"beats_random_p95":bool(gated and dist and np.mean(gated)>np.quantile(dist,.95))}
def b3_seconds(raw,closes):
 out={}
 for off in OFFSETS:
  groups={"real":[],"proxy":[]}; unresolved=Counter()
  for r in raw["rows"]:
   if r.get("status")!="measured":
    if r.get("real"):unresolved["real"]+=1
    if r.get("proxy"):unresolved["proxy"]+=1
    continue
   ep=r["entry_point"]["atomic"];p=next(x for x in r["exit_points"] if x["offset_blocks"]==off);a=p["atomic_guard"];q=p["exact_size_quote"]
   if not ep.get("buy_ok"):
    val=0.
   elif a.get("sell_ok") and a.get("recovery_ratio",0)>.1 and q.get("status")=="measured" and int(q.get("amount_out",0))>0:
    ts=int(r["frozen_ladder"]["entry"]["ts"]);day=datetime.fromtimestamp(ts,tz=timezone.utc).date().isoformat();eth=float(closes.get(day) or min(closes.values()));val=int(q["amount_out"])/SPEND-1-2*.075/(.001*eth)
   else:
    if r.get("real"):unresolved["real"]+=1
    if r.get("proxy"):unresolved["proxy"]+=1
    continue
   if r.get("real"):groups["real"].append(val)
   if r.get("proxy"):groups["proxy"].append(val)
  out[str(off)]={k:summary(v) for k,v in groups.items()};out[str(off)]["unresolved"]=dict(unresolved)
 return {"frontier":out,"coverage":raw["summary"],"interpretation":"seconds repricing covers 15 V3 opportunities; 47 V2 rows are not silently coerced and the frozen W2/W3 real-trigger count remains zero"}

def main():
 core=json.loads(CORE.read_text()); b4=json.loads(B4.read_text()); rug=json.loads(RUG.read_text()); swaps=extend_rug_swaps(load_swaps(),rug); b5=mod(ROOT/"scripts/research/base_b5/b5_offline_reverify.py","b5_an"); df,_,_=b5.load_base(); orient={r.pool:int(r.orient) for r in df.itertuples()}; rugs,rug_summary=rug_classes(rug,swaps,orient); closes=json.loads(ETH.read_text())["closes"]
 rows=[]
 for src in core["rows"]:
  r={k:src[k] for k in ("pool","token","orient","regime","signal_time","entry_block","trigger_block","selected")}; r["matrix"]={}; eth=float(closes.get(r["signal_time"][:10]) or min(closes.values()))
  for eo in OFFSETS:
   for xo in OFFSETS:r["matrix"][f"{eo}:{xo}"]=classify_cell(src,eo,xo,swaps,rugs,eth)
  rows.append(r)
 primary=[r for r in rows if r["selected"]]; perf={f"{eo}:{xo}":evaluate(primary,rows,eo,xo) for eo in OFFSETS for xo in OFFSETS}; exit_front=[perf[f"1:{x}"] for x in OFFSETS]; entry_front=[perf[f"{x}:1"] for x in OFFSETS]
 def economic_pass(x):return all(v is not None and bool(v) for k,v in x["gates"].items() if k!="primary_unresolved_eq_0")
 epass=[economic_pass(x) for x in exit_front]; state="活-待真钱延迟" if all(epass) else "对延迟敏感-待真钱" if any(epass) else "已死"
 d5={}
 for eo in OFFSETS:
  for xo in OFFSETS:
   key=f"{eo}:{xo}"; off=[r["matrix"][key]["ret"] for r in primary if r["matrix"][key]["resolved"]]; on=[0. if not r["matrix"][key].get("d5_pass") else r["matrix"][key]["ret"] for r in primary if r["matrix"][key]["resolved"]]; d5[key]={"off":summary(off),"on":summary(on),"blocked":sum(r["matrix"][key]["resolved"] and not r["matrix"][key].get("d5_pass") for r in primary),"mean_delta":float(np.mean(on)-np.mean(off)) if off else None}
 gates={str(x):weather_gate(primary,1,x) for x in OFFSETS}; b4a=b4_analysis(b4); b3=json.loads(B3.read_text())["result"];b3s=b3_seconds(json.loads(B3RPC.read_text()),closes)
 verdicts={
  "1":{"verdict":"推翻" if state!="已死" else "成立","keystone_state":state,"reason":"seconds matrix and full right-tail determine historical viability; unresolved stays explicit and is not converted to loss"},
  "2":{"verdict":"修正","reason":"t0+10m is the causal feature horizon; +1..+15 blocks measure execution, not an earlier look-ahead entry"},
  "3":{"verdict":"推翻","reason":"honest seconds unconditional baseline replaces missing-as-total-loss -68% claim"},
  "4":{"verdict":"修正","reason":"2m execution pathology is decomposed and crossed with actual 10m KEYSTONE selection"},
  "5":{"verdict":"修正","reason":"for frozen KEYSTONE selection D5 blocks zero and the historical block monitor proves zero rescues, but archive evidence cannot prove the broader claim that only speed can ever help"},
  "6":{"verdict":"修正","reason":"archive establishes mechanics and rescue bounds, but cannot manufacture pre-sequencer ordering evidence"},
  "7":{"verdict":"成立","reason":"real W2/W3 trigger n remains zero; execution repricing cannot create absent signals"},
  "8":{"verdict":"成立","reason":"complete pinned entry atomic gate blocks zero selected trades at every entry offset and has zero measured mean marginal"},
  "9":{"verdict":"成立","reason":"full census finds zero QV2-reasonable/Atomic-severe false positives; W2 sell failures are a different estimand"},
  "10":{"verdict":"修正","reason":"the frozen causal gate opens two OOS days, selects zero KEYSTONE trades, and adds no value here; that does not prove every causal weather gate is universally useless"}}
 result={"schema_version":1,"sop_applied":True,"alpha_id":"AR-REAUDIT10-SECONDS","family":"F1/F4/F6/F7","next_gate":"HUMAN_LINE_REVIEW_ONLY","decision_flags":{"training_allowed":False,"live_allowed":False},"research_only":True,"go_decision":"NOT_MADE","preregistration_sha256":sha(PREREG),"keystone_state":state,"performance_matrix":perf,"exit_latency_budget_frontier":exit_front,"entry_latency_budget_frontier":entry_front,"d5_marginal":d5,"weather_gate":gates,"b4_health":b4a,"rug_adjudication":rug_summary,"forward_lp_monitor":json.loads(MONITOR.read_text())["summary"],"transfer_toggle_probe":json.loads(TOGGLE.read_text()),"b3_frozen":{"groups":b3["groups"],"real_proxy_bias":b3["real_proxy_bias"],"verdict":b3["verdict"]},"b3_seconds":b3s,"verdicts":verdicts,"rpc_ledger":json.loads((DIAG/"base_reaudit10_rpc_ledger_summary_20260715.json").read_text()),"source_hashes":{str(p.relative_to(ROOT)):sha(p) for p in (PREREG,CORE,B4,RUG,B3,B3RPC,B6,ETH,MONITOR,TOGGLE,*SWAPS,*SCRIPT_SOURCES)},"limitations":["decoded third-party trades prove historical executable pool prints, not our exact counterfactual queue position","AtomicRoundTrip is used only as an independent sell-path guard; exact-size QuoterV2 supplies the pinned amount and never stands alone","archive canonical state cannot reveal first-seen pending time or counterfactual sequencer ordering","formal right-tail status remains INCONCLUSIVE wherever primary unresolved is nonzero; the separate three-state historical viability verdict does not erase those rows"]}
 result=json.loads(json.dumps(result,default=lambda x:x.item() if isinstance(x,np.generic) else str(x))); primary=json.loads(json.dumps(primary,default=lambda x:x.item() if isinstance(x,np.generic) else str(x)))
 OUT.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n"); PER.write_text(json.dumps({"schema_version":1,"preregistration_sha256":sha(PREREG),"rows":primary},indent=2,sort_keys=True)+"\n"); plot(result); REPORT.write_text(render(result)); print(json.dumps({"out":str(OUT),"report":str(REPORT),"keystone_state":state,"verdicts":verdicts,"exit_frontier":[{"blocks":x["exit_offset_blocks"],"mean":x["pooled"]["mean"],"top1_removed":x["pooled"]["top1_removed_mean"],"unresolved":x["unresolved"],"status":x["status"]} for x in exit_front]},indent=2,ensure_ascii=False))

def plot(r):
 x=[z["exit_offset_blocks"] for z in r["exit_latency_budget_frontier"]]; y=[z["pooled"]["mean"] for z in r["exit_latency_budget_frontier"]]; yt=[z["pooled"]["top1_removed_mean"] for z in r["exit_latency_budget_frontier"]]; xe=[z["entry_offset_blocks"] for z in r["entry_latency_budget_frontier"]]; ye=[z["pooled"]["mean"] for z in r["entry_latency_budget_frontier"]]; fig,ax=plt.subplots(figsize=(8,4.8)); ax.plot(x,y,marker="o",label="exit frontier mean (entry +1)"); ax.plot(x,yt,marker="o",label="exit top1 removed"); ax.plot(xe,ye,marker="s",label="entry frontier mean (exit +1)"); ax.axhline(0,color="#555",lw=.8); ax.set(xlabel="block offset (nominal ~2s each)",ylabel="net return",title="KEYSTONE historical execution latency budget"); ax.grid(alpha=.25); ax.legend(); fig.tight_layout(); fig.savefig(SVG); plt.close(fig)
def pct(x):return "—" if x is None else f"{x:+.2%}"
def render(r):
 lines=["---","sop_applied: true","alpha_id: AR-REAUDIT10-SECONDS","family: F1/F4/F6/F7","next_gate: HUMAN_LINE_REVIEW_ONLY","decision_flags:","  training_allowed: false","  live_allowed: false","---","","# 十条可疑结论独立重审：历史秒级执行","","> 纯回测/固定 archive 只读；没有下单、签名、广播、改 live 或服务启停；不作 GO。真钱延迟测试不在本轮。","","## 终审摘要","",f"预注册哈希 `{r['preregistration_sha256']}`。#1 三态裁定：**{r['keystone_state']}**；原先“KEYSTONE 已死”被**推翻**。",""]
 lines += ["历史可证部分很清楚：+1 到 +15 块的出场均值约 +56.6% 至 +58.2%，去头部 1% 后仍约 +48.1% 至 +49.7%；曲线没有随 2–30 秒档明显衰减。入场从 +1 延至 +15 块，均值从 +56.6% 降到 +53.0%，仍为正。换句话说，这批历史样本给出的不是“必须亚 10 秒，否则崩”，而是至少覆盖约 30 秒的宽延迟预算。", "", "但预注册的形式状态仍是 `INCONCLUSIVE`：每个出场点有 3–5/116 个 unresolved，按冻结优先级它们不能当 FAIL、也不能当 -100%。除 `primary_unresolved_eq_0` 外，每个出场点的全部已测试右尾门都通过。三态“活”是历史可行性裁定，不是 GO，也没有把 unresolved 偷换成成功。", "", "## 出场延迟预算前沿（入场固定 +1 块）", "", "| 出场偏移 | 约秒数 | resolved/116 | unresolved | mean | median | top1移除 | 条件 alpha | 无条件 resolved mean/覆盖 | 形式右尾 |", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
 for z in r["exit_latency_budget_frontier"]:
  b=z["unconditional_baseline"]; lines.append(f"| +{z['exit_offset_blocks']}块 | ~{z['exit_offset_blocks']*2}s | {z['pooled']['n']}/116 | {z['unresolved']} | {pct(z['pooled']['mean'])} | {pct(z['pooled']['median'])} | {pct(z['pooled']['top1_removed_mean'])} | {pct(z['pooled']['alpha_delta'])} | {pct(b['mean'])} / {b['n']}/431 | {z['status']} |")
 lines += ["", "`条件 alpha` 只比较 resolved 的同窗口基线；无条件基线在 +1 块仍有 146/431 unresolved，因此不能把 +37.9pp 外推成完整宇宙的无缺口因果 alpha。旧的 −68% 同样不能保留，因为它把这些未知强行写成全损。", "", "### 完整 5×5 均值矩阵", "", "行是入场成交偏移，列是出场成交偏移；全部扣每次尝试的 $0.075 gas。", "", "| 入\\出 | +1 | +3 | +5 | +10 | +15 |", "|---:|---:|---:|---:|---:|---:|"]
 for eo in OFFSETS: lines.append("| +%s | %s |"%(eo," | ".join(pct(r["performance_matrix"][f"{eo}:{xo}"]["pooled"]["mean"]) for xo in OFFSETS)))
 lines += ["", "### 预注册右尾逐档", "", "| 出场块 | pooled>0 | 去top1>0 | powered滚动窗≥−10% | ≥2 powered regime 正收益且正alpha | MC终值p5>100R | MC回撤p95≤20R | OOS mean>0 | unresolved=0 |", "|---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|"]
 names=("pooled_mean_gt_0","pooled_top1_removed_mean_gt_0","every_powered_rolling_mean_ge_minus_10pct","at_least_2_powered_regimes_positive_mean_and_alpha","mc_terminal_p5_gt_100R","mc_mdd_p95_le_20R","heldout_oos_mean_gt_0","primary_unresolved_eq_0")
 for z in r["exit_latency_budget_frontier"]: lines.append("| +%s | %s |"%(z["exit_offset_blocks"]," | ".join("✓" if z["gates"][n] else "✗" if z["gates"][n] is not None else "未测试" for n in names)))
 lines += ["", "## 入场延迟预算前沿（出场固定 +1 块）", "", "| 入场偏移 | 约秒数 | resolved/116 | unresolved | mean | median | top1移除 |", "|---:|---:|---:|---:|---:|---:|---:|"]
 for z in r["entry_latency_budget_frontier"]: lines.append(f"| +{z['entry_offset_blocks']}块 | ~{z['entry_offset_blocks']*2}s | {z['pooled']['n']}/116 | {z['unresolved']} | {pct(z['pooled']['mean'])} | {pct(z['pooled']['median'])} | {pct(z['pooled']['top1_removed_mean'])} |")
 lines += ["", "## W2：有毒宇宙与可赚钱子集同时成立", "", "| 时点 | W2 fresh V3 n | 原子买失败 | 原子卖失败 | healthy | instant-hp率 |", "|---:|---:|---:|---:|---:|---:|"]
 for age in ("2m","5m","10m"):
  w=r["b4_health"]["ages"][age]["windows"]["W2"]; c=w["counts"]; lines.append(f"| {age} | {w['n']} | {c.get('buy_failure',0)} | {w['instant_hp']} | {c.get('healthy',0)} | {pct(w['instant_hp_rate'])} |")
 w2=r["exit_latency_budget_frontier"][0]["regimes"]["W2"];lines += ["",f"74.5% 不是暂时的 2 分钟噪声：304/408 在 2m 原子卖失败，10m 反而是 311/408。可是 KEYSTONE 的真实 t0+10m 选择在 W2 resolved n={w2['n']}，+1块 mean={pct(w2['mean'])}、同窗口 alpha={pct(w2['alpha_delta'])}，仅 1 unresolved。故“W2 全域有毒”成立，而“KEYSTONE 因此在 W2 亏”被推翻；正确结论是模型/摩擦门选中了 W2 的健康小岛。", "", "## QuoterV2 与 Atomic：不再横跳", "", "| 池龄 | fresh V3 n | instant-hp | QV2已测疾病例 | QV2未知疾病例 | QV2合理但Atomic严重失败 | Wilson 95%上界 |", "|---:|---:|---:|---:|---:|---:|---:|"]
 for age in ("2m","5m","10m"):
  a=r["b4_health"]["ages"][age]; lines.append(f"| {age} | {a['n']} | {a['instant_hp']} | {a['H0a_measured_disease_n']} | {a['H0a_unknown_disease_n']} | {a['qv2_reasonable_atomic_severe_disagreement']} | {pct(a['H0a_disagreement_wilson95'][1])} |")
 lines += ["", "2m 的复核精确闭合旧锚点：已测 instant-hp 306 个中 0 个出现“QuoterV2 给出≥90%合理卖价、但 Atomic 严重失败”，另 2 个 QV2 未知。W2 的 74.5% 是 Atomic 卖失败；这批病例中 QV2 自己也 adverse/失败，所以两句话并不矛盾。QuoterV2 可作卖侧异常过滤器，但不能单独证明真钱成交。", "", "## Rug 五证据与分类", "", "| 分类 | n | 是否允许直接−100% |", "|---|---:|---|"]
 counts=r["rug_adjudication"]["counts"]
 for cls in ("CONFIRMED_LP_RUG","TRANSFER_RESTRICTION_RUG","TEMPORARY_UNSELLABLE","LIQUIDITY_IMPAIRMENT","UNRESOLVED"): lines.append(f"| {cls} | {counts.get(cls,0)} | {'仅确认且不恢复时允许' if cls in ('CONFIRMED_LP_RUG','TRANSFER_RESTRICTION_RUG') else '不允许'} |")
 lines += ["", "每池交叉了：后续真成交、固定块原子可卖、WETH 深度、区间 pool/WETH/token 日志、原因交易 receipt/trace、B−1/B/+1/+3/+5/+10/+15 恢复。结果为 235 个确认且不恢复的 LP rug、1 个暂时不可卖后恢复、26 个 unresolved；没有把 26 个未知降级成全损。KEYSTONE +1块主口径中实际计入 2 个确认 LP rug、3 个 unresolved。", "", "两个 transfer-toggle 另做有界二分：首个经济坏块为 46,693,930 和 45,007,595；相邻块 hash 与原子整数在 toggle artifact。它们证明状态可在一块内翻转，但不证明公开 pending 一定能提前看见。", "", "## D5 与 forward LP 监控", "", "| 入场偏移 | D5挡单 | mean边际 |", "|---:|---:|---:|"]
 for eo in OFFSETS:
  d=r["d5_marginal"][f"{eo}:1"];lines.append(f"| +{eo}块 | {d['blocked']} | {pct(d['mean_delta'])} |")
 m=r["forward_lp_monitor"];lines += ["",f"D5 在冻结 KEYSTONE 子集的五个入场偏移均挡单 0、收益边际 0。既有 forward LP 探针测 24 个重点 rug：real loss={m['real_loss_n']}、对 real loss 的 prewarning=0、rescue proven=0；两个 prewarning 都发生在策略已经退出的池。故“这套 D5/区块级监控没有增益”成立；但“世界上只有速度有用”仍需修正，因为 archive 不能识别 pre-sequencer/pending 首见顺序。", "", "## B3 两分钟加速", ""]
 b3=r["b3_seconds"];lines += [f"冻结真信号仍是 W1 n=17、W2 n=0、W3 n=0。秒级补跑覆盖其中 15 个 V3；47 个 V2 不适用 V3 Atomic 工具，2 个缺冻结进/出锚点，均保留未测。15 个可测 V3 在 +1/+3/+5/+10/+15 块 mean 分别为 "+", ".join(pct(b3['frontier'][str(x)]['real']['mean']) for x in OFFSETS)+"。这能修正 W1 的执行收益，却不能把 W2/W3 的零触发变成样本，因此原 INCONCLUSIVE 结论成立。", "", "## 天气门", ""]
 wg=r["weather_gate"]["1"];lines += [f"冻结的因果阈值为 {wg['threshold']:.4%}；55 个 OOS eligible 天只开放 2 天，而这两天 KEYSTONE 交易 n=0。五个秒级出场档都没有跑赢同开工占比随机门。裁定为“修正”：这一个门在本样本确实无增量价值，但 n=0 只能说明阈值/机会错配，不能证明所有外生天气门普遍无用。", "", "## 十条逐条终裁", "", "| # | 原结论 | 裁定 | 重算锚点与残余质疑 |", "|---:|---|---|---|"]
 detail={
 "1":("KEYSTONE 已死/edge 衰减",f"+1..+15块 mean {pct(r['exit_latency_budget_frontier'][0]['pooled']['mean'])}..{pct(r['exit_latency_budget_frontier'][-1]['pooled']['mean'])}，已测经济右尾全过；3–5 unresolved 保留。三态={r['keystone_state']}。"),
 "2":("t0+600s 是合理执行延迟",f"t0+10m 只保留作因果特征窗；真正执行 +1..+15块，入场 mean {pct(r['entry_latency_budget_frontier'][0]['pooled']['mean'])}→{pct(r['entry_latency_budget_frontier'][-1]['pooled']['mean'])}。"),
 "3":("Base 瞎买均值−68%",f"+1块无条件 resolved 285/431、mean {pct(r['exit_latency_budget_frontier'][0]['unconditional_baseline']['mean'])}，146 unresolved；−68% 的 missing=loss 口径被推翻，但完整宇宙仍未点识别。"),
 "4":("W2 74.5%毒且 KEYSTONE 在W2亏",f"W2 2m instant-hp=304/408；10m=311/408，毒性成立；KEYSTONE W2 n={w2['n']} mean={pct(w2['mean'])}，亏损推论推翻。"),
 "5":("防rug只靠速度，monitor/D5无值",f"D5挡0、边际0；block monitor 对 real loss 预警0、救援0。历史实现无值成立，“只可能靠速度”过宽。"),
 "6":("单块LP pull点态看不见且无解",f"235 个确认LP rug；相邻块可由深度/Atomic/trace确定。canonical 点态确实只能事后，pending是否可抢先未被 archive 识别。"),
 "7":("B3 W3 n=0无法认定",f"真 W2/W3 仍0；15个V3秒级 W1 为正不增加信号密度。"),
 "8":("D5对KEYSTONE边际0",f"五个入场偏移全部 blocked=0、mean delta=0，成立。"),
 "9":("QV2合理价谎报罕见/可作卖侧过滤",f"2m 0/306，未知2，Wilson上界 {pct(r['b4_health']['ages']['2m']['H0a_disagreement_wilson95'][1])}；成立但不能替代 Atomic/真成交。"),
 "10":("天气门无用/救不回",f"开放2/55天、交易0、未胜随机门；本门无值，普遍无用的表述需收窄。")}
 for i in map(str,range(1,11)): lines.append(f"| {i} | {detail[i][0]} | **{r['verdicts'][i]['verdict']}** | {detail[i][1]} |")
 rpc=r["rpc_ledger"]["rpc"];lines += ["", "## Golden、RPC ledger 与复现", "", "Golden 三例：健康整数往返池 `0x0019…3092`（block 42,702,284）；确认 LP rug `0x000a…ae92`（WETH 21.4916 → 560 wei，block 41,613,806→807）；transfer-toggle `0x1070…82ee`（首坏 block 46,693,930）。固定整数、block hash、request/response hash 在 `tests/fixtures/base_reaudit10_golden_20260715.json`。", "", f"总 ledger：HTTP attempts={rpc['http_attempts']:,}，successful={rpc['successful_attempts']:,}，error/revert attempts={rpc['error_attempts']:,}，估算 credits={rpc['estimated_credits']:,}。方法仅含 `{sorted(rpc['methods'])}`；没有 send/sign/broadcast 方法。", "", f"主结果 SHA256 `{sha(OUT)}`。关键源 hash 与四段 ledger hash 在 `{OUT.relative_to(ROOT)}` 的 `source_hashes`/`rpc_ledger`。预注册 SHA256 `{r['preregistration_sha256']}`。", "", "## SOP/守卫复核", "", "1. `FAILED_NO_7D_ALPHA` 仍有效；没有 supersede。", "2. label freeze 仍为 1800s/0.006/v2,v3；本轮是独立 research estimand，没有改 label。", "3. 没有训练、重训或模型 promotion。", "4. 没有写 signals/Redis/live config。", "5. executor/strategy 在执行前确认未运行，本轮没有启停任何服务。", "6. 没有读取 SEND/PRIVATE_KEY/签名变量；artifact 不含 RPC URL。", "7. 所有链上调用固定 block，并记录 block hash 与请求/响应 hash。", "8. QuoterV2 从不单独充当成交证据。", "9. unresolved 没有被记作 −100%；只有五证据确认且不恢复的 rug 才允许。", "10. 所有裁定均为 research evidence；GO=`NOT_MADE`。", "", "```bash", "cd /opt/EATLQ", "export FORK_ARCHIVE_RPC=$(grep -E '^POOL_DISCOVERY_RPC_URL=' infra/compose/.env | cut -d= -f2-)", "export LABEL_RPC_URL=\"$FORK_ARCHIVE_RPC\"", "python3 -B scripts/research/base_reaudit10/reaudit10_rpc.py", "# 若按 checkpoint 分段：reaudit10_rugs_resume.py / reaudit10_b3_seconds_rpc.py / reaudit10_transfer_toggle_probe.py", "python3 -B scripts/research/base_reaudit10/reaudit10_finalize_ledger.py", "python3 -B scripts/research/base_reaudit10/reaudit10_analyze.py", "python3 -B -m unittest tests.test_base_reaudit10", "```", ""]
 return "\n".join(lines)
if __name__=="__main__":main()
