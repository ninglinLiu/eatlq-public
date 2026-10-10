"""Normalize vendor snapshots into a single board table. Dune is overlay-only."""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.common import ROOT, norm_addr, read_json, safe_float, utc_now_iso, write_json

ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")
SOL_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def _iter_dicts(obj, acc=None):
    acc = acc if acc is not None else []
    if isinstance(obj, dict):
        acc.append(obj)
        for v in obj.values():
            _iter_dicts(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _iter_dicts(v, acc)
    return acc


def _pick(d: dict, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def rows_from_gmgn_json(payload, *, chain: str, captured_at: str, tag: str, window: str) -> list[dict]:
    rows = []
    for d in _iter_dicts(payload):
        addr = _pick(d, "address", "wallet_address", "wallet", "account")
        if not addr or not str(addr).startswith("0x"):
            continue
        rows.append(
            {
                "source": "gmgn",
                "chain": chain,
                "venue": "base_bsc_spot",
                "spot_reconcilable": True,
                "address": norm_addr(addr),
                "window": window,
                "tag": tag,
                "vendor_pnl": safe_float(_pick(d, "pnl_7d", "pnl_1d", "pnl_30d", "pnl", "realized_profit")),
                "vendor_pnl_pct": safe_float(_pick(d, "pnl_7d", "pnl_1d", "pnl_30d")),
                "vendor_realized_usd": safe_float(
                    _pick(d, "realized_profit", "realized_profit_7d", "profit", "total_profit")
                ),
                "vendor_winrate": safe_float(_pick(d, "winrate", "winrate_7d", "win_rate")),
                "vendor_txs": safe_float(_pick(d, "txs", "txs_7d", "tx_count")),
                "captured_at": captured_at,
                "fame_kind": "fame_now",
            }
        )
    # de-dupe address+window+tag
    seen = set()
    out = []
    for r in rows:
        key = (r["address"], r["window"], r["tag"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def rows_from_debot_discover(payload, *, captured_at: str, duration: str) -> list[dict]:
    """Debot open Discover = Hyperliquid perp. Tagged so it can never be
    reconciled against Base/BSC spot FIFO."""
    data = (payload or {}).get("data") or {}
    rows = []
    for d in data.get("records") or []:
        if not isinstance(d, dict):
            continue
        addr = norm_addr(d.get("wallet"))
        if not addr.startswith("0x"):
            continue
        rows.append(
            {
                "source": "debot",
                "chain": "hyperliquid",
                "venue": "hyperliquid_perp",
                "spot_reconcilable": False,
                "address": addr,
                "window": duration,
                "tag": d.get("tier") or "discover",
                "vendor_pnl": safe_float(d.get("realized_pnl")),
                "vendor_pnl_pct": None,
                "vendor_realized_usd": safe_float(d.get("realized_pnl")),
                "vendor_winrate": safe_float(d.get("win_rate")),
                "vendor_txs": safe_float(d.get("trade_count")),
                "vendor_long_pnl": safe_float(d.get("long_realized_pnl")),
                "vendor_short_pnl": safe_float(d.get("short_realized_pnl")),
                "vendor_max_drawdown_ratio": safe_float(d.get("max_drawdown_ratio")),
                "vendor_is_smart_money": bool(d.get("is_smart_money")),
                "captured_at": captured_at,
                "fame_kind": "fame_now",
            }
        )
    return rows


def rows_from_text(body: bytes, *, source: str, chain: str, captured_at: str) -> list[dict]:
    text = body.decode("utf-8", errors="replace")
    rows = []
    for addr in ADDR_RE.findall(text):
        rows.append(
            {
                "source": source,
                "chain": chain,
                "address": norm_addr(addr),
                "window": "unknown",
                "tag": "html_extract",
                "vendor_pnl": None,
                "vendor_pnl_pct": None,
                "vendor_realized_usd": None,
                "vendor_winrate": None,
                "vendor_txs": None,
                "captured_at": captured_at,
                "fame_kind": "fame_now",
            }
        )
    seen = set()
    out = []
    for r in rows:
        if r["address"] in seen:
            continue
        seen.add(r["address"])
        out.append(r)
    return out


def normalize() -> dict:
    boards: list[dict] = []
    source_status = {}
    for source in ("gmgn", "debot", "fomo"):
        summary_path = ROOT / "data/raw" / source / "collect_summary.json"
        if not summary_path.exists():
            source_status[source] = "SOURCE_MISSING"
            continue
        summary = read_json(summary_path)
        source_status[source] = summary.get("status", "SOURCE_BLOCKED")
        for snap in summary.get("snapshots", []):
            raw = snap.get("raw_path")
            if not raw or not Path(raw).exists():
                continue
            body = Path(raw).read_bytes()
            captured = snap.get("captured_at") or utc_now_iso()
            chain = snap.get("chain") or "unknown"
            parsed = None
            try:
                parsed = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                parsed = None
            if source == "gmgn" and isinstance(parsed, dict):
                boards.extend(
                    rows_from_gmgn_json(
                        parsed,
                        chain=chain,
                        captured_at=captured,
                        tag=str(snap.get("tag") or ""),
                        window=str(snap.get("window") or "7d"),
                    )
                )
            elif source == "debot" and snap.get("board") == "discover_wallets":
                boards.extend(
                    rows_from_debot_discover(
                        parsed,
                        captured_at=captured,
                        duration=str(snap.get("duration") or "all"),
                    )
                )
            elif source == "debot":
                # spot_smart_wallet refusal carries no rows
                continue
            elif isinstance(parsed, dict) and source == "fomo":
                traders = parsed.get("traders") or parsed.get("data") or []
                if isinstance(traders, list):
                    for t in traders:
                        if not isinstance(t, dict):
                            continue
                        wallets = t.get("wallets") or {}
                        evm = wallets.get("evm") or t.get("wallet") or t.get("address")
                        if evm:
                            boards.append(
                                {
                                    "source": "fomo",
                                    "chain": chain,
                                    "address": norm_addr(evm),
                                    "window": str(snap.get("window") or t.get("window") or "unknown"),
                                    "tag": t.get("handle") or "leaderboard",
                                    "vendor_pnl": safe_float(t.get("pnlUsd") or t.get("pnl")),
                                    "vendor_pnl_pct": None,
                                    "vendor_realized_usd": safe_float(t.get("pnlUsd")),
                                    "vendor_winrate": safe_float(t.get("winrate")),
                                    "vendor_txs": safe_float(t.get("trades")),
                                    "captured_at": captured,
                                    "fame_kind": "fame_now",
                                }
                            )
            else:
                boards.extend(rows_from_text(body, source=source, chain=chain, captured_at=captured))

    # drop empty / router-looking zero address
    boards = [b for b in boards if b["address"] and b["address"] != "0x" + "0" * 40]
    for b in boards:
        b.setdefault("venue", "unknown")
        b.setdefault("spot_reconcilable", False)

    unique_by_source = {}
    for b in boards:
        unique_by_source.setdefault(b["source"], set()).add(b["address"])

    n_sources_with_rows = sum(1 for s, addrs in unique_by_source.items() if addrs)
    lineage = "OK" if n_sources_with_rows else "DATA_LINEAGE_INSUFFICIENT"

    venue_counts: dict[str, int] = {}
    for b in boards:
        venue_counts[b["venue"]] = venue_counts.get(b["venue"], 0) + 1
    spot_addrs = {b["address"] for b in boards if b["spot_reconcilable"]}
    spot_sources = sorted({b["source"] for b in boards if b["spot_reconcilable"]})

    payload = {
        "generated_at": utc_now_iso(),
        "source_status": source_status,
        "n_rows": len(boards),
        "n_unique_addresses": len({b["address"] for b in boards}),
        "n_sources_with_rows": n_sources_with_rows,
        "unique_by_source": {k: len(v) for k, v in unique_by_source.items()},
        "venue_counts": venue_counts,
        "n_spot_reconcilable_addresses": len(spot_addrs),
        "spot_reconcilable_sources": spot_sources,
        "lineage": lineage,
        "lookahead_warning": "These addresses are fame_now as of captured_at. Not historical fame.",
        "venue_warning": (
            "Only rows with spot_reconcilable=true may be compared to Base/BSC "
            "ERC-20 FIFO. Debot Discover rows are Hyperliquid perp."
        ),
        "rows": boards,
    }
    write_json(ROOT / "data/normalized/boards.json", payload)
    return payload


if __name__ == "__main__":
    p = normalize()
    print(json.dumps({k: p[k] for k in ("lineage", "n_rows", "n_unique_addresses", "source_status")}, indent=2))
