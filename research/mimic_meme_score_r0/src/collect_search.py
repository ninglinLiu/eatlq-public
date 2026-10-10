#!/usr/bin/env python3
"""Read-only DexScreener collector. Name-match, then earliest N=10 for scoring.

Writes:
  data/tables/contracts_all.csv  — all name-matched tokens
  data/tables/contracts.csv      — earliest 10 per event (GOAL §2.2)
  data/tables/collect_qa.json
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from common import (
    EVENTS_DIR,
    RAW,
    TABLES,
    dump_json,
    load_csv,
    pair_created_unix,
    term_matches,
    write_csv,
)
from no_live import ROOT, require_no_live

N_MAX = 10
USER_AGENT = "mimic-meme-score-r0/0.2 (research-readonly)"

CONTRACT_FIELDS = [
    "event_id",
    "contract_id",
    "chain",
    "address",
    "symbol",
    "name",
    "pair_address",
    "pair_created_at_unix",
    "liquidity_usd_at_collect",
    "volume_24h_usd_at_collect",
    "source",
    "match_term",
    "on_primary_chain",
    "rank_by_created",
    "in_score_set",
    "notes",
]


def _get(url: str, cache_name: str, sleep_s: float = 0.8) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    cache_path = RAW / cache_name
    if cache_path.is_file():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        err = {"error": str(e), "url": url}
        cache_path.write_text(json.dumps(err, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(sleep_s)
        return err
    cache_path.write_text(body, encoding="utf-8")
    time.sleep(sleep_s)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"error": "non_json", "url": url, "body_prefix": body[:200]}


def dexscreener_search(q: str) -> list[dict]:
    safe = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff.]+", "_", q)[:80]
    url = "https://api.dexscreener.com/latest/dex/search?" + urllib.parse.urlencode({"q": q})
    data = _get(url, f"dex_search_{safe}.json")
    if not isinstance(data, dict):
        return []
    pairs = data.get("pairs") or []
    return [p for p in pairs if isinstance(p, dict)]


def aliases_by_event() -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for r in load_csv(EVENTS_DIR / "aliases.csv"):
        out[r["event_id"]].append(r["term"])
    return out


def pair_to_row(event_id: str, primary: str, term: str, p: dict) -> dict | None:
    base = p.get("baseToken") or {}
    addr = (base.get("address") or "").strip()
    chain = (p.get("chainId") or "").strip()
    symbol = base.get("symbol") or ""
    name = base.get("name") or ""
    if not addr or not chain:
        return None
    if not term_matches(term, symbol, name):
        return None
    created = pair_created_unix(p)
    liq = (p.get("liquidity") or {}).get("usd")
    vol = (p.get("volume") or {}).get("h24")
    return {
        "event_id": event_id,
        "contract_id": f"{event_id}__{chain}__{addr.lower()[:10]}",
        "chain": chain,
        "address": addr,
        "symbol": symbol,
        "name": name,
        "pair_address": p.get("pairAddress") or "",
        "pair_created_at_unix": "" if created is None else str(created),
        "liquidity_usd_at_collect": "" if liq is None else str(liq),
        "volume_24h_usd_at_collect": "" if vol is None else str(vol),
        "source": "dexscreener",
        "match_term": term,
        "on_primary_chain": "1" if chain.lower() == primary.lower() else "0",
        "rank_by_created": "",
        "in_score_set": "0",
        "notes": (p.get("url") or "")[:200],
    }


def dedup_tokens(rows: list[dict]) -> list[dict]:
    """Keep earliest pair per chain:address; sum volume across pairs of same token."""
    best: dict[str, dict] = {}
    vol_sum: dict[str, float] = defaultdict(float)
    for r in rows:
        key = f"{r['chain']}:{r['address'].lower()}"
        try:
            vol_sum[key] += float(r["volume_24h_usd_at_collect"] or 0)
        except ValueError:
            pass
        cur = best.get(key)
        if cur is None:
            best[key] = r
            continue
        a = int(r["pair_created_at_unix"] or 10**18)
        b = int(cur["pair_created_at_unix"] or 10**18)
        if a < b:
            best[key] = r
    out = []
    for key, r in best.items():
        r = dict(r)
        r["volume_24h_usd_at_collect"] = str(vol_sum[key]) if vol_sum[key] else r["volume_24h_usd_at_collect"]
        out.append(r)
    return out


def collect_event(eid: str, primary: str, terms: list[str]) -> tuple[list[dict], dict]:
    raw_n = 0
    rows: list[dict] = []
    for term in terms:
        pairs = dexscreener_search(term)
        raw_n += len(pairs)
        for p in pairs:
            row = pair_to_row(eid, primary, term, p)
            if row:
                rows.append(row)
    rows = dedup_tokens(rows)
    rows.sort(key=lambda r: int(r["pair_created_at_unix"] or 10**18))
    for i, r in enumerate(rows, 1):
        r["rank_by_created"] = str(i)
        r["in_score_set"] = "1" if i <= N_MAX else "0"
    qa = {
        "event_id": eid,
        "n_raw_pairs": raw_n,
        "n_matched_tokens": len(rows),
        "n_score_set": min(len(rows), N_MAX),
        "n_primary_chain": sum(1 for r in rows if r["on_primary_chain"] == "1"),
        "terms": terms,
    }
    return rows, qa


def main() -> None:
    require_no_live()
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--out-all", default=str(TABLES / "contracts_all.csv"))
    ap.add_argument("--out", default=str(TABLES / "contracts.csv"))
    args = ap.parse_args()

    events = load_csv(TABLES / "events.csv")
    aliases = aliases_by_event()
    if args.smoke:
        events = [e for e in events if e["event_id"] == "E01_niulai"]

    all_rows: list[dict] = []
    qa_list: list[dict] = []
    for ev in events:
        eid = ev["event_id"]
        terms = aliases.get(eid) or [ev["search_term"]]
        print(f"[collect] {eid} terms={terms}")
        rows, qa = collect_event(eid, ev["chain_primary"], terms)
        all_rows.extend(rows)
        qa_list.append(qa)
        print(f"  raw_pairs={qa['n_raw_pairs']} matched={qa['n_matched_tokens']} score_set={qa['n_score_set']}")

    write_csv(Path(args.out_all), all_rows, CONTRACT_FIELDS)
    score_set = [r for r in all_rows if r["in_score_set"] == "1"]
    write_csv(Path(args.out), score_set, CONTRACT_FIELDS)
    dump_json(
        TABLES / "collect_qa.json",
        {
            "n_events": len(events),
            "n_matched_tokens": len(all_rows),
            "n_score_set": len(score_set),
            "events": qa_list,
        },
    )
    print(f"[done] all={len(all_rows)} score_set={len(score_set)}")


if __name__ == "__main__":
    main()
