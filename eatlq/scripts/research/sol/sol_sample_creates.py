#!/usr/bin/env python3
"""Stage 1 — frugal, UNBIASED historical sample of pump.fun token creations.

Why this design (RPC is metered, used sparingly per the operator):
  - Free pump.fun APIs only expose a shallow "what's new now" feed (~1.5h deep),
    far too short to accumulate the RARE right-tail events a backtest needs.
  - Deep multi-day enumeration therefore requires the chain. getBlock sampling
    is wasteful (0.2 creates / 3.4MB block). Instead we exploit that EVERY pump
    "create" instruction references the single global mint-authority PDA
    (TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM) at instruction-account[1].
  - getSignaturesForAddress(mint_authority) thus enumerates creates directly and
    cheaply (1000 sigs / call). We page back across the target window, draw a
    uniform random sample of N, and only then getTransaction() the sampled sigs
    to recover the mint (create-ix account[0]). RPC cost = ~(window_pages) cheap
    sig-list calls + N getTransaction calls. One-time, then cached to JSONL.

Outputs: mvp/diagnostics/sol/creates_sample.jsonl  (one row per sampled create)
  {mint, creator, sig, slot, block_time, age_h}

Anti-lookahead: we ONLY sample creates older than --min-age-h so their outcome
(ATH etc., fetched in stage 2) is already settled, and we never use any field
observed after creation as an ex-ante feature.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE_DISC = bytes.fromhex("d6904cec5f8b31b4")  # verified anchor disc for pump create
MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"  # global, referenced only on create
OUT = "/opt/EATLQ/mvp/diagnostics/sol/creates_sample.jsonl"

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58dec(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + full


def rpc_url() -> str:
    for ln in open("/opt/EATLQ/infra/compose/.env"):
        if ln.startswith("SOLANA_RPC_URL="):
            return ln.strip().split("=", 1)[1]
    raise SystemExit("SOLANA_RPC_URL not found in infra/compose/.env")


class RPC:
    """Rate-limited JSON-RPC client. Honors a min interval (token-bucket-ish)
    and backs off hard on HTTP 429 / rate-limit errors (per-minute caps need
    multi-second waits, not sub-second). RPC is metered, so we spend it slowly."""

    def __init__(self, url, min_interval=0.22):
        self.url = url
        self.calls = 0
        self.min_interval = min_interval
        self._last = 0.0

    def __call__(self, method, params=None, _tries=7):
        self.calls += 1
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        for i in range(_tries):
            dt = time.time() - self._last
            if dt < self.min_interval:
                time.sleep(self.min_interval - dt)
            self._last = time.time()
            try:
                req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
                d = json.loads(urllib.request.urlopen(req, timeout=45).read())
                if "error" in d:
                    msg = str(d["error"])
                    if "rate" in msg.lower() or "429" in msg or "limit" in msg.lower():
                        time.sleep(min(2 ** i, 30))
                        continue
                    raise RuntimeError(msg[:120])
                return d.get("result")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    ra = e.headers.get("Retry-After")
                    time.sleep(float(ra) if ra and ra.isdigit() else min(2 ** i, 30))
                    continue
                if i == _tries - 1:
                    raise
                time.sleep(min(2 ** i, 20))
            except Exception:
                if i == _tries - 1:
                    raise
                time.sleep(min(2 ** i, 20))
        return None


def enumerate_creates(rpc, now, min_age_h, max_age_d, max_sig_calls):
    """Page getSignaturesForAddress(mint_authority) backward, keep sigs whose
    block_time falls in [now-max_age_d, now-min_age_h]. Returns list of dicts."""
    lo = now - int(max_age_d * 86400)
    hi = now - int(min_age_h * 3600)
    out = []
    before = None
    pages = 0
    while pages < max_sig_calls:
        params = [MINT_AUTHORITY, {"limit": 1000, **({"before": before} if before else {})}]
        sigs = rpc("getSignaturesForAddress", params) or []
        pages += 1
        if not sigs:
            break
        before = sigs[-1]["signature"]
        oldest = sigs[-1].get("blockTime") or 0
        for s in sigs:
            bt = s.get("blockTime")
            if bt is None or s.get("err"):
                continue
            if lo <= bt <= hi:
                out.append({"sig": s["signature"], "slot": s["slot"], "block_time": bt})
        # stop once we've paged past the old end of the window
        if oldest and oldest < lo:
            break
        if pages % 10 == 0:
            print(f"  [enum] pages={pages} collected={len(out)} oldest_age={ (now-oldest)/3600:.1f}h", flush=True)
    return out


def mint_from_sig(rpc, sig):
    """jsonParsed resolves Address-Lookup-Table accounts and gives custom-program
    instructions as {programId, accounts:[pubkey...], data(base58)} — robust to v0."""
    tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                      "commitment": "confirmed"}])
    if not tx:
        return None
    msg = tx["transaction"]["message"]
    fee_payer = None
    ak = msg.get("accountKeys", [])
    if ak:
        fee_payer = ak[0]["pubkey"] if isinstance(ak[0], dict) else ak[0]
    for ins in msg.get("instructions", []):
        if ins.get("programId") != PUMP:
            continue
        data = ins.get("data", "")
        try:
            is_create = data and b58dec(data)[:8] == CREATE_DISC
        except Exception:
            is_create = False
        if is_create:
            accs = ins.get("accounts", [])
            if accs:
                return {"mint": accs[0], "creator": fee_payer}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-h", type=float, default=24.0, help="only creates at least this old (settled outcome)")
    ap.add_argument("--max-age-d", type=float, default=6.0, help="window depth in days")
    ap.add_argument("--n", type=int, default=1500, help="target sample size")
    ap.add_argument("--max-sig-calls", type=int, default=120, help="budget: max getSignaturesForAddress pages")
    ap.add_argument("--max-tx-calls", type=int, default=1800, help="budget: max getTransaction calls")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--sleep", type=float, default=0.0, help="extra throttle (RPC class already rate-limits)")
    ap.add_argument("--min-interval", type=float, default=0.22, help="min seconds between RPC calls")
    ap.add_argument("--append", action="store_true", help="append to existing sample (dedup by mint)")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    random.seed(args.seed)
    rpc = RPC(rpc_url(), min_interval=args.min_interval)
    now = int(time.time())

    seen = set()
    mode = "w"
    if args.append and os.path.exists(OUT):
        for ln in open(OUT):
            try:
                seen.add(json.loads(ln)["mint"])
            except Exception:
                pass
        mode = "a"
        print(f"[append] {len(seen)} existing mints")

    print(f"[enum] window=[now-{args.max_age_d}d, now-{args.min_age_h}h]  target N={args.n}", flush=True)
    creates = enumerate_creates(rpc, now, args.min_age_h, args.max_age_d, args.max_sig_calls)
    print(f"[enum] {len(creates)} creates in window after {rpc.calls} sig-calls")
    if not creates:
        raise SystemExit("no creates collected; widen budget/window")

    random.shuffle(creates)
    sample = creates[: min(args.n, args.max_tx_calls)]
    print(f"[resolve] getTransaction on {len(sample)} sampled creates...", flush=True)

    n_ok = 0
    n_none = 0
    n_err = 0
    with open(OUT, mode) as f:
        for i, c in enumerate(sample):
            info = None
            try:
                info = mint_from_sig(rpc, c["sig"])
            except Exception as e:
                n_err += 1
                if n_err <= 3:
                    print(f"  [err] {type(e).__name__}: {str(e)[:160]}", flush=True)
            if args.sleep:
                time.sleep(args.sleep)
            if not info:
                n_none += 1
                continue
            if info["mint"] in seen:
                continue
            seen.add(info["mint"])
            row = {"mint": info["mint"], "creator": info["creator"], "sig": c["sig"],
                   "slot": c["slot"], "block_time": c["block_time"], "age_h": round((now - c["block_time"]) / 3600, 2)}
            f.write(json.dumps(row) + "\n")
            n_ok += 1
            if (i + 1) % 100 == 0:
                f.flush()
                print(f"  [resolve] {i+1}/{len(sample)} ok={n_ok} none={n_none} err={n_err} rpc_calls={rpc.calls}", flush=True)

    print(f"[done] wrote {n_ok} creates (none={n_none} err={n_err}) -> {OUT}  total_rpc_calls={rpc.calls}")


if __name__ == "__main__":
    main()
