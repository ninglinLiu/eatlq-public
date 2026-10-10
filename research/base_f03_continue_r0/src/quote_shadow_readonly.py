"""Read-only historical eth_call quotes for the 4 frozen F03 signals. No signer."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from ch_readonly import query_tsv
from safety import research_entry_guard

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

POOLS = {
    "0x82321f3beb69f503380d6b233857d5c43562e2d0": {
        "pair": "WETH/AERO",
        "token0": WETH,
        "token1": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
        "dec0": 18,
        "dec1": 18,
        "side": "HIGH",
        "quote_usd_via": "WETH",
    },
    "0xbe00ff35af70e8415d0eb605a286d8a45466a4c1": {
        "pair": "USDC/AERO",
        "token0": USDC,
        "token1": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
        "dec0": 6,
        "dec1": 18,
        "side": "HIGH",
        "quote_usd_via": "USDC",
    },
    "0xdfe5f275020def30993f042174fc2d335678b626": {
        "pair": "AERO/cbBTC",
        "token0": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
        "token1": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "dec0": 18,
        "dec1": 8,
        "side": "HIGH",
        "quote_usd_via": "UNSUPPORTED_NO_STABLE_LEG",
    },
    "0xe49b3f5614a926b5f468c9ee340343d1bb59c507": {
        "pair": "WETH/TSLA",
        "token0": WETH,
        "token1": "0x4409d3392019bae6b914a5d92fbff46fa2aca368",
        "dec0": 18,
        "dec1": 18,
        "side": "LOW",
        "quote_usd_via": "WETH",
    },
}


def _rpc():
    from web3 import Web3

    url = Path("/tmp/rpc_url.txt").read_text().strip().splitlines()[0].strip()
    return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 45})), url[:24] + "…"


def _call(w3, tx, block):
    blk = hex(int(block)) if isinstance(block, int) else block
    return w3.provider.make_request("eth_call", [tx, blk])


def main() -> None:
    research_entry_guard()
    OUT.mkdir(parents=True, exist_ok=True)
    pool_list = ", ".join(f"'{p}'" for p in POOLS)
    blocks_tsv = query_tsv(
        f"""
SELECT
    lower(pool_address) AS pool_address,
    max(block_number) AS last_block,
    min(block_number) AS first_block
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = 8453 AND removed = 0
  AND lower(pool_address) IN ({pool_list})
  AND block_time >= toDateTime64('2026-08-14 13:00:00', 3, 'UTC')
  AND block_time < toDateTime64('2026-08-14 14:00:00', 3, 'UTC')
GROUP BY pool_address
"""
    )
    later_tsv = query_tsv(
        f"""
SELECT
    lower(pool_address) AS pool_address,
    max(block_number) AS last_block
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = 8453 AND removed = 0
  AND lower(pool_address) IN ({pool_list})
  AND block_time >= toDateTime64('2026-08-14 17:00:00', 3, 'UTC')
  AND block_time < toDateTime64('2026-08-14 18:00:00', 3, 'UTC')
GROUP BY pool_address
"""
    )
    from io import StringIO
    import pandas as pd

    b0 = pd.read_csv(StringIO(blocks_tsv), sep="\t")
    b4 = pd.read_csv(StringIO(later_tsv), sep="\t")
    try:
        w3, url_hint = _rpc()
    except Exception as e:
        payload = {"status": "DATA_INSUFFICIENT", "reason": f"rpc_init_failed: {e}"}
        (OUT / "r0_shadow_quotes.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps(payload, indent=2))
        return

    fee_sel = w3.keccak(text="fee()")[:4].hex()
    slot0_sel = w3.keccak(text="slot0()")[:4].hex()
    qsel = w3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]

    def encode_quote(token_in, token_out, amount_in, fee):
        from eth_abi import encode

        data = qsel + encode(
            ["(address,address,uint256,uint24,uint160)"],
            [(w3.to_checksum_address(token_in), w3.to_checksum_address(token_out), int(amount_in), int(fee), 0)],
        )
        return {"to": w3.to_checksum_address(QUOTER), "data": "0x" + data.hex()}

    def decode_amount(resp):
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"])[:240], "amount_out": None}
        raw = resp.get("result")
        if not raw or raw == "0x":
            return {"ok": False, "error": "empty_result", "amount_out": None}
        try:
            from eth_abi import decode

            # QuoterV2 returns (amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)
            decoded = decode(["uint256", "uint160", "uint32", "uint256"], bytes.fromhex(raw[2:]))
            return {
                "ok": True,
                "amount_out": int(decoded[0]),
                "ticks_crossed": int(decoded[2]),
                "gas_estimate": int(decoded[3]),
            }
        except Exception:
            # QuoterV1-style single uint256
            try:
                return {"ok": True, "amount_out": int(raw, 16), "ticks_crossed": None, "gas_estimate": None}
            except Exception as e:
                return {"ok": False, "error": f"decode:{e}", "amount_out": None}

    # ETH usd via USDC/WETH 5bps if possible, else skip WETH notionals
    eth_usd = None
    usdc_weth = query_tsv(
        """
SELECT argMax(reinterpretAsInt32(reverse(unhex(right(tick_raw, 8)))), tuple(block_number, log_index)) AS close_tick
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = 8453 AND removed = 0 AND length(tick_raw) = 66
  AND lower(pool_address) = '0xd0b53d9277642d899df5c87a3966a349a798f224'
  AND block_time >= toDateTime64('2026-08-14 13:00:00', 3, 'UTC')
  AND block_time < toDateTime64('2026-08-14 14:00:00', 3, 'UTC')
"""
    )
    try:
        import math

        tick = int(usdc_weth.strip().splitlines()[-1])
        # token0 USDC / token1 WETH on this pool? verify later; price = 1.0001^tick = token1/token0
        price_t1_per_t0 = 1.0001 ** tick
        eth_usd = {"close_tick": tick, "raw_price_t1_per_t0": price_t1_per_t0, "pool": "0xd0b53d9277642d899df5c87a3966a349a798f224"}
    except Exception as e:
        eth_usd = {"error": str(e), "raw": usdc_weth[:200]}

    rows = []
    for _, br in b0.iterrows():
        pool = str(br.pool_address).lower()
        meta = POOLS[pool]
        block = int(br.last_block)
        fee_resp = _call(w3, {"to": w3.to_checksum_address(pool), "data": "0x" + fee_sel}, block)
        slot_resp = _call(w3, {"to": w3.to_checksum_address(pool), "data": "0x" + slot0_sel}, block)
        fee = None
        if "result" in fee_resp and fee_resp["result"] not in (None, "0x"):
            fee = int(fee_resp["result"], 16)
        later = b4[b4.pool_address.str.lower() == pool]
        later_block = int(later.iloc[0].last_block) if len(later) else None
        quotes = []
        if fee is None:
            rows.append({"pool": pool, "pair": meta["pair"], "status": "FEE_CALL_FAILED", "fee_resp": fee_resp, "block": block})
            continue
        sizes = [250, 500, 1000, 2000]
        for usd in sizes:
            if meta["quote_usd_via"] == "USDC":
                amt_in = usd * 10**6
                token_in, token_out = meta["token0"], meta["token1"]
                buy = decode_amount(_call(w3, encode_quote(token_in, token_out, amt_in, fee), block))
                sell_amt = buy.get("amount_out")
                sell = (
                    decode_amount(_call(w3, encode_quote(token_out, token_in, sell_amt, fee), later_block or block))
                    if sell_amt
                    else {"ok": False, "error": "no_buy_out"}
                )
                delay1 = decode_amount(_call(w3, encode_quote(token_in, token_out, amt_in, fee), block + 1))
                delay2 = decode_amount(_call(w3, encode_quote(token_in, token_out, amt_in, fee), block + 2))
                roundtrip = None
                if buy.get("ok") and sell.get("ok") and sell.get("amount_out") is not None:
                    roundtrip = (sell["amount_out"] / amt_in - 1) * 10_000
                quotes.append(
                    {
                        "usd": usd,
                        "buy_at_signal_block": buy,
                        "sell_at_plus4h_or_same": sell,
                        "buy_delay_1_block": delay1,
                        "buy_delay_2_block": delay2,
                        "roundtrip_bps_vs_usdc_in": roundtrip,
                    }
                )
            elif meta["quote_usd_via"] == "WETH":
                # without a trusted ETHUSD we still quote 0.1 WETH as a size smoke, plus attempt if eth_usd usable
                amt_in = 10**17  # 0.1 WETH
                buy = decode_amount(_call(w3, encode_quote(meta["token0"], meta["token1"], amt_in, fee), block))
                quotes.append({"usd": "UNSIZED_0.1_WETH", "buy_at_signal_block": buy, "note": "ETHUSD mapping not trusted this round"})
            else:
                quotes.append({"usd": usd, "status": "UNSUPPORTED_NO_STABLE_LEG"})
            time.sleep(0.05)
        rows.append(
            {
                "pool": pool,
                "pair": meta["pair"],
                "side": meta["side"],
                "signal_last_block": block,
                "plus4h_last_block": later_block,
                "onchain_fee": fee,
                "slot0_ok": "result" in slot_resp and bool(slot_resp.get("result")),
                "quotes": quotes,
            }
        )

    payload = {
        "status": "QUOTE_SMOKE",
        "rpc_hint": url_hint,
        "quoter": QUOTER,
        "eth_usd_probe": eth_usd,
        "rows": rows,
        "usd_capacity": "UNVERIFIED",
        "note": "eth_call only. Not a fill. AERO/cbBTC has no stable leg so USD size was not forced. WETH legs left unsized if ETHUSD tick mapping is ambiguous.",
    }
    (OUT / "r0_shadow_quotes.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "n": len(rows), "pairs": [r.get("pair") for r in rows], "fees": [r.get("onchain_fee") for r in rows]}, indent=2))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
