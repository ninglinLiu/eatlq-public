"""Shared read-only helpers. No network. No chain writes."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from no_live import ROOT

TABLES = ROOT / "data" / "tables"
EVENTS_DIR = ROOT / "events"
RAW = ROOT / "data" / "raw"

CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", (s or "")).casefold()


def term_matches(term: str, symbol: str, name: str) -> bool:
    t, sy, nm = norm(term), norm(symbol), norm(name)
    if not t:
        return False
    if CJK_RE.search(term):
        return t in sy or t in nm
    if len(t) <= 3:
        return sy == t or nm == t
    return t in sy or t in nm


def pair_created_unix(pair: dict) -> int | None:
    v = pair.get("pairCreatedAt")
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n > 10**12:
        n //= 1000
    if n <= 0 or n >= 10**17:
        return None
    return n


def load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if not (r.get("event_id") or "").startswith("#")]


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def dump_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fnum(s: str | None) -> float:
    try:
        return float(s or 0)
    except ValueError:
        return 0.0


def inum(s: str | None) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None
