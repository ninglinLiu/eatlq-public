#!/usr/bin/env python3
"""Fork-replay backtest engine (READ-ONLY, no broadcast).

For each historical trade, fork Base mainnet at the entry block in a local anvil
node, then have a BRAND-NEW EOA do a REAL buy -> hold -> REAL sell against the
real pool/router. Unlike state-override sell-sim (which writes the balance slot
and thus bypasses a token's sell-authorization logic), this exercises the true
transfer path, so it catches "buy succeeds but sell reverts" honeypots that
QuoterV2 cannot see.

Nothing is broadcast to the real chain: anvil runs a local fork; all txs live
and die inside the fork. Usage:
    python3 fork_replay_backtest.py --selftest          # 2 PARADAENGI pools
    python3 fork_replay_backtest.py --set 131           # full set
"""
from __future__ import annotations
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from typing import Optional

from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
ANVIL = os.path.expanduser("~/.foundry/bin/anvil")
ARCHIVE_RPC = os.environ.get("FORK_ARCHIVE_RPC", "")
if not ARCHIVE_RPC:
    raise SystemExit("FORK_ARCHIVE_RPC is not set — export an archive RPC endpoint (the token must never be committed)")
WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")  # SwapRouter02

# anvil default account[0]
ACCT = Web3.to_checksum_address("0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266")
PK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

WETH_ABI = json.loads('[{"name":"deposit","inputs":[],"outputs":[],"stateMutability":"payable","type":"function"},{"name":"approve","inputs":[{"name":"s","type":"address"},{"name":"a","type":"uint256"}],"outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"name":"balanceOf","inputs":[{"name":"o","type":"address"}],"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
ERC20_ABI = json.loads('[{"name":"approve","inputs":[{"name":"s","type":"address"},{"name":"a","type":"uint256"}],"outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"name":"balanceOf","inputs":[{"name":"o","type":"address"}],"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
ROUTER_ABI = json.loads('[{"name":"exactInputSingle","inputs":[{"components":[{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},{"name":"fee","type":"uint24"},{"name":"recipient","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"amountOutMinimum","type":"uint256"},{"name":"sqrtPriceLimitX96","type":"uint160"}],"name":"params","type":"tuple"}],"outputs":[{"name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"}]')


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_anvil_once(block: int, port: int, wait_s: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [ANVIL, "--fork-url", ARCHIVE_RPC, "--fork-block-number", str(block),
         "--port", str(port), "--silent", "--no-rate-limit"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}", request_kwargs={"timeout": 60}))
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            if w3.is_connected() and w3.eth.block_number > 0:
                return proc
        except Exception:
            pass
        time.sleep(0.5)
    try:
        proc.terminate(); proc.wait(timeout=5)
    except Exception:
        proc.kill()
    return None


def start_anvil(block: int, port: int, retries: int = 2, wait_s: int = 90) -> subprocess.Popen:
    for attempt in range(retries + 1):
        proc = _start_anvil_once(block, port, wait_s)
        if proc is not None:
            return proc
        time.sleep(2 + attempt * 3)  # back off; relieve archive contention
    raise RuntimeError("anvil failed to start")


def _send(w3, fn, value=0, gas=1_500_000):
    tx = fn.build_transaction({
        "from": ACCT, "nonce": w3.eth.get_transaction_count(ACCT),
        "value": value, "gas": gas, "gasPrice": w3.eth.gas_price, "chainId": w3.eth.chain_id,
    })
    signed = w3.eth.account.sign_transaction(tx, PK)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(h, timeout=60)


def replay_one(port: int, token: str, fee: int, spend_eth: float = 0.01,
               pool: Optional[str] = None) -> dict:
    w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}", request_kwargs={"timeout": 60}))
    token = Web3.to_checksum_address(token)
    weth = w3.eth.contract(address=WETH, abi=WETH_ABI)
    tok = w3.eth.contract(address=token, abi=ERC20_ABI)
    router = w3.eth.contract(address=ROUTER, abi=ROUTER_ABI)
    spend = int(spend_eth * 1e18)
    out = {"token": token, "fee": fee, "buy_ok": False, "sell_ok": False,
           "tokens_bought": 0, "weth_back": 0, "buy_gas": 0, "sell_gas": 0,
           "slippage": None, "error": None, "pool_weth": None}
    # pool WETH depth (drain-vs-toggle discriminator) - read before any tx
    if pool:
        try:
            out["pool_weth"] = weth.functions.balanceOf(Web3.to_checksum_address(pool)).call()
        except Exception:
            pass
    try:
        _send(w3, weth.functions.deposit(), value=spend)
        _send(w3, weth.functions.approve(ROUTER, spend))
        # BUY: WETH -> token
        try:
            r = _send(w3, router.functions.exactInputSingle((WETH, token, fee, ACCT, spend, 0, 0)))
            out["buy_gas"] = r["gasUsed"]
            bought = tok.functions.balanceOf(ACCT).call()
            out["tokens_bought"] = bought
            out["buy_ok"] = r["status"] == 1 and bought > 0
        except Exception as e:
            out["error"] = f"buy:{type(e).__name__}:{str(e)[:80]}"
            return out
        if not out["buy_ok"]:
            out["error"] = "buy_zero_tokens"
            return out
        # SELL: token -> WETH (the real authorization path)
        try:
            _send(w3, tok.functions.approve(ROUTER, bought))
            weth_before = weth.functions.balanceOf(ACCT).call()
            r2 = _send(w3, router.functions.exactInputSingle((token, WETH, fee, ACCT, bought, 0, 0)))
            out["sell_gas"] = r2["gasUsed"]
            weth_after = weth.functions.balanceOf(ACCT).call()
            got = weth_after - weth_before
            out["weth_back"] = got
            out["sell_ok"] = r2["status"] == 1 and got > 0
            if out["sell_ok"]:
                out["slippage"] = got / spend - 1.0
        except Exception as e:
            out["sell_ok"] = False
            out["error"] = f"sell_revert:{str(e)[:80]}"
    except Exception as e:
        out["error"] = f"setup:{type(e).__name__}:{str(e)[:80]}"
    return out


def replay_at_block(block: int, token: str, fee: int, spend_eth: float = 0.01,
                    pool: Optional[str] = None) -> dict:
    port = _free_port()
    proc = start_anvil(block, port)
    try:
        return replay_one(port, token, fee, spend_eth, pool=pool)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


# ---------------------------------------------------------------------------
# B: multi-point per-block ground-truth labeler (clean/instant_hp/delayed_rug/dead)
# ---------------------------------------------------------------------------
# Default sample offsets (in blocks) across the ~4h (7200-block) hold window.
# +30 covers PARADAENGI-class fast flips (~16-23 blocks); coarse tail covers slow rugs.
HOLD_BLOCKS = 7200
DEFAULT_OFFSETS = [0, 30, 180, 900, 3600, 7200]
# Lighter grid for the 644-pool population scan: entry, fast-toggle (PARADAENGI
# class ~16-23 blocks), ~10min, ~1h, exit. Keeps cost ~5 forks/clean pool.
UNIVERSE_OFFSETS = [0, 30, 300, 1800, 7200]


def _tradeable(rep: dict) -> bool:
    return bool(rep["buy_ok"] and rep["sell_ok"])


def label_pool(entry_block: int, token: str, fee: int, *, offsets=None,
               spend_eth: float = 0.01, refine: bool = False, pool: Optional[str] = None) -> dict:
    """Multi-point fork-replay across the hold window.

    Labels:
      dead        - cannot even buy at entry (no liquidity / unbuyable) -> strategy
                    never holds it; not a honeypot risk to us.
      instant_hp  - buy ok at entry but sell reverts at entry (classic honeypot).
      delayed_rug - sellable at entry, becomes untradeable at a later block.
                    mechanism sub-field:
                      lp_pull         - pool WETH depth collapsed (deployer removed
                                        liquidity). This is the PARADAENGI class.
                      transfer_toggle - depth still present but sell reverts / buy
                                        zero (token contract blocks transfers).
      clean       - tradeable at every sampled point through the hold window.
    """
    offsets = offsets if offsets is not None else DEFAULT_OFFSETS
    scans = []
    # 1) entry point
    e = replay_at_block(entry_block, token, fee, spend_eth, pool=pool)
    entry_weth = e.get("pool_weth")
    scans.append({"offset": 0, "block": entry_block, "buy_ok": e["buy_ok"],
                  "sell_ok": e["sell_ok"], "error": e["error"], "pool_weth": entry_weth})
    if not e["buy_ok"]:
        return {"label": "dead", "flip_block": None, "entry_block": entry_block,
                "scans": scans, "note": e["error"]}
    if not e["sell_ok"]:
        return {"label": "instant_hp", "flip_block": entry_block, "entry_block": entry_block,
                "scans": scans, "note": e["error"]}
    # 2) later points; stop at first untradeable
    last_clean = entry_block
    for off in offsets:
        if off == 0:
            continue
        blk = entry_block + off
        r = replay_at_block(blk, token, fee, spend_eth, pool=pool)
        later_weth = r.get("pool_weth")
        scans.append({"offset": off, "block": blk, "buy_ok": r["buy_ok"],
                      "sell_ok": r["sell_ok"], "error": r["error"], "pool_weth": later_weth})
        if not _tradeable(r):
            flip = blk
            if refine and blk - last_clean > 2:
                flip = _bisect_flip(last_clean, blk, token, fee, spend_eth, pool=pool)
            # mechanism: LP removal (depth collapsed) vs transfer toggle (depth kept).
            depth_kept = (entry_weth and later_weth is not None
                          and later_weth >= 0.5 * entry_weth)
            sell_revert = r["buy_ok"] and not r["sell_ok"]
            mechanism = "transfer_toggle" if (depth_kept or sell_revert) else "lp_pull"
            return {"label": "delayed_rug", "mechanism": mechanism, "flip_block": flip,
                    "flip_interval": [last_clean, blk], "entry_block": entry_block,
                    "scans": scans, "note": r["error"], "entry_pool_weth": entry_weth,
                    "flip_pool_weth": later_weth, "flip_offset": off}
        last_clean = blk
    return {"label": "clean", "flip_block": None, "entry_block": entry_block, "scans": scans}


def _bisect_flip(lo: int, hi: int, token: str, fee: int, spend_eth: float,
                 pool: Optional[str] = None) -> int:
    """Binary-search the first block where the pool becomes untradeable."""
    while hi - lo > 1:
        mid = (lo + hi) // 2
        r = replay_at_block(mid, token, fee, spend_eth, pool=pool)
        if _tradeable(r):
            lo = mid
        else:
            hi = mid
    return hi


def selftest():
    # Each case: (name, entry_block, untradeable_block, token, fee, pool).
    # Proven by direct archive read: sellable at entry; ~40s later the deployer
    # removed ALL liquidity (pool WETH 4.06 -> 0) => delayed_rug / lp_pull.
    cases = [
        ("PARADAENGI#1", 47699292, 47699312, "0x6c8a7e7f4574dca6f0e5ca8e6772d27cc53ead38", 100,
         "0x7f72018C642630a998e9d63b87d356D7FC63D93D"),
        ("PARADAENGI#2", 47700474, 47700490, "0x56c6ee3498ca94b7cd2fc363e465b75828f12738", 100,
         "0x2fd5596d9C57d7d8670202C204f56B3e166b5160"),
    ]
    print("#### FORK-REPLAY MULTI-POINT SELF-TEST (delayed rug; mechanism=lp_pull) ####")
    print(f"archive={ARCHIVE_RPC[:48]}...")
    results = []
    ok_all = True
    for name, eblk, ublk, tok, fee, pool in cases:
        lab = label_pool(eblk, tok, fee, refine=True, pool=pool)
        results.append({"name": name, **lab})
        print(f"\n[{name}] entry_block={eblk} pool={pool[:12]}")
        for s in lab["scans"]:
            pw = s.get("pool_weth")
            print(f"  +{s['offset']:>5} block={s['block']}: buy_ok={s['buy_ok']} "
                  f"sell_ok={s['sell_ok']} poolWETH={round(pw/1e18,3) if pw else pw}")
        print(f"  LABEL={lab['label']} mechanism={lab.get('mechanism')} "
              f"flip_block={lab['flip_block']} (known ~{ublk})")
        case_ok = (lab["label"] == "delayed_rug" and lab.get("mechanism") == "lp_pull"
                   and lab["flip_block"] is not None and abs(lab["flip_block"] - ublk) <= 30)
        ok_all = ok_all and case_ok
        print(f"  VERDICT: {'delayed_rug/lp_pull reproduced + flip located' if case_ok else 'NOT reproduced'}")
    json.dump(results, open(os.path.join(HERE, "_fork_replay_selftest.json"), "w"), indent=2)
    print(f"\nSELF-TEST {'PASS' if ok_all else 'FAIL'}: both = delayed_rug/lp_pull w/ flip near known = {ok_all}")
    print("=> mechanism is delayed LIQUIDITY REMOVAL, not a transfer toggle; entry-instant")
    print("   checks cannot catch it, and bytecode capability analysis would miss LP-pull.")
    return 0 if ok_all else 1


def run_universe(set_file: str, out_file: str, shard: int = 0, nshards: int = 1,
                 spend_eth: float = 0.01, refine: bool = False) -> dict:
    """Label a whole candidate-universe file via multi-point fork-replay.

    Input rows: {pool, token, entry_block, fee}. Sharded for parallelism
    (shard i handles rows where index % nshards == shard). Checkpoints per shard.
    """
    rows = json.load(open(set_file))
    rows = [r for i, r in enumerate(rows) if i % nshards == shard]
    done = {}
    if os.path.exists(out_file):
        try:
            for r in json.load(open(out_file)).get("pools", []):
                done[r["pool"]] = r
        except Exception:
            pass
    results = []
    n = len(rows)
    for i, row in enumerate(rows):
        pool = row["pool"]
        if pool in done:
            results.append(done[pool]); continue
        tok = row.get("token"); fee = row.get("fee"); eb = row.get("entry_block")
        rec = {"pool": pool, "token": tok, "fee": fee, "entry_block": eb}
        if not tok or not fee or not eb:
            rec.update({"label": "skip", "note": "missing_token_fee_or_block"})
            results.append(rec); continue
        t0 = time.time()
        try:
            lab = label_pool(int(eb), tok, int(fee), spend_eth=spend_eth, refine=refine,
                             offsets=UNIVERSE_OFFSETS, pool=pool)
            rec.update({"label": lab["label"], "mechanism": lab.get("mechanism"),
                        "flip_block": lab["flip_block"],
                        "flip_interval": lab.get("flip_interval"), "n_scans": len(lab["scans"]),
                        "flip_offset": lab.get("flip_offset"),
                        "entry_pool_weth": lab.get("entry_pool_weth"),
                        "flip_pool_weth": lab.get("flip_pool_weth"), "note": lab.get("note")})
        except Exception as e:
            rec.update({"label": "error", "note": f"{type(e).__name__}:{str(e)[:60]}"})
        rec["elapsed_s"] = round(time.time() - t0, 1)
        results.append(rec)
        json.dump({"pools": results}, open(out_file, "w"), indent=2)
        if (i + 1) % 5 == 0 or rec["label"] in ("instant_hp", "delayed_rug"):
            from collections import Counter
            c = Counter(r["label"] for r in results)
            print(f"  [shard{shard} {i+1}/{n}] {pool[:12]} -> {rec['label']} | {dict(c)}", flush=True)
    from collections import Counter
    counts = Counter(r["label"] for r in results)
    json.dump({"summary": dict(counts), "pools": results}, open(out_file, "w"), indent=2)
    print(f"\n=== shard {shard} DONE: {dict(counts)} ===")
    return dict(counts)


def run_set(set_file: str, out_file: str, limit: Optional[int] = None,
            spend_eth: float = 0.01) -> dict:
    """Fork at each trade's backtest EXIT block (exb), do a REAL buy+sell there.

    Compares to the QuoterV2 net the backtest used. A trade where QuoterV2 said
    survivable (net > -0.5) but the real buy+sell at exb fails/zeros = a honeypot
    the old (QuoterV2) ruler missed. No state-override, no broadcast.
    """
    rows = json.load(open(set_file))
    if limit:
        rows = rows[:limit]
    results = []
    # resume support
    done = {}
    if os.path.exists(out_file):
        try:
            for r in json.load(open(out_file)).get("trades", []):
                done[r["pool"]] = r
        except Exception:
            pass
    n = len(rows)
    for i, row in enumerate(rows):
        pool = row["pool"]
        if pool in done:
            results.append(done[pool]); continue
        exb = row.get("exb"); fee = row.get("fee"); tok = row.get("token")
        qv2 = row.get("qv2_net")
        rec = {"pool": pool, "token": tok, "exb": exb, "qv2_net": qv2, "fee": fee}
        if not exb or not fee or not tok:
            rec.update({"skip": "no_exb_or_fee"}); results.append(rec)
            continue
        t0 = time.time()
        try:
            rep = replay_at_block(int(exb), tok, int(fee), spend_eth)
        except Exception as e:
            rec.update({"error": f"replay_fail:{type(e).__name__}:{str(e)[:60]}"})
            results.append(rec); continue
        tradeable = bool(rep["buy_ok"] and rep["sell_ok"])
        rec.update({
            "real_buy_ok": rep["buy_ok"], "real_sell_ok": rep["sell_ok"],
            "real_tradeable_at_exit": tradeable, "real_slippage": rep["slippage"],
            "fork_error": rep["error"], "elapsed_s": round(time.time() - t0, 1),
            # QuoterV2 said survivable but real trade at exit impossible:
            "qv2_missed_honeypot": bool((qv2 is not None and qv2 > -0.5) and not tradeable),
        })
        results.append(rec)
        if (i + 1) % 5 == 0 or rec.get("qv2_missed_honeypot"):
            json.dump({"trades": results}, open(out_file, "w"), indent=2)
            print(f"  [{i+1}/{n}] {pool[:12]} qv2={qv2} tradeable@exit={tradeable} "
                  f"missed={rec.get('qv2_missed_honeypot')}", flush=True)
    # summary
    valid = [r for r in results if "real_tradeable_at_exit" in r]
    missed = [r for r in valid if r.get("qv2_missed_honeypot")]
    summary = {
        "set_file": set_file, "n_total": len(results), "n_replayed": len(valid),
        "n_qv2_survivable": sum(1 for r in results if (r.get("qv2_net") or -1) > -0.5),
        "n_real_untradeable_at_exit": sum(1 for r in valid if not r["real_tradeable_at_exit"]),
        "n_qv2_missed_honeypot": len(missed),
        "missed_pools": [r["pool"] for r in missed],
    }
    json.dump({"summary": summary, "trades": results}, open(out_file, "w"), indent=2)
    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        if k != "missed_pools":
            print(f"  {k}: {v}")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--set", dest="set_file", help="path to *_blocks.json (exit-block mode)")
    ap.add_argument("--universe", dest="uni_file", help="path to universe *_blocks.json (multi-point label mode)")
    ap.add_argument("--out", dest="out_file", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    ap.add_argument("--refine", action="store_true")
    ap.add_argument("--spend-eth", type=float, default=0.01)
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.uni_file:
        out = args.out_file or args.uni_file.replace("_blocks.json", f"_labeled_s{args.shard}.json")
        run_universe(args.uni_file, out, shard=args.shard, nshards=args.nshards,
                     spend_eth=args.spend_eth, refine=args.refine)
        return 0
    if args.set_file:
        out = args.out_file or args.set_file.replace("_blocks.json", "_forkreplay.json")
        run_set(args.set_file, out, limit=args.limit, spend_eth=args.spend_eth)
        return 0
    print("use --selftest | --universe <file> | --set <file>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
