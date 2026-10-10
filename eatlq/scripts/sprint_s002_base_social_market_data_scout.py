#!/usr/bin/env python3
"""Sprint S002 — Base Social Market Data Scout (research only).

Cross-venue Base token market coverage around S001 social posts using OKX Onchain OS
Market/DEX APIs with DexScreener + GeckoTerminal fallbacks.

Forbidden: Redis signals, executor, live trading, model training, PK/swap/broadcast.
Secrets: never logged or written to artifacts — only presence flags.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
S001_DIR = REPO_ROOT / "mvp" / "audits" / "social_catalyst_s001"
OUT_DIR = REPO_ROOT / "mvp" / "audits" / "social_catalyst_s002"
OUT_DIR.mkdir(parents=True, exist_ok=True)

RAW_POSTS_CSV = S001_DIR / "raw_x_posts.csv"
ALIASES_CSV = S001_DIR / "cashtag_base_aliases.csv"
ALIAS_TODO_CSV = S001_DIR / "cashtag_alias_todo.csv"
VENUE_CSV = S001_DIR / "social_token_venue_inventory.csv"
LOCAL_VERIFY_CSV = S001_DIR / "local_pool_verification.csv"
S001_FLAGS_JSON = S001_DIR / "decision_flags.json"

PREFLIGHT_MD = OUT_DIR / "okx_api_preflight_readout.md"
COVERAGE_CSV = OUT_DIR / "token_data_source_coverage.csv"
EVENTS_CSV = OUT_DIR / "social_market_events.csv"
READOUT_MD = OUT_DIR / "base_social_market_data_scout_readout.md"
FLAGS_JSON = OUT_DIR / "decision_flags.json"

CHAIN_INDEX_BASE = "8453"
WETH_BASE = "0x4200000000000000000000000000000000000006"
OKX_HOST = "web3.okx.com"

PRIORITY_CASHTAGS = [
    "CLANKER",
    "AEON",
    "VVV",
    "UPEG",
    "LFI",
    "AUTONO",
    "DIEM",
    "AI16Z",
]

CASHTAG_RE = re.compile(r"(?<!\w)\$([A-Za-z][A-Za-z0-9_]{1,15})\b")
CA_RE = re.compile(r"\b(0x[a-fA-F0-9]{40})\b")


def _valid_evm_address(addr: str | None) -> bool:
    if not addr or not isinstance(addr, str):
        return False
    s = addr.strip().lower()
    return (
        len(s) == 42
        and s.startswith("0x")
        and all(c in "0123456789abcdef" for c in s[2:])
    )


CAP_OKX_SOFT = 500
CAP_OKX_HARD = 800
CAP_DEX_SOFT = 200
CAP_GECKO_SOFT = 100
CAP_PER_TOKEN_OKX = 30
CONSEC_ERRORS_STOP = 10
PREFLIGHT_MAX_OKX = 20
CAP_QUOTE_SMOKE = 50
QUOTE_CONSEC_FAIL_STOP = 10

# Base USDC (canonical)
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
# Native ETH placeholder (OKX / many aggregators)
ETH_NATIVE_PLACEHOLDER = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


def _iso_timestamp_ms(dt: datetime) -> str:
    dt = dt.astimezone(UTC)
    micro = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{micro:03d}Z"


def _present(val: str | None) -> bool:
    return bool(val and str(val).strip())


def _bool_flag(v: bool) -> str:
    return "true" if v else "false"


def _preflight_true_flag(v: Any) -> bool:
    return str(v or "").strip().lower() == "true"


def acceptance_gate_snapshot(preflight: dict[str, Any], budget: CallBudget) -> dict[str, Any]:
    """S002 acceptance: real OKX usage + market-path usability (≥2 of price/candles/trades)."""
    mkt = [
        _preflight_true_flag(preflight.get("OKX_PRICE_AVAILABLE")),
        _preflight_true_flag(preflight.get("OKX_CANDLES_AVAILABLE")),
        _preflight_true_flag(preflight.get("OKX_TRADES_AVAILABLE")),
    ]
    cnt = int(sum(mkt))
    return {
        "OKX_CALLS_USED": budget.okx,
        "OKX_REAL_PATH_RAN": budget.okx > 0,
        "OKX_MARKET_ENDPOINTS_TRUE_COUNT": cnt,
        "OKX_MARKET_PATH_USABLE": cnt >= 2,
        "OKX_MARKET_PATH_USABLE_RULE": (
            "OKX_MARKET_PATH_USABLE iff at least 2 of OKX_PRICE_AVAILABLE, "
            "OKX_CANDLES_AVAILABLE, OKX_TRADES_AVAILABLE are true."
        ),
    }


def env_credentials() -> tuple[bool, bool, bool, bool]:
    """Return presence-only tuple for required secrets."""
    pid = _present(os.environ.get("OKX_PROJECT_ID"))
    key = _present(os.environ.get("OKX_API_KEY"))
    sec = _present(os.environ.get("OKX_SECRET_KEY"))
    pw = _present(os.environ.get("OKX_API_PASSPHRASE")) or _present(
        os.environ.get("OKX_PASSPHRASE")
    )
    return key, sec, pw, pid


@dataclass
class CallBudget:
    okx: int = 0
    dex: int = 0
    gecko: int = 0
    okx_consecutive_errors: int = 0
    okx_blocked_reason: str = ""
    dex_consecutive_errors: int = 0
    gecko_consecutive_errors: int = 0

    def okx_ok(self) -> bool:
        if self.okx_blocked_reason:
            return False
        if self.okx >= CAP_OKX_HARD:
            self.okx_blocked_reason = "hard_cap"
            return False
        return True

    def note_okx_success(self) -> None:
        self.okx_consecutive_errors = 0

    def note_okx_error(self, is_auth: bool) -> None:
        self.okx_consecutive_errors += 1
        if is_auth:
            self.okx_blocked_reason = "auth_or_permission"
        elif self.okx_consecutive_errors >= CONSEC_ERRORS_STOP:
            self.okx_blocked_reason = "consecutive_errors"


class OKXClient:
    def __init__(self, budget: CallBudget):
        self.budget = budget
        self.api_key = (os.environ.get("OKX_API_KEY") or "").strip()
        self.secret_key = (os.environ.get("OKX_SECRET_KEY") or "").strip()
        self.passphrase = (
            (os.environ.get("OKX_API_PASSPHRASE") or "").strip()
            or (os.environ.get("OKX_PASSPHRASE") or "").strip()
        )
        self.project_id = (os.environ.get("OKX_PROJECT_ID") or "").strip()

    def _sign(self, ts: str, method: str, path_with_query: str, body: str) -> str:
        import base64
        import hashlib
        import hmac

        prehash = ts + method.upper() + path_with_query + body
        sig = hmac.new(
            self.secret_key.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(sig).decode("utf-8")

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body_obj: Any | None = None,
    ) -> tuple[int, dict[str, Any] | None, str]:
        """Returns (http_status, json_or_none, error_kind).

        error_kind: '', 'auth', 'http', 'rate', 'network', 'parse', 'cap'
        """
        import json as json_lib

        if not self.budget.okx_ok():
            return 0, None, "cap"
        if self.budget.okx >= CAP_OKX_SOFT:
            pass

        method_u = method.upper()
        query = ""
        if params and method_u == "GET":
            query = "?" + urlencode(params)
        path_q = path + query
        body_raw = ""
        if body_obj is not None and method_u == "POST":
            body_raw = json_lib.dumps(body_obj, separators=(",", ":"))

        ts = _iso_timestamp_ms(datetime.now(UTC))
        sign_path = path_q
        sign_body = body_raw if method_u == "POST" else ""
        sign = self._sign(ts, method_u, sign_path, sign_body)

        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "User-Agent": "EATLQ-S002-MarketScout/1.0",
        }
        if self.project_id:
            headers["OK-ACCESS-PROJECT"] = self.project_id

        url = f"https://{OKX_HOST}{path_q}"
        data_bytes = body_raw.encode("utf-8") if body_raw else None
        req = Request(url, data=data_bytes, headers=headers, method=method_u)

        self.budget.okx += 1
        try:
            with urlopen(req, timeout=35) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
                doc = json_lib.loads(raw)
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")[:500]
            auth_like = e.code in (401, 403) or "Unauthorized" in body
            rate_like = e.code == 429
            self.budget.note_okx_error(bool(auth_like))
            kind = "auth" if auth_like else ("rate" if rate_like else "http")
            return e.code, None, kind
        except URLError:
            self.budget.note_okx_error(False)
            return 0, None, "network"
        except json_lib.JSONDecodeError:
            self.budget.note_okx_error(False)
            return 0, None, "parse"

        code_str = str(doc.get("code", ""))
        if code_str and code_str != "0":
            msg = str(doc.get("msg", ""))[:200]
            auth_like = code_str in {"50004", "50105", "50111", "50112"} or any(
                x in msg.lower() for x in ("invalid api key", "sign", "timestamp", "passphrase")
            )
            rate_like = code_str in {"50008", "50011"} or "too many" in msg.lower()
            self.budget.note_okx_error(auth_like)
            kind = "auth" if auth_like else ("rate" if rate_like else "http")
            return 400, doc, kind

        self.budget.note_okx_success()
        return 200, doc, ""


def http_json_public(
    budget: CallBudget,
    which: str,
    url: str,
    headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    import json as json_lib

    hdrs = {"User-Agent": "EATLQ-S002-MarketScout/1.0"}
    if headers:
        hdrs.update(headers)
    if which == "dex":
        if budget.dex >= CAP_DEX_SOFT:
            return None, "cap"
        budget.dex += 1
    elif which == "gecko":
        if budget.gecko >= CAP_GECKO_SOFT:
            return None, "cap"
        if budget.gecko_consecutive_errors >= 4:
            return None, "gecko_errors"
        if budget.gecko > 0:
            time.sleep(0.7)
        budget.gecko += 1
    try:
        req = Request(url, headers=hdrs, method="GET")
        with urlopen(req, timeout=30) as resp:
            doc = json_lib.loads(resp.read().decode("utf-8", errors="ignore"))
        if which == "dex":
            budget.dex_consecutive_errors = 0
        else:
            budget.gecko_consecutive_errors = 0
        return doc, ""
    except HTTPError as e:
        if which == "dex":
            budget.dex_consecutive_errors += 1
        else:
            budget.gecko_consecutive_errors += 1
        if e.code == 429:
            return None, "rate"
        return None, f"http_{e.code}"
    except URLError:
        if which == "dex":
            budget.dex_consecutive_errors += 1
        else:
            budget.gecko_consecutive_errors += 1
        return None, "network"
    except json_lib.JSONDecodeError:
        return None, "parse"


def load_aliases() -> dict[str, str]:
    """cashtag_upper -> token_address lower."""
    out: dict[str, str] = {}
    if not ALIASES_CSV.exists():
        return out
    with ALIASES_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tag = (row.get("cashtag") or "").strip().upper()
            tok = (row.get("token_address") or "").strip().lower()
            chain = (row.get("chain") or "").strip().lower()
            if not tag or not tok:
                continue
            if chain and chain != "base":
                continue
            out[tag] = tok
    return out


def load_todo_cashtags() -> list[str]:
    tags: list[str] = []
    if not ALIAS_TODO_CSV.exists():
        return tags
    with ALIAS_TODO_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = (row.get("cashtag") or "").strip().upper()
            if t:
                tags.append(t)
    return tags


def load_venue_guess() -> dict[tuple[str, str], str]:
    """(tweet_id, mention_key_upper) -> venue_assessment."""
    m: dict[tuple[str, str], str] = {}
    if not VENUE_CSV.exists():
        return m
    with VENUE_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tid = (row.get("tweet_id") or "").strip()
            mv = (row.get("mention_value") or "").strip()
            va = (row.get("venue_assessment") or "").strip()
            if not tid:
                continue
            key = mv.upper() if mv.startswith("0X") else mv.upper()
            m[(tid, key)] = va
    return m


def load_raw_posts() -> list[dict[str, str]]:
    if not RAW_POSTS_CSV.exists():
        return []
    with RAW_POSTS_CSV.open(newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def reverse_alias(addr_lower: str, aliases: dict[str, str]) -> str | None:
    for sym, a in aliases.items():
        if a == addr_lower:
            return sym
    return None


def build_events(
    posts: list[dict[str, str]],
    aliases: dict[str, str],
    venue_map: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    events: dict[tuple[str, str], dict[str, Any]] = {}
    for row in posts:
        tid = (row.get("tweet_id") or "").strip()
        handle = (row.get("handle") or "").strip()
        created = (row.get("created_at") or "").strip()
        text = row.get("text") or ""
        if not tid:
            continue
        thash = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
        url = f"https://x.com/i/web/status/{tid}"

        found_syms: dict[str, tuple[str | None, str]] = {}
        for raw_tag in CASHTAG_RE.findall(text):
            sym = raw_tag.upper()
            addr = aliases.get(sym)
            conf = "high" if addr else "needs_alias"
            found_syms[sym] = (addr, conf)

        for ca in CA_RE.findall(text):
            low = ca.lower()
            sym = reverse_alias(low, aliases)
            if sym:
                found_syms.setdefault(sym, (low, "high"))
            else:
                ghost = f"CA:{low[:10]}"
                found_syms.setdefault(ghost, (low, "needs_alias"))

        for sym, (addr, conf) in found_syms.items():
            vm = ""
            for k in (sym, sym.replace("CA:", "")):
                vm = venue_map.get((tid, k), "") or vm
            key = (tid, (addr or sym).lower())
            prev = events.get(key)
            if prev and prev["alias_confidence"] == "high" and conf != "high":
                continue
            events[key] = {
                "author": handle,
                "post_id": tid,
                "post_time_utc": created,
                "token_symbol": sym if not sym.startswith("CA:") else "",
                "token_address": addr or "",
                "chain": "base",
                "source_post_url": url,
                "text_hash_short": thash,
                "alias_confidence": conf,
                "venue_guess": vm,
            }
    return sorted(events.values(), key=lambda x: (x["post_time_utc"], x["post_id"]))


def okx_price_batch(client: OKXClient, addresses: list[str]) -> dict[str, bool]:
    if not addresses:
        return {}
    body = [
        {"chainIndex": CHAIN_INDEX_BASE, "tokenContractAddress": a.lower()}
        for a in addresses
    ]
    st, doc, kind = client.request("POST", "/api/v6/dex/market/price", body_obj=body)
    ok: dict[str, bool] = {a.lower(): False for a in addresses}
    if st != 200 or not doc or kind:
        return ok
    data = doc.get("data") or []
    if isinstance(data, list):
        for item in data:
            addr = str(item.get("tokenContractAddress", "")).lower()
            pr = item.get("price")
            if addr and pr not in (None, "", "null"):
                ok[addr] = True
    return ok


def okx_candles(
    client: OKXClient,
    token: str,
    bar: str = "1m",
    limit: str = "120",
    before_ms: int | None = None,
    after_ms: int | None = None,
) -> tuple[list[list[str]], str]:
    params: dict[str, Any] = {
        "chainIndex": CHAIN_INDEX_BASE,
        "tokenContractAddress": token.lower(),
        "bar": bar,
        "limit": limit,
    }
    if before_ms is not None:
        params["before"] = str(before_ms)
    if after_ms is not None:
        params["after"] = str(after_ms)
    st, doc, kind = client.request("GET", "/api/v6/dex/market/candles", params=params)
    if st != 200 or not doc or kind:
        return [], kind or "empty"
    data = doc.get("data") or []
    rows = [x for x in data if isinstance(x, list) and len(x) >= 6]
    return rows, ""


def okx_trades(client: OKXClient, token: str, lim: str = "100") -> tuple[list[dict[str, Any]], str]:
    params = {
        "chainIndex": CHAIN_INDEX_BASE,
        "tokenContractAddress": token.lower(),
        "limit": lim,
    }
    st, doc, kind = client.request("GET", "/api/v6/dex/market/trades", params=params)
    if st != 200 or not doc or kind:
        return [], kind or "empty"
    data = doc.get("data") or []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)], ""
    return [], ""


def okx_quote(
    client: OKXClient,
    from_token: str,
    to_token: str,
    amount: str,
    swap_mode: str = "exactIn",
) -> tuple[bool, str]:
    """GET aggregator quote only (no swap). Returns (ok, reason_bucket)."""
    params = {
        "chainIndex": CHAIN_INDEX_BASE,
        "amount": amount,
        "swapMode": swap_mode,
        "fromTokenAddress": from_token.lower(),
        "toTokenAddress": to_token.lower(),
    }
    st, doc, kind = client.request("GET", "/api/v5/dex/aggregator/quote", params=params)
    if kind == "cap":
        return False, "cap"
    if kind == "auth":
        return False, "auth"
    if kind == "rate":
        return False, "rate"
    if not doc:
        return False, kind or "no_body"
    code_raw = doc.get("code")
    if code_raw is not None and str(code_raw) != "0":
        return False, f"api_code_{code_raw}"
    data = doc.get("data")
    if isinstance(data, list) and len(data) > 0:
        row = data[0]
        if isinstance(row, dict) and row.get("dexRouterList"):
            return True, "ok"
    return False, "no_route"


def okx_all_token_addresses_sample(
    client: OKXClient, budget: CallBudget, sample: int = 20
) -> list[str]:
    """Try documented token-list style endpoints; return lowercase addresses."""
    if not budget.okx_ok():
        return []
    paths = (
        "/api/v5/dex/aggregator/all-tokens",
        "/api/v6/dex/aggregator/all-tokens",
    )
    params = {"chainIndex": CHAIN_INDEX_BASE}
    for path in paths:
        st, doc, kind = client.request("GET", path, params=params)
        if kind == "cap" or not budget.okx_ok():
            return []
        if st != 200 or not doc or str(doc.get("code")) != "0":
            continue
        data = doc.get("data")
        if not isinstance(data, list):
            continue
        out: list[str] = []
        for item in data:
            if len(out) >= sample:
                break
            if not isinstance(item, dict):
                continue
            a = (
                item.get("tokenContractAddress")
                or item.get("tokenAddress")
                or item.get("address")
            )
            if _valid_evm_address(str(a or "")):
                al = str(a).strip().lower()
                if al not in out:
                    out.append(al)
        if out:
            return out
    return []


def run_quote_smoke(auth: bool) -> tuple[dict[str, Any], int]:
    """Isolated quote probes (hard cap CAP_QUOTE_SMOKE OKX calls).

    Uses a dedicated CallBudget so quote-path auth/rate issues do not block
    the rest of the scout's OKX market endpoints on the main budget.
    """
    from collections import Counter

    out: dict[str, Any] = {
        "OKX_QUOTE_AVAILABLE": "false",
        "quote_smoke_attempts": 0,
        "quote_error_top_reason": "n/a",
        "quote_smoke_blocker": "",
    }
    if not auth:
        return out, 0

    qbudget = CallBudget()
    qclient = OKXClient(qbudget)

    clanker = "0x1bc0c42215582d5a085795f4badbac3ff36d1bcb".lower()
    weth = WETH_BASE.lower()
    reasons: Counter[str] = Counter()
    consec_fail = 0
    attempts = 0

    trials: list[tuple[str, str, str, str]] = [
        (weth, clanker, "1000000000000000", "exactIn"),
        (ETH_NATIVE_PLACEHOLDER, clanker, "1000000000000000", "exactIn"),
        (clanker, weth, "1000000000000000000", "exactIn"),
        (USDC_BASE.lower(), clanker, "10000", "exactIn"),
        (clanker, USDC_BASE.lower(), "1000000000000000000", "exactIn"),
    ]

    def attempt(ft: str, tt: str, amt: str, sm: str) -> bool:
        nonlocal attempts, consec_fail
        if attempts >= CAP_QUOTE_SMOKE or consec_fail >= QUOTE_CONSEC_FAIL_STOP:
            return False
        if not qbudget.okx_ok():
            out["quote_smoke_blocker"] = out["quote_smoke_blocker"] or "quote_budget_blocked"
            return False
        ok, reason = okx_quote(qclient, ft, tt, amt, sm)
        attempts += 1
        reasons[reason] += 1
        out["quote_smoke_attempts"] = attempts
        if ok:
            consec_fail = 0
            out["OKX_QUOTE_AVAILABLE"] = "true"
            return True
        consec_fail += 1
        if reason == "auth":
            out["quote_smoke_blocker"] = "auth_or_permission"
        return False

    for tup in trials:
        if attempt(*tup):
            break
        if out["quote_smoke_blocker"] == "auth_or_permission":
            break

    if out["OKX_QUOTE_AVAILABLE"] == "false" and attempts < CAP_QUOTE_SMOKE:
        if consec_fail < QUOTE_CONSEC_FAIL_STOP and qbudget.okx_ok():
            extras = okx_all_token_addresses_sample(qclient, qbudget, sample=25)
            for tok in extras:
                if tok in (weth, clanker):
                    continue
                if attempt(weth, tok, "1000000000000000", "exactIn"):
                    break
                if out["quote_smoke_blocker"] == "auth_or_permission":
                    break
                if consec_fail >= QUOTE_CONSEC_FAIL_STOP:
                    out["quote_smoke_blocker"] = (
                        out["quote_smoke_blocker"] or "quote_consecutive_failures"
                    )
                    break
                if attempts >= CAP_QUOTE_SMOKE:
                    break

    if consec_fail >= QUOTE_CONSEC_FAIL_STOP and out["OKX_QUOTE_AVAILABLE"] == "false":
        out["quote_smoke_blocker"] = out["quote_smoke_blocker"] or "quote_consecutive_failures"

    if reasons:
        out["quote_error_top_reason"] = reasons.most_common(1)[0][0]
    return out, qbudget.okx


def okx_chain_liquidity_sources(client: OKXClient) -> tuple[bool, str]:
    params = {"chainIndex": CHAIN_INDEX_BASE}
    st, doc, kind = client.request(
        "GET", "/api/v6/dex/aggregator/get-liquidity", params=params
    )
    if st != 200 or not doc or kind:
        return False, kind
    data = doc.get("data")
    return bool(isinstance(data, list) and len(data) > 0), ""


def dexscreener_token(budget: CallBudget, addr: str) -> tuple[bool, str | None]:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{addr.lower()}"
    doc, err = http_json_public(budget, "dex", url)
    if err:
        return False, None
    pairs = (doc or {}).get("pairs") or []
    base_pairs = [
        p
        for p in pairs
        if isinstance(p, dict)
        and str(p.get("chainId", "")).lower() == "base"
    ]
    if not base_pairs:
        return False, None
    top = sorted(
        base_pairs,
        key=lambda p: float(p.get("liquidity", {}).get("usd") or 0),
        reverse=True,
    )[0]
    dex_id = str(top.get("dexId") or "")
    return True, dex_id


def gecko_ohlcv_available(
    budget: CallBudget, token_lower: str
) -> tuple[bool, str | None]:
    """Pick deepest Gecko pool with a valid 20-byte pool address (one HTTP call)."""
    pools_url = (
        f"https://api.geckoterminal.com/api/v2/networks/base/tokens/"
        f"{token_lower}/pools?page=1"
    )
    doc, err = http_json_public(budget, "gecko", pools_url)
    if err or not doc:
        return False, None
    pdata = doc.get("data") or []
    if not pdata:
        return False, None
    pool_addr = None
    best_liq = -1.0
    for item in pdata:
        if not isinstance(item, dict):
            continue
        attr = item.get("attributes") or {}
        addr = attr.get("address")
        if not _valid_evm_address(addr):
            continue
        liq = float(
            attr.get("reserve_in_usd")
            or attr.get("volume_usd")
            or attr.get("fdv_usd")
            or 0
        )
        if addr and liq >= best_liq:
            best_liq = liq
            pool_addr = addr.strip().lower()
    if not pool_addr:
        return False, None
    return True, pool_addr


def gecko_fetch_window(
    budget: CallBudget, pool_addr: str, before_ts_sec: int, limit: int = 300
) -> list[tuple[int, float, float]]:
    """Return list of (open_time_ms, close_usd, volume_usd) sorted ascending."""
    url = (
        f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
        f"{pool_addr}/ohlcv/minute?aggregate=1&limit={limit}&currency=usd"
        f"&before_timestamp={before_ts_sec}"
    )
    doc, err = http_json_public(budget, "gecko", url)
    out: list[tuple[int, float, float]] = []
    if err or not doc:
        return out
    data = doc.get("data") or {}
    attrs = data.get("attributes") or {}
    ohlcv = attrs.get("ohlcv_list") or []
    for row in ohlcv:
        if isinstance(row, list) and len(row) >= 5:
            ts_sec = int(row[0])
            close = float(row[4])
            vol = float(row[5]) if len(row) > 5 else 0.0
            out.append((ts_sec * 1000, close, vol))
    out.sort(key=lambda x: x[0])
    return out


def candles_to_series(rows: list[list[str]]) -> list[tuple[int, float, float]]:
    """OKX candle rows -> sorted (open_ms, close, volUsd)."""
    parsed: list[tuple[int, float, float]] = []
    for row in rows:
        try:
            ts = int(float(row[0]))
            c = float(row[4])
            vol_usd = float(row[6]) if len(row) > 6 else float(row[5])
        except (ValueError, IndexError):
            continue
        parsed.append((ts, c, vol_usd))
    parsed.sort(key=lambda x: x[0])
    return parsed


def price_at_or_before(series: list[tuple[int, float, float]], t_ms: int) -> float | None:
    best = None
    for ts, c, _ in series:
        if ts <= t_ms:
            best = c
        else:
            break
    return best


def slice_forward(series: list[tuple[int, float, float]], t_ms: int, horizon_ms: int):
    return [(ts, c, v) for ts, c, v in series if t_ms <= ts <= t_ms + horizon_ms]


def max_drawdown_from_closes(closes: list[float]) -> float | None:
    if len(closes) < 2:
        return None
    peak = closes[0]
    max_dd = 0.0
    for p in closes:
        peak = max(peak, p)
        dd = (p / peak) - 1.0 if peak else 0.0
        max_dd = min(max_dd, dd)
    return max_dd


def compute_trade_counts(
    trades: list[dict[str, Any]], t0_ms: int, windows: list[int]
) -> dict[int, tuple[int, int]]:
    """For each window seconds: (count_after, count_before_same_length)."""
    times = sorted(int(str(x.get("time") or 0)) for x in trades if x.get("time"))
    out: dict[int, tuple[int, int]] = {}
    for wsec in windows:
        wms = wsec * 1000
        after = sum(1 for t in times if t0_ms <= t <= t0_ms + wms)
        before = sum(1 for t in times if t0_ms - wms <= t < t0_ms)
        out[wsec] = (after, before)
    return out


def run_preflight(client: OKXClient | None, budget: CallBudget) -> dict[str, Any]:
    key_ok, sec_ok, pw_ok, pid_ok = env_credentials()
    auth_present = key_ok and sec_ok and pw_ok
    out: dict[str, Any] = {
        "OKX_AUTH_PRESENT": _bool_flag(auth_present),
        "OKX_PROJECT_ID_PRESENT": _bool_flag(pid_ok),
        "OKX_SECRET_PRESENT": _bool_flag(sec_ok),
        "OKX_PASSPHRASE_PRESENT": _bool_flag(pw_ok),
        "OKX_PRICE_AVAILABLE": "false",
        "OKX_CANDLES_AVAILABLE": "false",
        "OKX_TRADES_AVAILABLE": "false",
        "OKX_QUOTE_AVAILABLE": "false",
        "preflight_calls_used": 0,
        "preflight_notes": [],
        "preflight_error_type": "",
    }
    if not auth_present or client is None:
        out["preflight_notes"].append("missing_credentials_or_client")
        out["preflight_error_type"] = "no_auth"
        return out

    clanker = "0x1bc0c42215582d5a085795f4badbac3ff36d1bcb"
    okx0 = budget.okx

    def cap_ok() -> bool:
        return (budget.okx - okx0) < PREFLIGHT_MAX_OKX and budget.okx_ok()

    # Price
    if cap_ok():
        pb = okx_price_batch(client, [clanker])
        out["OKX_PRICE_AVAILABLE"] = _bool_flag(pb.get(clanker.lower(), False))

    # Candles
    if cap_ok():
        rows, ck = okx_candles(client, clanker, limit="60")
        out["OKX_CANDLES_AVAILABLE"] = _bool_flag(bool(rows) and not ck)

    # Trades
    if cap_ok():
        tr, tk = okx_trades(client, clanker, lim="30")
        out["OKX_TRADES_AVAILABLE"] = _bool_flag(bool(tr) and not tk)

    out["preflight_calls_used"] = budget.okx - okx0
    if budget.okx_blocked_reason:
        out["preflight_error_type"] = budget.okx_blocked_reason
        out["preflight_notes"].append(budget.okx_blocked_reason)

    return out


def token_coverage_row(
    client: OKXClient | None,
    budget: CallBudget,
    symbol: str,
    addr_lower: str,
    chain_liquidity_ok: bool,
    token_okx_used: dict[str, int],
) -> dict[str, Any]:
    """Per-token coverage; respects per-token OKX cap."""
    row: dict[str, Any] = {
        "token_symbol": symbol,
        "token_address": addr_lower,
        "okx_price_available": False,
        "okx_candles_available": False,
        "okx_trade_activity_available": False,
        "okx_liquidity_sources_available": False,
        "okx_quote_buy_available": False,
        "okx_quote_sell_available": False,
        "dexscreener_pair_available": False,
        "dexscreener_primary_dex": "",
        "gecko_terminal_ohlcv_available": False,
        "gecko_terminal_pool_id": "",
        "primary_venue": "",
        "can_compute_5m_return": False,
        "can_compute_15m_return": False,
        "can_compute_30m_return": False,
        "notes": "",
    }

    def tok_cap() -> bool:
        return token_okx_used.get(addr_lower, 0) < CAP_PER_TOKEN_OKX

    row["okx_liquidity_sources_available"] = chain_liquidity_ok

    if client and budget.okx_ok() and tok_cap():
        pr = okx_price_batch(client, [addr_lower])
        token_okx_used[addr_lower] = token_okx_used.get(addr_lower, 0) + 1
        row["okx_price_available"] = pr.get(addr_lower, False)

    if client and budget.okx_ok() and tok_cap():
        candles, ck = okx_candles(client, addr_lower, limit="40")
        token_okx_used[addr_lower] = token_okx_used.get(addr_lower, 0) + 1
        row["okx_candles_available"] = bool(candles) and not ck

    if client and budget.okx_ok() and tok_cap():
        trades, tk = okx_trades(client, addr_lower, lim="100")
        token_okx_used[addr_lower] = token_okx_used.get(addr_lower, 0) + 1
        row["okx_trade_activity_available"] = bool(trades) and not tk

    tiny = "1000000000000000"
    if client and budget.okx_ok() and tok_cap():
        qb, _ = okx_quote(client, WETH_BASE, addr_lower, tiny, "exactIn")
        token_okx_used[addr_lower] = token_okx_used.get(addr_lower, 0) + 1
        row["okx_quote_buy_available"] = qb

    if client and budget.okx_ok() and tok_cap():
        qs, _ = okx_quote(client, addr_lower, WETH_BASE, tiny, "exactIn")
        token_okx_used[addr_lower] = token_okx_used.get(addr_lower, 0) + 1
        row["okx_quote_sell_available"] = qs

    ds_ok, ds_dex = dexscreener_token(budget, addr_lower)
    row["dexscreener_pair_available"] = ds_ok
    row["dexscreener_primary_dex"] = ds_dex or ""

    g_ok, g_pool = gecko_ohlcv_available(budget, addr_lower)
    row["gecko_terminal_ohlcv_available"] = g_ok
    row["gecko_terminal_pool_id"] = g_pool or ""

    if row["okx_candles_available"]:
        row["primary_venue"] = "okx_market_candles"
        row["can_compute_5m_return"] = True
        row["can_compute_15m_return"] = True
        row["can_compute_30m_return"] = True
    elif g_ok:
        row["primary_venue"] = "gecko_terminal"
        row["can_compute_5m_return"] = True
        row["can_compute_15m_return"] = True
        row["can_compute_30m_return"] = True
    elif ds_ok:
        row["primary_venue"] = "dexscreener_snapshot_only"
        row["notes"] = "pair_found_no_intrabar_history_in_scout"

    if budget.okx_blocked_reason:
        row["notes"] = (row["notes"] + ";" if row["notes"] else "") + budget.okx_blocked_reason

    return row


def series_covers_window(
    series: list[tuple[int, float, float]], t_min: int, t_max: int
) -> bool:
    if not series:
        return False
    ts0 = series[0][0]
    ts1 = series[-1][0]
    return ts0 <= t_min + 60_000 and ts1 >= t_max - 120_000


def fetch_trades_cache(
    client: OKXClient | None,
    budget: CallBudget,
    coverage_by_addr: dict[str, dict[str, Any]],
    token_okx_used: dict[str, int],
) -> dict[str, list[dict[str, Any]]]:
    """One OKX trades fetch per token when coverage marks activity available."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not client:
        return out
    for addr, cov in coverage_by_addr.items():
        if not cov.get("okx_trade_activity_available"):
            continue
        if token_okx_used.get(addr, 0) >= CAP_PER_TOKEN_OKX:
            continue
        if not budget.okx_ok():
            break
        trades, _ = okx_trades(client, addr, lim="100")
        token_okx_used[addr] = token_okx_used.get(addr, 0) + 1
        out[addr] = trades
    return out


def build_price_series_cache(
    events: list[dict[str, Any]],
    coverage_by_addr: dict[str, dict[str, Any]],
    client: OKXClient | None,
    budget: CallBudget,
    token_okx_used: dict[str, int],
) -> dict[str, tuple[list[tuple[int, float, float]], str]]:
    """Per-token OHLCV: OKX candles first if they cover the event window; else Gecko.

    DexScreener public endpoints used here expose pair snapshots, not minute OHLCV,
    so intrabar returns fall back to Gecko Terminal when OKX is insufficient.
    """
    by_addr: dict[str, list[int]] = {}
    for ev in events:
        if ev.get("alias_confidence") == "needs_alias":
            continue
        addr = (ev.get("token_address") or "").strip().lower()
        if not addr:
            continue
        ts_str = ev.get("post_time_utc") or ""
        try:
            ts_iso = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
            t0_ms = int(datetime.fromisoformat(ts_iso).astimezone(UTC).timestamp() * 1000)
        except ValueError:
            continue
        by_addr.setdefault(addr, []).append(t0_ms)

    cache: dict[str, tuple[list[tuple[int, float, float]], str]] = {}
    pad_before_ms = 20 * 60 * 1000
    pad_after_ms = 45 * 60 * 1000

    for addr, times in by_addr.items():
        cov = coverage_by_addr.get(addr, {})
        t_min = min(times) - pad_before_ms
        t_max = max(times) + pad_after_ms

        okx_series: list[tuple[int, float, float]] = []
        if (
            client
            and cov.get("okx_candles_available")
            and budget.okx_ok()
            and token_okx_used.get(addr, 0) < CAP_PER_TOKEN_OKX
        ):
            rows, err = okx_candles(
                client,
                addr,
                bar="1m",
                limit="299",
                before_ms=t_max,
                after_ms=t_min,
            )
            token_okx_used[addr] = token_okx_used.get(addr, 0) + 1
            okx_series = candles_to_series(rows)
            okx_ok = (
                bool(okx_series)
                and not err
                and len(okx_series) >= 5
                and series_covers_window(okx_series, t_min, t_max)
            )
            if okx_ok:
                cache[addr] = (okx_series, "okx_market_candles")
                continue

        gecko_pool = str(cov.get("gecko_terminal_pool_id") or "")
        pool = gecko_pool
        if not pool:
            _gk, p2 = gecko_ohlcv_available(budget, addr)
            pool = str(p2 or "")
        if pool:
            gser = gecko_fetch_window(
                budget, pool, before_ts_sec=int(t_max // 1000) + 120, limit=300
            )
            if gser:
                cache[addr] = (gser, "gecko_terminal")
    return cache


def enrich_events_with_metrics(
    events: list[dict[str, Any]],
    coverage_by_addr: dict[str, dict[str, Any]],
    series_cache: dict[str, tuple[list[tuple[int, float, float]], str]],
    trades_cache: dict[str, list[dict[str, Any]]],
) -> None:
    """Populate per-event metrics using shared price/trades caches."""
    for ev in events:
        addr = (ev.get("token_address") or "").strip().lower()
        ev["ret_5m"] = ""
        ev["ret_15m"] = ""
        ev["ret_30m"] = ""
        ev["volume_change_5m"] = ""
        ev["volume_change_15m"] = ""
        ev["volume_change_30m"] = ""
        ev["tx_count_change_5m"] = ""
        ev["tx_count_change_15m"] = ""
        ev["tx_count_change_30m"] = ""
        ev["max_drawdown_30m"] = ""
        ev["data_source_used"] = ""
        ev["fallback_used"] = "false"
        ev["skip_reason"] = ""

        if ev.get("alias_confidence") == "needs_alias" or not addr:
            ev["skip_reason"] = "NEEDS_ALIAS_OR_NO_ADDRESS"
            continue

        cov = coverage_by_addr.get(addr, {})
        ts_str = ev.get("post_time_utc") or ""
        try:
            ts_iso = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
            t0 = datetime.fromisoformat(ts_iso).astimezone(UTC)
            t0_ms = int(t0.timestamp() * 1000)
        except ValueError:
            ev["skip_reason"] = "BAD_TIMESTAMP"
            continue

        cached = series_cache.get(addr)
        if not cached:
            ev["skip_reason"] = "NO_PRICE_SERIES"
            continue

        series, source = cached
        ev["fallback_used"] = _bool_flag(source != "okx_market_candles")
        ev["data_source_used"] = source

        p0 = price_at_or_before(series, t0_ms)
        if p0 is None or p0 <= 0:
            ev["skip_reason"] = "NO_ENTRY_PRICE_BEFORE_EVENT"
            continue

        def ret_after(sec: int) -> float | None:
            p1 = price_at_or_before(series, t0_ms + sec * 1000)
            if p1 is None:
                return None
            return (p1 / p0) - 1.0

        for sec, key in ((300, "ret_5m"), (900, "ret_15m"), (1800, "ret_30m")):
            r = ret_after(sec)
            ev[key] = f"{r:.6f}" if r is not None else ""

        win = slice_forward(series, t0_ms, 1800 * 1000)
        closes = [c for _, c, _ in win]
        mdd = max_drawdown_from_closes(closes)
        ev["max_drawdown_30m"] = f"{mdd:.6f}" if mdd is not None else ""

        def vol_sum(ms: int) -> float:
            return sum(v for ts, _, v in series if t0_ms <= ts <= t0_ms + ms)

        def vol_prev(ms: int) -> float:
            return sum(v for ts, _, v in series if t0_ms - ms <= ts < t0_ms)

        for ms, lab in (
            (300_000, "volume_change_5m"),
            (900_000, "volume_change_15m"),
            (1_800_000, "volume_change_30m"),
        ):
            a = vol_sum(ms)
            b = vol_prev(ms)
            if b > 0:
                ev[lab] = f"{(a / b) - 1.0:.6f}"
            elif a > 0:
                ev[lab] = "inf"
            else:
                ev[lab] = ""

        trades = trades_cache.get(addr, [])
        if trades:
            counts = compute_trade_counts(trades, t0_ms, [300, 900, 1800])
            for sec, lab in (
                (300, "tx_count_change_5m"),
                (900, "tx_count_change_15m"),
                (1800, "tx_count_change_30m"),
            ):
                after, before = counts.get(sec, (0, 0))
                if before > 0:
                    ev[lab] = f"{(after / before) - 1.0:.6f}"
                elif after > 0:
                    ev[lab] = "inf"
                else:
                    ev[lab] = ""


def load_local_verification_notes() -> dict[str, str]:
    """cashtag_upper -> verification_note from S001 local_pool_verification.csv."""
    m: dict[str, str] = {}
    if not LOCAL_VERIFY_CSV.exists():
        return m
    with LOCAL_VERIFY_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sym = (row.get("cashtag") or "").strip().upper()
            note = (row.get("verification_note") or "").strip()
            if sym and note:
                m[sym] = note
    return m


def load_s001_decision_snapshot() -> dict[str, Any]:
    if not S001_FLAGS_JSON.exists():
        return {}
    try:
        return json.loads(S001_FLAGS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_okx_preflight_readout(
    preflight: dict[str, Any],
    quote_smoke: dict[str, Any],
    budget: CallBudget,
    acceptance: dict[str, Any],
) -> None:
    """Single OKX preflight markdown; OKX_CALLS_USED matches final budget.okx."""
    lines = [
        "# S002 OKX API Preflight",
        "",
        f"- Generated (UTC): `{_iso_timestamp_ms(datetime.now(UTC))}`",
        "",
        "## Credential presence (values redacted)",
        "",
        f"- OKX_AUTH_PRESENT: **{preflight['OKX_AUTH_PRESENT']}**",
        f"- OKX_PROJECT_ID_PRESENT: **{preflight['OKX_PROJECT_ID_PRESENT']}**",
        f"- OKX_SECRET_PRESENT: **{preflight['OKX_SECRET_PRESENT']}**",
        f"- OKX_PASSPHRASE_PRESENT: **{preflight['OKX_PASSPHRASE_PRESENT']}**",
        "",
        "## Smoke capability flags (CLANKER Base — price / candles / trades)",
        "",
        f"- OKX_PRICE_AVAILABLE: **{preflight['OKX_PRICE_AVAILABLE']}**",
        f"- OKX_CANDLES_AVAILABLE: **{preflight['OKX_CANDLES_AVAILABLE']}**",
        f"- OKX_TRADES_AVAILABLE: **{preflight['OKX_TRADES_AVAILABLE']}**",
        "",
        f"## Quote smoke (GET `/api/v5/dex/aggregator/quote` only; cap {CAP_QUOTE_SMOKE})",
        "",
        f"- OKX_QUOTE_AVAILABLE: **{quote_smoke.get('OKX_QUOTE_AVAILABLE', 'false')}**",
        f"- quote_smoke_attempts: **`{quote_smoke.get('quote_smoke_attempts', 0)}`**",
        f"- quote_error_top_reason: **`{quote_smoke.get('quote_error_top_reason', 'n/a')}`**",
        f"- quote_smoke_blocker: **`{quote_smoke.get('quote_smoke_blocker') or '(none)'}`**",
        "",
        "## Acceptance (S002)",
        "",
        f"- OKX_CALLS_USED: **`{acceptance['OKX_CALLS_USED']}`** (> 0 ⇒ real OKX network path ran)",
        f"- OKX_REAL_PATH_RAN: **{_bool_flag(bool(acceptance['OKX_REAL_PATH_RAN']))}**",
        f"- OKX_MARKET_ENDPOINTS_TRUE_COUNT (price/candles/trades): **`{acceptance['OKX_MARKET_ENDPOINTS_TRUE_COUNT']}`** / 3",
        f"- OKX_MARKET_PATH_USABLE (need ≥2): **{_bool_flag(bool(acceptance['OKX_MARKET_PATH_USABLE']))}**",
        "",
        "- Quote: `QUOTE_UNAVAILABLE_LIMITS_EXECUTABLE_ALPHA_CLAIMS` only caps **executable / routing** claims; "
        "it does **not** negate OKX or fallback **market-data** research.",
        "",
        "## Budget (final run)",
        "",
        f"- Preflight-only OKX calls (price/candles/trades): `{preflight.get('preflight_calls_used', 0)}`",
        f"- **OKX_CALLS_USED (total)**: **`{budget.okx}`** (hard cap {CAP_OKX_HARD})",
        "",
        "## Notes",
        "",
    ]
    for n in preflight.get("preflight_notes", []) or ["(none)"]:
        lines.append(f"- {n}")
    if preflight.get("preflight_error_type"):
        lines.append(f"- preflight_error_type: `{preflight['preflight_error_type']}`")
    lines.append("")
    lines.append(
        "Signing: `timestamp + METHOD + pathWithQuery` (+ JSON body for POST). "
        "`OK-ACCESS-PROJECT` is sent only when `OKX_PROJECT_ID` is set (value never logged)."
    )
    lines.append("")
    PREFLIGHT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    posts = load_raw_posts()
    aliases = load_aliases()
    todo_tags = load_todo_cashtags()
    venue_map = load_venue_guess()
    events = build_events(posts, aliases, venue_map)

    key_ok, sec_ok, pw_ok, _pid_ok = env_credentials()
    auth = key_ok and sec_ok and pw_ok
    budget = CallBudget()
    client = OKXClient(budget) if auth else None

    # Preflight resets partial budget counting — track preflight calls from returned dict
    preflight_budget = CallBudget()
    preflight_client = OKXClient(preflight_budget) if auth else None
    preflight = run_preflight(preflight_client, preflight_budget)

    # Merge preflight OKX usage into main budget
    budget.okx += preflight_budget.okx
    budget.okx_blocked_reason = preflight_budget.okx_blocked_reason
    budget.okx_consecutive_errors = preflight_budget.okx_consecutive_errors

    quote_smoke, quote_okx_calls = run_quote_smoke(auth)
    budget.okx += quote_okx_calls
    preflight["OKX_QUOTE_AVAILABLE"] = quote_smoke.get("OKX_QUOTE_AVAILABLE", "false")

    chain_liquidity_ok = False
    if client and budget.okx_ok():
        chain_liquidity_ok, _ = okx_chain_liquidity_sources(client)

    # Unique resolved tokens for coverage
    resolved_tokens: dict[str, str] = {}
    for sym, addr in aliases.items():
        resolved_tokens[addr.lower()] = sym
    for tag in PRIORITY_CASHTAGS:
        if tag in aliases:
            resolved_tokens[aliases[tag].lower()] = tag
    for tag in todo_tags:
        if tag in aliases:
            resolved_tokens[aliases[tag].lower()] = tag

    coverage_rows: list[dict[str, Any]] = []
    token_okx_used: dict[str, int] = {}
    coverage_by_addr: dict[str, dict[str, Any]] = {}

    ordered_addrs = sorted(resolved_tokens.keys(), key=lambda a: resolved_tokens[a])
    for addr in ordered_addrs:
        if not budget.okx_ok():
            break
        sym = resolved_tokens[addr]
        row = token_coverage_row(
            client, budget, sym, addr, chain_liquidity_ok, token_okx_used
        )
        coverage_rows.append(row)
        coverage_by_addr[addr] = row

    lv_notes = load_local_verification_notes()
    for row in coverage_rows:
        vn = lv_notes.get(str(row.get("token_symbol") or "").strip().upper(), "")
        if vn:
            snippet = vn[:200].replace("\n", " ").strip()
            row["notes"] = (
                (str(row.get("notes") or "") + ";") if row.get("notes") else ""
            ) + f"s001_local_pool_verification={snippet}"

    # Serialize coverage CSV
    cov_cols = [
        "token_symbol",
        "token_address",
        "okx_price_available",
        "okx_candles_available",
        "okx_trade_activity_available",
        "okx_liquidity_sources_available",
        "okx_quote_buy_available",
        "okx_quote_sell_available",
        "dexscreener_pair_available",
        "dexscreener_primary_dex",
        "gecko_terminal_ohlcv_available",
        "gecko_terminal_pool_id",
        "primary_venue",
        "can_compute_5m_return",
        "can_compute_15m_return",
        "can_compute_30m_return",
        "notes",
    ]
    with COVERAGE_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cov_cols)
        w.writeheader()
        for r in coverage_rows:
            w.writerow({k: r.get(k, "") for k in cov_cols})

    trades_cache = fetch_trades_cache(client, budget, coverage_by_addr, token_okx_used)
    series_cache = build_price_series_cache(
        events, coverage_by_addr, client, budget, token_okx_used
    )
    enrich_events_with_metrics(events, coverage_by_addr, series_cache, trades_cache)

    ev_cols = [
        "author",
        "post_id",
        "post_time_utc",
        "token_symbol",
        "token_address",
        "chain",
        "source_post_url",
        "text_hash_short",
        "alias_confidence",
        "venue_guess",
        "ret_5m",
        "ret_15m",
        "ret_30m",
        "volume_change_5m",
        "volume_change_15m",
        "volume_change_30m",
        "tx_count_change_5m",
        "tx_count_change_15m",
        "tx_count_change_30m",
        "max_drawdown_30m",
        "data_source_used",
        "fallback_used",
        "skip_reason",
    ]
    with EVENTS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ev_cols)
        w.writeheader()
        for r in events:
            w.writerow({k: r.get(k, "") for k in ev_cols})

    # Aggregate gates
    raw_posts_used = len(posts)
    all_tags = set()
    for row in posts:
        all_tags.update(t.upper() for t in CASHTAG_RE.findall(row.get("text") or ""))

    def valid_ret(col: str) -> int:
        return sum(
            1
            for e in events
            if (e.get(col) or "").strip() and not (e.get("skip_reason") or "").strip()
        )

    valid_5 = valid_ret("ret_5m")
    valid_15 = valid_ret("ret_15m")
    valid_30 = valid_ret("ret_30m")
    valid_union = sum(
        1
        for e in events
        if any((e.get(h) or "").strip() for h in ("ret_5m", "ret_15m", "ret_30m"))
        and not (e.get("skip_reason") or "").strip()
    )

    okx_px = sum(1 for r in coverage_rows if r.get("okx_price_available"))
    okx_cd = sum(1 for r in coverage_rows if r.get("okx_candles_available"))
    okx_td = sum(1 for r in coverage_rows if r.get("okx_trade_activity_available"))
    fb_pairs = sum(1 for r in coverage_rows if r.get("dexscreener_pair_available"))
    qb = sum(1 for r in coverage_rows if r.get("okx_quote_buy_available"))
    qs = sum(1 for r in coverage_rows if r.get("okx_quote_sell_available"))

    from collections import Counter

    fallback_only_events = sum(
        1
        for e in events
        if e.get("fallback_used") == "true"
        and (e.get("ret_5m") or e.get("ret_15m") or e.get("ret_30m"))
    )

    scored_ev = [
        e
        for e in events
        if any((e.get(h) or "").strip() for h in ("ret_5m", "ret_15m", "ret_30m"))
        and not (e.get("skip_reason") or "").strip()
    ]
    n_scored = len(scored_ev)
    event_source_distribution = dict(
        Counter((e.get("data_source_used") or "unknown") for e in scored_ev)
    )

    quote_unavailable = (preflight["OKX_QUOTE_AVAILABLE"] != "true") or (
        qb == 0 or qs == 0
    )

    conc_auth = 0.0
    conc_tok = 0.0
    if n_scored:
        ac = Counter(e.get("author") or "" for e in scored_ev)
        tc = Counter(
            (e.get("token_symbol") or e.get("token_address") or "").lower()
            for e in scored_ev
        )
        conc_auth = max(ac.values()) / n_scored
        conc_tok = max(tc.values()) / n_scored

    concentration_risk = conc_auth > 0.6 or conc_tok > 0.6
    gecko_scored = sum(
        1 for e in scored_ev if e.get("data_source_used") == "gecko_terminal"
    )
    gecko_share = (gecko_scored / n_scored) if n_scored else 0.0
    fallback_only_scored = n_scored > 0 and all(
        (e.get("data_source_used") or "") != "okx_market_candles" for e in scored_ev
    )
    data_source_risk = fallback_only_scored

    economic = "INCONCLUSIVE_SAMPLE_TOO_SMALL"
    if valid_union < 12:
        economic = "INCONCLUSIVE_SAMPLE_TOO_SMALL"
    elif 12 <= valid_union < 20:
        economic = "WEAK_PASS_OR_INCONCLUSIVE"
    else:
        economic = "PASS_FAIL_ALLOWED_BAND"

    s1 = load_s001_decision_snapshot()
    inherits: dict[str, Any] = {
        "S001_PASS_FOR_DIAGNOSIS_ONLY": True,
        "S001_NOT_PASS_FOR_ALPHA": True,
        "X_API_WORKING": True,
        "SOCIAL_UNIVERSE_WIDER_THAN_CURRENT_V3": bool(
            s1.get("SOCIAL_UNIVERSE_WIDER_THAN_CURRENT_V3", True)
        ),
        "CURRENT_V3_SOCIAL_OVERLAP_ZERO": bool(
            s1.get("CURRENT_V3_SOCIAL_OVERLAP_ZERO", True)
        ),
        "EATLQ_BASE_V3_MAINLINE_INDEPENDENT": True,
        "S001_VERDICT_SNAPSHOT": s1.get("verdict"),
        "S001_TOKEN_EVENTS_TOTAL_SNAPSHOT": s1.get("token_events_total"),
    }

    acceptance = acceptance_gate_snapshot(preflight, budget)

    flags = {
        "sprint": "S002",
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "acceptance_gate": acceptance,
        "inherits": inherits,
        "constraints_respected": {
            "no_redis_signals": True,
            "no_executor": True,
            "no_live_trading": True,
            "no_model_training": True,
            "no_private_keys": True,
            "no_swap_broadcast": True,
        },
        "preflight": {
            "OKX_AUTH_PRESENT": preflight["OKX_AUTH_PRESENT"],
            "OKX_PROJECT_ID_PRESENT": preflight["OKX_PROJECT_ID_PRESENT"],
            "OKX_PRICE_AVAILABLE": preflight["OKX_PRICE_AVAILABLE"],
            "OKX_CANDLES_AVAILABLE": preflight["OKX_CANDLES_AVAILABLE"],
            "OKX_TRADES_AVAILABLE": preflight["OKX_TRADES_AVAILABLE"],
            "OKX_QUOTE_AVAILABLE": preflight["OKX_QUOTE_AVAILABLE"],
            "OKX_CALLS_USED": budget.okx,
            "OKX_UNDER_HARD_CAP": budget.okx <= CAP_OKX_HARD,
            "quote_error_top_reason": quote_smoke.get("quote_error_top_reason", "n/a"),
            "quote_smoke_attempts": quote_smoke.get("quote_smoke_attempts", 0),
            "quote_smoke_blocker": quote_smoke.get("quote_smoke_blocker") or "",
        },
        "coverage_gate": {
            "raw_posts_used": raw_posts_used,
            "unique_cashtags": len(all_tags),
            "aliases_loaded": len(aliases),
            "tokens_resolved": len(coverage_rows),
            "okx_price_available_count": okx_px,
            "okx_candle_available_count": okx_cd,
            "okx_trade_activity_available_count": okx_td,
            "fallback_pair_available_count": fb_pairs,
            "fallback_dex_calls": budget.dex,
            "fallback_gecko_calls": budget.gecko,
            "quote_buy_available_count": qb,
            "quote_sell_available_count": qs,
            "event_count_with_5m_return": valid_5,
            "event_count_with_15m_return": valid_15,
            "event_count_with_30m_return": valid_30,
            "event_count_valid_any_horizon": valid_union,
            "valid_events_any_horizon": valid_union,
            "event_source_distribution": event_source_distribution,
        },
        "economic_gate": {
            "verdict": economic,
            "valid_events_any_horizon": valid_union,
            "CONCENTRATION_RISK": concentration_risk,
            "top_author_share_among_scored": round(conc_auth, 4),
            "top_token_share_among_scored": round(conc_tok, 4),
            "DATA_SOURCE_RISK": data_source_risk,
            "gecko_share_among_scored": round(gecko_share, 4),
            "QUOTE_UNAVAILABLE_LIMITS_EXECUTABLE_ALPHA_CLAIMS": quote_unavailable,
            "QUOTE_UNAVAILABLE_DOES_NOT_NEGATE_MARKET_DATA_RESEARCH": True,
        },
        "documentation_paths": {
            "note": "wiki/* paths from sprint brief map to repo-root Architecture.md / Whitepaper-v2*.md (no wiki/ dir in-tree).",
            "read_architecture": str(REPO_ROOT / "Architecture.md"),
            "read_whitepaper_v2": str(REPO_ROOT / "whitepaper-v2.md"),
            "read_whitepaper_v2_continued": str(REPO_ROOT / "Whitepaper-v2-continued.md"),
            "s001_script": str(REPO_ROOT / "scripts/sprint_s001_social_catalyst_event_study.py"),
        },
    }

    FLAGS_JSON.write_text(
        json.dumps(flags, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    write_okx_preflight_readout(preflight, quote_smoke, budget, acceptance)

    esd_lines = ", ".join(f"{k}: {v}" for k, v in sorted(event_source_distribution.items()))

    lines = [
        "# S002 Base Social Market Data Scout — Readout",
        "",
        "## Inherited premises (S001)",
        "",
        "- S001 = PASS_FOR_DIAGNOSIS / NOT PASS_FOR_ALPHA (social vs current Base V3 universe mismatch documented).",
        "- This sprint does not repair V3 registry overlap; it scouts cross-venue market data APIs only.",
        "",
        "## OKX status (aligned with `decision_flags.json`)",
        "",
        "| Field | Value |",
        "|------|-------|",
        f"| OKX_AUTH_PRESENT | `{preflight['OKX_AUTH_PRESENT']}` |",
        f"| OKX_PROJECT_ID_PRESENT | `{preflight['OKX_PROJECT_ID_PRESENT']}` |",
        f"| OKX_PRICE_AVAILABLE | `{preflight['OKX_PRICE_AVAILABLE']}` |",
        f"| OKX_CANDLES_AVAILABLE | `{preflight['OKX_CANDLES_AVAILABLE']}` |",
        f"| OKX_TRADES_AVAILABLE | `{preflight['OKX_TRADES_AVAILABLE']}` |",
        f"| OKX_QUOTE_AVAILABLE | `{preflight['OKX_QUOTE_AVAILABLE']}` |",
        f"| OKX_CALLS_USED | `{budget.okx}` |",
        f"| quote_error_top_reason | `{quote_smoke.get('quote_error_top_reason', 'n/a')}` |",
        f"| quote_smoke_attempts | `{quote_smoke.get('quote_smoke_attempts', 0)}` |",
        "",
        "## Acceptance gate (S002 policy)",
        "",
        f"- OKX_REAL_PATH_RAN (`OKX_CALLS_USED` > 0): **{acceptance['OKX_REAL_PATH_RAN']}**",
        f"- OKX_MARKET_ENDPOINTS_TRUE_COUNT (price/candles/trades): **{acceptance['OKX_MARKET_ENDPOINTS_TRUE_COUNT']}** / 3",
        f"- OKX_MARKET_PATH_USABLE (≥2 endpoints true): **{acceptance['OKX_MARKET_PATH_USABLE']}**",
        "",
        "- `QUOTE_UNAVAILABLE_LIMITS_EXECUTABLE_ALPHA_CLAIMS` applies to **executable / aggregator-quote** claims only; "
        "it does **not** invalidate OKX or DexScreener / Gecko **market-data** scouting.",
        "",
        "- Economic sample rule unchanged: `valid_events_any_horizon` **< 12** ⇒ **INCONCLUSIVE_SAMPLE_TOO_SMALL**.",
        "",
        "## Fallback API calls",
        "",
        f"- DexScreener HTTP calls: **{budget.dex}**",
        f"- GeckoTerminal HTTP calls: **{budget.gecko}**",
        "",
        "## Event source distribution (scored events only)",
        "",
        f"- {esd_lines or '(none scored)'}",
        "",
        "## Token coverage (counts)",
        "",
        f"- Tokens with OKX price: **{okx_px}** / {len(coverage_rows)}",
        f"- Tokens with OKX candles: **{okx_cd}**",
        f"- Tokens with OKX trades feed: **{okx_td}**",
        f"- Tokens with DexScreener Base pair: **{fb_pairs}**",
        f"- Tokens buy-quote OK: **{qb}**, sell-quote OK: **{qs}**",
        "",
        "## Event metrics",
        "",
        f"- Social events emitted: **{len(events)}**",
        f"- event_count_with_5m_return: **{valid_5}**",
        f"- event_count_with_15m_return: **{valid_15}**",
        f"- event_count_with_30m_return: **{valid_30}**",
        f"- valid_events_any_horizon: **{valid_union}**",
        "",
        "## Risk flags",
        "",
        f"- CONCENTRATION_RISK: **{concentration_risk}** (top author share {conc_auth:.2f}, top token {conc_tok:.2f})",
        f"- DATA_SOURCE_RISK (scored events all non-OKX candle series): **{data_source_risk}**",
        f"- QUOTE_UNAVAILABLE_LIMITS_EXECUTABLE_ALPHA_CLAIMS: **{quote_unavailable}** (executable-alpha scope only; "
        "does not negate market-data research)",
        "",
        "## Economic gate",
        "",
        f"- **{economic}** (valid_events_any_horizon={valid_union})",
        "",
        "## Artifacts",
        "",
        f"- `{PREFLIGHT_MD.relative_to(REPO_ROOT)}`",
        f"- `{COVERAGE_CSV.relative_to(REPO_ROOT)}`",
        f"- `{EVENTS_CSV.relative_to(REPO_ROOT)}`",
        f"- `{FLAGS_JSON.relative_to(REPO_ROOT)}`",
        "",
    ]
    READOUT_MD.write_text("\n".join(lines), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
