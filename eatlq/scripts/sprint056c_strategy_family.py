"""Sprint056C — Strategy Family Map.

Expands from single cell (fee_100 + heat_20_50 + 15m cd) to a strategy family:
  - heat buckets × fee tiers
  - pool_age-at-entry bins (approximated via entry_time relative to week start)
  - WETH-leg direction (weth_is_token0)
  - price_velocity_5m buckets (momentum regime)
  - buy_pressure_ratio buckets
  - temporal regime (pre/post 2026-01-01)
  - regime_week stability: is there a "stratum" or just scattered wins?

Answers: isolated cell or broad stratum of alpha?
Outputs:
  - mvp/diagnostics/sprint056/strategy_family_matrix.csv
  - mvp/audits/sprint056/strategy_family_readout.md
"""

from __future__ import annotations
import csv, json, glob
from pathlib import Path
from collections import defaultdict
import statistics

REPO    = Path(__file__).parent.parent
Q4_DIR  = REPO / "mvp/audits/sprint039a/q4_bundle_combined"
S054_JSON = REPO / "mvp/diagnostics/sprint054/labeled_candidates.json"
EXP_CSV = REPO / "mvp/diagnostics/sprint056/expansion_combined.csv"
OUT_DIR = REPO / "mvp/diagnostics/sprint056"
AUD_DIR = REPO / "mvp/audits/sprint056"
OUT_DIR.mkdir(parents=True, exist_ok=True)
AUD_DIR.mkdir(parents=True, exist_ok=True)

GAS_PROXY = 0.002

# ── helpers ───────────────────────────────────────────────────────────────────

def _pf(gains, losses):
    g, l = sum(gains), sum(losses)
    if l == 0:
        return float("inf") if g > 0 else None
    return round(g / l, 3)

def _stats(rets: list[float], label: str = "") -> dict:
    if not rets:
        return {"label": label, "n": 0, "hit": 0, "pf": None, "mean": 0, "signal": "EMPTY"}
    pos  = [r for r in rets if r > 0]
    neg  = [-r for r in rets if r <= 0]
    pf   = _pf(pos, neg)
    hit  = sum(1 for r in rets if r > 0) / len(rets)
    signal = "STRONG" if (pf or 0) > 2.0 and hit > 0.55 and len(rets) >= 30 else \
             "MODERATE" if (pf or 0) > 1.2 and hit > 0.50 else \
             "WEAK" if (pf or 0) > 1.0 else "NEGATIVE"
    return {
        "label":  label,
        "n":      len(rets),
        "hit":    round(hit, 4),
        "pf":     pf,
        "mean":   round(statistics.mean(rets), 6),
        "signal": signal,
    }

# ── load combined dataset ─────────────────────────────────────────────────────

def load_combined() -> list[dict]:
    """Load from expansion_combined.csv (Sprint056A output) or rebuild."""
    if EXP_CSV.exists():
        rows = []
        for r in csv.DictReader(open(EXP_CSV)):
            rows.append({
                "pool_address":      r["pool_address"],
                "regime_week":       r["regime_week"],
                "entry_time_sec":    float(r["entry_time_sec"]),
                "activity_heat":     float(r.get("activity_heat") or 0),
                "price_velocity_5m": float(r.get("price_velocity_5m") or 0),
                "buy_pressure_ratio":float(r.get("buy_pressure_ratio") or 0),
                "weth_is_token0":    r.get("weth_is_token0") in ("True", "1", "true"),
                "net_return_net":    float(r["net_return_net"]),
                "label":             int(r.get("label", 0)),
                "source":            r.get("source", "?"),
            })
        return rows

    # fallback: rebuild
    rows = []
    for fn in sorted(glob.glob(str(Q4_DIR / "*.csv"))):
        for r in csv.DictReader(open(fn)):
            if r.get("fee_bps") != "100" or r.get("exit_ok_5m") != "True" or r.get("entry_quote_ok") != "True":
                continue
            nr = float(r["net_return_5m"]) - GAS_PROXY
            rows.append({
                "pool_address":      r["pool_address"],
                "regime_week":       r["regime_week"],
                "entry_time_sec":    float(r["entry_time_sec"]),
                "activity_heat":     float(r.get("activity_heat") or 0),
                "price_velocity_5m": float(r.get("price_velocity_5m") or 0),
                "buy_pressure_ratio":float(r.get("buy_pressure_ratio") or 0),
                "weth_is_token0":    r.get("weth_is_token0") in ("1", True),
                "net_return_net":    nr,
                "label":             int(nr > 0),
                "source":            "in_bundle",
            })
    oos = json.load(open(S054_JSON))
    for c in oos:
        if not (c.get("fee_bps") == 100 and c.get("exit_quote_ok") and c.get("net_return_5m") is not None):
            continue
        nr = c["net_return_5m"] - GAS_PROXY
        rows.append({
            "pool_address":      c["pool_address"],
            "regime_week":       c.get("week", "oos"),
            "entry_time_sec":    float(c.get("entry_time_sec", 0)),
            "activity_heat":     float(c.get("activity_heat") or 0),
            "price_velocity_5m": float(c.get("price_velocity_5m") or 0),
            "buy_pressure_ratio":float(c.get("buy_pressure_ratio") or 0),
            "weth_is_token0":    bool(c.get("weth_is_token0")),
            "net_return_net":    nr,
            "label":             int(nr > 0),
            "source":            "oos",
        })
    return rows


# ── bucketing helpers ─────────────────────────────────────────────────────────

def heat_bucket(h: float) -> str:
    if h < 10:  return "lt10"
    if h < 20:  return "10_20"
    if h < 50:  return "20_50"
    if h < 100: return "50_100"
    return "ge100"

def vel_bucket(v: float) -> str:
    if v < -0.05: return "strong_down"
    if v < 0:     return "mild_down"
    if v < 0.02:  return "flat"
    if v < 0.10:  return "mild_up"
    return "strong_up"

def bpr_bucket(b: float) -> str:
    if b < 0.4:  return "bear_heavy"
    if b < 0.55: return "neutral"
    if b < 0.70: return "bull_mild"
    return "bull_heavy"

def era(week: str) -> str:
    return "2025_H2" if week < "2026-01-01" else "2026_H1"


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    rows = load_combined()
    print(f"Combined rows: {len(rows)}")
    print()

    matrix: list[dict] = []

    def analyze(label: str, subset: list[dict]) -> dict:
        rets = [r["net_return_net"] for r in subset]
        return _stats(rets, label)

    # ── heat buckets ─────────────────────────────────────────────────────────
    print("=== HEAT BUCKET MAP ===")
    for bucket in ["lt10","10_20","20_50","50_100","ge100"]:
        sub = [r for r in rows if heat_bucket(r["activity_heat"]) == bucket]
        s   = analyze(f"heat_{bucket}", sub)
        print(f"  {bucket:10s}: n={s['n']:5d} hit={s['hit']:.3f} pf={s['pf']} [{s['signal']}]")
        matrix.append({"dim": "heat", **s})

    print()

    # ── velocity buckets ──────────────────────────────────────────────────────
    print("=== PRICE VELOCITY MAP ===")
    for bucket in ["strong_down","mild_down","flat","mild_up","strong_up"]:
        sub = [r for r in rows if vel_bucket(r["price_velocity_5m"]) == bucket]
        s   = analyze(f"vel_{bucket}", sub)
        print(f"  {bucket:15s}: n={s['n']:5d} hit={s['hit']:.3f} pf={s['pf']} [{s['signal']}]")
        matrix.append({"dim": "velocity", **s})

    print()

    # ── buy_pressure_ratio buckets ────────────────────────────────────────────
    print("=== BUY PRESSURE MAP ===")
    for bucket in ["bear_heavy","neutral","bull_mild","bull_heavy"]:
        sub = [r for r in rows if bpr_bucket(r["buy_pressure_ratio"]) == bucket]
        s   = analyze(f"bpr_{bucket}", sub)
        print(f"  {bucket:15s}: n={s['n']:5d} hit={s['hit']:.3f} pf={s['pf']} [{s['signal']}]")
        matrix.append({"dim": "bpr", **s})

    print()

    # ── WETH-leg direction ────────────────────────────────────────────────────
    print("=== WETH-LEG DIRECTION ===")
    for weth_tok0, label in [(True, "weth_is_token0"), (False, "weth_is_token1")]:
        sub = [r for r in rows if r["weth_is_token0"] == weth_tok0]
        s   = analyze(label, sub)
        print(f"  {label:20s}: n={s['n']:5d} hit={s['hit']:.3f} pf={s['pf']} [{s['signal']}]")
        matrix.append({"dim": "weth_leg", **s})

    print()

    # ── temporal era ──────────────────────────────────────────────────────────
    print("=== TEMPORAL ERA ===")
    for e in ["2025_H2", "2026_H1"]:
        sub = [r for r in rows if era(r["regime_week"]) == e]
        s   = analyze(f"era_{e}", sub)
        print(f"  {e}: n={s['n']:5d} hit={s['hit']:.3f} pf={s['pf']} [{s['signal']}]")
        matrix.append({"dim": "era", **s})

    print()

    # ── cross-dim: heat × velocity ────────────────────────────────────────────
    print("=== CROSS: heat_20_50 × VELOCITY ===")
    heat20_50 = [r for r in rows if 20 <= r["activity_heat"] < 50]
    for bucket in ["strong_down","mild_down","flat","mild_up","strong_up"]:
        sub = [r for r in heat20_50 if vel_bucket(r["price_velocity_5m"]) == bucket]
        s   = analyze(f"h20_50×vel_{bucket}", sub)
        if s["n"] > 0:
            print(f"  {bucket:15s}: n={s['n']:4d} hit={s['hit']:.3f} pf={s['pf']} [{s['signal']}]")
        matrix.append({"dim": "heat×velocity", **s})

    print()

    # ── cross-dim: heat × bpr ─────────────────────────────────────────────────
    print("=== CROSS: heat_20_50 × BUY PRESSURE ===")
    for bucket in ["bear_heavy","neutral","bull_mild","bull_heavy"]:
        sub = [r for r in heat20_50 if bpr_bucket(r["buy_pressure_ratio"]) == bucket]
        s   = analyze(f"h20_50×bpr_{bucket}", sub)
        if s["n"] > 0:
            print(f"  {bucket:15s}: n={s['n']:4d} hit={s['hit']:.3f} pf={s['pf']} [{s['signal']}]")
        matrix.append({"dim": "heat×bpr", **s})

    print()

    # ── week-level consistency ────────────────────────────────────────────────
    print("=== WEEK-LEVEL CONSISTENCY (core signal: heat_20_50) ===")
    by_week: dict[str, list[float]] = defaultdict(list)
    for r in heat20_50:
        by_week[r["regime_week"]].append(r["net_return_net"])

    pos_weeks = neg_weeks = 0
    for wk in sorted(by_week):
        rets = by_week[wk]
        pos  = [r for r in rets if r > 0]; neg_l = [-r for r in rets if r <= 0]
        pf   = round(sum(pos)/sum(neg_l), 3) if neg_l else None
        flag = "+" if (pf or 0) > 1.0 else "-"
        if (pf or 0) > 1.0: pos_weeks += 1
        else: neg_weeks += 1
        print(f"  {wk}: n={len(rets):4d} hit={sum(1 for r in rets if r>0)/len(rets):.3f} "
              f"pf={pf} [{flag}]")

    stratum_verdict = "STRATUM" if pos_weeks / max(1, pos_weeks+neg_weeks) >= 0.65 else "ISOLATED"
    print(f"\n  → {pos_weeks}/{pos_weeks+neg_weeks} weeks positive PF → [{stratum_verdict}]")
    print()

    # ── source consistency ────────────────────────────────────────────────────
    print("=== SOURCE CONSISTENCY ===")
    for src in ["in_bundle","oos_sprint054","oos"]:
        sub = [r for r in rows if r["source"] == src]
        if not sub:
            continue
        s = analyze(src, sub)
        print(f"  {src:20s}: n={s['n']:5d} hit={s['hit']:.3f} pf={s['pf']} [{s['signal']}]")
        matrix.append({"dim": "source", **s})

    print()

    # ── family verdict ────────────────────────────────────────────────────────
    strong_cells = [m for m in matrix if m.get("signal") == "STRONG"]
    moderate_cells = [m for m in matrix if m.get("signal") == "MODERATE"]
    print(f"=== FAMILY VERDICT ===")
    print(f"  Strong cells: {len(strong_cells)}")
    print(f"  Moderate cells: {len(moderate_cells)}")
    family_verdict = (
        "BROAD_STRATUM"     if len(strong_cells) >= 5 else
        "NARROW_STRATUM"    if len(strong_cells) >= 2 or len(moderate_cells) >= 5 else
        "SINGLE_CELL"
    )
    print(f"  → [{family_verdict}]")

    # ── write CSV ─────────────────────────────────────────────────────────────
    with open(OUT_DIR / "strategy_family_matrix.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["dim","label","n","hit","pf","mean","signal"])
        w.writeheader()
        w.writerows(matrix)
    print(f"\nWrote strategy_family_matrix.csv ({len(matrix)} rows)")

    write_readout(matrix=matrix, stratum_verdict=stratum_verdict,
                  family_verdict=family_verdict,
                  n_total=len(rows), strong_cells=strong_cells, moderate_cells=moderate_cells,
                  pos_weeks=pos_weeks, n_weeks=pos_weeks+neg_weeks)
    print("Wrote strategy_family_readout.md")


def write_readout(**kw) -> None:
    matrix = kw["matrix"]; family_verdict = kw["family_verdict"]
    stratum_verdict = kw["stratum_verdict"]; n_total = kw["n_total"]
    strong = kw["strong_cells"]; moderate = kw["moderate_cells"]
    pos_wk = kw["pos_weeks"]; n_wk = kw["n_weeks"]

    def table_rows(dim_filter):
        return [m for m in matrix if m.get("dim") == dim_filter]

    def fmt_row(m):
        return (f"| {m.get('label','?')} | {m.get('n',0)} | {m.get('hit',0):.3f} "
                f"| {m.get('pf','?')} | {m.get('mean',0):.4f} | **{m.get('signal','?')}** |")

    lines = [
        "# Sprint056C — Strategy Family Map Readout",
        "",
        f"**Dataset:** {n_total} fee_100bps labeled rows (in-bundle + OOS)",
        "",
        "## Heat Bucket Map",
        "",
        "| Bucket | N | Hit | PF | Mean | Signal |",
        "|--------|---|-----|-----|------|--------|",
    ]
    for m in table_rows("heat"):
        lines.append(fmt_row(m))

    lines += ["", "## Price Velocity Map", "", "| Bucket | N | Hit | PF | Mean | Signal |",
              "|--------|---|-----|-----|------|--------|"]
    for m in table_rows("velocity"):
        lines.append(fmt_row(m))

    lines += ["", "## Buy Pressure Ratio Map", "", "| Bucket | N | Hit | PF | Mean | Signal |",
              "|--------|---|-----|-----|------|--------|"]
    for m in table_rows("bpr"):
        lines.append(fmt_row(m))

    lines += ["", "## WETH-Leg Direction", "", "| Direction | N | Hit | PF | Mean | Signal |",
              "|-----------|---|-----|-----|------|--------|"]
    for m in table_rows("weth_leg"):
        lines.append(fmt_row(m))

    lines += ["", "## Temporal Era", "", "| Era | N | Hit | PF | Mean | Signal |",
              "|-----|---|-----|-----|------|--------|"]
    for m in table_rows("era"):
        lines.append(fmt_row(m))

    lines += [
        "",
        "## Week-Level Consistency (heat_20_50)",
        "",
        f"{pos_wk}/{n_wk} weeks positive PF → **{stratum_verdict}**",
        "",
        "## Strong Cells Identified",
        "",
    ]
    for m in strong:
        lines.append(f"- `{m['label']}`: n={m['n']} hit={m['hit']:.3f} PF={m['pf']}")

    lines += [
        "",
        "## Family Verdict",
        "",
        f"**{family_verdict}**",
        "",
        f"- Strong cells: {len(strong)}",
        f"- Moderate cells: {len(moderate)}",
        "",
        {
            "BROAD_STRATUM":  "Alpha appears across multiple feature dimensions — we are touching a geological layer, not an isolated point.",
            "NARROW_STRATUM": "Alpha is present in multiple cells but concentrated. Promising but needs more coverage.",
            "SINGLE_CELL":    "Alpha is isolated in one cell. Risk of data-mining artifact. Need more samples.",
        }.get(family_verdict, ""),
        "",
        "## Artifacts",
        "",
        "- `mvp/diagnostics/sprint056/strategy_family_matrix.csv`",
    ]

    (AUD_DIR / "strategy_family_readout.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
