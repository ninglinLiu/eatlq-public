#!/usr/bin/env python3
"""Build auditable tables from the already saved Midnight reads.

Reads /root/midnight-research (previous round, not modified) and writes
CSV plus summary JSON into this project. No network.
"""
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

OLD = Path("/root/midnight-research")
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
WAD = 10**18


def max_lif(lltv: int, cursor: int) -> int:
    inner = (cursor * (WAD - lltv)) // WAD
    return (WAD * WAD) // (WAD - inner)


def iso(t: int) -> str:
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    rows = json.loads((OLD / "data/base_liquidations_flat.json").read_text())
    markets = {
        m["market_id"]: m
        for m in json.loads((OLD / "raw/markets_base_all.json").read_text())["data"]
    }
    debt_rows = json.loads((OLD / "raw/cands_alldebt_base.json").read_text())["data"]

    liq_path = DATA / "base_liquidations.csv"
    fields = [
        "chain_id", "market_id", "tx_hash", "event_id", "event_type", "block_time",
        "maturity", "seconds_after_maturity", "post_maturity", "repaid_raw",
        "seized_raw", "bad_debt_raw", "loan_symbol", "loan_decimals", "loan_token",
        "collateral", "caller", "payer", "receiver", "callback_payer", "listed",
        "borrower",
    ]
    with liq_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({
                "chain_id": 8453,
                "market_id": r["market_id"],
                "tx_hash": r["tx"],
                "event_id": r["id"],
                "event_type": r["type"],
                "block_time": iso(r["ts"]),
                "maturity": iso(r["maturity"]),
                "seconds_after_maturity": r["ts"] - r["maturity"],
                "post_maturity": int(bool(r["post"])),
                "repaid_raw": r["repaid"],
                "seized_raw": r["seized"],
                "bad_debt_raw": r["bad"],
                "loan_symbol": r["sym"],
                "loan_decimals": "" if r["dec"] is None else r["dec"],
                "loan_token": r["loan"],
                "collateral": r["collateral"],
                "caller": r["caller"],
                "payer": r["payer"],
                "receiver": r["receiver"],
                "callback_payer": int(bool(r["callback_payer"])),
                "listed": int(bool(r["listed"])),
                "borrower": r["borrower"],
            })

    groups = defaultdict(list)
    for r in rows:
        groups[(r["market_id"], r["borrower"].lower())].append(r)
    windows = []
    for (mid, borrower), evs in groups.items():
        evs.sort(key=lambda r: (r["ts"], r["id"]))
        cur = [evs[0]]
        for r in evs[1:]:
            if r["ts"] - cur[-1]["ts"] > 6 * 3600:
                windows.append(cur)
                cur = [r]
            else:
                cur.append(r)
        windows.append(cur)

    win_path = DATA / "liquidation_windows.csv"
    with win_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "market_id", "borrower", "n_events", "n_txs", "loan_symbol",
            "repaid_raw", "first_time", "last_time", "span_seconds",
            "starts_post_maturity", "any_callback", "listed",
        ])
        w.writeheader()
        for evs in windows:
            e0 = evs[0]
            w.writerow({
                "market_id": e0["market_id"],
                "borrower": e0["borrower"],
                "n_events": len(evs),
                "n_txs": len({e["tx"] for e in evs}),
                "loan_symbol": e0["sym"],
                "repaid_raw": sum(e["repaid"] for e in evs),
                "first_time": iso(e0["ts"]),
                "last_time": iso(evs[-1]["ts"]),
                "span_seconds": evs[-1]["ts"] - e0["ts"],
                "starts_post_maturity": int(bool(e0["post"])),
                "any_callback": int(any(e["callback_payer"] for e in evs)),
                "listed": int(bool(e0["listed"])),
            })

    by_debt = defaultdict(int)
    for d in debt_rows:
        by_debt[d["market_id"]] += int(d["debt"])
    tu_path = DATA / "total_units_vs_debt.csv"
    n_gt = n_eq = n_lt = 0
    with tu_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "market_id", "loan_token", "total_units_raw", "sum_debt_raw", "gap_raw",
        ])
        w.writeheader()
        for mid, debt_sum in sorted(by_debt.items()):
            tu = int(markets[mid]["total_units"])
            gap = tu - debt_sum
            if gap > 0:
                n_gt += 1
            elif gap < 0:
                n_lt += 1
            else:
                n_eq += 1
            w.writerow({
                "market_id": mid,
                "loan_token": markets[mid]["loan_token"],
                "total_units_raw": tu,
                "sum_debt_raw": debt_sum,
                "gap_raw": gap,
            })

    oct_pos = json.loads((OLD / "data/oct30_collateral_balances.json").read_text())["rows"]
    oct_path = DATA / "oct30_positions.csv"
    with oct_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "borrower", "debt_raw", "health_factor", "weth_raw", "tenor_vault_raw", "read_status",
        ])
        w.writeheader()
        for r in oct_pos:
            cols = {c["i"]: c["bal"] for c in r["cols"]}
            status = "ok" if all(v != "ERR" for v in cols.values()) else "partial_read_error"
            w.writerow({
                "borrower": r["borrower"],
                "debt_raw": r["debt"],
                "health_factor": r["hf"],
                "weth_raw": cols.get(0, ""),
                "tenor_vault_raw": cols.get(1, ""),
                "read_status": status,
            })

    # Observed winner cash from receipt nets. Not a forecast.
    observed = [
        ("2026-08-27", "tenor_cbbtc_vault", 398259832936, 946433217, 1417, "receipt"),
        ("2026-08-27", "tenor_weth_vault", 10063886027, 23946981, 1419, "receipt"),
        ("2026-08-27", "tenor_weth_vault", 1003902468, 3106443, 1845, "receipt"),
        ("2026-09-24", "tenor_cbbtc_vault", 180476259735, 2210428, 7, "receipt"),
        ("2026-07-31", "cbbtc_dex", 10004347889, 12108082, 123, "receipt"),
    ]
    # seconds are approximate labels already used; exact block times live in the liquidation csv.
    cash = sum(x[3] for x in observed) / 1e6
    usdc = [r for r in rows if r["sym"] == "USDC"]
    repaid_post = sum(r["repaid"] for r in usdc if r["post"])
    repaid_health = sum(r["repaid"] for r in usdc if not r["post"])
    without_top = cash - 946.433217

    lif98 = max_lif(980_000_000_000_000_000, 300_000_000_000_000_000)
    lif86 = max_lif(860_000_000_000_000_000, 300_000_000_000_000_000)
    tenor_debt = 87948801026  # raw USDC
    weth_debt = 134358866826
    def upper(raw, lif, frac):
        return raw * (lif - WAD) * frac // WAD // 1_000_000

    # frac is elapsed/3600 in 1e18 for integer? keep simple float only in labeled uppers via integer seconds.
    def upper_s(raw, lif, seconds):
        seconds = min(seconds, 3600)
        return raw * (lif - WAD) * seconds // WAD // 3600 / 1e6

    cap_path = DATA / "capital_scenarios.csv"
    capitals = [500, 1000, 3000, 10000, 30000, 100000]
    with cap_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "own_capital_usd", "working_capital_usd", "ops_reserve_usd",
            "independent_events_note", "monthly_fixed_usd", "monthly_net_usd",
            "annualization", "bottleneck", "evidence",
        ])
        w.writeheader()
        for c in capitals:
            w.writerow({
                "own_capital_usd": c,
                "working_capital_usd": 0,
                "ops_reserve_usd": "20 ETH-for-gas reserve is enough for observed gas; extra capital idle",
                "independent_events_note": "599 indexed events, 306 six-hour windows, dollars in 3 maturity dates; 2026-07-09 to 2026-09-25",
                "monthly_fixed_usd": 0,
                "monthly_net_usd": "不可估",
                "annualization": "不适用",
                "bottleneck": "competition on unlisted post-maturity redemptions, not own capital",
                "evidence": "historical receipts plus one open book; capture rate unknown",
            })

    summary = {
        "liquidation_events": len(rows),
        "windows_6h": len(windows),
        "borrower_markets": len(groups),
        "usdc_repaid_post_raw": repaid_post,
        "usdc_repaid_health_raw": repaid_health,
        "post_partial": sum(1 for r in rows if r["post"] and r["type"] == "partial_liquidation"),
        "post_full": sum(1 for r in rows if r["post"] and r["type"] == "full_liquidation"),
        "bad_debt_events": sum(1 for r in rows if r["bad"] > 0),
        "callback_events": sum(1 for r in rows if r["callback_payer"]),
        "observed_winner_cash_usdc": round(cash, 6),
        "observed_winner_cash_without_largest_usdc": round(without_top, 6),
        "debt_markets": len(by_debt),
        "total_units_gt_debt": n_gt,
        "total_units_eq_debt": n_eq,
        "total_units_lt_debt": n_lt,
        "onchain_identity_market": "0x0b8fe70a0597e371bdcdbf1bbca019826f819d9a1a434e5d54a6793c3393c5ab",
        "onchain_total_units": 1112582904662105,
        "onchain_withdrawable": 995981889769698,
        "onchain_block": 52288300,
        "onchain_block_ts": 1791365947,
        "lif98": lif98,
        "lif86": lif86,
        "oct30_tenor_full_ramp_usdc": round(upper_s(tenor_debt, lif98, 3600), 2),
        "oct30_tenor_7s_usdc": round(upper_s(tenor_debt, lif98, 7), 2),
        "oct30_weth_full_ramp_oracle_usdc": round(upper_s(weth_debt, lif86, 3600), 2),
        "oct30_weth_7s_oracle_usdc": round(upper_s(weth_debt, lif86, 7), 2),
        "time_min": iso(min(r["ts"] for r in rows)),
        "time_max": iso(max(r["ts"] for r in rows)),
    }
    (DATA / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
