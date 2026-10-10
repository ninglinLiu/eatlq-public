#!/usr/bin/env python3
"""Sprint086 — Real-Cost Overlay over existing paper-shadow / dry-run CSVs.

Reads (read-only):
  - logs/sprint055_paper_shadow.csv
  - logs/sprint068/alpha_*_paper_shadow.csv
  - logs/sprint083_fee500_paper_shadow.csv (if present)

Writes (additive, never modifies sources):
  - mvp/audits/sprint086_real_cost_overlay/{source}_realcost.csv
      adds: gas_real_pct, slip_real_buy_pct, slip_real_sell_pct,
            pool_fee_pct, sell_path_ok, honeypot_flag, liquidity_depth_ok,
            net_return_{h}_real (for h in 5m..24h)
  - mvp/audits/sprint086_real_cost_overlay/{source}_realcost_summary.json
  - mvp/audits/sprint086_real_cost_overlay/overlay_readout.md
  - mvp/audits/sprint086_real_cost_overlay/decision_flags.json

Notional ladder: 10U / 25U / 50U / 100U (per sprint056b + charter §8).
Costs aligned with whitepaper §交易执行:
  - Honeypot detection via Quoter sell-sim revert OR round-trip loss >10%
  - Slippage measured at real notional vs micro baseline
  - Gas in USD / notional (NOT flat 0.2%)
  - Sell path failure treated as -100% on that trade
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS / "lib"))

from real_cost_model import (  # type: ignore  # noqa: E402
    NOTIONAL_LADDER_USD,
    RealCostModel,
    aggregate_breakdowns,
)
from v3_quoterv2_struct import JsonRpcClient  # type: ignore  # noqa: E402

EXIT_LABELS = ["5m", "10m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "24h"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint086 real-cost overlay")
    p.add_argument(
        "--out-dir",
        default="mvp/audits/sprint086_real_cost_overlay",
    )
    p.add_argument("--rpc-url", default=os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", ""))
    p.add_argument("--notional-usd", type=float, default=10.0, help="Primary notional for overlay (default 10U)")
    p.add_argument("--ladder", action="store_true", help="Also compute 25/50/100 USD ladder summaries")
    p.add_argument("--max-rows-per-source", type=int, default=500, help="Cap rows per CSV for RPC budget")
    p.add_argument(
        "--sources",
        nargs="+",
        default=[
            "logs/sprint055_paper_shadow.csv",
            "logs/sprint068/alpha_001_paper_shadow.csv",
            "logs/sprint068/alpha_005_paper_shadow.csv",
            "logs/sprint083_fee500_paper_shadow.csv",
        ],
    )
    p.add_argument("--rpc-delay", type=float, default=0.05)
    p.add_argument("--block-snapshot", action="store_true", help="Use entry_block for snapshot quotes if available")
    return p.parse_args()


def _load_env_rpc() -> str:
    env = REPO / "infra/compose/.env"
    if not env.exists():
        return ""
    for line in env.read_text().splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        if k.strip() in ("LABEL_RPC_URL", "EVM_HTTP_URL"):
            v = v.strip().strip('"').strip("'")
            if v.startswith("http"):
                return v
    return ""


def _as_float(v) -> float | None:
    if v in (None, "", "None", "\\N", "nan"):
        return None
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def overlay_row(model: RealCostModel, row: dict, notional_usd: float, use_block: bool) -> dict:
    token = row.get("token_other") or row.get("token") or ""
    fee_bps = int(_as_float(row.get("fee_bps")) or 0)
    if not token or fee_bps <= 0:
        return {
            "gas_real_pct": "", "slip_real_buy_pct": "", "slip_real_sell_pct": "",
            "pool_fee_pct": "", "sell_path_ok": "False", "honeypot_flag": "True",
            "liquidity_depth_ok": "False", "cost_total_pct": "",
            "cost_status": "missing_token_or_fee",
        }
    block = None
    if use_block:
        b = _as_float(row.get("entry_block"))
        if b and b > 0:
            block = int(b)
    cb = model.estimate(token=token, fee_bps=fee_bps, notional_usd=notional_usd, block_identifier=block)
    out = {
        "gas_real_pct": round(cb.gas_buy_pct + cb.gas_sell_pct, 6),
        "slip_real_buy_pct": round(cb.slippage_buy_pct, 6),
        "slip_real_sell_pct": round(cb.slippage_sell_pct, 6),
        "pool_fee_pct": round(cb.pool_fee_pct, 6),
        "sell_path_ok": str(cb.sell_path_ok),
        "honeypot_flag": str(cb.honeypot_flag),
        "liquidity_depth_ok": str(cb.liquidity_depth_ok),
        "cost_total_pct": round(cb.round_trip_cost_pct(), 6),
        "cost_status": cb.sell_revert_reason or "ok",
    }
    out["rpc_error"] = str(cb.rpc_error)
    # Apply to each available net_return_{h}
    import math as _math
    for h in EXIT_LABELS:
        gross_col = f"net_return_{h}"
        gross = _as_float(row.get(gross_col))
        if gross is None:
            continue
        net_real = cb.apply_to_gross_return(gross)
        if isinstance(net_real, float) and _math.isnan(net_real):
            out[f"net_return_{h}_real"] = ""  # RPC failure → leave blank
        else:
            out[f"net_return_{h}_real"] = round(net_real, 6)
    return out


def process_source(model: RealCostModel, source: Path, out_dir: Path, args) -> dict:
    if not source.exists():
        return {"source": str(source), "status": "missing", "rows": 0}
    name = source.stem
    out_csv = out_dir / f"{name}_realcost.csv"
    overlay_breakdowns: list = []
    with source.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if args.max_rows_per_source > 0 and len(rows) > args.max_rows_per_source:
        # Stratified sample: keep first, last, evenly spaced middle
        step = max(1, len(rows) // args.max_rows_per_source)
        rows = rows[::step][: args.max_rows_per_source]
    fieldnames = list(rows[0].keys()) if rows else []
    extra = [
        "gas_real_pct", "slip_real_buy_pct", "slip_real_sell_pct",
        "pool_fee_pct", "sell_path_ok", "honeypot_flag", "liquidity_depth_ok",
        "cost_total_pct", "cost_status", "rpc_error",
    ] + [f"net_return_{h}_real" for h in EXIT_LABELS]
    full_fields = fieldnames + [c for c in extra if c not in fieldnames]

    started = time.time()
    written = 0
    with out_csv.open("w", newline="", encoding="utf-8") as fout:
        writer = csv.DictWriter(fout, fieldnames=full_fields)
        writer.writeheader()
        for r in rows:
            ovl = overlay_row(model, r, args.notional_usd, args.block_snapshot)
            for k in full_fields:
                if k not in r:
                    r[k] = ovl.get(k, "")
            writer.writerow(r)
            written += 1
            # Stash breakdown for aggregate
            overlay_breakdowns.append(ovl)

    # Aggregate before/after PF at each horizon
    horizons_summary = {}
    for h in EXIT_LABELS:
        gross_col = f"net_return_{h}"
        real_col = f"net_return_{h}_real"
        gross_vals = [_as_float(r.get(gross_col)) for r in rows]
        gross_vals = [v for v in gross_vals if v is not None]
        real_vals = [_as_float(r.get(real_col)) for r in rows]
        real_vals = [v for v in real_vals if v is not None]
        horizons_summary[h] = {
            "n": len(gross_vals),
            "gross_mean": round(sum(gross_vals) / len(gross_vals), 6) if gross_vals else None,
            "gross_hit": round(sum(1 for v in gross_vals if v > 0) / len(gross_vals), 4) if gross_vals else None,
            "gross_pf": _pf(gross_vals),
            "real_mean": round(sum(real_vals) / len(real_vals), 6) if real_vals else None,
            "real_hit": round(sum(1 for v in real_vals if v > 0) / len(real_vals), 4) if real_vals else None,
            "real_pf": _pf(real_vals),
        }

    rpc_err = sum(1 for o in overlay_breakdowns if o.get("rpc_error") == "True")
    # Exclude rpc_error rows from sell-fail / honeypot rates to avoid contamination
    decidable = max(written - rpc_err, 1)
    cost_rates = {
        "rows_processed": written,
        "rpc_error_rows": rpc_err,
        "rpc_error_rate": round(rpc_err / max(written, 1), 4),
        "sell_fail_rate_decidable": round(sum(1 for o in overlay_breakdowns if o.get("sell_path_ok") == "False" and o.get("rpc_error") != "True") / decidable, 4),
        "honeypot_rate_decidable": round(sum(1 for o in overlay_breakdowns if o.get("honeypot_flag") == "True") / decidable, 4),
        "liquidity_depth_fail_rate": round(sum(1 for o in overlay_breakdowns if o.get("liquidity_depth_ok") == "False" and o.get("rpc_error") != "True") / decidable, 4),
        "median_cost_total_pct": _median([
            o["cost_total_pct"] for o in overlay_breakdowns
            if isinstance(o.get("cost_total_pct"), (int, float))
        ]),
        "max_cost_total_pct": _maxor(
            [o["cost_total_pct"] for o in overlay_breakdowns if isinstance(o.get("cost_total_pct"), (int, float))]
        ),
    }

    return {
        "source": str(source),
        "status": "ok",
        "out_csv": str(out_csv.relative_to(REPO)),
        "elapsed_sec": round(time.time() - started, 1),
        "rows": written,
        "horizons_before_after": horizons_summary,
        "cost_rates": cost_rates,
        "notional_usd": args.notional_usd,
    }


def _pf(returns: list[float]) -> float | None:
    if not returns:
        return None
    wins = sum(r for r in returns if r > 0)
    losses = abs(sum(r for r in returns if r < 0))
    if losses > 0:
        return round(wins / losses, 4)
    if wins > 0:
        return float("inf")
    return None


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return round(s[len(s) // 2], 6)


def _maxor(vals: list[float]) -> float | None:
    if not vals:
        return None
    return round(max(vals), 6)


def write_readout(out_dir: Path, summaries: list[dict], args) -> None:
    decision_flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "alpha_id": "AR-SP086-REAL-COST-OVERLAY",
        "family": "F4",
        "notional_usd": args.notional_usd,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": False,
        "OVERLAY_DONE": True,
        "sources": summaries,
        "whitepaper_alignment": {
            "honeypot_via_quoter_sell_sim": True,
            "slippage_at_real_notional": True,
            "gas_in_usd_not_flat_pct": True,
            "sell_path_failure_treated_as_full_loss": True,
            "liquidity_depth_20x_gate": True,
        },
        "next_step": (
            "Review per-horizon gross_pf vs real_pf. "
            "Any alpha with real_pf < 1 should be demoted regardless of gross_pf. "
            "Do NOT modify sprint055/068 cron; overlay is post-processing only."
        ),
    }
    (out_dir / "decision_flags.json").write_text(json.dumps(decision_flags, indent=2) + "\n")

    md = [
        "# Sprint086 — Real-Cost Overlay Readout",
        "",
        f"Generated: {decision_flags['generated_at_utc']}  ",
        f"Notional: **${args.notional_usd:.0f}U**  ",
        f"Sources: {len(summaries)} CSVs scanned",
        "",
        "## Methodology",
        "",
        "Replays each row of an existing paper-shadow CSV through "
        "[`scripts/lib/real_cost_model.py`](../../scripts/lib/real_cost_model.py):",
        "",
        "- Buy quote at real notional (10U default) — measures real-size slippage",
        "- Sell quote on bought tokens — measures sell path + sell slippage",
        "- Round-trip loss > 10% → `honeypot_flag = true`",
        "- Sell-quote revert → `sell_path_ok = false`, return = -100%",
        "- Gas in USD / notional (Base L2 ~$0.05 buy + $0.05 sell)",
        "- Liquidity depth heuristic via implied round-trip-loss vs pool fee",
        "",
        "**Original CSVs are not modified.** Output goes to `*_realcost.csv` with additive columns.",
        "",
        "## Per-source headline (30m horizon)",
        "",
        "| source | rows | gross_pf | real_pf | gross_hit | real_hit | sell_fail | honeypot |",
        "|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for s in summaries:
        if s.get("status") != "ok":
            md.append(f"| {Path(s['source']).name} | (missing) | – | – | – | – | – | – |")
            continue
        h30 = s["horizons_before_after"].get("30m", {})
        cr = s["cost_rates"]
        md.append(
            f"| {Path(s['source']).name} | {s['rows']} | "
            f"{h30.get('gross_pf')} | {h30.get('real_pf')} | "
            f"{h30.get('gross_hit')} | {h30.get('real_hit')} | "
            f"{cr.get('sell_fail_rate_decidable')} | {cr.get('honeypot_rate_decidable')} |"
        )
    md.append("")
    md.append("## Per-source detail")
    md.append("")
    for s in summaries:
        md.append(f"### {Path(s['source']).name}")
        md.append("")
        if s.get("status") != "ok":
            md.append(f"- status: `{s.get('status')}`")
            md.append("")
            continue
        md.append(f"- rows: {s['rows']}  ")
        md.append(f"- out: `{s['out_csv']}`  ")
        md.append(f"- median round-trip cost: **{s['cost_rates']['median_cost_total_pct']}**  ")
        md.append(f"- max round-trip cost: {s['cost_rates']['max_cost_total_pct']}  ")
        md.append(f"- RPC error rate (excluded): {s['cost_rates']['rpc_error_rate']} ({s['cost_rates']['rpc_error_rows']} rows)  ")
        md.append(f"- sell-fail rate (decidable): **{s['cost_rates']['sell_fail_rate_decidable']}**  ")
        md.append(f"- honeypot rate (decidable): **{s['cost_rates']['honeypot_rate_decidable']}**  ")
        md.append(f"- liquidity-depth fail rate: {s['cost_rates']['liquidity_depth_fail_rate']}  ")
        md.append("")
        md.append("| horizon | n | gross_pf | real_pf | gross_mean | real_mean | gross_hit | real_hit |")
        md.append("|---|--:|--:|--:|--:|--:|--:|--:|")
        for h in EXIT_LABELS:
            x = s["horizons_before_after"].get(h, {})
            if not x or x.get("n", 0) == 0:
                continue
            md.append(
                f"| {h} | {x['n']} | {x['gross_pf']} | {x['real_pf']} | "
                f"{x['gross_mean']} | {x['real_mean']} | {x['gross_hit']} | {x['real_hit']} |"
            )
        md.append("")
    md.append("## Decision flags")
    md.append("")
    md.append("```json")
    md.append(json.dumps(decision_flags, indent=2))
    md.append("```")
    (out_dir / "overlay_readout.md").write_text("\n".join(md) + "\n")


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    rpc_url = args.rpc_url or _load_env_rpc()
    if not rpc_url:
        print("ERROR: no RPC URL (set LABEL_RPC_URL / EVM_HTTP_URL or infra/compose/.env)", file=sys.stderr)
        return 2
    rpc = JsonRpcClient(rpc_url, delay_s=args.rpc_delay)
    # Public Base RPC fallback if primary returns SSL/HTTP errors
    fallback_rpc = JsonRpcClient("https://mainnet.base.org", delay_s=max(args.rpc_delay, 0.1))
    model = RealCostModel(rpc=rpc, fallback_rpc=fallback_rpc)

    summaries = []
    for s in args.sources:
        path = REPO / s
        print(f"[overlay] {s}", flush=True)
        try:
            summary = process_source(model, path, out_dir, args)
        except Exception as exc:
            summary = {"source": s, "status": f"error:{type(exc).__name__}", "error": str(exc)[:300]}
        summaries.append(summary)
        print(f"  -> {summary.get('status')} rows={summary.get('rows', 0)} elapsed={summary.get('elapsed_sec', 0)}s", flush=True)

    write_readout(out_dir, summaries, args)
    print(json.dumps({"sources": len(summaries), "out_dir": args.out_dir}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
