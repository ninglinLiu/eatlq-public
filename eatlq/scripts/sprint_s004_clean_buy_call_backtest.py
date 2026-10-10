"""
S004 — Clean Buy-Call Backtest + 1m Data Feasibility Sprint
============================================================
Reads S003 artifacts, classifies intent for each unique post×token event,
re-backtests only clean actionable bullish events, smokes 1m data sources,
and produces a route4 targeted repair preflight.

No X API calls. No live trading. No Redis. No executor. No private keys.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from statistics import mean, median
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
S003_DIR = REPO_ROOT / "mvp" / "audits" / "social_catalyst_s003"
S004_DIR = REPO_ROOT / "mvp" / "audits" / "social_catalyst_s004"
S004_DIR.mkdir(parents=True, exist_ok=True)

S003_POSTS    = S003_DIR / "raw_kol_posts.csv"
S003_EVENTS   = S003_DIR / "resolved_social_events.csv"
S003_GRID     = S003_DIR / "event_return_grid.csv"
S003_COVERAGE = S003_DIR / "data_source_coverage.csv"

OUT_INTENT        = S004_DIR / "post_intent_labels.csv"
OUT_CLEAN_EVENTS  = S004_DIR / "clean_actionable_events.csv"
OUT_INTENT_AUTHOR = S004_DIR / "intent_distribution_by_author.csv"
OUT_INTENT_TOKEN  = S004_DIR / "intent_distribution_by_token.csv"
OUT_CLEAN_GRID    = S004_DIR / "clean_event_return_grid.csv"
OUT_GRID_SUMMARY  = S004_DIR / "clean_policy_summary_by_grid.csv"
OUT_AUTH_SUMMARY  = S004_DIR / "clean_policy_summary_by_author.csv"
OUT_SMOKE         = S004_DIR / "one_minute_data_source_smoke.csv"
OUT_ROUTE4        = S004_DIR / "route4_targeted_repair_preflight.md"
OUT_READOUT       = S004_DIR / "s004_clean_buy_call_readout.md"
OUT_FLAGS         = S004_DIR / "decision_flags.json"

# ---------------------------------------------------------------------------
# Grid constants
# ---------------------------------------------------------------------------
ENTRY_DELAYS = [0, 1, 3, 5, 10, 15, 30]
EXIT_AFTERS  = [5, 15, 30, 60, 120]
FIXED_POLICIES = {
    "policy_fast": (1, 15),
    "policy_mid":  (5, 30),
    "policy_slow": (15, 60),
    "policy_late": (30, 120),
}
HOURLY_SOURCES = ("okx_candles_1h", "gecko_terminal_1h")
PRE_RUNUP_CHASE_THRESHOLD = 0.50   # pre_return_60m > 50% → chase bucket
SMOKE_TOP_TOKENS  = 10
SMOKE_TOP_EVENTS  = 30
SMOKE_PER_TOKEN   = 20
CAP_OKX_SMOKE     = 2500
CAP_GECKO_SMOKE   = 300

# ---------------------------------------------------------------------------
# Intent labels
# ---------------------------------------------------------------------------
PRIMARY_BUY_LABELS = {"BULLISH_SHORT_TERM", "BULLISH_POSITION", "QUOTE_WITH_BULLISH_COMMENT"}
OBSERVE_LABELS     = {"BULLISH_LONG_TERM"}
EXCLUDE_LABELS     = {
    "BEARISH_AVOID", "TAKE_PROFIT_OR_REDUCE", "RETROSPECTIVE",
    "NEUTRAL_MENTION", "QUOTE_NEUTRAL_OR_OTHER", "AMBIGUOUS",
}
ALL_LABELS = PRIMARY_BUY_LABELS | OBSERVE_LABELS | EXCLUDE_LABELS

# ---------------------------------------------------------------------------
# Load S002
# ---------------------------------------------------------------------------
def _load_s002():
    p = REPO_ROOT / "scripts" / "sprint_s002_base_social_market_data_scout.py"
    name = "eatlq_s002_market"
    spec = importlib.util.spec_from_file_location(name, str(p))
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore
    return mod

S002 = _load_s002()
S002.CAP_OKX_HARD = 2500
S002.CAP_OKX_SOFT = 2400
S002.CAP_DEX_SOFT = 500
S002.CAP_GECKO_SOFT = 300
S002.CAP_PER_TOKEN_OKX = 20

# ===========================================================================
# Intent Classifier (rule-based, multilingual EN + ZH)
# ===========================================================================

_EN_BULL_S = re.compile(
    r"\b(buy|buying|bought|load(ing|ed)?|ape(d|ing)?|"
    r"dip|entry|accumulate|accumulating|moon(ing)?|"
    r"lfg|let'?s go|letsgo|wagmi|send it|gem|alpha|"
    r"bull(ish)?|going up|breaking out|break(ing)? out|"
    r"just (bought|snagged|grabbed)|add(ing|ed)?( more| here)?|"
    r"all in|going all in|max pain up|"
    r"scalp(ing)?|long(ing)?|open(ing)? (long|position)|"
    r"take (a )?position|getting in|bags?|target|"
    r"pump(ing)?|rip(ping)?|fly(ing)?|"
    r"watching (closely|this)|keep(ing)? an eye)\b",
    re.I,
)
_EN_BULL_P = re.compile(
    r"\b(hold(ing)?|hodl|never sell(ing)?|not sell(ing)?|"
    r"diamond hand|conviction|bag(ging)?|stack(ing)?|"
    r"in since|been hold|still hold|not moving)\b",
    re.I,
)
_EN_BULL_L = re.compile(
    r"\b(long.?term|2025|2026|roadmap|fundament|years?|"
    r"patient|patience|next (bull|cycle)|thesis|vision|building|"
    r"utility|ecosystem|undervalued)\b",
    re.I,
)
_EN_BEAR = re.compile(
    r"\b(sell(ing)?|sold|exit(ed|ing)?|avoid|rug(pull)?|scam|"
    r"dump(ing|ed)?|careful|stay away|red flag|ponzi|"
    r"not (buying|touching)|pass(ing)?|no thanks|"
    r"bearish|short(ing)?|warning|sus(picious)?)\b",
    re.I,
)
_EN_TP = re.compile(
    r"\b(take profit|taking profit|took profit|"
    r"reduce|reducing|reduced|trim(med|ming)?|"
    r"sold (some|half|part)|partial(ly)? (sold|exit)|"
    r"lock(ing|ed)? in (gains|profit)|de.?risk)\b",
    re.I,
)
_EN_RETRO = re.compile(
    r"\b(should.?have|should.?of|missed( it| the)?|"
    r"already pump(ed)?|too late|late (again|entry)|"
    r"hindsight|looking back|last (week|month|time)|"
    r"was (at|trading at)|could.?ve|would.?ve|if (only|i had)|"
    r"after the (fact|run|pump|move)|used to be)\b",
    re.I,
)
_ZH_BULL_S = re.compile(r"(买入|买了|接了|抄底|进场|加仓|买它|上车|看涨|看多|看好|涨啊|冲啊|拿上|入手|信号|捡|埋|蹲|挂单|做多|多单|开多|上多|必涨)", re.I)
_ZH_BULL_P = re.compile(r"(持有|不卖|拿着|死拿|重仓|仓位|长期持有|坚定|信念|捂盘|不动|仓还在|一直拿|hodl)", re.I)
_ZH_BULL_L = re.compile(r"(长期|路线图|基本面|生态|赛道|叙事|未来|价值|长线|耐心|等待|相信项目|相信团队)", re.I)
_ZH_BEAR   = re.compile(r"(卖出|卖了|跑了|跑路|避开|风险|小心|谨慎|别买|不碰|别碰|诈骗|骗局|rug|割韭菜|被套|止损|清仓|看空|做空|空单|看跌)", re.I)
_ZH_TP     = re.compile(r"(止盈|获利|出了一部分|减仓|减了|出掉|锁利|锁住|套现|拿走|降低仓位|减了点|出了些)", re.I)
_ZH_RETRO  = re.compile(r"(之前|当时|早知道|回顾|复盘|没买|可惜|涨完|已经涨|那时候|那个时候|要是|如果买了|就好了|错过|没赶上|后知后觉)", re.I)

_MINIMAL = re.compile(
    r"^[\s\U00010000-\U0010FFFF\U0001F300-\U0001FAFF"
    r"\u2600-\u27BF\u2700-\u27BF\uFE0F\u200D"
    r"😀-🙏]*"
    r"(https?://\S+\s*)*$"
)


def _span(text: str, pat: re.Pattern, mx: int = 40) -> str:
    m = pat.search(text)
    if not m:
        return ""
    s = max(0, m.start() - 8)
    e = min(len(text), m.end() + 8)
    sp = text[s:e].strip()
    return sp[:mx] if len(sp) > mx else sp


def _hits(text: str, pat: re.Pattern) -> float:
    return float(min(len(pat.findall(text)), 3))


def classify_event(text: str, is_quote: bool, token_symbol: str) -> dict[str, Any]:
    t = " ".join((text or "").lower().split())
    qo = "quote" if is_quote else "original"

    # Minimal quote → neutral
    if is_quote and (len(t) < 20 or bool(_MINIMAL.match((text or "").strip()))):
        return {"intent_label": "QUOTE_NEUTRAL_OR_OTHER", "polarity": "neutral",
                "time_horizon": "unknown", "actionability": "non_actionable",
                "quote_or_original": qo, "evidence_span": (text or "").strip()[:40],
                "confidence": 0.85, "classifier_method": "rule_minimal_quote"}

    bs = _hits(t, _EN_BULL_S) + _hits(t, _ZH_BULL_S)
    bp = _hits(t, _EN_BULL_P) + _hits(t, _ZH_BULL_P)
    bl = _hits(t, _EN_BULL_L) + _hits(t, _ZH_BULL_L)
    be = _hits(t, _EN_BEAR)   + _hits(t, _ZH_BEAR)
    tp = _hits(t, _EN_TP)     + _hits(t, _ZH_TP)
    rt = _hits(t, _EN_RETRO)  + _hits(t, _ZH_RETRO)

    bull_total = bs + bp + bl
    neg_total  = be + tp + rt

    # Token symbol proximity boost
    sym = (token_symbol or "").upper().strip()
    sym_bull = False
    sym_bear = False
    if sym:
        sp = re.compile(r"\$?" + re.escape(sym) + r"\b", re.I)
        for m in sp.finditer(t):
            w = t[max(0, m.start()-60):m.end()+60]
            if _EN_BULL_S.search(w) or _ZH_BULL_S.search(w): sym_bull = True
            if _EN_BEAR.search(w)   or _ZH_BEAR.search(w):   sym_bear = True
            if _EN_TP.search(w)     or _ZH_TP.search(w):     sym_bear = True

    # Bearish
    if be > 0 and be >= bull_total:
        sp = _span(text, _EN_BEAR) or _span(text, _ZH_BEAR)
        return {"intent_label": "BEARISH_AVOID", "polarity": "negative",
                "time_horizon": "short_term", "actionability": "non_actionable",
                "quote_or_original": qo, "evidence_span": sp,
                "confidence": round(min(0.60 + 0.10 * be, 0.95), 2),
                "classifier_method": "rule_bearish"}

    # Take profit
    if tp > 0 and tp >= bull_total:
        sp = _span(text, _EN_TP) or _span(text, _ZH_TP)
        return {"intent_label": "TAKE_PROFIT_OR_REDUCE", "polarity": "mixed",
                "time_horizon": "short_term", "actionability": "non_actionable",
                "quote_or_original": qo, "evidence_span": sp,
                "confidence": round(min(0.60 + 0.10 * tp, 0.95), 2),
                "classifier_method": "rule_tp"}

    # Retrospective
    if rt > 0 and rt >= bs:
        sp = _span(text, _EN_RETRO) or _span(text, _ZH_RETRO)
        conf = min(0.58 + 0.10 * rt, 0.92)
        return {"intent_label": "RETROSPECTIVE", "polarity": "neutral",
                "time_horizon": "past", "actionability": "non_actionable",
                "quote_or_original": qo, "evidence_span": sp,
                "confidence": round(conf, 2),
                "classifier_method": "rule_retro"}

    # Bullish long-term only
    if bl > bs and bl > 0.5:
        sp = _span(text, _EN_BULL_L) or _span(text, _ZH_BULL_L)
        conf = min(0.55 + 0.08 * bl, 0.88)
        if neg_total > bl: conf *= 0.7
        if conf < 0.50:
            return _amb(text, qo)
        return {"intent_label": "BULLISH_LONG_TERM", "polarity": "positive",
                "time_horizon": "long_term", "actionability": "informational",
                "quote_or_original": qo, "evidence_span": sp,
                "confidence": round(conf, 2),
                "classifier_method": "rule_bull_long"}

    # Bullish position (holding)
    if bp > 0 and bp >= bs * 0.8:
        sp = _span(text, _EN_BULL_P) or _span(text, _ZH_BULL_P)
        conf = min(0.58 + 0.10 * bp, 0.90)
        if sym_bear: conf *= 0.6
        if conf < 0.50: return _amb(text, qo)
        return {"intent_label": "BULLISH_POSITION", "polarity": "positive",
                "time_horizon": "medium_term", "actionability": "directional",
                "quote_or_original": qo, "evidence_span": sp,
                "confidence": round(conf, 2),
                "classifier_method": "rule_bull_pos"}

    # Bullish short-term
    if bs > 0:
        sp = _span(text, _EN_BULL_S) or _span(text, _ZH_BULL_S)
        conf = min(0.58 + 0.10 * bs, 0.93)
        if sym_bull: conf = min(conf + 0.05, 0.97)
        if sym_bear or neg_total > bs: conf *= 0.65
        if is_quote: conf = min(conf, 0.88)
        if conf < 0.60: return _amb(text, qo)
        lbl = "QUOTE_WITH_BULLISH_COMMENT" if is_quote else "BULLISH_SHORT_TERM"
        return {"intent_label": lbl, "polarity": "positive",
                "time_horizon": "short_term", "actionability": "actionable",
                "quote_or_original": qo, "evidence_span": sp,
                "confidence": round(conf, 2),
                "classifier_method": "rule_bull_short"}

    if is_quote:
        return {"intent_label": "QUOTE_NEUTRAL_OR_OTHER", "polarity": "neutral",
                "time_horizon": "unknown", "actionability": "non_actionable",
                "quote_or_original": qo, "evidence_span": (text or "").strip()[:40],
                "confidence": 0.75, "classifier_method": "rule_quote_neutral"}

    return {"intent_label": "NEUTRAL_MENTION", "polarity": "neutral",
            "time_horizon": "unknown", "actionability": "non_actionable",
            "quote_or_original": qo, "evidence_span": (text or "").strip()[:40],
            "confidence": 0.70, "classifier_method": "rule_neutral"}


def _amb(text: str, qo: str) -> dict[str, Any]:
    return {"intent_label": "AMBIGUOUS", "polarity": "unknown",
            "time_horizon": "unknown", "actionability": "non_actionable",
            "quote_or_original": qo, "evidence_span": (text or "").strip()[:40],
            "confidence": 0.0, "classifier_method": "rule_ambiguous"}


# ===========================================================================
# Data loading
# ===========================================================================

def load_s003_data():
    posts_map: dict[str, dict] = {}
    for r in csv.DictReader(open(S003_POSTS, newline="", encoding="utf-8")):
        posts_map[r["tweet_id"]] = r

    unique_ev: dict[tuple, dict] = {}
    for r in csv.DictReader(open(S003_EVENTS, newline="", encoding="utf-8")):
        k = (r["post_id"], r["token_address"].lower().strip())
        unique_ev.setdefault(k, r)

    grid_rows = list(csv.DictReader(open(S003_GRID, newline="", encoding="utf-8")))

    cov_map: dict[str, dict] = {}
    for r in csv.DictReader(open(S003_COVERAGE, newline="", encoding="utf-8")):
        cov_map[r["token_address"].lower().strip()] = r

    return posts_map, unique_ev, grid_rows, cov_map


# ===========================================================================
# Classification pass
# ===========================================================================

def run_classification(unique_ev: dict, posts_map: dict) -> list[dict]:
    labeled: list[dict] = []
    for (post_id, token_addr), ev in unique_ev.items():
        post = posts_map.get(post_id, {})
        text = post.get("text", "")
        is_quote = str(post.get("is_quote", "False")).strip().lower() in ("true", "1")
        token_sym = ev.get("token_symbol", "")
        cls = classify_event(text, is_quote, token_sym)
        include = cls["intent_label"] in PRIMARY_BUY_LABELS and cls["confidence"] >= 0.70
        excl_reason = ""
        if cls["intent_label"] not in PRIMARY_BUY_LABELS:
            excl_reason = f"label={cls['intent_label']}"
        elif cls["confidence"] < 0.70:
            excl_reason = f"low_confidence={cls['confidence']:.2f}"
        labeled.append({
            "post_id": post_id,
            "author": ev.get("author", ""),
            "token_symbol": token_sym,
            "token_address": token_addr,
            "post_time_utc": ev.get("post_time_utc", ""),
            "chain": ev.get("chain", ""),
            **cls,
            "include_in_primary_buy_call_backtest": str(include).lower(),
            "exclude_reason": excl_reason,
        })
    return labeled


# ===========================================================================
# Pre-runup filter
# ===========================================================================

def annotate_pre_runup(events: list[dict], series_cache: dict) -> list[dict]:
    PAD = 130 * 60 * 1000
    for row in events:
        row.setdefault("pre_return_30m", "")
        row.setdefault("pre_return_60m", "")
        row.setdefault("pre_return_120m", "")
        row["pre_runup_bucket"] = "PRE_RUNUP_UNKNOWN"
        addr = row.get("token_address", "").lower().strip()
        ts_str = row.get("post_time_utc", "")
        if not ts_str or addr not in series_cache:
            continue
        try:
            ts_iso = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
            t0 = int(datetime.fromisoformat(ts_iso).astimezone(UTC).timestamp() * 1000)
        except ValueError:
            continue
        series, _ = series_cache[addr]
        p_base = S002.price_at_or_before(series, t0 - PAD)
        if p_base is None or p_base <= 0:
            continue
        for mins, key in [(30, "pre_return_30m"), (60, "pre_return_60m"), (120, "pre_return_120m")]:
            p = S002.price_at_or_before(series, t0 - mins * 60_000)
            if p and p > 0:
                row[key] = round((p - p_base) / p_base, 6)
        p60 = row["pre_return_60m"]
        if p60 == "":
            row["pre_runup_bucket"] = "PRE_RUNUP_UNKNOWN"
        elif float(p60) > PRE_RUNUP_CHASE_THRESHOLD:
            row["pre_runup_bucket"] = "POST_PUMP_RETROSPECTIVE"
        elif float(p60) > 0.20:
            row["pre_runup_bucket"] = "MID_MOVE_COMMENT"
        else:
            row["pre_runup_bucket"] = "EARLY_CALL"
    return events


# ===========================================================================
# OKX paginated candles (before=t_min newer-than, after=t_max older-than)
# ===========================================================================

def _okx_paged(client, addr: str, bar: str, used: dict, t_min: int, t_max: int, pages: int = 4) -> list:
    all_rows: list = []
    after_ms: int | None = t_max
    for _ in range(pages):
        if used.get(addr, 0) >= S002.CAP_PER_TOKEN_OKX:
            break
        if not client.budget.okx_ok():
            break
        rows, err = S002.okx_candles(client, addr, bar=bar, limit="300", before_ms=t_min, after_ms=after_ms)
        used[addr] = used.get(addr, 0) + 1
        if err or not rows:
            break
        all_rows.extend(rows)
        oldest = min(int(r[0]) for r in rows)
        if oldest <= t_min:
            break
        after_ms = oldest
    return S002.candles_to_series(all_rows)


# ===========================================================================
# Series cache
# ===========================================================================

def build_series_cache(events: list[dict], cov_map: dict, client, budget, used: dict) -> dict:
    by_addr: dict[str, list[int]] = {}
    for ev in events:
        addr = ev.get("token_address", "").lower().strip()
        ts_str = ev.get("post_time_utc", "")
        if not addr or not ts_str:
            continue
        try:
            ts_iso = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
            t0 = int(datetime.fromisoformat(ts_iso).astimezone(UTC).timestamp() * 1000)
        except ValueError:
            continue
        by_addr.setdefault(addr, []).append(t0)

    PAD_B = 130 * 60 * 1000
    PAD_A = 330 * 60 * 1000
    H1    = 3_600_000
    cache: dict[str, tuple] = {}

    for addr, times in by_addr.items():
        cov   = cov_map.get(addr, {})
        t_min = min(times) - PAD_B
        t_max = max(times) + PAD_A

        if client and budget.okx_ok() and used.get(addr, 0) < S002.CAP_PER_TOKEN_OKX:
            s = _okx_paged(client, addr, "1m", used, t_min, t_max, 4)
            if s and len(s) >= 5 and S002.series_covers_window(s, t_min, t_max):
                cache[addr] = (s, "okx_market_candles"); continue

        if client and budget.okx_ok() and used.get(addr, 0) < S002.CAP_PER_TOKEN_OKX:
            s = _okx_paged(client, addr, "1H", used, t_min, t_max, 4)
            if s and len(s) >= 3 and s[0][0] <= t_min + 2*H1 and s[-1][0] >= t_max - 2*H1:
                cache[addr] = (s, "okx_candles_1h"); continue

        pool = str(cov.get("gecko_terminal_pool_id") or "")
        if not pool:
            _, p2 = S002.gecko_ohlcv_available(budget, addr)
            pool = str(p2 or "")
        if pool:
            gs = S002.gecko_fetch_window(budget, pool, before_ts_sec=int(t_max//1000)+180, limit=500)
            if gs and len(gs) >= 5 and S002.series_covers_window(gs, t_min, t_max):
                cache[addr] = (gs, "gecko_terminal"); continue
            if not getattr(budget, "gecko_blocked", False) and budget.gecko < S002.CAP_GECKO_SOFT:
                url = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
                       f"{pool}/ohlcv/hour?aggregate=1&limit=500&currency=usd"
                       f"&before_timestamp={int(t_max//1000)+7200}")
                doc, err = S002.http_json_public(budget, "gecko", url)
                if not err and doc:
                    ohlcv = (doc.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
                    hs: list = []
                    for row in ohlcv:
                        if isinstance(row, list) and len(row) >= 5:
                            hs.append((int(row[0])*1000, float(row[4]), float(row[5]) if len(row) > 5 else 0.0))
                    hs.sort(key=lambda x: x[0])
                    if hs and len(hs) >= 3 and hs[0][0] <= t_min+2*H1 and hs[-1][0] >= t_max-2*H1:
                        cache[addr] = (hs, "gecko_terminal_1h")
    return cache


# ===========================================================================
# Grid backtest
# ===========================================================================

def grid_row_for(ev: dict, series: list, source: str, ed: int, xa: int) -> dict:
    ts_str = ev.get("post_time_utc", "")
    skip = ""
    t0 = 0
    try:
        ts_iso = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
        t0 = int(datetime.fromisoformat(ts_iso).astimezone(UTC).timestamp() * 1000)
    except ValueError:
        skip = "BAD_TIMESTAMP"

    if not skip and source in HOURLY_SOURCES and xa < 60:
        skip = "HOURLY_RESOLUTION_SHORT_WINDOW_SKIP"

    base = {
        "author": ev.get("author", ""), "post_id": ev.get("post_id", ""),
        "post_time_utc": ts_str, "token_symbol": ev.get("token_symbol", ""),
        "token_address": ev.get("token_address", ""),
        "intent_label": ev.get("intent_label", ""),
        "pre_runup_bucket": ev.get("pre_runup_bucket", ""),
        "data_source_used": source,
        "data_resolution": "1m" if source == "okx_market_candles" else "1h",
        "entry_delay_min": ed, "exit_after_min": xa,
        "entry_price": "", "exit_price": "", "return_pct": "",
        "max_drawdown_before_exit": "", "max_runup_before_exit": "",
        "skip_reason": skip,
    }
    if skip:
        return base

    te = t0 + ed * 60_000
    tx = te + xa * 60_000
    ep = S002.price_at_or_before(series, te)
    xp = S002.price_at_or_before(series, tx)
    if not ep or ep <= 0:
        base["skip_reason"] = "NO_ENTRY_PRICE"; return base
    if not xp or xp <= 0:
        base["skip_reason"] = "NO_EXIT_PRICE"; return base

    fwd = S002.slice_forward(series, te, xa * 60_000)
    closes = [c for _, c, _ in fwd]
    ret = (xp - ep) / ep
    mdd = (min(closes) - ep) / ep if closes else 0.0
    mru = (max(closes) - ep) / ep if closes else 0.0
    base.update({"entry_price": round(ep, 8), "exit_price": round(xp, 8),
                 "return_pct": round(ret, 6), "max_drawdown_before_exit": round(mdd, 6),
                 "max_runup_before_exit": round(mru, 6)})
    return base


def run_backtest(actionable: list[dict], cache: dict) -> list[dict]:
    rows: list[dict] = []
    for ev in actionable:
        addr = ev.get("token_address", "").lower().strip()
        if addr not in cache:
            rows.append({
                "author": ev.get("author", ""), "post_id": ev.get("post_id", ""),
                "post_time_utc": ev.get("post_time_utc", ""),
                "token_symbol": ev.get("token_symbol", ""),
                "token_address": ev.get("token_address", ""),
                "intent_label": ev.get("intent_label", ""),
                "pre_runup_bucket": ev.get("pre_runup_bucket", ""),
                "data_source_used": "", "data_resolution": "",
                "entry_delay_min": 0, "exit_after_min": 5,
                "entry_price": "", "exit_price": "", "return_pct": "",
                "max_drawdown_before_exit": "", "max_runup_before_exit": "",
                "skip_reason": "NO_PRICE_SERIES",
            })
            continue
        series, source = cache[addr]
        chase = ev.get("pre_runup_bucket") == "POST_PUMP_RETROSPECTIVE"
        for ed, xa in product(ENTRY_DELAYS, EXIT_AFTERS):
            r = grid_row_for(ev, series, source, ed, xa)
            if chase and not r.get("skip_reason"):
                r["skip_reason"] = "CHASE_EXCLUDED_PRE_RUNUP"
            rows.append(r)
    return rows


# ===========================================================================
# Aggregation
# ===========================================================================

def _pf(rets: list) -> str:
    g = sum(x for x in rets if x > 0)
    l = abs(sum(x for x in rets if x < 0))
    return "" if not rets else ("inf" if l == 0 else str(round(g / l, 4)))


def agg(rows: list[dict], sname: str, gkeys: list) -> dict:
    rets = [float(r["return_pct"]) for r in rows if str(r.get("return_pct", "")).strip()]
    n = len(rets)
    if n == 0:
        return {"slice_name": sname, "group_keys": json.dumps(gkeys), "n": 0,
                "hit_rate": 0.0, "mean_return": 0.0, "median_return": 0.0,
                "profit_factor": "", "max_drawdown_avg": "", "best_return": "", "worst_return": "",
                "data_resolution": "", "data_source_distribution": "{}"}
    src = Counter(r["data_source_used"] for r in rows if str(r.get("return_pct", "")).strip())
    res = Counter(r.get("data_resolution", "") for r in rows if str(r.get("return_pct", "")).strip())
    mdd = [float(r["max_drawdown_before_exit"]) for r in rows if str(r.get("max_drawdown_before_exit", "")).strip()]
    return {
        "slice_name": sname, "group_keys": json.dumps(gkeys), "n": n,
        "hit_rate": round(sum(1 for x in rets if x > 0) / n, 4),
        "mean_return": round(mean(rets), 6), "median_return": round(median(rets), 6),
        "profit_factor": _pf(rets),
        "max_drawdown_avg": round(mean(mdd), 6) if mdd else "",
        "best_return": round(max(rets), 6), "worst_return": round(min(rets), 6),
        "data_resolution": ",".join(k for k, _ in res.most_common()),
        "data_source_distribution": json.dumps(dict(src)),
    }


def dedupe_valid(rows: list[dict]) -> list[dict]:
    seen: dict = {}
    for r in rows:
        k = (str(r.get("post_id")), str(r.get("token_address", "")).lower())
        seen.setdefault(k, r)
    return list(seen.values())


def build_grid_summaries(clean_grid: list[dict]) -> list[dict]:
    valid = [r for r in clean_grid if not str(r.get("skip_reason", "")).strip()]
    out: list[dict] = []
    out.append({**agg(valid, "overall", []), "policy": "", "entry_delay_min": "", "exit_after_min": ""})
    for ed, xa in product(ENTRY_DELAYS, EXIT_AFTERS):
        cell = [r for r in valid if str(r.get("entry_delay_min")) == str(ed)
                and str(r.get("exit_after_min")) == str(xa)]
        if len(cell) >= 3:
            out.append({**agg(cell, f"grid_{ed}_{xa}", [str(ed), str(xa)]),
                        "policy": "", "entry_delay_min": ed, "exit_after_min": xa})
    for pol, (ed, xa) in FIXED_POLICIES.items():
        cell = [r for r in valid if str(r.get("entry_delay_min")) == str(ed)
                and str(r.get("exit_after_min")) == str(xa)]
        out.append({**agg(cell, pol, [str(ed), str(xa)]),
                    "policy": pol, "entry_delay_min": ed, "exit_after_min": xa})
    return out


def build_author_summaries(clean_grid: list[dict]) -> list[dict]:
    valid = [r for r in clean_grid if not str(r.get("skip_reason", "")).strip()]
    by_auth: dict[str, list] = defaultdict(list)
    for r in valid:
        by_auth[r.get("author", "")].append(r)
    out: list[dict] = []
    for auth, rr in sorted(by_auth.items()):
        for pol, (ed, xa) in FIXED_POLICIES.items():
            cell = [r for r in rr if str(r.get("entry_delay_min")) == str(ed)
                    and str(r.get("exit_after_min")) == str(xa)]
            out.append({"author": auth, "policy": pol, "entry_delay_min": ed, "exit_after_min": xa,
                        **agg(cell, f"{auth}:{pol}", [auth, pol])})
    return out


# ===========================================================================
# 1m smoke
# ===========================================================================

def smoke_1m(actionable: list[dict], cov_map: dict, client, budget) -> list[dict]:
    tok_ctr: Counter = Counter()
    for ev in actionable:
        a = ev.get("token_address", "").lower().strip()
        if a: tok_ctr[a] += 1
    top_toks = {a for a, _ in tok_ctr.most_common(SMOKE_TOP_TOKENS)}

    ev_smoke: list[dict] = []
    tok_seen: Counter = Counter()
    for ev in sorted(actionable, key=lambda x: x.get("post_time_utc", ""), reverse=True):
        a = ev.get("token_address", "").lower().strip()
        if a in top_toks and tok_seen[a] < 5:
            ev_smoke.append(ev); tok_seen[a] += 1
        if len(ev_smoke) >= SMOKE_TOP_EVENTS:
            break

    used: dict[str, int] = {}
    PAD_B = 130 * 60 * 1000
    PAD_A = 330 * 60 * 1000
    out: list[dict] = []

    for ev in ev_smoke:
        addr = ev.get("token_address", "").lower().strip()
        ts_str = ev.get("post_time_utc", "")
        try:
            ts_iso = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
            t0 = int(datetime.fromisoformat(ts_iso).astimezone(UTC).timestamp() * 1000)
        except ValueError:
            continue
        t_min = t0 - PAD_B
        t_max = t0 + PAD_A
        row: dict[str, Any] = {
            "token_address": addr, "token_symbol": ev.get("token_symbol", ""),
            "post_id": ev.get("post_id", ""), "post_time_utc": ts_str,
            "okx_1m_available": False, "okx_1m_bars": 0,
            "okx_1h_available": False, "okx_1h_bars": 0,
            "gecko_1m_available": False, "gecko_1m_bars": 0,
            "best_available": "", "calls_used": 0,
        }
        c0 = budget.okx
        if client and budget.okx_ok() and used.get(addr, 0) < SMOKE_PER_TOKEN:
            bars, _ = S002.okx_candles(client, addr, bar="1m", limit="300", before_ms=t_min, after_ms=t_max)
            used[addr] = used.get(addr, 0) + 1
            if bars:
                s = S002.candles_to_series(bars)
                if S002.series_covers_window(s, t_min, t_max):
                    row["okx_1m_available"] = True; row["okx_1m_bars"] = len(bars)
        if not row["okx_1m_available"] and client and budget.okx_ok() and used.get(addr, 0) < SMOKE_PER_TOKEN:
            bars2, _ = S002.okx_candles(client, addr, bar="1H", limit="30", before_ms=t_min, after_ms=t_max)
            used[addr] = used.get(addr, 0) + 1
            if bars2 and len(bars2) >= 3:
                row["okx_1h_available"] = True; row["okx_1h_bars"] = len(bars2)
        pool = str(cov_map.get(addr, {}).get("gecko_terminal_pool_id") or "")
        if not pool:
            _, p2 = S002.gecko_ohlcv_available(budget, addr)
            pool = str(p2 or "")
        if pool and budget.gecko < S002.CAP_GECKO_SOFT:
            gs = S002.gecko_fetch_window(budget, pool, before_ts_sec=int(t_max//1000)+180, limit=300)
            if gs and len(gs) >= 5 and S002.series_covers_window(gs, t_min, t_max):
                row["gecko_1m_available"] = True; row["gecko_1m_bars"] = len(gs)
        row["calls_used"] = budget.okx - c0
        row["best_available"] = (
            "okx_1m" if row["okx_1m_available"] else
            "gecko_1m" if row["gecko_1m_available"] else
            "okx_1h" if row["okx_1h_available"] else "none"
        )
        out.append(row)
        if budget.okx >= CAP_OKX_SMOKE:
            break
    return out


# ===========================================================================
# Route4 preflight
# ===========================================================================

BASE_BLOCK_TIME  = 2
BLOCKS_PER_HOUR  = 3600 // BASE_BLOCK_TIME

def route4_preflight(actionable: list[dict], smoke_rows: list[dict], one_min_cov: float) -> dict:
    n_act = len({(r["post_id"], r["token_address"]) for r in actionable})
    recommendation = "NOT_RECOMMENDED"
    reason = ""
    if n_act < 30:
        reason = f"actionable_events={n_act} < 30"
    elif one_min_cov >= 0.50:
        recommendation = "API_SUFFICIENT"
        reason = f"1m_coverage={one_min_cov:.1%} >= 50%"
    else:
        recommendation = "TARGETED_ROUTE4_VIABLE"
        reason = f"actionable={n_act} >= 30 and 1m_coverage={one_min_cov:.1%} < 50%"

    no_1m = {r["token_address"] for r in smoke_rows if r["best_available"] not in ("okx_1m", "gecko_1m")}
    cands = [r for r in actionable if r["token_address"] in no_1m]
    window_blocks = int(12 * BLOCKS_PER_HOUR)
    return {
        "candidate_events_for_onchain_repair": len(cands),
        "estimated_blocks_per_event": window_blocks,
        "estimated_rpc_calls_min": len(cands) * (window_blocks // 100),
        "estimated_rpc_calls_max": len(cands) * (window_blocks // 20),
        "required_protocols": ["Uniswap V3", "Uniswap V2", "Aerodrome", "BaseSwap"],
        "required_decoders": ["Swap event", "Sync event", "Transfer event"],
        "expected_clickhouse_tables": ["base.swap_events", "base.token_prices_1m", "base.pool_ticks"],
        "route4_recommendation": recommendation,
        "route4_reason": reason,
        "note": "Targeted only; full Base non-V3 backfill is forbidden by sprint spec.",
    }


# ===========================================================================
# Writers
# ===========================================================================

INTENT_COLS = [
    "post_id", "author", "token_symbol", "token_address", "post_time_utc", "chain",
    "intent_label", "polarity", "time_horizon", "actionability", "quote_or_original",
    "evidence_span", "confidence", "classifier_method",
    "include_in_primary_buy_call_backtest", "exclude_reason",
]
CLEAN_EV_COLS = [
    "post_id", "author", "token_symbol", "token_address", "post_time_utc", "chain",
    "intent_label", "confidence", "pre_return_30m", "pre_return_60m", "pre_return_120m",
    "pre_runup_bucket", "evidence_span",
]
GRID_COLS = [
    "author", "post_id", "post_time_utc", "token_symbol", "token_address",
    "intent_label", "pre_runup_bucket", "data_source_used", "data_resolution",
    "entry_delay_min", "exit_after_min", "entry_price", "exit_price", "return_pct",
    "max_drawdown_before_exit", "max_runup_before_exit", "skip_reason",
]
SUMMARY_COLS = [
    "policy", "entry_delay_min", "exit_after_min", "slice_name", "group_keys",
    "n", "hit_rate", "mean_return", "median_return", "profit_factor",
    "max_drawdown_avg", "best_return", "worst_return",
    "data_resolution", "data_source_distribution",
]
SMOKE_COLS = [
    "token_address", "token_symbol", "post_id", "post_time_utc",
    "okx_1m_available", "okx_1m_bars", "okx_1h_available", "okx_1h_bars",
    "gecko_1m_available", "gecko_1m_bars", "best_available", "calls_used",
]


def _csv(path: Path, cols: list, rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def write_intent_dist(labeled: list[dict]) -> None:
    by_a: dict[str, list] = defaultdict(list)
    for r in labeled: by_a[r["author"]].append(r)
    auth_rows = []
    for auth, rr in sorted(by_a.items()):
        lc = Counter(r["intent_label"] for r in rr)
        inc = sum(1 for r in rr if r["include_in_primary_buy_call_backtest"] == "true")
        auth_rows.append({"author": auth, "total": len(rr), "actionable": inc, **{f"n_{k}": v for k, v in lc.items()}})
    acols = sorted({k for r in auth_rows for k in r if k.startswith("n_")})
    _csv(OUT_INTENT_AUTHOR, ["author", "total", "actionable"] + acols, auth_rows)

    by_t: dict[str, list] = defaultdict(list)
    for r in labeled: by_t[r["token_symbol"] or r["token_address"][:10]].append(r)
    tok_rows = []
    for tok, rr in sorted(by_t.items()):
        lc = Counter(r["intent_label"] for r in rr)
        inc = sum(1 for r in rr if r["include_in_primary_buy_call_backtest"] == "true")
        tok_rows.append({"token": tok, "total": len(rr), "actionable": inc, **{f"n_{k}": v for k, v in lc.items()}})
    tcols = sorted({k for r in tok_rows for k in r if k.startswith("n_")})
    _csv(OUT_INTENT_TOKEN, ["token", "total", "actionable"] + tcols, tok_rows)


def write_route4_md(r4: dict, smoke_rows: list[dict]) -> None:
    okx1m = sum(1 for r in smoke_rows if r.get("okx_1m_available"))
    gk1m  = sum(1 for r in smoke_rows if r.get("gecko_1m_available"))
    okx1h = sum(1 for r in smoke_rows if r.get("okx_1h_available"))
    none_ = sum(1 for r in smoke_rows if r.get("best_available") == "none")
    lines = [
        "# Route4 Targeted On-Chain Repair Preflight — S004",
        f"\n- Generated UTC: `{datetime.now(UTC).isoformat()}`",
        f"\n## Recommendation\n",
        f"**`route4_recommendation`**: **{r4['route4_recommendation']}**",
        f"- Reason: {r4['route4_reason']}",
        f"- candidate_events_for_onchain_repair: {r4['candidate_events_for_onchain_repair']}",
        f"- estimated_blocks_per_event: {r4['estimated_blocks_per_event']:,}",
        f"- estimated_rpc_calls_min: {r4['estimated_rpc_calls_min']:,}",
        f"- estimated_rpc_calls_max: {r4['estimated_rpc_calls_max']:,}",
        f"\n## Required Protocols\n" + "\n".join(f"- {p}" for p in r4["required_protocols"]),
        f"\n## Required Decoders\n" + "\n".join(f"- {d}" for d in r4["required_decoders"]),
        f"\n## Expected ClickHouse Tables\n" + "\n".join(f"- `{t}`" for t in r4["expected_clickhouse_tables"]),
        f"\n> {r4['note']}",
        f"\n## 1m Smoke Summary (n={len(smoke_rows)})\n",
        f"- okx_1m: {okx1m}",
        f"- gecko_1m: {gk1m}",
        f"- okx_1h_only: {okx1h - okx1m}",
        f"- none: {none_}",
    ]
    with OUT_ROUTE4.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ===========================================================================
# S003 grid fallback (reuse existing price data when OKX unavailable)
# ===========================================================================

def load_s3_grid_lookup() -> dict:
    """
    Build a lookup from (post_id, token_addr_lower, entry_delay_str, exit_after_str)
    -> grid row dict, from S003's event_return_grid.csv.
    Only valid (non-skip) rows are included.
    """
    lookup: dict = {}
    try:
        for r in csv.DictReader(open(S003_GRID, newline="", encoding="utf-8")):
            if str(r.get("skip_reason", "")).strip():
                continue
            k = (r["post_id"], r["token_address"].lower().strip(),
                 str(r["entry_delay_min"]), str(r["exit_after_min"]))
            lookup[k] = r
    except FileNotFoundError:
        pass
    return lookup


def run_backtest_s3_fallback(actionable: list, s3_lookup: dict) -> list:
    """
    Build clean_grid from S003 price data for events that have coverage.
    Enriches each row with intent_label and pre_runup_bucket.
    """
    intent_map = {(r["post_id"], r["token_address"].lower()): r for r in actionable}
    rows: list[dict] = []
    covered: set = set()

    for ev in actionable:
        k_base = (ev["post_id"], ev["token_address"].lower())
        has_any = False
        for ed in ENTRY_DELAYS:
            for xa in EXIT_AFTERS:
                k = (*k_base, str(ed), str(xa))
                s3r = s3_lookup.get(k)
                if s3r:
                    has_any = True
                    covered.add(k_base)
                    row = {
                        "author": ev.get("author", ""),
                        "post_id": ev["post_id"],
                        "post_time_utc": ev.get("post_time_utc", ""),
                        "token_symbol": ev.get("token_symbol", ""),
                        "token_address": ev.get("token_address", ""),
                        "intent_label": ev.get("intent_label", ""),
                        "pre_runup_bucket": ev.get("pre_runup_bucket", ""),
                        "data_source_used": s3r.get("data_source_used", "s3_fallback"),
                        "data_resolution": "1h" if "1h" in s3r.get("data_source_used", "") else "1m",
                        "entry_delay_min": ed, "exit_after_min": xa,
                        "entry_price": s3r.get("entry_price", ""),
                        "exit_price": s3r.get("exit_price", ""),
                        "return_pct": s3r.get("return_pct", ""),
                        "max_drawdown_before_exit": s3r.get("max_drawdown_before_exit", ""),
                        "max_runup_before_exit": s3r.get("max_runup_before_exit", ""),
                        "skip_reason": "",
                    }
                    # Chase filter
                    if ev.get("pre_runup_bucket") == "POST_PUMP_RETROSPECTIVE":
                        row["skip_reason"] = "CHASE_EXCLUDED_PRE_RUNUP"
                    # Hourly skip for short windows
                    if "1h" in row["data_source_used"] and xa < 60:
                        row["skip_reason"] = "HOURLY_RESOLUTION_SHORT_WINDOW_SKIP"
                    rows.append(row)
                else:
                    rows.append({
                        "author": ev.get("author", ""),
                        "post_id": ev["post_id"],
                        "post_time_utc": ev.get("post_time_utc", ""),
                        "token_symbol": ev.get("token_symbol", ""),
                        "token_address": ev.get("token_address", ""),
                        "intent_label": ev.get("intent_label", ""),
                        "pre_runup_bucket": ev.get("pre_runup_bucket", ""),
                        "data_source_used": "", "data_resolution": "",
                        "entry_delay_min": ed, "exit_after_min": xa,
                        "entry_price": "", "exit_price": "", "return_pct": "",
                        "max_drawdown_before_exit": "", "max_runup_before_exit": "",
                        "skip_reason": "NO_PRICE_SERIES",
                    })
        if not has_any:
            rows.append({
                "author": ev.get("author", ""), "post_id": ev["post_id"],
                "post_time_utc": ev.get("post_time_utc", ""),
                "token_symbol": ev.get("token_symbol", ""),
                "token_address": ev.get("token_address", ""),
                "intent_label": ev.get("intent_label", ""),
                "pre_runup_bucket": ev.get("pre_runup_bucket", ""),
                "data_source_used": "", "data_resolution": "",
                "entry_delay_min": 0, "exit_after_min": 5,
                "entry_price": "", "exit_price": "", "return_pct": "",
                "max_drawdown_before_exit": "", "max_runup_before_exit": "",
                "skip_reason": "NO_PRICE_SERIES",
            })
    print(f"  s3_fallback: covered {len(covered)}/{len(actionable)} events, {len(rows)} grid rows")
    return rows


def smoke_from_s3(actionable: list, s3_lookup: dict) -> list:
    """Generate smoke rows for top tokens using S003 data availability."""
    tok_ctr: Counter = Counter(ev.get("token_address", "").lower() for ev in actionable)
    top_toks = {a for a, _ in tok_ctr.most_common(SMOKE_TOP_TOKENS)}
    out: list[dict] = []
    seen: Counter = Counter()
    for ev in sorted(actionable, key=lambda x: x.get("post_time_utc", ""), reverse=True):
        addr = ev.get("token_address", "").lower().strip()
        if addr not in top_toks or seen[addr] >= 5:
            continue
        seen[addr] += 1
        # Check S3 grid for any valid row
        has_s3 = any(s3_lookup.get((ev["post_id"], addr, str(ed), str(xa)))
                     for ed in [0, 5] for xa in [60, 120])
        src = ""
        if has_s3:
            k = next((s3_lookup.get((ev["post_id"], addr, str(ed), str(xa)))
                      for ed in [0, 5] for xa in [60, 120]
                      if s3_lookup.get((ev["post_id"], addr, str(ed), str(xa)))), {})
            src = k.get("data_source_used", "s3_fallback") if k else "s3_fallback"
        out.append({
            "token_address": addr, "token_symbol": ev.get("token_symbol", ""),
            "post_id": ev["post_id"], "post_time_utc": ev.get("post_time_utc", ""),
            "okx_1m_available": "1m" in src and "okx" in src,
            "okx_1m_bars": 0,
            "okx_1h_available": "1h" in src or "okx" in src,
            "okx_1h_bars": 0,
            "gecko_1m_available": "gecko" in src and "1m" not in src.replace("terminal",""),
            "gecko_1m_bars": 0,
            "best_available": "okx_1h" if has_s3 else "none",
            "calls_used": 0,
        })
        if len(out) >= SMOKE_TOP_EVENTS:
            break
    return out


def economic_verdict(n: int, ca: float, ct: float) -> str:
    if n < 12: return "INCONCLUSIVE_ACTIONABLE_SAMPLE_TOO_SMALL"
    if n < 30:
        return "FAIL_CLEAN_BUY_CALL" if ca > 0.60 else "WEAK_EVIDENCE_ONLY"
    return "FAIL_CLEAN_BUY_CALL" if ca > 0.60 else "PASS_CLEAN_BUY_CALL_DIAGNOSTIC"


# ===========================================================================
# Main
# ===========================================================================

def main() -> None:
    print("[S004] Loading S003 data ...")
    posts_map, unique_ev, _, cov_map = load_s003_data()
    print(f"  posts={len(posts_map)}  unique_events={len(unique_ev)}")

    print("[S004] Classifying intent ...")
    labeled = run_classification(unique_ev, posts_map)
    lc = Counter(r["intent_label"] for r in labeled)
    print(f"  {dict(sorted(lc.items()))}")
    _csv(OUT_INTENT, INTENT_COLS, labeled)
    write_intent_dist(labeled)

    actionable = [r for r in labeled if r["include_in_primary_buy_call_backtest"] == "true"]
    print(f"[S004] Actionable bullish: {len(actionable)}")

    print("[S004] Building series cache ...")
    budget = S002.CallBudget()
    client = None
    ok_key = os.environ.get("OKX_API_KEY", "")
    ok_sec = os.environ.get("OKX_SECRET_KEY", "")
    ok_pp  = os.environ.get("OKX_API_PASSPHRASE", "")
    if ok_key and ok_sec and ok_pp:
        client = S002.OKXClient(budget)
        print(f"  OKX key={ok_key[:8]}...")

    used: dict[str, int] = {}
    cache = build_series_cache(actionable, cov_map, client, budget, used)
    print(f"  cache_entries={len(cache)}  OKX_calls={budget.okx}")

    print("[S004] Pre-runup annotation ...")
    actionable = annotate_pre_runup(actionable, cache)
    pre_filt = sum(1 for r in actionable if r.get("pre_runup_bucket") == "POST_PUMP_RETROSPECTIVE")
    print(f"  POST_PUMP events: {pre_filt}")
    _csv(OUT_CLEAN_EVENTS, CLEAN_EV_COLS, actionable)

    print("[S004] Running clean backtest ...")
    s3_lookup = load_s3_grid_lookup()
    print(f"  s3_lookup entries: {len(s3_lookup)}")
    if cache:
        clean_grid = run_backtest(actionable, cache)
    else:
        print("  [fallback] No live series cache; using S003 grid price data")
        clean_grid = run_backtest_s3_fallback(actionable, s3_lookup)
    _csv(OUT_CLEAN_GRID, GRID_COLS, clean_grid)
    valid_rows = [r for r in clean_grid if not str(r.get("skip_reason", "")).strip()]
    uv = dedupe_valid(valid_rows)
    vpe = len(uv)
    print(f"  valid_grid_rows={len(valid_rows)}  unique_valid_events={vpe}")

    summaries = build_grid_summaries(clean_grid)
    _csv(OUT_GRID_SUMMARY, SUMMARY_COLS, summaries)
    asums = build_author_summaries(clean_grid)
    _csv(OUT_AUTH_SUMMARY, ["author"] + SUMMARY_COLS, asums)

    grid_cells = [s for s in summaries if str(s.get("slice_name", "")).startswith("grid_")
                  and int(s.get("n", 0)) >= 3]
    best_grid = max(grid_cells, key=lambda x: float(x.get("mean_return") or 0), default={})
    pol_rows  = [s for s in summaries if s.get("policy") in FIXED_POLICIES and int(s.get("n", 0)) > 0]
    best_pol  = max(pol_rows, key=lambda x: float(x.get("mean_return") or 0), default={})

    auth_ctr: Counter = Counter()
    tok_ctr:  Counter = Counter()
    for r in uv:
        auth_ctr[r.get("author", "")] += 1
        tok_ctr[r.get("token_address", "").lower()] += 1
    top_auth_share = max(auth_ctr.values()) / vpe if vpe else 0.0
    top_tok_share  = max(tok_ctr.values())  / vpe if vpe else 0.0
    top_auth = auth_ctr.most_common(1)[0][0] if auth_ctr else ""
    top_tok  = tok_ctr.most_common(1)[0][0]  if tok_ctr  else ""

    src_dist = Counter(r["data_source_used"] for r in valid_rows)
    okx_1m_valid = src_dist.get("okx_market_candles", 0)
    data_src_risk = (okx_1m_valid / (sum(src_dist.values()) or 1)) < 0.30

    print("[S004] 1m smoke ...")
    smoke_bud = S002.CallBudget()
    if client:
        smoke_client = S002.OKXClient(smoke_bud)
        smoke_rows = smoke_1m(actionable, cov_map, smoke_client, smoke_bud)
    else:
        print("  [fallback] No live client; deriving smoke from S003 grid")
        smoke_rows = smoke_from_s3(actionable, s3_lookup)
    _csv(OUT_SMOKE, SMOKE_COLS, smoke_rows)
    okx1m_hits  = sum(1 for r in smoke_rows if r.get("okx_1m_available"))
    geck1m_hits = sum(1 for r in smoke_rows if r.get("gecko_1m_available"))
    one_min_cov = (okx1m_hits + geck1m_hits) / len(smoke_rows) if smoke_rows else 0.0
    print(f"  smoke_n={len(smoke_rows)}  okx_1m={okx1m_hits}  gecko_1m={geck1m_hits}  cov={one_min_cov:.1%}")

    r4 = route4_preflight(actionable, smoke_rows, one_min_cov)
    write_route4_md(r4, smoke_rows)

    verdict = economic_verdict(vpe, top_auth_share, top_tok_share)

    flags: dict[str, Any] = {
        "sprint": "S004",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "S004_VERDICT": verdict,
        "VALID_EVENTS": vpe,
        "BEST_DIAGNOSTIC_GRID": {
            "entry_delay_min": best_grid.get("entry_delay_min", ""),
            "exit_after_min":  best_grid.get("exit_after_min", ""),
            "n": best_grid.get("n", 0),
            "hit_rate": best_grid.get("hit_rate", 0.0),
            "mean_return": best_grid.get("mean_return", 0.0),
            "data_resolution": best_grid.get("data_resolution", ""),
        },
        "BEST_FIXED_POLICY": {
            "policy": best_pol.get("policy", ""),
            "entry_delay_min": best_pol.get("entry_delay_min", ""),
            "exit_after_min":  best_pol.get("exit_after_min", ""),
            "n": best_pol.get("n", 0),
            "hit_rate": best_pol.get("hit_rate", 0.0),
            "mean_return": best_pol.get("mean_return", 0.0),
        },
        "CONCENTRATION_RISK": top_auth_share > 0.40 or top_tok_share > 0.40,
        "SINGLE_AUTHOR_RESEARCH_ONLY": top_auth_share > 0.60,
        "TOKEN_CONCENTRATION_RISK": top_tok_share > 0.40,
        "NO_GENERAL_ALPHA_CLAIM": top_auth_share > 0.60,
        "DATA_SOURCE_RISK": data_src_risk,
        "ONE_MINUTE_DATA_INSUFFICIENT": one_min_cov < 0.50,
        "QUOTE_UNAVAILABLE_LIMITS_EXECUTABLE_ALPHA_CLAIMS": True,
        "TOP_AUTHOR": top_auth,
        "TOP_AUTHOR_SHARE": round(top_auth_share, 4),
        "TOP_TOKEN": top_tok,
        "TOP_TOKEN_SHARE": round(top_tok_share, 4),
        "total_resolved_events": len(unique_ev),
        "classified_events": len(labeled),
        "ambiguous_count": lc.get("AMBIGUOUS", 0),
        "actionable_bullish_events": len(actionable),
        "actionable_bullish_valid_price_events": vpe,
        "btcbabycow_actionable_events": sum(1 for r in actionable if r.get("author", "").lower() == "@btcbabycow"),
        "quote_events_count": sum(1 for r in labeled if r["quote_or_original"] == "quote"),
        "quote_events_included_count": sum(1 for r in labeled
            if r["quote_or_original"] == "quote" and r["include_in_primary_buy_call_backtest"] == "true"),
        "retrospective_excluded_count": lc.get("RETROSPECTIVE", 0),
        "bearish_excluded_count": lc.get("BEARISH_AVOID", 0),
        "take_profit_excluded_count": lc.get("TAKE_PROFIT_OR_REDUCE", 0),
        "pre_runup_filtered_count": pre_filt,
        "clean_best_grid": {k: best_grid.get(k) for k in
            ["slice_name","entry_delay_min","exit_after_min","n","hit_rate","mean_return"]},
        "clean_best_fixed_policy": {k: best_pol.get(k) for k in
            ["policy","entry_delay_min","exit_after_min","n","hit_rate","mean_return"]},
        "one_minute_coverage_rate": round(one_min_cov, 4),
        "okx_1m_success_count": okx1m_hits,
        "gecko_1m_success_count": geck1m_hits,
        "route4_estimated_rpc_calls": r4["estimated_rpc_calls_min"],
        "route4_recommendation": r4["route4_recommendation"],
        "budget_counters": {
            "OKX_CALLS_USED": budget.okx,
            "GECKO_CALLS_USED": budget.gecko,
            "OKX_SMOKE_CALLS": smoke_bud.okx,
            "GECKO_SMOKE_CALLS": smoke_bud.gecko,
        },
        "inherits": {
            "S003_verdict": "FAIL_NO_GENERALIZATION_GATE",
            "S001_research_only": True,
            "S002_data_smoke_not_alpha": True,
        },
        "constraints_respected": {
            "no_x_api_calls": True, "no_executor": True, "no_live_trading": True,
            "no_model_training": True, "no_private_keys": True,
            "no_redis_signals": True, "no_swap_broadcast": True,
            "no_x_web_scrape": True, "no_full_route4_backfill": True,
        },
    }

    with OUT_FLAGS.open("w", encoding="utf-8") as f:
        json.dump(flags, f, indent=2, default=str)

    # ---- Readout ----
    def _fl(k: str) -> Any: return flags.get(k)
    readout_lines = [
        "# S004 — Clean Buy-Call Backtest",
        f"\n- Generated UTC: `{flags['generated_at_utc']}`",
        "\n## Classification counts\n",
        f"- total_resolved_events: **{_fl('total_resolved_events')}**",
        f"- classified_events: **{_fl('classified_events')}**",
        f"- ambiguous_count: **{_fl('ambiguous_count')}**",
        f"- actionable_bullish_events: **{_fl('actionable_bullish_events')}**",
        f"- actionable_bullish_valid_price_events: **{_fl('actionable_bullish_valid_price_events')}**",
        f"- btcbabycow_actionable_events: **{_fl('btcbabycow_actionable_events')}**",
        f"- quote_events: **{_fl('quote_events_count')}** ({_fl('quote_events_included_count')} included)",
        "\n## Intent distribution\n",
    ] + [f"- {l}: **{lc.get(l,0)}**" for l in sorted(ALL_LABELS)] + [
        "\n## Exclusions\n",
        f"- retrospective: **{_fl('retrospective_excluded_count')}**",
        f"- bearish: **{_fl('bearish_excluded_count')}**",
        f"- take_profit: **{_fl('take_profit_excluded_count')}**",
        f"- pre_runup_chase: **{_fl('pre_runup_filtered_count')}**",
        "\n## Clean backtest\n",
        f"- S004_VERDICT: **{_fl('S004_VERDICT')}**",
        f"- BEST_DIAGNOSTIC_GRID: `{json.dumps(_fl('BEST_DIAGNOSTIC_GRID'))}`",
        f"- BEST_FIXED_POLICY: `{json.dumps(_fl('BEST_FIXED_POLICY'))}`",
        f"- CONCENTRATION_RISK: **{_fl('CONCENTRATION_RISK')}**",
        f"- SINGLE_AUTHOR_RESEARCH_ONLY: **{_fl('SINGLE_AUTHOR_RESEARCH_ONLY')}**",
        f"- TOP_AUTHOR: **{_fl('TOP_AUTHOR')}** ({_fl('TOP_AUTHOR_SHARE'):.1%})",
        "\n## 1m smoke\n",
        f"- okx_1m_success_count: **{_fl('okx_1m_success_count')}**",
        f"- gecko_1m_success_count: **{_fl('gecko_1m_success_count')}**",
        f"- one_minute_coverage_rate: **{_fl('one_minute_coverage_rate'):.1%}**",
        f"- ONE_MINUTE_DATA_INSUFFICIENT: **{_fl('ONE_MINUTE_DATA_INSUFFICIENT')}**",
        "\n## Route4\n",
        f"- route4_recommendation: **{_fl('route4_recommendation')}**",
        f"- route4_estimated_rpc_calls: **{_fl('route4_estimated_rpc_calls')}**",
    ]
    with OUT_READOUT.open("w", encoding="utf-8") as f:
        f.write("\n".join(readout_lines) + "\n")

    print(f"\n{'='*60}")
    print(f"S004 COMPLETE")
    print(f"  S004_VERDICT:          {flags['S004_VERDICT']}")
    print(f"  actionable_bullish:    {flags['actionable_bullish_events']}")
    print(f"  valid_price_events:    {flags['actionable_bullish_valid_price_events']}")
    print(f"  TOP_AUTHOR:            {flags['TOP_AUTHOR']} ({flags['TOP_AUTHOR_SHARE']:.1%})")
    print(f"  1m_coverage:           {flags['one_minute_coverage_rate']:.1%}")
    print(f"  route4:                {flags['route4_recommendation']}")
    print(f"  OKX calls:             {budget.okx} (main) + {smoke_bud.okx} (smoke)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

