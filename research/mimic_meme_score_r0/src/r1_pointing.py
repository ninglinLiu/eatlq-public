#!/usr/bin/env python3
"""R1 pointing log. Dated public pointer only. Does not rewrite R0 scores."""
from __future__ import annotations

import json
from pathlib import Path

from common import RAW, TABLES, inum, load_csv, write_csv
from http_cache import get_json
from no_live import require_no_live

POINT_FIELDS = [
    "event_id",
    "contract_id",
    "source",
    "url",
    "dated",
    "t_point_unix",
    "notes",
]
I1_FIELDS = [
    "event_id",
    "contract_id",
    "role",
    "i1_create_order",
    "i1_pointing",
    "t_point_unix",
    "t_attach_unix",
    "point_before_attach",
    "rationale",
]


def load_r0_i1() -> dict[str, str]:
    out: dict[str, str] = {}
    for r in load_csv(TABLES / "scores.csv"):
        if r.get("dim_id") == "I1":
            out[r["contract_id"]] = r["score"]
    return out


def socials_from_dex_cache() -> dict[str, list[dict]]:
    """address_lower -> list of undated/dated pointers from cached search JSON."""
    by_addr: dict[str, list[dict]] = {}
    for path in RAW.glob("dex_search_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        pairs = data.get("pairs") or []
        if not isinstance(pairs, list):
            continue
        for p in pairs:
            if not isinstance(p, dict):
                continue
            addr = ((p.get("baseToken") or {}).get("address") or "").strip().lower()
            if not addr:
                continue
            info = p.get("info") or {}
            items = []
            for s in info.get("socials") or []:
                if isinstance(s, dict) and s.get("url"):
                    items.append(
                        {
                            "source": f"dex_search_{s.get('type') or 'social'}",
                            "url": s["url"],
                            "dated": "0",
                            "t_point_unix": "",
                            "notes": f"cache={path.name};undated",
                        }
                    )
            for w in info.get("websites") or []:
                if isinstance(w, dict) and w.get("url"):
                    items.append(
                        {
                            "source": "dex_search_website",
                            "url": w["url"],
                            "dated": "0",
                            "t_point_unix": "",
                            "notes": f"cache={path.name};undated",
                        }
                    )
            if items:
                by_addr.setdefault(addr, []).extend(items)
    return by_addr


def fetch_token_info(chain: str, address: str) -> list[dict]:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
    data = get_json(url, f"dex_token_{chain}_{address[:16]}.json", sleep_s=0.8)
    if data.get("error"):
        return [
            {
                "source": "dex_token",
                "url": url,
                "dated": "0",
                "t_point_unix": "",
                "notes": f"error={data.get('error')}",
            }
        ]
    items = []
    for p in data.get("pairs") or []:
        if not isinstance(p, dict):
            continue
        info = p.get("info") or {}
        created = p.get("pairCreatedAt")
        # pairCreatedAt is pool birth, NOT a public finger-point
        for s in info.get("socials") or []:
            if isinstance(s, dict) and s.get("url"):
                items.append(
                    {
                        "source": f"dex_token_{s.get('type') or 'social'}",
                        "url": s["url"],
                        "dated": "0",
                        "t_point_unix": "",
                        "notes": "social_url_undated;pairCreatedAt_is_not_pointing",
                    }
                )
        if created and not items:
            items.append(
                {
                    "source": "dex_token_pair_created",
                    "url": p.get("url") or url,
                    "dated": "0",
                    "t_point_unix": "",
                    "notes": "pairCreatedAt_exists_but_not_a_public_pointer",
                }
            )
    return items


def main() -> None:
    require_no_live()
    events = load_csv(TABLES / "events.csv")
    all_rows = {r["contract_id"]: r for r in load_csv(TABLES / "contracts_all.csv")}
    paths = {r["event_id"]: r for r in load_csv(TABLES / "attach_path.csv")}
    r0_i1 = load_r0_i1()
    cache_socials = socials_from_dex_cache()

    targets: list[tuple[str, str, str]] = []
    for ev in events:
        eid = ev["event_id"]
        pr = paths.get(eid) or {}
        for role, cid in (
            ("path_attach", pr.get("path_attach_contract_id") or ""),
            ("earliest_primary", pr.get("earliest_primary_contract_id") or ""),
        ):
            if cid and cid in all_rows:
                targets.append((eid, cid, role))

    # unique contracts
    seen: set[str] = set()
    point_rows: list[dict] = []
    pointers_by_cid: dict[str, list[dict]] = {}
    for eid, cid, role in targets:
        if cid in seen:
            continue
        seen.add(cid)
        row = all_rows[cid]
        addr = row["address"]
        items = list(cache_socials.get(addr.lower(), []))
        items.extend(fetch_token_info(row["chain"], addr))
        if not items:
            items = [
                {
                    "source": "none",
                    "url": "",
                    "dated": "0",
                    "t_point_unix": "",
                    "notes": "no_socials_in_cache_or_token_endpoint",
                }
            ]
        for it in items:
            rec = {
                "event_id": eid,
                "contract_id": cid,
                "source": it["source"],
                "url": it.get("url") or "",
                "dated": it.get("dated") or "0",
                "t_point_unix": it.get("t_point_unix") or "",
                "notes": it.get("notes") or "",
            }
            point_rows.append(rec)
            pointers_by_cid.setdefault(cid, []).append(rec)

    i1_rows: list[dict] = []
    for eid, cid, role in targets:
        pr = paths.get(eid) or {}
        t_attach = pr.get("t_attach_unix") or ""
        dated = [p for p in pointers_by_cid.get(cid, []) if p.get("dated") == "1" and inum(p.get("t_point_unix"))]
        create = r0_i1.get(cid, "空")
        if dated:
            t_point = min(inum(p["t_point_unix"]) or 10**18 for p in dated)
            before = ""
            pointing = "空"
            if inum(t_attach) is not None:
                before = "1" if t_point < inum(t_attach) else "0"
                pointing = "高" if before == "1" else "空"
            rationale = "dated pointer exists"
        else:
            t_point = ""
            before = ""
            pointing = "空"
            rationale = "no_dated_public_pointer;undated_socials_do_not_count"
        i1_rows.append(
            {
                "event_id": eid,
                "contract_id": cid,
                "role": role,
                "i1_create_order": create,
                "i1_pointing": pointing,
                "t_point_unix": str(t_point) if t_point != "" else "",
                "t_attach_unix": t_attach,
                "point_before_attach": before,
                "rationale": rationale,
            }
        )

    write_csv(TABLES / "i1_pointing.csv", point_rows, POINT_FIELDS)
    write_csv(TABLES / "i1_r1.csv", i1_rows, I1_FIELDS)
    n_dated = sum(1 for r in point_rows if r["dated"] == "1")
    n_high = sum(1 for r in i1_rows if r["i1_pointing"] == "高")
    print(f"[done] pointing={len(point_rows)} dated={n_dated} i1_high={n_high}")


if __name__ == "__main__":
    main()
