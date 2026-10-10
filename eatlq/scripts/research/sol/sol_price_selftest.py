#!/usr/bin/env python3
"""P4 — self-test for the pump TradeEvent price reconstruction used everywhere
(sol_realized_ev.price_at / sol_smart_buyers.decode_event).

We do NOT trust the decoded spot price (vSol/vTok) blindly. Three independent
ground-truth checks per coin, on REAL on-chain trades:

  (1) [PRIMARY] Execution vs spot: the trade's realized average price
      exec = solAmt/tokAmt must sit close to the spot vSol/vTok (a single small
      trade barely moves the curve). Astronomical / wrong-field decodes blow this
      up. This is the decisive check.

  (2) [PRIMARY] On-chain reserve cross-check: vTok (virtual) minus the bonding
      curve's REAL token balance (postTokenBalances of the bc token account) must
      be a CONSTANT (the fixed virtual-token offset ~ const). Confirms field
      offset AND decimal scale simultaneously.

  (3) [INFO ONLY] k = vSol*vTok. NOTE: pump's k is NOT invariant during the early
      ramp (fee accretion + curve filling push k up), so its CV is reported for
      information but is NOT a pass/fail gate (an earlier version wrongly gated on
      it and produced false FAILs despite a perfect exec/spot match).

PASS if: |exec/spot - 1| median < 15%, vTok-realTok offset CV < 2% (over trades
where the real reserve was resolvable), and no |price| outside a sane band.
"""
from __future__ import annotations
import base64
import json
import struct
import importlib.util
import statistics as st

_e = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("ev", "/opt/EATLQ/scripts/research/sol/sol_realized_ev.py"))
importlib.util.spec_from_file_location("ev", "/opt/EATLQ/scripts/research/sol/sol_realized_ev.py").loader.exec_module(_e)
_s = _e._s


def decode_full(raw, mint_b):
    NEED = 8 + 32 + 8 + 8 + 1 + 32 + 8 + 8 + 8
    if len(raw) < NEED or raw[8:40] != mint_b:
        return None
    sol = struct.unpack_from("<Q", raw, 40)[0] / 1e9
    tok = struct.unpack_from("<Q", raw, 48)[0] / 1e6
    is_buy = raw[56]
    off = 8 + 32 + 8 + 8 + 1 + 32 + 8
    vSol = struct.unpack_from("<Q", raw, off)[0] / 1e9
    vTok = struct.unpack_from("<Q", raw, off + 8)[0] / 1e6
    return dict(sol=sol, tok=tok, is_buy=is_buy, vSol=vSol, vTok=vTok)


def cv(xs):
    xs = [x for x in xs if x]
    if len(xs) < 2:
        return 0.0
    m = st.mean(xs)
    return (st.pstdev(xs) / m) if m else 0.0


def main():
    rpc = _e.RPC(_e.rpc_url(), min_interval=0.18)
    rows = [json.loads(l) for l in open("/opt/EATLQ/mvp/diagnostics/sol/creates_earlypath.jsonl")]
    # take a few diverse coins (winners and a graduate)
    winners = [r for r in rows if (r.get("ath_mc") or 0) >= 20000][:5]
    overall_ratio = []
    fails = 0
    for r in winners:
        mint_b = _s.b58dec(r["mint"])
        bc = r["bonding_curve"]
        created = r["created_ms"] / 1000.0
        trades, _ = _e.collect_trades(rpc, bc, created - 5)
        succ = [(t, sig) for (t, sig, err) in trades if not err][:15]
        ks, ratios, offsets, prices = [], [], [], []
        for t, sig in succ:
            tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                              "commitment": "confirmed"}])
            if not tx or tx["meta"].get("err"):
                continue
            ev = None
            for L in tx["meta"].get("logMessages", []):
                if L.startswith("Program data:"):
                    try:
                        ev = decode_full(base64.b64decode(L.split("Program data: ")[1]), mint_b)
                    except Exception:
                        ev = None
                    if ev:
                        break
            if not ev:
                continue
            spot = ev["vSol"] / ev["vTok"]
            prices.append(spot)
            ks.append(ev["vSol"] * ev["vTok"])
            if ev["tok"] > 0 and ev["sol"] > 0:
                ratios.append((ev["sol"] / ev["tok"]) / spot)
            # on-chain real token reserve of bonding curve (only count if resolved >0)
            real_tok = None
            for b in tx["meta"].get("postTokenBalances", []):
                if b.get("owner") == bc and b.get("mint") == r["mint"]:
                    real_tok = float(b["uiTokenAmount"]["uiAmount"] or 0)
            if real_tok and real_tok > 0:
                offsets.append(ev["vTok"] - real_tok)
        k_cv = cv(ks)
        off_cv = cv(offsets)
        rmed = st.median(ratios) if ratios else float("nan")
        overall_ratio += ratios
        sane = all(1e-12 < p < 1e-3 for p in prices)
        ok = (off_cv < 0.02 and abs(rmed - 1) < 0.15 and sane)  # k_cv is info-only
        fails += 0 if ok else 1
        print("%s n=%2d  exec/spot_med=%.3f  vTok-offset_CV=%.4f (n_off=%d)  k_CV=%.3f[info]  price[%.2e..%.2e]  %s" % (
            r["mint"][:8], len(prices), rmed, off_cv, len(offsets), k_cv,
            min(prices) if prices else -1, max(prices) if prices else -1,
            "PASS" if ok else "FAIL"))
    print("\nOVERALL exec/spot: median=%.3f p10=%.3f p90=%.3f  (1.0 = perfect)  coins_failed=%d" % (
        st.median(overall_ratio), sorted(overall_ratio)[len(overall_ratio)//10],
        sorted(overall_ratio)[9*len(overall_ratio)//10], fails))


if __name__ == "__main__":
    main()
