#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase D: right-tail timing autopsy.

API hit100/hit300/hit1000 tokens from Phase B → windowed full replay.
No live trading; only right-tail cohort (~168 hit100).
"""
from __future__ import annotations

import json
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

from _pump_lowcost_lib import COST_RT, LC_ROOT, load_jsonl, save_json
from _pump_tail_realgas_replay import (
    SOL_USD,
    b58dec,
    collect_trades,
    reserves_near,
    rpc_url,
    simulate_roundtrip,
)
from _pump_tail_realgas_replay import RPC

OUT_DIR = Path(__file__).resolve().parent
PHASE_D_ROOT = LC_ROOT / "phase_d"

HIT100_MC = 15_000
HIT300_MC = 40_000
HIT1000_MC = 100_000

WINDOWS = [
    ("t0_t15", 0, 15),
    ("t15_t90", 15, 90),
    ("t90_t300", 90, 300),
]

COST = COST_RT
MAX_SIG_PAGES = 24


def load_phase_b() -> list[dict]:
    rows: list[dict] = []
    for p in sorted((LC_ROOT / "phase_b").glob("*/coins.jsonl")):
        rows.extend(load_jsonl(p))
    return rows


def load_phase_c_by_mint() -> dict[str, dict]:
    p = LC_ROOT / "phase_c" / "full_replay.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return {r["mint"]: r for r in data.get("results", [])}


def api_tiers(ath_mc: float | None) -> list[str]:
    ath = ath_mc or 0
    tiers = []
    if ath >= HIT1000_MC:
        tiers.append("hit1000")
    if ath >= HIT300_MC:
        tiers.append("hit300")
    if ath >= HIT100_MC:
        tiers.append("hit100")
    return tiers


def collect_trades_until(rpc: RPC, bc: str, until_t: float) -> tuple[list[tuple[float, str, bool]], bool]:
    rows: list[tuple[float, str, bool]] = []
    before = None
    reached = False
    for _ in range(MAX_SIG_PAGES):
        params = [bc, {"limit": 1000, **({"before": before} if before else {})}]
        sigs = rpc("getSignaturesForAddress", params) or []
        if not sigs:
            reached = True
            break
        before = sigs[-1]["signature"]
        for s in sigs:
            bt = s.get("blockTime")
            if bt is not None:
                rows.append((bt, s["signature"], bool(s.get("err"))))
        if (sigs[-1].get("blockTime") or 0) <= until_t:
            reached = True
            break
        if len(sigs) < 1000:
            reached = True
            break
    rows.sort()
    return rows, reached


def trade_counts(trades: list[tuple[float, str, bool]], created_s: float) -> dict[str, int | bool]:
    rel = [t - created_s for t, _, err in trades if not err and t >= created_s - 2]
    return {
        "n_trades_15s": sum(1 for x in rel if x <= 15),
        "n_trades_30s": sum(1 for x in rel if x <= 30),
        "n_trades_90s": sum(1 for x in rel if x <= 90),
        "n_trades_180s": sum(1 for x in rel if x <= 180),
        "alive_at_180s": any(120 <= x <= 180 for x in rel),
    }


def replay_window(
    rpc: RPC,
    trades: list[tuple[float, str, bool]],
    mint_b: bytes,
    created_s: float,
    entry_s: float,
    exit_s: float,
    tx_cache: dict,
) -> dict:
    entry_t = created_s + entry_s
    exit_t = created_s + exit_s
    ent = reserves_near(rpc, trades, entry_t, mint_b, tx_cache, before=True)
    ex = reserves_near(rpc, trades, exit_t, mint_b, tx_cache, before=True)
    sim10 = {}
    if ent and ex:
        sim10 = simulate_roundtrip((ent[0], ent[1]), (ex[0], ex[1]), 10 / SOL_USD)
    return {
        "entry_s": entry_s,
        "exit_s": exit_s,
        "ok": bool(ent and ex and sim10.get("ok")),
        "real_return_10U": sim10.get("real_return"),
        "spot_return": sim10.get("spot_return"),
        "tradability": "OK" if ent and ex else "NEEDS_RPC_REPLAY",
    }


def grad_exit_s(row: dict) -> float | None:
    if not row.get("graduated"):
        return None
    created_ms = row.get("created_ms")
    if not created_ms:
        return None
    created_s = created_ms / 1000.0
    t2ath = row.get("time_to_ath_s")
    if t2ath is not None and t2ath > 300:
        return float(t2ath)
    # fallback: extend 1h past T+300 for graduated tokens without ath timing
    return 3600.0


def classify_disconnect(row: dict, windows: dict[str, dict]) -> str:
    """Primary reason API right-tail ≠ T+15→T+90 tradability."""
    w015 = windows.get("t0_t15") or {}
    w1590 = windows.get("t15_t90") or {}
    w90300 = windows.get("t90_t300") or {}
    w300g = windows.get("t300_grad") or {}

    r1590 = w1590.get("real_return_10U")
    sparse = row.get("sparse_ret_t15_t90")
    ath = row.get("ath_mc") or 0
    t2ath = row.get("time_to_ath_s")
    graduated = bool(row.get("graduated"))

    if w1590.get("tradability") != "OK" or r1590 is None:
        return "untradable/failed_pricing"

    if r1590 is not None and r1590 >= 1.0:
        return "tradable_in_window"

    # sparse missed spike: full good, sparse floor
    if (
        r1590 is not None
        and r1590 >= 0.5
        and sparse is not None
        and sparse <= 0
        and abs(sparse + COST) < 0.02
    ):
        return "sparse_missed_spike"

    r015 = w015.get("real_return_10U")
    r90300 = w90300.get("real_return_10U")
    r300g = w300g.get("real_return_10U")

    # pre-entry already pumped: most gain before T+15
    if r015 is not None and r015 >= 0.5 and (r1590 is None or r1590 < 0.2):
        return "pre_entry_already_pumped"

    # post-exit / migration-only
    post_rets = [x for x in [r90300, r300g] if x is not None]
    if post_rets and max(post_rets) >= 0.5:
        if graduated and (t2ath is None or t2ath > 300):
            return "migration_only_pumped" if (t2ath or 0) > 600 else "post_exit_pumped"
        if t2ath is not None and t2ath > 90:
            return "post_exit_pumped"

    # sparse missed spike variant: API hit but sparse bad, full modest
    if sparse is not None and sparse <= 0 and ath >= HIT100_MC and r1590 is not None and r1590 > sparse + 0.2:
        return "sparse_missed_spike"

    if t2ath is not None and t2ath <= 15 and r1590 is not None and r1590 < 0:
        return "pre_entry_already_pumped"

    if r1590 is not None and r1590 <= -0.25:
        return "untradable/failed_pricing"

    return "within_window_but_negative"


def max_tradable_window(windows: dict[str, dict]) -> str | None:
    best_name = None
    best_ret = None
    for name, w in windows.items():
        r = w.get("real_return_10U")
        if r is None or not w.get("ok"):
            continue
        if best_ret is None or r > best_ret:
            best_ret = r
            best_name = name
    return best_name


def autopsy_one(rpc: RPC, row: dict, tx_cache: dict, phase_c: dict | None) -> dict:
    mint = row["mint"]
    bc = row.get("bonding_curve")
    created_ms = row.get("created_ms")
    if not bc or not created_ms:
        return {"mint": mint, "ok": False, "reason": "missing_bc_or_created"}

    created_s = created_ms / 1000.0
    mint_b = b58dec(mint)
    grad_s = grad_exit_s(row)
    until_t = created_s + max(300, grad_s or 0, row.get("time_to_ath_s") or 0) + 60
    trades, reached = collect_trades_until(rpc, bc, until_t)
    counts = trade_counts(trades, created_s)

    windows: dict[str, dict] = {}
    for name, entry_s, exit_s in WINDOWS:
        windows[name] = replay_window(rpc, trades, mint_b, created_s, entry_s, exit_s, tx_cache)

    if grad_s is not None and grad_s > 300:
        windows["t300_grad"] = replay_window(rpc, trades, mint_b, created_s, 300, grad_s, tx_cache)
    else:
        windows["t300_grad"] = {
            "entry_s": 300,
            "exit_s": None,
            "ok": False,
            "real_return_10U": None,
            "tradability": "N/A",
            "note": "not_graduated_or_too_early",
        }

    # overlay Phase C T+15→T+90 if available
    if phase_c and phase_c.get("real_return_10U") is not None:
        windows["t15_t90"]["real_return_10U"] = phase_c["real_return_10U"]
        windows["t15_t90"]["ok"] = phase_c.get("ok", False)
        windows["t15_t90"]["source"] = "phase_c_cache"

    disconnect = classify_disconnect(row, windows)
    max_win = max_tradable_window(windows)

    created_iso = datetime.fromtimestamp(created_s, tz=timezone.utc).isoformat()
    ath_ts = row.get("ath_ts")
    ath_iso = (
        datetime.fromtimestamp(ath_ts / 1000, tz=timezone.utc).isoformat() if ath_ts else None
    )

    return {
        "mint": mint,
        "day": row.get("day"),
        "api_tiers": api_tiers(row.get("ath_mc")),
        "ath_mc": row.get("ath_mc"),
        "ath_ts": ath_ts,
        "ath_time_utc": ath_iso,
        "time_to_ath_s": row.get("time_to_ath_s"),
        "graduated": bool(row.get("graduated")),
        "created_ms": created_ms,
        "created_time_utc": created_iso,
        "creator": row.get("creator"),
        "bonding_curve": bc,
        **counts,
        "alive_at_180s_phase_b": row.get("alive_at_180s"),
        "sparse_ret_t15_t90": row.get("sparse_ret_t15_t90"),
        "windows": windows,
        "disconnect_reason": disconnect,
        "max_tradable_window": max_win,
        "trades_collected": len(trades),
        "reached_birth": reached,
        "ok": any(w.get("ok") for w in windows.values()),
    }


def summarize(tokens: list[dict]) -> dict:
    reasons: dict[str, int] = {}
    for t in tokens:
        r = t.get("disconnect_reason", "unknown")
        reasons[r] = reasons.get(r, 0) + 1

    w1590 = [
        (t["windows"]["t15_t90"].get("real_return_10U"), t)
        for t in tokens
        if t.get("windows", {}).get("t15_t90", {}).get("real_return_10U") is not None
    ]
    rets = [x[0] for x in w1590]

    tier_counts = {"hit100": 0, "hit300": 0, "hit1000": 0}
    for t in tokens:
        for tier in t.get("api_tiers", []):
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

    return {
        "n_tokens": len(tokens),
        "tier_counts": tier_counts,
        "disconnect_reasons": reasons,
        "t15_t90": {
            "n_priced": len(rets),
            "median": st.median(rets) if rets else None,
            "mean": sum(rets) / len(rets) if rets else None,
            "p_ge_100": sum(1 for x in rets if x >= 1.0) / len(rets) if rets else None,
            "p_ge_300": sum(1 for x in rets if x >= 3.0) / len(rets) if rets else None,
        },
        "max_window_counts": {},
    }


def write_readout(tokens: list[dict], summary: dict, meta: dict, path: Path) -> None:
    lines = [
        "# Phase D: Right-Tail Timing Autopsy",
        "",
        f"> API hit100+ cohort from Phase B 14d | n=**{summary['n_tokens']}** | RPC=**{meta['rpc_calls']}**",
        "",
        "## 1. Cohort",
        "",
        "| tier | count | threshold |",
        "|------|-------|-----------|",
        f"| hit100 | {summary['tier_counts'].get('hit100', 0)} | ath_mc ≥ $15k |",
        f"| hit300 | {summary['tier_counts'].get('hit300', 0)} | ath_mc ≥ $40k |",
        f"| hit1000 | {summary['tier_counts'].get('hit1000', 0)} | ath_mc ≥ $100k |",
        "",
        "## 2. T+15→T+90 Full Replay（右尾 cohort）",
        "",
    ]
    t1590 = summary["t15_t90"]
    if t1590.get("n_priced"):
        lines.extend([
            f"| 指标 | 值 |",
            f"|------|-----|",
            f"| priced | {t1590['n_priced']} |",
            f"| median | {t1590['median']*100:.1f}% |",
            f"| mean | {t1590['mean']*100:.1f}% |",
            f"| ≥+100% | {t1590['p_ge_100']*100:.1f}% |",
            f"| ≥+300% | {t1590['p_ge_300']*100:.1f}% |",
            "",
        ])

    lines.extend([
        "## 3. API 右尾 vs T+15→T+90 脱节原因",
        "",
        "| reason | count | share |",
        "|--------|-------|-------|",
    ])
    n = summary["n_tokens"]
    for reason, cnt in sorted(summary["disconnect_reasons"].items(), key=lambda x: -x[1]):
        lines.append(f"| {reason} | {cnt} | {cnt/n*100:.1f}% |")

    lines.extend([
        "",
        "## 4. 代表 token（按 tier / reason）",
        "",
    ])

    # top winners T+15→T+90
    priced = [t for t in tokens if t.get("windows", {}).get("t15_t90", {}).get("real_return_10U") is not None]
    top = sorted(priced, key=lambda t: t["windows"]["t15_t90"]["real_return_10U"], reverse=True)[:8]
    lines.append("### Top T+15→T+90 full replay")
    lines.append("")
    lines.append("| mint | day | ath | tiers | t15→90 | sparse | reason | max_window |")
    lines.append("|------|-----|-----|-------|--------|--------|--------|------------|")
    for t in top:
        r = t["windows"]["t15_t90"]["real_return_10U"]
        sp = t.get("sparse_ret_t15_t90")
        sp_s = f"{sp*100:.0f}%" if sp is not None else "NA"
        lines.append(
            f"| `{t['mint'][:12]}…` | {t.get('day','')} | ${t.get('ath_mc',0):,.0f} | "
            f"{','.join(t.get('api_tiers',[]))} | {r*100:+.0f}% | {sp_s} | "
            f"{t.get('disconnect_reason','')} | {t.get('max_tradable_window','')} |"
        )

    # post-exit examples
    post = [t for t in tokens if t.get("disconnect_reason") in ("post_exit_pumped", "migration_only_pumped")][:5]
    if post:
        lines.extend(["", "### Post-exit / migration-only 样本", ""])
        for t in post:
            w = t["windows"]
            lines.append(
                f"- `{t['mint'][:16]}…` ath=${t.get('ath_mc',0):,.0f} "
                f"t2ath={t.get('time_to_ath_s')}s grad={t.get('graduated')} | "
                f"t15→90={_fmt(w['t15_t90'])} t90→300={_fmt(w['t90_t300'])} "
                f"t300→grad={_fmt(w.get('t300_grad',{}))}"
            )

    lines.extend([
        "",
        "## 5. 结论",
        "",
        _verdict(summary),
        "",
        f"原始数据：`data/pump_lowcost/phase_d/tail_autopsy.json`",
        "",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt(w: dict) -> str:
    r = w.get("real_return_10U") if w else None
    return f"{r*100:+.0f}%" if r is not None else "NA"


def _verdict(summary: dict) -> str:
    reasons = summary.get("disconnect_reasons", {})
    n = summary["n_tokens"]
    tradable = reasons.get("tradable_in_window", 0)
    post = reasons.get("post_exit_pumped", 0) + reasons.get("migration_only_pumped", 0)
    pre = reasons.get("pre_entry_already_pumped", 0)
    sparse = reasons.get("sparse_missed_spike", 0)
    fail = reasons.get("untradable/failed_pricing", 0)
    t1590 = summary.get("t15_t90", {})

    bullets = [
        f"- **{tradable}/{n}** ({tradable/n*100:.0f}%) API 右尾在 T+15→T+90 **可交易兑现**（full ≥+100%）",
        f"- **{post}/{n}** ({post/n*100:.0f}%) 主要涨幅在 **T+90 之后**（post-exit / migration）",
        f"- **{pre}/{n}** ({pre/n*100:.0f}%) **T+15 前已 pump**，标准入场太晚",
        f"- **{sparse}/{n}** ({sparse/n*100:.0f}%) sparse 快照 **漏 spike**（full >> sparse）",
        f"- **{fail}/{n}** ({fail/n*100:.0f}%) **定价/可交易失败**",
    ]
    if t1590.get("median") is not None:
        bullets.append(
            f"- 右尾 cohort T+15→T+90 median = **{t1590['median']*100:.1f}%**（API 标签 ≠ 该窗口可交易性）"
        )
    bullets.append(
        "\n**判定**：API hit100 衡量 universe 右尾频率；T+15→T+90 ex-ante 策略需单独验证 timing。"
        " 大部分脱节来自 **post-exit pump** 或 **pre-entry already pumped**，非 sparse  alone。"
    )
    return "\n".join(bullets)


def run_phase_d() -> None:
    PHASE_D_ROOT.mkdir(parents=True, exist_ok=True)
    rows = load_phase_b()
    cohort = [r for r in rows if (r.get("ath_mc") or 0) >= HIT100_MC]
    cohort.sort(key=lambda r: -(r.get("ath_mc") or 0))

    phase_c_map = load_phase_c_by_mint()
    print(f"Phase D: autopsy {len(cohort)} API hit100+ tokens", flush=True)

    rpc = RPC(rpc_url(), min_interval=0.20)
    rpc_before = rpc.calls
    tx_cache: dict = {}
    results: list[dict] = []

    for i, row in enumerate(cohort):
        pc = phase_c_map.get(row["mint"])
        r = autopsy_one(rpc, row, tx_cache, pc)
        results.append(r)
        if (i + 1) % 10 == 0:
            print(
                f"  [D] {i+1}/{len(cohort)} rpc={rpc.calls - rpc_before} "
                f"ok={sum(1 for x in results if x.get('ok'))}",
                flush=True,
            )

    rpc_used = rpc.calls - rpc_before
    summary = summarize(results)
    # max window distribution
    mw: dict[str, int] = {}
    for t in results:
        m = t.get("max_tradable_window") or "none"
        mw[m] = mw.get(m, 0) + 1
    summary["max_window_counts"] = mw

    out = PHASE_D_ROOT / "tail_autopsy.json"
    payload = {
        "meta": {
            "phase": "D",
            "n": len(results),
            "rpc_calls": rpc_used,
            "hit_thresholds": {"hit100": HIT100_MC, "hit300": HIT300_MC, "hit1000": HIT1000_MC},
            "windows": WINDOWS + [("t300_grad", 300, "grad")],
            "cost_rt": COST,
            "built_at": datetime.now(timezone.utc).isoformat(),
        },
        "summary": summary,
        "tokens": results,
    }
    save_json(out, payload)

    readout = OUT_DIR / "_pump_lowcost_phase_d_tail_timing_readout.md"
    write_readout(results, summary, payload["meta"], readout)
    print(f"Phase D -> {out} ({len(results)} tokens, rpc={rpc_used})")
    print(f"Readout -> {readout}")


if __name__ == "__main__":
    run_phase_d()
