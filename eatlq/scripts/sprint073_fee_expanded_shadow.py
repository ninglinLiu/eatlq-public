"""
Sprint 073 — Fee-Expanded Independent Shadow Track (fee ≤ 30bps)
=================================================================
Runs ALONGSIDE (not replacing) the existing fee=100 paper shadow.
Discovers and tracks pools with fee ∈ {1, 5, 30} basis points — adding
the Uniswap 0.3% tier (fee_bps=30) which is where all fresh meme pools live.

This is pure RESEARCH: no signals emitted to Redis, no live trades.
Gate criteria are intentionally stricter than paper shadow to compensate
for higher-noise 0.3% pools:
  - has_weth_leg = 1
  - pool_age_sec < 86400  (young pools only)
  - activity_heat >= 5    (meaningful activity)
  - buy_pressure_ratio >= 0.55 in last 3 consecutive 5m windows
  - pool NOT already in fee=100 paper shadow

State:  logs/sprint073_fee_expanded_state.json
Output: mvp/audits/sprint073_fee_expanded/

Cron (add after this is validated):
  3-58/5 * * * * cd /opt/EATLQ && python3 scripts/sprint073_fee_expanded_shadow.py \
                  --once >> logs/sprint073_fee_expanded.log 2>&1
"""
from __future__ import annotations

import argparse, json, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

# ─── Paths ──────────────────────────────────────────────────────────────────
REPO       = Path("/opt/EATLQ")
STATE_FILE = REPO / "logs/sprint073_fee_expanded_state.json"
OUT_DIR    = REPO / "mvp/audits/sprint073_fee_expanded"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── Gates ──────────────────────────────────────────────────────────────────
FEE_BPS_ALLOWED    = {1, 5, 30}        # 0.01%, 0.05%, 0.30%
FEE_BPS_NEW        = {5, 30}           # tiers not in original paper shadow
MAX_POOL_AGE_SEC   = 86_400            # 24h
MIN_HEAT           = 5                 # activity_heat minimum
MIN_BPR_CONSEC     = 0.55             # consecutive window BPR threshold
N_CONSEC_WINDOWS   = 3                # require N consecutive high-BPR windows
MOMENTUM_LOOKBACK_H = 3               # hours of feature data to scan
CHAIN_ID           = 8453

# Existing paper shadow pools — exclude from this track
PAPER_SHADOW_POOLS = {
    "0x0a423a23c97ce0e4296d13c2b846f8b8b66085d6",
    "0x369adee33d0d5c5c4cbaabf75965976c60909451",
    "0xa5a98447c7e10dbf35da4a4c4ba151539c200344",
    "0xa68c78d969e51ac10d488c6d1da97541151f599c",
    "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",
    "0xd0b53d9277642d899df5c87a3966a349a798f224",  # WETH/USDC LP
}

MEM_CAP = "max_memory_usage=4000000000"

# ─── Helpers ────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%SZ")
    print(f"[fee_expanded] {ts} {msg}", flush=True)


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def ch(sql: str, timeout: int = 60) -> tuple[str, str]:
    r = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
         "--database", "eatlq", "--format", "TSV", "--query", sql],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout.strip(), r.stderr.strip()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "created_at":      utc_now().isoformat(),
        "last_run":        None,
        "run_count":       0,
        "watchlist":       {},   # pool -> {first_seen, fee_bps, consecutive_windows, ...}
        "graduated":       [],   # pools that hit N_CONSEC_WINDOWS threshold
        "stats":           {"scans": 0, "pools_checked": 0, "new_candidates": 0, "graduated": 0},
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ─── Discovery: active fee-expanded pools ───────────────────────────────────

def discover_active_pools(now_ts: int) -> list[dict]:
    """
    Find pools with has_weth_leg=1, fee_bps ∈ FEE_BPS_ALLOWED, young, and
    recently active in pool_features_5m.
    """
    dt_3h  = datetime.fromtimestamp(now_ts - MOMENTUM_LOOKBACK_H * 3600,
                                    tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    dt_24h = datetime.fromtimestamp(now_ts - 86400, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    fee_list = ",".join(str(f) for f in FEE_BPS_ALLOWED)
    excl     = "'" + "','".join(PAPER_SHADOW_POOLS) + "'"

    sql = f"""
SELECT
    f.pool_address,
    d.fee_bps,
    d.token0_symbol,
    d.token1_symbol,
    d.has_weth_leg,
    toUnixTimestamp(d.discovered_at) AS disc_ts,
    count() AS windows_3h,
    avg(f.activity_heat) AS avg_heat,
    avg(f.buy_pressure_ratio) AS avg_bpr,
    countIf(f.buy_pressure_ratio >= {MIN_BPR_CONSEC}) AS win_high_bpr,
    max(f.window_start) AS last_window
FROM eatlq.pool_features_5m f
INNER JOIN eatlq.discovered_pools d ON f.pool_address = d.pool_address
WHERE f.window_start >= toDateTime('{dt_3h}', 'UTC')
  AND d.fee_bps IN ({fee_list})
  AND d.has_weth_leg = 1
  AND f.pool_address NOT IN ({excl})
  AND toUnixTimestamp(now()) - toUnixTimestamp(d.discovered_at) < {MAX_POOL_AGE_SEC}
GROUP BY f.pool_address, d.fee_bps, d.token0_symbol, d.token1_symbol,
         d.has_weth_leg, disc_ts
HAVING avg_heat >= {MIN_HEAT}
ORDER BY avg_bpr DESC, avg_heat DESC
SETTINGS {MEM_CAP}
"""
    out, err = ch(sql, timeout=90)
    if not out:
        if err:
            log(f"discover query error: {err[:200]}")
        return []

    pools = []
    for line in out.split("\n"):
        parts = line.split("\t")
        if len(parts) < 11:
            continue
        try:
            pools.append({
                "pool":         parts[0],
                "fee_bps":      int(parts[1]),
                "tok0":         parts[2] or "?",
                "tok1":         parts[3] or "?",
                "has_weth":     int(parts[4]),
                "disc_ts":      int(parts[5]),
                "windows_3h":   int(parts[6]),
                "avg_heat":     float(parts[7]),
                "avg_bpr":      float(parts[8]),
                "win_high_bpr": int(parts[9]),
                "last_window":  parts[10],
                "age_sec":      now_ts - int(parts[5]) if parts[5].isdigit() else 0,
            })
        except (ValueError, IndexError):
            continue
    return pools


def check_consecutive_bpr(pool: str, n_windows: int = N_CONSEC_WINDOWS) -> int:
    """
    Return the max consecutive run of windows with BPR >= MIN_BPR_CONSEC
    in the last MOMENTUM_LOOKBACK_H hours.
    Returns 0 if none found.
    """
    now_ts = int(time.time())
    dt_3h  = datetime.fromtimestamp(now_ts - MOMENTUM_LOOKBACK_H * 3600,
                                    tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
SELECT buy_pressure_ratio
FROM eatlq.pool_features_5m
WHERE pool_address = '{pool}'
  AND window_start >= toDateTime('{dt_3h}', 'UTC')
ORDER BY window_start
SETTINGS {MEM_CAP}
"""
    out, err = ch(sql)
    if not out:
        return 0

    bprs = []
    for line in out.split("\n"):
        line = line.strip()
        if line:
            try:
                bprs.append(float(line))
            except ValueError:
                pass

    max_consec = 0
    cur = 0
    for bpr in bprs:
        if bpr >= MIN_BPR_CONSEC:
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0
    return max_consec


# ─── Main run ────────────────────────────────────────────────────────────────

def run_once(_args) -> None:
    now_ts = int(time.time())
    state  = load_state()
    state["last_run"]  = utc_now().isoformat()
    state["run_count"] = state.get("run_count", 0) + 1
    state["stats"]["scans"] += 1

    log(f"Run #{state['run_count']} | watchlist={len(state['watchlist'])} | "
        f"graduated={len(state['graduated'])}")

    # ── Discovery ─────────────────────────────────────────────────────────
    pools = discover_active_pools(now_ts)
    state["stats"]["pools_checked"] += len(pools)
    log(f"Active fee-expanded pools: {len(pools)}")

    new_entries: list[dict] = []
    graduated:   list[dict] = []

    for p in pools:
        pool     = p["pool"]
        consec   = check_consecutive_bpr(pool)
        p["max_consec_bpr"] = consec

        entry = state["watchlist"].get(pool)
        if entry is None:
            # New pool on watchlist
            entry = {
                "pool":          pool,
                "fee_bps":       p["fee_bps"],
                "tok0":          p["tok0"],
                "tok1":          p["tok1"],
                "age_sec":       p["age_sec"],
                "first_seen":    utc_now().isoformat(),
                "max_consec_bpr": consec,
                "avg_heat":      p["avg_heat"],
                "avg_bpr":       p["avg_bpr"],
                "scans":         1,
                "graduated":     False,
            }
            state["watchlist"][pool] = entry
            state["stats"]["new_candidates"] += 1
            new_entries.append(entry)
            log(f"  NEW  {pool[:12]}… {p['tok0']}/{p['tok1']} fee={p['fee_bps']}bps "
                f"age={p['age_sec']//3600:.1f}h heat={p['avg_heat']:.1f} "
                f"consec={consec}/{N_CONSEC_WINDOWS}")
        else:
            # Update existing
            entry["max_consec_bpr"] = max(entry.get("max_consec_bpr", 0), consec)
            entry["avg_heat"]   = p["avg_heat"]
            entry["avg_bpr"]    = p["avg_bpr"]
            entry["scans"]      = entry.get("scans", 0) + 1

        # Graduation check
        if (not entry.get("graduated") and
                consec >= N_CONSEC_WINDOWS and
                p["avg_heat"] >= MIN_HEAT and
                p["avg_bpr"] >= MIN_BPR_CONSEC):
            entry["graduated"]      = True
            entry["graduated_at"]   = utc_now().isoformat()
            entry["consec_at_grad"] = consec
            state["graduated"].append(entry)
            state["stats"]["graduated"] += 1
            graduated.append(entry)
            log(f"  GRAD {pool[:12]}… consec={consec} heat={p['avg_heat']:.1f} "
                f"→ PAPER SHADOW CANDIDATE (fee gate change required)")

    # ── Write summary ─────────────────────────────────────────────────────
    summary = {
        "run_at_utc":    utc_now().isoformat(),
        "run_count":     state["run_count"],
        "stats":         state["stats"],
        "watchlist_size": len(state["watchlist"]),
        "graduated_count": len(state["graduated"]),
        "new_this_run":  [e["pool"] for e in new_entries],
        "graduated_this_run": [e["pool"] for e in graduated],
        "graduated_pools": [
            {k: v for k, v in g.items() if k in
             ("pool","fee_bps","tok0","tok1","age_sec","graduated_at","consec_at_grad","avg_heat","avg_bpr")}
            for g in state["graduated"]
        ],
        "active_watchlist": [
            {k: v for k, v in entry.items() if k in
             ("pool","fee_bps","tok0","tok1","age_sec","max_consec_bpr","avg_heat","avg_bpr","scans","graduated")}
            for entry in state["watchlist"].values()
            if not entry.get("graduated")
        ],
        "note": (
            "RESEARCH ONLY — no signals emitted. "
            "Graduated pools require fee gate change in sprint055_fee100_paper_shadow.py "
            "before they can enter paper shadow."
        ),
    }
    (OUT_DIR / "sprint073_fee_expanded_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )
    save_state(state)

    log(f"Done: new={len(new_entries)} graduated={len(graduated)} watchlist={len(state['watchlist'])}")


# ─── Entrypoint ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once",  action="store_true")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        log("State reset.")

    if args.once:
        run_once(args)
        return

    while True:
        run_once(args)
        time.sleep(300)


if __name__ == "__main__":
    main()
