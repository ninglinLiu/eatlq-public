#!/usr/bin/env python3
"""Offline B4 reducer. Input is JSONL exported from archive eth_call runs.
No RPC, signing, or transaction broadcast is performed here.
"""
import argparse, json, math

def wilson(k, n, z=1.96):
    if n == 0: return [None, None]
    p=k/n; d=1+z*z/n
    c=(p+z*z/(2*n))/d
    h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [c-h, c+h]

def disagreement(r):
    q_ok=(not r['q_revert']) and r['q_tax'] < .10
    b_bad=r['b_revert'] or r['b_tax'] >= .90 or r['b_weth_out'] == 0
    return q_ok and b_bad

def main(path):
    rows=[json.loads(x) for x in open(path) if x.strip()]
    groups={}
    for r in rows:
        g=(r['group'], r.get('kind','na')); a=groups.setdefault(g,[0,0]); a[1]+=1; a[0]+=disagreement(r)
    out=[]
    for (g,k),(bad,n) in sorted(groups.items()):
        out.append({'group':g,'kind':k,'k':bad,'n':n,'rate':None if not n else bad/n,'wilson95':wilson(bad,n)})
    print(json.dumps({'rows':len(rows),'table':out}, indent=2, sort_keys=True))

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('jsonl'); main(p.parse_args().jsonl)
