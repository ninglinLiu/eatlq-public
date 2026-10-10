#!/usr/bin/env python3
"""
Alpha line health monitor — scan paper shadow CSVs and classify each line.

Outputs:
  mvp/audits/alpha_health/dashboard.json
  mvp/audits/alpha_health/dashboard.md
  mvp/audits/alpha_health/status_snapshot.json  (for cron transition detection)

Status rules:
  SILENT    : n_24h == 0
  POISONED  : n_24h >= 3 AND honeypot_rate_24h >= 0.5
  DEAD      : n_24h >= 3 AND pf_real_24h < 0.5
  ALIVE     : n_24h >= 3 AND pf_real_24h >= 1.0 AND honeypot_rate_24h < 0.3
  WATCH     : everything else
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "mvp" / "audits" / "alpha_health"
SNAPSHOT_PATH = OUT_DIR / "status_snapshot.json"
TRANSITIONS_LOG = OUT_DIR / "transitions.log"

PAPER_GLOBS = [
    REPO / "logs" / "sprint055_paper_shadow.csv",
    REPO / "logs" / "sprint087_expanded_paper_shadow.csv",
    REPO / "logs" / "sprint083_fee500_paper_shadow.csv",
]

# Wallet Profile v0 research lines (not production paper cron)
FUSION_SHADOW_CSV = REPO / "mvp" / "audits" / "wallet_profile_v0" / "fusion_shadow" / "scores.csv"
XGB_HOLDOUT_SUMMARY = REPO / "mvp" / "audits" / "wallet_profile_v0" / "holdout" / "summary.json"
SENDER_GATED_CSV = REPO / "mvp" / "audits" / "wallet_profile_v0" / "sender_gated" / "sender_gated_paper_shadow.csv"


def _discover_csv_paths() -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for p in PAPER_GLOBS:
        if p.exists() and str(p) not in seen:
            paths.append(p)
            seen.add(str(p))
    for p in sorted((REPO / "logs" / "sprint068").glob("alpha_*_paper_shadow.csv")):
        if p.stat().st_size > 50 and str(p) not in seen:
            paths.append(p)
            seen.add(str(p))
    return paths


def _line_id(path: Path) -> str:
    name = path.stem.replace("_paper_shadow", "")
    if name == "sprint055":
        return "sprint055_fee100"
    return name


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pf(vals: list[float]) -> Optional[float]:
    wins = sum(v for v in vals if v > 0)
    losses = abs(sum(v for v in vals if v < 0))
    if losses == 0:
        return None if wins == 0 else float("inf")
    return wins / losses


def _bool_rate(rows: list[dict], field: str, bad_values: tuple[str, ...] = ("true", "1")) -> Optional[float]:
    present = [r for r in rows if (r.get(field) or "").strip() != ""]
    if not present:
        return None
    bad = sum(1 for r in present if str(r.get(field, "")).lower() in bad_values)
    return bad / len(present)


def _liq_fail_rate(rows: list[dict]) -> Optional[float]:
    present = [r for r in rows if (r.get("liquidity_depth_ok") or "").strip() != ""]
    if not present:
        return None
    fails = sum(
        1 for r in present
        if str(r.get("liquidity_depth_ok", "")).lower() not in ("true", "1")
    )
    return fails / len(present)


def _returns(rows: list[dict], key: str) -> list[float]:
    out: list[float] = []
    for r in rows:
        v = r.get(key)
        if v is None or str(v).strip() == "":
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            pass
    return out


def _filter_window(rows: list[dict], start: datetime, end: datetime) -> list[dict]:
    out = []
    for r in rows:
        ts = _parse_ts(r.get("signal_time", ""))
        if ts is None:
            continue
        if start <= ts < end:
            out.append(r)
    return out


def _classify_status(
    n_24h: int,
    pf_real: Optional[float],
    honeypot_rate: Optional[float],
    pf_gross: Optional[float] = None,
) -> str:
    if n_24h == 0:
        return "SILENT"
    hp = honeypot_rate if honeypot_rate is not None else 0.0
    pf = pf_real
    if pf is None and pf_gross is not None:
        pf = pf_gross  # legacy CSV without real-cost columns
    if pf is None:
        return "WATCH"
    if n_24h >= 3 and hp >= 0.5:
        return "POISONED"
    if n_24h >= 3 and pf < 0.5:
        return "DEAD"
    if n_24h >= 3 and pf >= 1.0 and hp < 0.3:
        return "ALIVE"
    return "WATCH"


def _analyze_rows(
    line_id: str,
    csv_path: Path,
    rows: list[dict],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    win = _filter_window(rows, window_start, window_end)
    n_total = len(rows)
    n_24h = len(win)
    last_ts = max((_parse_ts(r["signal_time"]) for r in rows if _parse_ts(r.get("signal_time", ""))), default=None)
    pools_24h = sorted({(r.get("pool") or "").lower() for r in win if r.get("pool")})
    gross = _returns(win, "net_return_30m_ag")
    real = _returns(win, "net_return_30m_real")
    pf_gross = _pf(gross)
    pf_real = _pf(real)
    hp_rate = _bool_rate(win, "honeypot_flag")
    liq_fail = _liq_fail_rate(win)
    status = _classify_status(n_24h, pf_real, hp_rate, pf_gross)
    return {
        "line_id": line_id,
        "csv_path": str(csv_path.relative_to(REPO)),
        "n_total": n_total,
        "n_24h": n_24h,
        "last_signal_time": last_ts.isoformat() if last_ts else None,
        "pf_gross_24h": round(pf_gross, 4) if pf_gross is not None else None,
        "pf_real_24h": round(pf_real, 4) if pf_real is not None else None,
        "honeypot_rate_24h": round(hp_rate, 4) if hp_rate is not None else None,
        "liq_fail_rate_24h": round(liq_fail, 4) if liq_fail is not None else None,
        "unique_pools_24h": pools_24h,
        "unique_pools_24h_count": len(pools_24h),
        "status": status,
    }


def load_all_rows(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size < 10:
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _analyze_fusion_shadow(now: datetime, win_start: datetime) -> dict[str, Any]:
    """Analyze fusion_shadow scores.csv using final_score as return proxy."""
    line_id = "fusion_shadow"
    if not FUSION_SHADOW_CSV.exists():
        return {
            "line_id": line_id,
            "csv_path": str(FUSION_SHADOW_CSV.relative_to(REPO)),
            "n_total": 0,
            "n_24h": 0,
            "status": "SILENT",
        }
    rows = load_all_rows(FUSION_SHADOW_CSV)
    win = []
    for r in rows:
        ts = _parse_ts(r.get("computed_at", ""))
        if ts and win_start <= ts < now:
            win.append(r)
    scores = []
    for r in win:
        try:
            scores.append(float(r.get("final_score", 0)) - 0.5)
        except (TypeError, ValueError):
            pass
    pf = _pf(scores)
    status = _classify_status(len(win), pf, None, pf)
    return {
        "line_id": line_id,
        "csv_path": str(FUSION_SHADOW_CSV.relative_to(REPO)),
        "n_total": len(rows),
        "n_24h": len(win),
        "last_signal_time": rows[-1].get("computed_at") if rows else None,
        "pf_gross_24h": round(pf, 4) if pf is not None else None,
        "pf_real_24h": None,
        "honeypot_rate_24h": None,
        "liq_fail_rate_24h": None,
        "unique_pools_24h": sorted({(r.get("pool_address") or "").lower() for r in win if r.get("pool_address")}),
        "unique_pools_24h_count": len({(r.get("pool_address") or "").lower() for r in win if r.get("pool_address")}),
        "status": status,
    }


def _analyze_xgb_research() -> dict[str, Any]:
    line_id = "xgb_research"
    if not XGB_HOLDOUT_SUMMARY.exists():
        return {
            "line_id": line_id,
            "csv_path": str(XGB_HOLDOUT_SUMMARY.relative_to(REPO)),
            "n_total": 0,
            "n_24h": 0,
            "status": "SILENT",
        }
    try:
        data = json.loads(XGB_HOLDOUT_SUMMARY.read_text())
    except Exception:
        return {"line_id": line_id, "n_total": 0, "n_24h": 0, "status": "WATCH"}
    pf = data.get("holdout_pf") or data.get("holdout_pf_proxy")
    n = int(data.get("n_holdout") or 0)
    status = "WATCH"
    if pf is not None and n >= 3:
        status = _classify_status(min(n, 100), float(pf) if pf != float("inf") else 2.0, None, float(pf) if pf != float("inf") else 2.0)
    return {
        "line_id": line_id,
        "csv_path": str(XGB_HOLDOUT_SUMMARY.relative_to(REPO)),
        "n_total": n,
        "n_24h": n,
        "last_signal_time": data.get("generated_at"),
        "pf_gross_24h": pf,
        "pf_real_24h": data.get("holdout_pf_top1_removed"),
        "honeypot_rate_24h": None,
        "liq_fail_rate_24h": None,
        "unique_pools_24h": [],
        "unique_pools_24h_count": data.get("unique_pools", 0),
        "status": status,
    }


def _analyze_sender_gated_v0(now: datetime, win_start: datetime) -> dict[str, Any]:
    line_id = "sender_gated_v0"
    if not SENDER_GATED_CSV.exists():
        return {
            "line_id": line_id,
            "csv_path": str(SENDER_GATED_CSV.relative_to(REPO)),
            "n_total": 0,
            "n_24h": 0,
            "status": "SILENT",
        }
    rows = load_all_rows(SENDER_GATED_CSV)
    win = _filter_window(rows, win_start, now) if rows and _parse_ts(rows[0].get("signal_time", "")) else rows
    rets = _returns(win, "net_return_30m_tick")
    if not rets:
        rets = _returns(rows, "net_return_30m_tick")
    pf = _pf(rets)
    status = _classify_status(len(win) or len(rows), pf, None, pf)
    return {
        "line_id": line_id,
        "csv_path": str(SENDER_GATED_CSV.relative_to(REPO)),
        "n_total": len(rows),
        "n_24h": len(win) if win else len(rows),
        "last_signal_time": rows[-1].get("signal_time") if rows else None,
        "pf_gross_24h": round(pf, 4) if pf is not None else None,
        "pf_real_24h": None,
        "honeypot_rate_24h": None,
        "liq_fail_rate_24h": None,
        "unique_pools_24h": sorted({(r.get("pool") or "").lower() for r in (win or rows) if r.get("pool")}),
        "unique_pools_24h_count": len({(r.get("pool") or "").lower() for r in (win or rows) if r.get("pool")}),
        "status": status,
    }


def build_dashboard(now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    win_start = now - timedelta(hours=24)
    lines: list[dict[str, Any]] = []
    for path in _discover_csv_paths():
        lid = _line_id(path)
        rows = load_all_rows(path)
        if not rows:
            continue
        lines.append(_analyze_rows(lid, path, rows, win_start, now))
    lines.append(_analyze_fusion_shadow(now, win_start))
    lines.append(_analyze_xgb_research())
    lines.append(_analyze_sender_gated_v0(now, win_start))
    alive = [l for l in lines if l["status"] == "ALIVE"]
    return {
        "generated_at": now.isoformat(),
        "window_hours": 24,
        "window_start": win_start.isoformat(),
        "lines": sorted(lines, key=lambda x: (-x["n_24h"], x["line_id"])),
        "summary": {
            "total_lines": len(lines),
            "alive_count": len(alive),
            "silent_count": sum(1 for l in lines if l["status"] == "SILENT"),
            "watch_count": sum(1 for l in lines if l["status"] == "WATCH"),
            "dead_count": sum(1 for l in lines if l["status"] == "DEAD"),
            "poisoned_count": sum(1 for l in lines if l["status"] == "POISONED"),
            "alive_line_ids": [l["line_id"] for l in alive],
            "dynamic_whitelist_pools": sorted(
                {p for l in alive for p in l.get("unique_pools_24h", [])}
            ),
        },
    }


def build_backfill(start_date: str, end_date: str) -> dict[str, Any]:
    """Daily status per line for each UTC day in [start, end]."""
    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
    paths = _discover_csv_paths()
    cached: dict[str, list[dict]] = {str(p): load_all_rows(p) for p in paths}
    daily: list[dict[str, Any]] = []
    day = start
    while day < end:
        day_end = day + timedelta(days=1)
        entry: dict[str, Any] = {"date": day.date().isoformat(), "lines": {}}
        for path in paths:
            lid = _line_id(path)
            rows = cached[str(path)]
            if not rows:
                continue
            win_start = day_end - timedelta(hours=24)
            m = _analyze_rows(lid, path, rows, win_start, day_end)
            entry["lines"][lid] = {
                "status": m["status"],
                "n_24h": m["n_24h"],
                "pf_real_24h": m["pf_real_24h"],
                "honeypot_rate_24h": m["honeypot_rate_24h"],
            }
        daily.append(entry)
        day = day_end
    return {"backfill_start": start_date, "backfill_end": end_date, "daily": daily}


def write_dashboard(dashboard: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "dashboard.json"
    json_path.write_text(json.dumps(dashboard, indent=2))
    md_lines = [
        "# Alpha Line Health Dashboard",
        f"\nGenerated: {dashboard['generated_at']}\n",
        f"Window: last {dashboard['window_hours']}h ending {dashboard['generated_at']}\n",
        f"**ALIVE**: {dashboard['summary']['alive_count']} | "
        f"**SILENT**: {dashboard['summary']['silent_count']} | "
        f"**WATCH**: {dashboard['summary']['watch_count']} | "
        f"**DEAD**: {dashboard['summary']['dead_count']} | "
        f"**POISONED**: {dashboard['summary']['poisoned_count']}\n",
        "| line | status | n_24h | pf_real | pf_gross | honeypot% | liq_fail% | last_signal |",
        "|------|--------|------:|--------:|---------:|----------:|----------:|-------------|",
    ]
    for line in dashboard["lines"]:
        hp = line.get("honeypot_rate_24h")
        lf = line.get("liq_fail_rate_24h")
        hp_s = f"{hp * 100:.0f}%" if hp is not None else "-"
        lf_s = f"{lf * 100:.0f}%" if lf is not None else "-"
        last = (line.get("last_signal_time") or "-")[:19]
        md_lines.append(
            f"| {line['line_id']} | **{line['status']}** | {line['n_24h']} | "
            f"{line.get('pf_real_24h') or '-'} | {line.get('pf_gross_24h') or '-'} | "
            f"{hp_s} | {lf_s} | {last} |"
        )
    if dashboard["summary"]["dynamic_whitelist_pools"]:
        md_lines.append("\n## Dynamic whitelist (ALIVE lines)\n")
        for p in dashboard["summary"]["dynamic_whitelist_pools"]:
            md_lines.append(f"- `{p}`")
    (OUT_DIR / "dashboard.md").write_text("\n".join(md_lines) + "\n")


def load_snapshot() -> dict[str, str]:
    if not SNAPSHOT_PATH.exists():
        return {}
    try:
        data = json.loads(SNAPSHOT_PATH.read_text())
        return {k: v.get("status", "") for k, v in data.items()}
    except Exception:
        return {}


def save_snapshot(dashboard: dict[str, Any]) -> None:
    snap = {
        l["line_id"]: {"status": l["status"], "updated_at": dashboard["generated_at"]}
        for l in dashboard["lines"]
    }
    SNAPSHOT_PATH.write_text(json.dumps(snap, indent=2))


def detect_transitions(
    dashboard: dict[str, Any],
    prev: dict[str, str],
) -> list[dict[str, Any]]:
    transitions = []
    for line in dashboard["lines"]:
        lid = line["line_id"]
        new_status = line["status"]
        old_status = prev.get(lid)
        if old_status is None:
            continue
        if old_status != new_status:
            transitions.append({
                "line_id": lid,
                "from": old_status,
                "to": new_status,
                "at": dashboard["generated_at"],
                "n_24h": line["n_24h"],
                "pf_real_24h": line.get("pf_real_24h"),
            })
    return transitions


def log_transitions(transitions: list[dict[str, Any]]) -> None:
    if not transitions:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRANSITIONS_LOG, "a") as f:
        for t in transitions:
            f.write(json.dumps(t) + "\n")


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message[:4000],
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        return False


def notify_alive_transitions(transitions: list[dict[str, Any]], dry_run: bool = False) -> None:
    alive_new = [t for t in transitions if t["to"] == "ALIVE" and t["from"] != "ALIVE"]
    if not alive_new:
        return
    for t in alive_new:
        msg = (
            f"EATLQ alpha ALIVE: {t['line_id']}\n"
            f"{t['from']} -> {t['to']}\n"
            f"n_24h={t['n_24h']} pf_real={t.get('pf_real_24h')}\n"
            f"Review dashboard before live."
        )
        if dry_run:
            print(f"[telegram dry-run] {msg}")
        else:
            ok = send_telegram(msg)
            print(f"telegram sent={ok} line={t['line_id']}")


def get_line_status(line_id: str) -> Optional[str]:
    path = OUT_DIR / "dashboard.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    for line in data.get("lines", []):
        if line["line_id"] == line_id:
            return line["status"]
    return None


def get_dynamic_whitelist() -> set[str]:
    path = OUT_DIR / "dashboard.json"
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    return {p.lower() for p in data.get("summary", {}).get("dynamic_whitelist_pools", [])}


def main() -> None:
    p = argparse.ArgumentParser(description="Alpha line health monitor")
    p.add_argument("--once", action="store_true", help="Run once and write dashboard")
    p.add_argument("--backfill", action="store_true", help="Daily status backfill")
    p.add_argument("--start-date", default="2026-05-14")
    p.add_argument("--end-date", default="2026-05-22")
    p.add_argument("--notify", action="store_true", help="Send Telegram on ALIVE transitions")
    p.add_argument("--telegram-dry-run", action="store_true")
    args = p.parse_args()

    if args.backfill:
        result = build_backfill(args.start_date, args.end_date)
        out = OUT_DIR / "backfill_daily.json"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
        print(f"backfill written: {out} days={len(result['daily'])}")
        # Print summary for key lines
        for day in result["daily"]:
            s055 = day["lines"].get("sprint055_fee100", {})
            s087 = day["lines"].get("sprint087_expanded", {})
            if s055 or s087:
                print(
                    f"  {day['date']} sprint055={s055.get('status','-')} "
                    f"sprint087={s087.get('status','-')}"
                )
        return

    prev = load_snapshot()
    dashboard = build_dashboard()
    write_dashboard(dashboard)
    transitions = detect_transitions(dashboard, prev)
    log_transitions(transitions)
    save_snapshot(dashboard)
    print(json.dumps(dashboard["summary"], indent=2))
    if args.notify or args.telegram_dry_run:
        notify_alive_transitions(transitions, dry_run=args.telegram_dry_run)
    if transitions:
        print(f"transitions: {len(transitions)}")
        for t in transitions:
            print(f"  {t['line_id']}: {t['from']} -> {t['to']}")


if __name__ == "__main__":
    main()
