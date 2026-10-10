"""H18.14 dashcam diagnostics: confirm pool-first fields populate on new rows."""
import csv,collections,json
PATH="/opt/EATLQ/logs/dashcam.csv"
NEW_COLS=["pool_address","pool_type","dex","fee_bps","token0","token1","quote_token_address","quote_symbol","target_token_address","target_resolution_status","normalized_side"]
with open(PATH,newline="") as f:
    rd=csv.DictReader(f)
    rows=list(rd)
hdr=rd.fieldnames or []
new_rows=[r for r in rows if (r.get("pool_address") or "")]
n_total=len(rows); n_new=len(new_rows)

def pct(n,d):
    return round(n/d*100,2) if d else 0.0

field_pop={c:sum(1 for r in new_rows if (r.get(c) or "")!="") for c in NEW_COLS}
pool_type_dist=collections.Counter((r.get("pool_type") or "?") for r in new_rows)
dex_dist=collections.Counter((r.get("dex") or "?") for r in new_rows)
quote_dist=collections.Counter((r.get("quote_symbol") or "?") for r in new_rows)
trs_dist=collections.Counter((r.get("target_resolution_status") or "?") for r in new_rows)
side_dist=collections.Counter((r.get("normalized_side") or "?") for r in new_rows)
fee_dist=collections.Counter((r.get("fee_bps") or "?") for r in new_rows)
target_pop=sum(1 for r in new_rows if (r.get("target_token_address") or "") and r.get("target_token_address")!="")
no_known=sum(1 for r in new_rows if r.get("target_resolution_status")=="no_known_quote_leg")
pool_meta_missing=sum(1 for r in new_rows if r.get("target_resolution_status")=="pool_meta_missing")
unique_pools=sorted({r.get("pool_address") for r in new_rows if r.get("pool_address")})
unique_targets=sorted({r.get("target_token_address") for r in new_rows if r.get("target_token_address")})
sample=[]
seen=set()
for r in new_rows[-400:]:
    pa=r.get("pool_address","")
    if pa in seen: continue
    seen.add(pa)
    sample.append({k:r.get(k,"") for k in ["timestamp","pool_address","pool_type","dex","fee_bps","quote_symbol","target_token_address","target_resolution_status","normalized_side"]})
    if len(sample)>=10: break

out={
 "header_columns":hdr,
 "header_upgraded_to_h1814":hdr[10:]==NEW_COLS,
 "rows_total":n_total,
 "rows_with_new_schema":n_new,
 "rows_with_new_schema_pct_of_total":pct(n_new,n_total),
 "field_population_pct_of_new_rows":{c:pct(v,n_new) for c,v in field_pop.items()},
 "pool_type_dist":dict(pool_type_dist),
 "dex_dist":dict(dex_dist),
 "fee_bps_dist_top":dict(fee_dist.most_common(8)),
 "quote_symbol_dist":dict(quote_dist),
 "target_resolution_status_dist":dict(trs_dist),
 "normalized_side_dist":dict(side_dist),
 "target_token_populated_pct":pct(target_pop,n_new),
 "no_known_quote_leg":no_known,
 "pool_meta_missing":pool_meta_missing,
 "unique_pools_in_new_rows":len(unique_pools),
 "unique_target_tokens_in_new_rows":len(unique_targets),
 "sample_first_10_distinct_pools":sample,
}
print(json.dumps(out,indent=2,default=str))
