"""Sprint056D — Paper Shadow Daily Monitor (Sprint057S schema patch).

Reads logs/sprint055_paper_shadow.csv and writes daily JSON summary.
Sprint057S: Patched to match the 56-column schema of sprint055_fee100_paper_shadow.py.
            Fails fast on schema mismatch. Supports all 10 exit horizons.
            Paper validation horizons: 5m/10m/15m/30m (real QuoterV2).
            Research horizons: 1h/2h/4h/6h/12h/24h — DIAGNOSTIC_ONLY, never trigger live gate.
            Primary gate horizon: 30m.

Run: python3 scripts/sprint056d_paper_shadow_monitor.py [--status] [--dry-run]
     --status:   print summary and exit (no JSON write)
     --dry-run:  validate schema and report without needing live CSV data
"""
from __future__ import annotations
import csv, json, os, sys, argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
import statistics

REPO        = Path(__file__).parent.parent
SHADOW_CSV  = REPO / "logs/sprint055_paper_shadow.csv"
CRON_LOG    = REPO / "logs/sprint055_paper_shadow_cron.log"
DAILY_JSON  = REPO / "logs/sprint056_paper_shadow_daily.json"
MONITOR_LOG = REPO / "logs/sprint056_paper_shadow_daily.log"
CONTRACT_OUT = REPO / "mvp/diagnostics/sprint057s/paper_shadow_schema_contract.json"

GAS_PROXY   = 0.002   # 0.2% round-trip gas proxy

# ── Schema contract (derived from sprint055_fee100_paper_shadow.py CSV_COLS) ─────
# EXIT_HORIZONS = [300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400]
# EXIT_LABELS   = ["5m","10m","15m","30m","1h","2h","4h","6h","12h","24h"]

PAPER_HORIZONS    = ["5m", "10m", "15m", "30m"]   # real QuoterV2 — gate-eligible
RESEARCH_HORIZONS = ["1h", "2h", "4h", "6h", "12h", "24h"]  # live QuoterV2 forward only — DIAGNOSTIC
ALL_LABELS        = PAPER_HORIZONS + RESEARCH_HORIZONS
PRIMARY_HORIZON   = "30m"                          # primary acceptance gate

REQUIRED_BASE_COLS = [
    "signal_time", "pool", "token_other", "fee_bps", "weth_is_token1",
    "pool_age_sec", "onset_lag_sec",
    "activity_heat", "buy_pressure_ratio", "price_velocity_5m",
    "composite_flag",
    "entry_block", "entry_quote_ok", "entry_amount_out_tokens",
]
REQUIRED_EXIT_COLS = []
for lbl in ALL_LABELS:
    REQUIRED_EXIT_COLS += [f"exit_{lbl}_ok", f"exit_{lbl}_block",
                           f"net_return_{lbl}", f"net_return_{lbl}_ag"]
REQUIRED_META_COLS = ["gas_proxy_pct", "entry_fail_reason"]
EXPECTED_COLS = REQUIRED_BASE_COLS + REQUIRED_EXIT_COLS + REQUIRED_META_COLS
EXPECTED_COL_COUNT = len(EXPECTED_COLS)   # should be 56

# ── go/no-go thresholds ──────────────────────────────────────────────────────────
GATES = {
    "min_days":         7,
    "min_trades":       10,
    "min_win_rate":     0.50,
    "min_pf":           1.2,
    "max_cron_gap_min": 10,
}

# ── helpers ──────────────────────────────────────────────────────────────────────

def _pf(wins: list[float], losses: list[float]) -> float | None:
    g, l = sum(wins), abs(sum(losses))
    return round(g / l, 3) if l > 0 else None


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _horizon_stats(rows: list[dict], label: str) -> dict:
    """Per-horizon stats. Only rows where entry_quote_ok=True contribute."""
    ok_key  = f"exit_{label}_ok"
    ret_key = f"net_return_{label}"
    valid   = [r for r in rows if r.get("entry_quote_ok")]
    labeled = [r for r in valid if str(r.get(ok_key, "")).lower() == "true"
               and r.get(ret_key) is not None]
    rets_net = [r[ret_key] - GAS_PROXY for r in labeled]

    pos  = [r for r in rets_net if r > 0]
    neg  = [-r for r in rets_net if r <= 0]
    pf   = _pf(pos, neg)

    # pool concentration
    pool_counts: dict[str, int] = defaultdict(int)
    for r in labeled:
        pool_counts[r.get("pool", "?")] += 1
    top_pool_share = 0.0
    if labeled:
        top_pool_share = round(max(pool_counts.values()) / len(labeled), 4)

    return {
        "horizon":        label,
        "label_source":   "live_paper_quoter" if label in PAPER_HORIZONS else "live_paper_quoter_research",
        "horizon_type":   "PAPER_VALIDATION" if label in PAPER_HORIZONS else "RESEARCH_ONLY",
        "valid_count":    len(valid),
        "quote_ok_count": len(labeled),
        "hit_rate":       round(sum(1 for r in rets_net if r > 0) / len(rets_net), 4) if rets_net else 0.0,
        "profit_factor":  pf,
        "median_return":  round(statistics.median(rets_net), 6) if rets_net else None,
        "mean_return":    round(statistics.mean(rets_net), 6) if rets_net else None,
        "top_pool_share": top_pool_share,
        "gate_eligible":  label in PAPER_HORIZONS,
        "note":           ("PRIMARY_GATE" if label == PRIMARY_HORIZON
                           else "DIAGNOSTIC_ONLY — research label only; never triggers live gate"
                           if label in RESEARCH_HORIZONS else "paper_validation"),
    }


# ── schema validation ────────────────────────────────────────────────────────────

def validate_schema(csv_path: Path) -> dict:
    """Fail fast if CSV header does not match expected 56-column schema."""
    if not csv_path.exists():
        return {"valid": True, "status": "NO_CSV_YET", "message": "CSV not yet created; schema unverifiable"}

    with csv_path.open(newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return {"valid": True, "status": "EMPTY_CSV", "message": "CSV exists but has no header row"}

    actual_set   = set(header)
    expected_set = set(EXPECTED_COLS)
    missing = sorted(expected_set - actual_set)
    extra   = sorted(actual_set - expected_set)

    if missing or extra:
        return {
            "valid":   False,
            "status":  "SCHEMA_MISMATCH",
            "actual_count":   len(header),
            "expected_count": EXPECTED_COL_COUNT,
            "missing_cols":   missing,
            "extra_cols":     extra,
            "message": (f"FATAL: CSV schema mismatch — "
                        f"{len(missing)} missing, {len(extra)} extra columns. "
                        f"Fix sprint055_fee100_paper_shadow.py or this monitor."),
        }
    return {
        "valid":          True,
        "status":         "SCHEMA_OK",
        "col_count":      len(header),
        "col_order_match": header == EXPECTED_COLS,
        "message":        f"Schema valid: {len(header)} columns match expected contract.",
    }


def write_schema_contract(schema_check: dict) -> None:
    CONTRACT_OUT.parent.mkdir(parents=True, exist_ok=True)
    contract = {
        "artifact":       "sprint057s paper_shadow_schema_contract",
        "generated_at":   utcnow().isoformat(),
        "source_script":  "scripts/sprint055_fee100_paper_shadow.py",
        "monitor_script": "scripts/sprint056d_paper_shadow_monitor.py",
        "expected_col_count": EXPECTED_COL_COUNT,
        "expected_cols":  EXPECTED_COLS,
        "paper_horizons":    PAPER_HORIZONS,
        "research_horizons": RESEARCH_HORIZONS,
        "primary_gate_horizon": PRIMARY_HORIZON,
        "schema_validation": schema_check,
        "horizon_policy": {
            lbl: ("GATE_ELIGIBLE" if lbl == PRIMARY_HORIZON
                  else "PAPER_VALIDATION" if lbl in PAPER_HORIZONS
                  else "DIAGNOSTIC_ONLY")
            for lbl in ALL_LABELS
        },
    }
    CONTRACT_OUT.write_text(json.dumps(contract, indent=2))


# ── cron health check ─────────────────────────────────────────────────────────────

def check_cron_health() -> dict:
    if not CRON_LOG.exists():
        return {"ok": False, "reason": "cron_log_missing", "errors_24h": 0}

    now   = utcnow()
    lines = CRON_LOG.read_text(errors="replace").splitlines()
    errors_24h = sum(1 for l in lines if "ERROR" in l or "Traceback" in l)

    log_mtime   = CRON_LOG.stat().st_mtime
    log_age_min = (now.timestamp() - log_mtime) / 60
    cron_ok     = log_age_min < GATES["max_cron_gap_min"]

    csv_age_min = None
    if SHADOW_CSV.exists():
        csv_age_min = round((now.timestamp() - SHADOW_CSV.stat().st_mtime) / 60, 1)

    return {
        "ok":          cron_ok,
        "log_age_min": round(log_age_min, 1),
        "csv_age_min": csv_age_min,
        "errors_24h":  errors_24h,
        "reason":      "ok" if cron_ok else f"cron_log_stale_{log_age_min:.0f}min",
    }


# ── load shadow rows ──────────────────────────────────────────────────────────────

def load_shadow() -> list[dict]:
    if not SHADOW_CSV.exists():
        return []
    rows = []
    with SHADOW_CSV.open(newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                parsed: dict = {
                    "signal_time":       r.get("signal_time", ""),
                    "pool":              r.get("pool", "?"),
                    "token_other":       r.get("token_other", ""),
                    "fee_bps":           int(r["fee_bps"]) if r.get("fee_bps") else None,
                    "composite_flag":    str(r.get("composite_flag", "False")).lower() == "true",
                    "entry_quote_ok":    str(r.get("entry_quote_ok", "False")).lower() == "true",
                    "activity_heat":     float(r["activity_heat"]) if r.get("activity_heat") else None,
                    "buy_pressure_ratio": float(r["buy_pressure_ratio"]) if r.get("buy_pressure_ratio") else None,
                }
                # parse signal_time to timestamp for date grouping
                if parsed["signal_time"]:
                    try:
                        parsed["ts"] = datetime.fromisoformat(
                            parsed["signal_time"].replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        try:
                            parsed["ts"] = float(parsed["signal_time"])
                        except ValueError:
                            parsed["ts"] = None
                else:
                    parsed["ts"] = None

                # parse per-horizon exit data
                for lbl in ALL_LABELS:
                    ok_key  = f"exit_{lbl}_ok"
                    ret_key = f"net_return_{lbl}"
                    parsed[ok_key] = str(r.get(ok_key, "")).lower() == "true"
                    raw = r.get(ret_key, "")
                    parsed[ret_key] = float(raw) if raw not in ("", "None", None) else None

                rows.append(parsed)
            except (ValueError, KeyError):
                pass
    return rows


# ── daily summary ─────────────────────────────────────────────────────────────────

def build_daily_summary(rows: list[dict]) -> dict:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("ts"):
            d = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
            by_date[d].append(r)

    daily: list[dict] = []
    for d in sorted(by_date):
        day_rows = by_date[d]
        # per-horizon stats for primary only in daily breakdown (full stats in cumulative)
        ph_stats = _horizon_stats(day_rows, PRIMARY_HORIZON)
        daily.append({
            "date":    d,
            "signals": len(day_rows),
            f"{PRIMARY_HORIZON}_labeled":  ph_stats["quote_ok_count"],
            f"{PRIMARY_HORIZON}_hit_rate": ph_stats["hit_rate"],
            f"{PRIMARY_HORIZON}_pf":       ph_stats["profit_factor"],
            f"{PRIMARY_HORIZON}_mean_net": ph_stats["mean_return"],
        })
    return {"daily": daily}


# ── cumulative stats ──────────────────────────────────────────────────────────────

def build_cumulative(rows: list[dict]) -> dict:
    dates = sorted({
        datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        for r in rows if r.get("ts")
    })
    per_horizon = {lbl: _horizon_stats(rows, lbl) for lbl in ALL_LABELS}
    primary     = per_horizon[PRIMARY_HORIZON]

    return {
        "total_signals": len(rows),
        "n_days":        len(dates),
        "first_day":     dates[0] if dates else None,
        "last_day":      dates[-1] if dates else None,
        "primary_horizon": PRIMARY_HORIZON,
        "primary_stats": primary,
        "per_horizon":   per_horizon,
    }


# ── go/no-go evaluation ───────────────────────────────────────────────────────────

def evaluate_gates(cum: dict, cron: dict) -> dict:
    primary = cum.get("primary_stats", {})
    results = {
        "min_days":     cum.get("n_days", 0) >= GATES["min_days"],
        "min_trades":   primary.get("quote_ok_count", 0) >= GATES["min_trades"],
        "min_win_rate": (primary.get("hit_rate") or 0) >= GATES["min_win_rate"],
        "min_pf":       (primary.get("profit_factor") or 0) >= GATES["min_pf"],
        "cron_healthy": cron.get("ok", False),
    }
    all_pass = all(results.values())
    results["all_pass"] = all_pass
    results["verdict"]  = "PAPER_GO" if all_pass else "PAPER_NO_GO"
    results["live_verdict"] = "NO_GO"  # live always NO_GO until explicit human approval
    results["note"] = ("Research horizons (1h+) are DIAGNOSTIC_ONLY and never contribute to this gate. "
                       "Gate is based on 30m real QuoterV2 paper returns only.")
    return results


# ── main ─────────────────────────────────────────────────────────────────────────

def main(status_only: bool = False, dry_run: bool = False) -> None:
    now_str = utcnow().isoformat()
    print(f"[{now_str}] Sprint056D paper shadow monitor (Sprint057S schema patch)")

    # Step 1: schema validation — FAIL FAST
    schema_check = validate_schema(SHADOW_CSV)
    write_schema_contract(schema_check)

    if not schema_check["valid"]:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"FATAL SCHEMA MISMATCH — ABORTING MONITOR", file=sys.stderr)
        print(schema_check["message"], file=sys.stderr)
        print(f"Missing: {schema_check.get('missing_cols', [])}", file=sys.stderr)
        print(f"Extra:   {schema_check.get('extra_cols', [])}", file=sys.stderr)
        print(f"Schema contract written to: {CONTRACT_OUT}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        sys.exit(1)

    print(f"Schema: {schema_check['status']} ({schema_check.get('col_count', 'N/A')} cols)")

    if dry_run:
        print(f"DRY-RUN: schema valid — {schema_check['message']}")
        if not SHADOW_CSV.exists():
            print("DRY-RUN: CSV not yet present — schema valid but no signals")
        else:
            rows = load_shadow()
            print(f"DRY-RUN: {len(rows)} rows parseable from CSV")
        print(f"Schema contract written: {CONTRACT_OUT}")
        return

    # Step 2: load data
    rows = load_shadow()
    cron = check_cron_health()

    if not rows:
        msg = "Schema valid but no signals yet — paper shadow may not have fired"
        print(msg)
        summary = {
            "generated_at":   now_str,
            "schema_status":  schema_check["status"],
            "cron_health":    cron,
            "signal_status":  "NO_SIGNALS",
            "message":        msg,
            "gates":          {"verdict": "PAPER_NO_GO", "live_verdict": "NO_GO",
                               "all_pass": False, "min_trades": False},
        }
        DAILY_JSON.parent.mkdir(parents=True, exist_ok=True)
        DAILY_JSON.write_text(json.dumps(summary, indent=2, default=str))
        print(f"Wrote: {DAILY_JSON}")
        return

    cum        = build_cumulative(rows)
    daily_data = build_daily_summary(rows)
    gates      = evaluate_gates(cum, cron)

    summary = {
        "generated_at":  now_str,
        "schema_status": schema_check["status"],
        "cron_health":   cron,
        "cumulative":    cum,
        **daily_data,
        "gates":         gates,
    }

    DAILY_JSON.parent.mkdir(parents=True, exist_ok=True)
    DAILY_JSON.write_text(json.dumps(summary, indent=2, default=str))

    # ── console output ──────────────────────────────────────────────────────────
    primary = cum["primary_stats"]
    out_lines = [
        f"=== Paper Shadow Daily Summary — {now_str} ===",
        f"",
        f"Schema:       {schema_check['status']} ({schema_check.get('col_count', 'N/A')} cols)",
        f"Cron health:  {'OK' if cron['ok'] else 'STALE/MISSING'} "
        f"(log_age={cron.get('log_age_min','?')}min, errors_24h={cron.get('errors_24h',0)})",
        f"",
        f"Cumulative:   signals={cum['total_signals']} "
        f"days={cum.get('n_days',0)} ({cum.get('first_day','?')} → {cum.get('last_day','?')})",
        f"",
        f"── Primary gate horizon: {PRIMARY_HORIZON} (real QuoterV2 paper label) ──",
        f"  labeled={primary['quote_ok_count']}  hit={primary['hit_rate']:.3f}  "
        f"PF={primary['profit_factor']}  mean_net={primary.get('mean_return') or 0:.4f}  "
        f"top_pool={primary['top_pool_share']:.2f}",
        f"",
        f"── Per-horizon breakdown ──",
    ]

    for lbl in ALL_LABELS:
        h = cum["per_horizon"][lbl]
        tag = " [PRIMARY]" if lbl == PRIMARY_HORIZON else ""
        diag = " [DIAGNOSTIC_ONLY]" if lbl in RESEARCH_HORIZONS else ""
        out_lines.append(
            f"  {lbl:>4}{tag}{diag}: labeled={h['quote_ok_count']:3d} "
            f"hit={h['hit_rate']:.3f} PF={str(h['profit_factor']):>6} "
            f"mean_net={str(h['mean_return'] or 0):>9}"
        )

    out_lines += [
        f"",
        f"── Daily breakdown ({PRIMARY_HORIZON} horizon) ──",
    ]
    for d in daily_data.get("daily", []):
        pf_val = d.get(f"{PRIMARY_HORIZON}_pf")
        flag = ("✓" if pf_val and pf_val > 1.0 else "✗") if pf_val else "~"
        out_lines.append(
            f"  {d['date']}: signals={d['signals']:3d}  "
            f"labeled={d[f'{PRIMARY_HORIZON}_labeled']:3d}  "
            f"hit={d.get(f'{PRIMARY_HORIZON}_hit_rate',0):.3f}  "
            f"PF={d.get(f'{PRIMARY_HORIZON}_pf','?')}  {flag}"
        )

    out_lines += [
        f"",
        f"── Go/No-Go Gates (30m paper only; research horizons EXCLUDED) ──",
    ]
    for gate, result in gates.items():
        if gate in ("all_pass", "verdict", "live_verdict", "note"):
            continue
        threshold = GATES.get(gate, "?")
        out_lines.append(f"  {'PASS' if result else 'FAIL'} {gate}  threshold={threshold}")

    out_lines += [
        f"",
        f"PAPER VERDICT: {gates['verdict']}",
        f"LIVE VERDICT:  {gates['live_verdict']}  ← requires explicit human approval",
        f"NOTE: {gates['note']}",
        f"{'━'*60}",
    ]

    text = "\n".join(out_lines)
    print(text)

    MONITOR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(MONITOR_LOG, "a") as f:
        f.write(text + "\n\n")

    if not status_only and not gates["all_pass"] and rows:
        if not cron.get("ok"):
            print("\n⚠ CRON HEALTH FAIL — check logs/sprint055_paper_shadow_cron.log", file=sys.stderr)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--status",  action="store_true", help="Print status and exit (no JSON write)")
    p.add_argument("--dry-run", action="store_true", help="Validate schema only; no full processing")
    args = p.parse_args()
    main(status_only=args.status, dry_run=args.dry_run)
