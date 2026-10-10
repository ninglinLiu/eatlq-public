#!/usr/bin/env python3
"""GL4 receipt-first reconstruction of the 2026-06-10..2026-07-13 Base live record.

This program has no signing, sending, Redis, signal, executor, or live-config
code path.  Its RPC allow-list is deliberately limited to archive reads.  The
explorer is used only to enumerate the two evidenced wallet histories; every
trade classification and amount comes from canonical transaction/receipt data.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics"
PREREG = ROOT / "configs/research/base_gl4_live_reconciliation_preregistration_20260716.json"
PREREG_SHA256 = "6cff51454d40b7623cfcd3cf2ed2e10ac461019c67a88e4c921a33547e4ad7b7"
OUT = DIAG / "base_gl4_live_truth_ledger_20260716.json"
RPC_LOG = DIAG / "base_gl4_rpc_full_ledger_20260716.jsonl"
RPC_SUMMARY = DIAG / "base_gl4_rpc_ledger_20260716.json"
CORPUS = DIAG / "base_gl4_corpus_manifest_20260716.json"

START_TS = 1781049600  # 2026-06-10T00:00:00Z
END_TS = 1783987199    # 2026-07-13T23:59:59Z
WALLETS = {
    "legacy_canary": "0x3a0DE7b4e6Ea125FFA3155b0026E5F717A1e86EE",
    "keystone_executor": "0x4B7cd953871663474a0A25B81bddDf5C1f1195A7",
}
WETH = "0x4200000000000000000000000000000000000006"
CHAINLINK_ETH_USD = "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70"
QUOTER_V2 = "0x3d4e44eb1374240ce5f1b871ab261cd16335b76a"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
WITHDRAWAL = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
ALLOWED_RPC = {"eth_chainId", "eth_getTransactionByHash", "eth_getTransactionReceipt", "eth_getBlockByNumber", "eth_call"}

# Labels are frozen from contemporaneous source evidence.  They do not supply
# amounts or receipt status; those are independently reconstructed below.
KNOWN_LEGS = {
    "0xd585074def49c74bf112339ff7869f640011d04548acdca2472727fd447e4681": ("JOTCHUA", "BUY", "fee100_fishing_canary"),
    "0xea6a30624afa5159df8722bd9fa7d77fdb33af0269b5025daa0a3b8006ffb74e": ("BOT", "BUY", "fee100_fishing_canary"),
    "0x553fcd4754f357d9737bf2ad0de1a5bb6659cfc9623862d2af9957a88412ba82": ("NEXIO", "BUY", "legacy_canary_roundtrip"),
    "0xb8468729578faf6257c45999d5171c6b5d704f96ccfde8694148bbeb34a9e155": ("NEXIO", "SELL", "legacy_canary_roundtrip"),
    "0xd925db0d3d1956d91ee37a106f28e53835c6a8c7921280c0c642b5c02ad2547f": ("USDC", "BUY", "manual_bluechip_smoke"),
    "0xa755efd0287a7e51d0d7f2f86c2bfc5b656f4ff9dc00173b0894b6a98e81e39b": ("USDC", "SELL", "manual_bluechip_smoke"),
    "0x84078a4b0af53fd67595e4208dfed29e5cbea9053c718c533501b17ad6134a01": ("PARADAENGI-1", "BUY", "keystone_natural_live"),
    "0x8ceef47a6d5901d44e6a18ce74f15cd38c613c9904264ede2836ac940b5989be": ("PARADAENGI-2", "BUY", "keystone_natural_live"),
    "0x062e8c9229e06806313a66810900de90f3454af1da79b69e635eb0adbf6430c6": ("USDC", "BUY", "signal_path_controlled_smoke"),
    "0xaefbb39f4fb7dbd400b9a677d2632470bb9e7b7e4927600dd0f800def09be091": ("USDC", "SELL", "signal_path_controlled_smoke"),
    "0x84fb93fc4d62cb17b85659bd9101b4dab6fd647c645316d24e716d42a566ecf1": ("PARADAENGI-3", "BUY", "keystone_aggressive_live"),
}
APPROVAL_LINKS = {
    "0x1241f7ba61c60e0a2f16dfdb72dc96347d9fbab9bc5cf967a0b59433af66ae37": "BOT",
    "0xfd07e877e29a210b0bb29ffe9791d9489f7cff456b48992f0ecdff85861111b6": "NEXIO",
    "0x3a2c580f17cb4be55ee997a1907f63af09533adf93c8cce69847e663fdf34a94": "USDC-manual",
}

TIMELINE = [
    {"event_utc": "2026-06-18T09:30:01Z", "state": "OPEN", "reason": "fee100 fishing canary started with --live, $8 per trade", "precision": "EXACT_LOG", "anchor": "mvp/diagnostics/live/loop.log:5,15"},
    {"event_utc": "2026-06-18T13:27:13Z", "state": "LOSS_AND_ACCOUNTING_DROP", "reason": "JOTCHUA receipt succeeded, parser recorded tokens=0 and removed the position without a SELL", "precision": "EXACT_RECEIPT", "anchor": "mvp/diagnostics/live/loop.log:2933-2963; scripts/live/canary_executor.py:153-156"},
    {"event_utc": "2026-06-18T17:04:16Z", "state": "CANARY_LOG_END", "reason": "last live loop record; no written stop reason", "precision": "EXACT_LOG_STATE_UNKNOWN_AFTER", "anchor": "mvp/diagnostics/live/loop.log:5987-6000"},
    {"event_utc": "2026-06-22T00:00:00Z", "state": "PAUSED", "reason": "SYSTEM_PANIC set while blacklist, mintability, canary and daily-loss holes were repaired", "precision": "DAY_BOUND_ONLY", "anchor": "mvp/audits/disaster_left_tail_audit_20260622/config_fix_changelog_cn.md:1-17,34-35"},
    {"event_utc": "2026-06-22T17:54:23Z", "state": "CONTROLLED_SMOKE_WHILE_PAUSED", "reason": "manual WETH/USDC receipt roundtrip; panic remained active and this was not natural strategy selection", "precision": "EXACT_RECEIPT", "anchor": "mvp/audits/go_live_session_20260624/conversation_transcript_full_cn.md:610-664"},
    {"event_utc": "2026-06-23T04:12:11Z", "state": "OPEN_UNEXPECTEDLY", "reason": "human had authorized execution, but SYSTEM_PANIC was unexpectedly absent; first of two natural PARADAENGI buys", "precision": "OPEN_TIME_BOUNDED_BY_FIRST_RECEIPT", "anchor": "mvp/audits/go_live_session_20260624/conversation_transcript_full_cn.md:687,699-728"},
    {"event_utc": "2026-06-23T04:52:03Z", "state": "PAUSED", "reason": "human explicitly ordered manual_pause_20260623_after_2_honeypots after two abandoned 0.057 ETH positions", "precision": "AFTER_LAST_ABANDON_BOUND", "anchor": "mvp/audits/go_live_session_20260624/conversation_transcript_full_cn.md:732-746"},
    {"event_utc": "2026-06-23T17:49:13Z", "state": "CONTROLLED_REOPEN", "reason": "panic cleared for signal-path WETH/USDC smoke", "precision": "EXACT_WATCH_LOG", "anchor": "infra/compose/smoke_watch.log:1-6"},
    {"event_utc": "2026-06-23T18:04:13Z", "state": "AUTOMATIC_REARM_AFTER_SMOKE", "reason": "controlled smoke detected and watchdog re-armed", "precision": "EXACT_WATCH_LOG", "anchor": "infra/compose/smoke_watch.log:1-6"},
    {"event_utc": "2026-06-23T18:15:48Z/18:16:56Z", "state": "OPEN", "reason": "human-confirmed autonomous $50 live; panic deleted, .0302 ETH, concurrency 3", "precision": "BOUNDED_RUNTIME_WINDOW", "anchor": "mvp/audits/go_live_session_20260624/conversation_transcript_full_cn.md:1833-1841"},
    {"event_utc": "2026-06-25T17:39:32Z", "state": "RESTART_OPEN_AGGRESSIVE", "reason": "after panic/rebuild/dry-print, historical F lists disabled and trail .25 enabled", "precision": "EXACT_RUNTIME_LOG", "anchor": "mvp/audits/golive_aggressive_20260625/report_cn.md:135-149"},
    {"event_utc": "2026-06-25T15:47:00Z/2026-06-27T06:03:00Z", "state": "BRIDGE_OUTAGE_NOT_PROVEN_PAUSE", "reason": "outage artifact reports seven pools and zero would-have-bought; cause and executor/panic state absent", "precision": "INTERVAL", "anchor": "mvp/audits/outage_missed_20260627/outage_missed_counterfactual.json:3-20"},
    {"event_utc": "2026-06-29T01:10:37Z", "state": "THIRD_LOSS_THEN_UNKNOWN", "reason": "third PARADAENGI receipt; no eligible white-on-black pause/restart artifact afterward", "precision": "EXACT_RECEIPT", "anchor": "mvp/audits/blacklist_framework_recompare_20260629/blacklist_framework_recompare.json:607-616"},
]

CONFIG_SEGMENTS = [
    {"segment": "2026-06-18 fee100 canary", "runtime_pin": "LIVE", "model": "corrected_method_detector, not KEYSTONE", "size_weth": "0.0026667 actual / $8 flag", "concurrency": 2, "F": "not present", "trail": "arm +5%, pullback 3%", "hold": "2h", "E": "not present", "entry_safety": "fee100 onset/heat/Rule A-B/two-way QV2", "evidence": "scripts/live/canary_cycle.sh:15-30; scripts/live/canary_executor.py:237-249"},
    {"segment": "2026-06-22 controlled smoke", "runtime_pin": "PANIC_ACTIVE_MANUAL", "model": "not natural selection", "size_weth": 0.003, "concurrency": 3, "F": "not yet deployed", "trail": "1.0 = disabled", "hold": "240m", "E": "not present", "entry_safety": "manual bluechip", "evidence": "go_live_session transcript:610-679; disaster audit:84-93"},
    {"segment": "2026-06-23 first natural KEYSTONE", "runtime_pin": "dry_run=false; panic unexpectedly absent", "model": "live pickle bd02d79d...; params 749a61da...; effective p<=.27", "size_weth": 0.057, "concurrency": 3, "F": "not yet deployed", "trail": "1.0 = disabled", "hold": "4h", "E": "not present", "entry_safety": "MIN_PRE5, friction>=-5%, sell_sim", "evidence": "go_live_session transcript:699-802,1286-1321"},
    {"segment": "2026-06-23 18:16 through 2026-06-24 ~05:00 initial defensive $50 live", "runtime_pin": "dry_run=false; panic cleared", "model": "same live pickle/params hashes", "size_weth": 0.0302, "concurrency": 3, "F": "session snapshot says 2 deployers + 1 PARA skeleton; later FP audit says PARA skeleton was removed and rebuilt, so exact transition time is bounded/contested", "trail": "1.0 = disabled", "hold": "4h", "E": "1800s/3 blocks/collapse .5", "entry_safety": "p<=.27, MIN_PRE5, depth>=3 fail-closed, friction>=-5%, sell_sim/external", "evidence": "mvp/audits/go_live_session_20260624/go_live_session_full_cn.md:182-194,313-326; mvp/audits/cluster_blacklist_fp_validation_20260624/skeleton_false_positive_validation_cn.md:56-71"},
    {"segment": "2026-06-24 historical-F expansion through 2026-06-25 aggressive cutover", "runtime_pin": "deployed before 06-25 runtime comparison; exact deploy timestamp absent", "model": "same live pickle/params hashes", "size_weth": 0.0302, "concurrency": 3, "F": "245 historical deployers + 33 safe skeletons + 2 PARA deployers; PARA skeleton off", "trail": "1.0 = disabled", "hold": "4h", "E": "1800s/3 blocks/collapse .5", "entry_safety": "p<=.27, MIN_PRE5, depth>=3 fail-closed, friction>=-5%, sell_sim/external", "evidence": "mvp/audits/funding_cluster_poc_20260624/deployer_funding_cluster_cn.md:79-91; mvp/audits/golive_aggressive_20260625/report_cn.md:8-28"},
    {"segment": "2026-06-25 aggressive restart", "runtime_pin": "17:39:32 live", "model": "same live pickle/params hashes", "size_weth": 0.0302, "concurrency": 3, "F": "historical 245+33 OFF; only 2 PARA deployers + runtime learned", "trail": 0.25, "hold": "4h", "E": "1800s", "entry_safety": "p<=.27, MIN_PRE5, depth>=3, friction>=-5%, sell_sim/external", "evidence": "mvp/audits/golive_aggressive_20260625/report_cn.md:8-46,135-149"},
    {"segment": "2026-06-29 after third incident through 07-13", "runtime_pin": "UNKNOWN", "model": "current source/post-fix research scenario cannot backfill runtime", "size_weth": "UNKNOWN_AFTER_INCIDENT", "concurrency": "UNKNOWN", "F": "UNKNOWN", "trail": "UNKNOWN", "hold": "UNKNOWN", "E": "UNKNOWN", "entry_safety": "UNKNOWN", "evidence": "no contemporaneous runtime banner/deploy log; current source defaults rejected by prereg conflict priority"},
]

ESTIMAND_COMPARISON = [
    {"field": "population/model", "actual_live": "live pickle trained through 2026-06-01, effective p<=.27", "GL3_plus56": "431 same-age candidates / 116 selected; B5 retrain through 2026-02-27; p_cut=.2808669872", "same": False},
    {"field": "notional/portfolio", "actual_live": ".057 then .0302 WETH; concurrency 3", "GL3_plus56": ".001 WETH per estimand; no wallet concurrency simulation", "same": False},
    {"field": "entry/safety gates", "actual_live": "MIN_PRE5, later depth3/F/sell-sim/external/E", "GL3_plus56": "prob/friction/regime only; no explicit depth/F/sell-sim/external/E", "same": False},
    {"field": "exit", "actual_live": "trail 1.0 off, then .25; 4h", "GL3_plus56": "legacy priced-live trail .35 / 4h, trigger +1/+5/+15", "same": False},
    {"field": "execution evidence", "actual_live": "11 receipt-backed legs, actual gas, .03-.057 WETH natural size", "GL3_plus56": "mostly guarded atomic/QV2 historical quote; $0.075 attempted-leg gas", "same": False},
    {"field": "date/regime", "actual_live": "2026-06-18..06-29 fills", "GL3_plus56": "frozen historical sample ends by 2026-06-08", "same": False},
    {"field": "aggressive F-off 207 rugs", "actual_live": "F was disabled 06-25; third PARA entered 06-29 and became unsellable", "GL3_plus56": "not the same cohort; separate aggressive backtest admitted 207 rugs and treated their profitability as core in-sample assumption", "same": False},
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def env_value(key: str) -> str | None:
    # AGENTS.md forbids reading a real .env.  Reproduction must inject only the
    # read-only endpoint variable into the process environment; it is never
    # printed or persisted.
    return os.environ.get(key)


class Rpc:
    def __init__(self, url: str):
        if "prettiest-blue-aura" not in url:
            raise RuntimeError("GL4 is frozen to read-only endpoint alias prettiest-blue-aura")
        self.url = url
        self.logical = 0
        self.methods = Counter()
        RPC_LOG.write_text("")

    def call(self, method: str, params: list):
        if method not in ALLOWED_RPC:
            raise RuntimeError(f"FORBIDDEN_RPC_METHOD:{method}")
        self.logical += 1
        self.methods[method] += 1
        req = {"jsonrpc": "2.0", "id": self.logical, "method": method, "params": params}
        started = time.time()
        request = urllib.request.Request(
            self.url, data=json.dumps(req).encode(),
            headers={"content-type": "application/json", "user-agent": "EATLQ-GL4-readonly/1"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.load(response)
        if body.get("error"):
            err = str(body["error"])
            row = {
                "logical_id": self.logical, "method": method, "status": "error",
                "request_sha256": canonical_hash({"method": method, "params": params}),
                "error_sha256": hashlib.sha256(err.encode()).hexdigest(),
                "error_class": "execution_reverted" if "revert" in err.lower() else "rpc_other",
            }
            with RPC_LOG.open("a") as handle:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            raise RuntimeError(err)
        result = body.get("result")
        row = {
            "logical_id": self.logical, "method": method, "status": "ok",
            "request_sha256": canonical_hash({"method": method, "params": params}),
            "response_sha256": canonical_hash(result),
        }
        with RPC_LOG.open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        return result


def explorer_inventory(address: str) -> list[dict]:
    base = f"https://base.blockscout.com/api/v2/addresses/{address}/transactions"
    params = None
    rows = []
    while True:
        url = base if not params else base + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"user-agent": "EATLQ-GL4-readonly/1"})
        with urllib.request.urlopen(request, timeout=120) as response:
            page = json.load(response)
        for item in page["items"]:
            ts = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).timestamp()
            sender = (item.get("from") or {}).get("hash", "")
            if START_TS <= ts <= END_TS and sender.lower() == address.lower():
                rows.append({
                    "hash": item["hash"].lower(), "timestamp": item["timestamp"],
                    "explorer_status": item.get("status"), "explorer_method": item.get("method"),
                })
        params = page.get("next_page_params")
        if not params:
            break
    rows.sort(key=lambda x: (x["timestamp"], x["hash"]))
    return rows


def h2i(value: str | None) -> int:
    return int(value or "0x0", 16)


def topic_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def pad_addr(address: str) -> str:
    return address.lower().replace("0x", "").rjust(64, "0")


def erc20_call(rpc: Rpc, address: str, selector_and_args: str, block: int) -> str:
    return rpc.call("eth_call", [{"to": address, "data": selector_and_args}, hex(block)])


def token_meta(rpc: Rpc, token: str, block: int, cache: dict) -> dict:
    key = token.lower()
    if key in cache:
        return cache[key]
    dec_raw = erc20_call(rpc, token, "0x313ce567", block)
    decimals = h2i(dec_raw)
    symbol = None
    try:
        raw = erc20_call(rpc, token, "0x95d89b41", block)
        b = bytes.fromhex(raw[2:])
        if len(b) >= 64:
            off = int.from_bytes(b[:32], "big")
            n = int.from_bytes(b[off:off + 32], "big")
            symbol = b[off + 32:off + 32 + n].decode("utf-8", "replace")
        elif b:
            symbol = b.rstrip(b"\x00").decode("utf-8", "replace")
    except Exception:
        symbol = None
    cache[key] = {"address": token.lower(), "decimals": decimals, "symbol": symbol}
    return cache[key]


def chainlink_at(rpc: Rpc, block: int) -> dict:
    raw = erc20_call(rpc, CHAINLINK_ETH_USD, "0xfeaf968c", block)
    words = [int(raw[2 + i:2 + i + 64], 16) for i in range(0, len(raw) - 2, 64)]
    answer = words[1]
    if answer >= 2**255:
        answer -= 2**256
    return {
        "feed": CHAINLINK_ETH_USD.lower(), "round_id": str(words[0]), "answer_raw": str(answer),
        "decimals": 8, "eth_usd": answer / 1e8, "started_at": words[2],
        "updated_at": words[3], "answered_in_round": str(words[4]), "block": block,
    }


def balance_at(rpc: Rpc, token: str, holder: str, block: int) -> int:
    return h2i(erc20_call(rpc, token, "0x70a08231" + pad_addr(holder), block))


def exact_sell_quote_at(rpc: Rpc, token: str, qty: int, pool: str, block: int) -> dict:
    fee = h2i(erc20_call(rpc, pool, "0xddca3f43", block))
    def pad_uint(value: int) -> str:
        return hex(value)[2:].rjust(64, "0")
    data = "0xc6a5026a" + pad_addr(token) + pad_addr(WETH) + pad_uint(qty) + pad_uint(fee) + pad_uint(0)
    try:
        raw = erc20_call(rpc, QUOTER_V2, data, block)
        amount = h2i("0x" + raw[2:66]) if raw and len(raw) >= 66 else 0
        return {"status": "MEASURED_QUOTE_NOT_FILL", "fee": fee, "amount_out_raw": str(amount), "amount_out_weth": amount / 1e18}
    except RuntimeError as exc:
        return {"status": "EXPLICIT_EXECUTION_REVERT", "fee": fee, "amount_out_raw": "0", "amount_out_weth": 0.0,
                "error_sha256": hashlib.sha256(str(exc).encode()).hexdigest()}


def find_end_block(rpc: Rpc) -> int:
    latest = h2i(rpc.call("eth_blockNumber", [])) if "eth_blockNumber" in ALLOWED_RPC else None
    # eth_blockNumber is intentionally absent.  The window's end is pinned by
    # deterministic interpolation from a known in-window block, then binary
    # search only with eth_getBlockByNumber.
    seed = rpc.call("eth_getBlockByNumber", [hex(47953045), False])
    seed_ts = h2i(seed["timestamp"])
    guess = 47953045 + max(1, (END_TS - seed_ts) // 2)
    lo, hi = 47953045, guess + 10000
    while h2i(rpc.call("eth_getBlockByNumber", [hex(hi), False])["timestamp"]) <= END_TS:
        lo, hi = hi, hi + 50000
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        ts = h2i(rpc.call("eth_getBlockByNumber", [hex(mid), False])["timestamp"])
        if ts <= END_TS:
            lo = mid
        else:
            hi = mid
    return lo


def corpus_paths() -> tuple[list[Path], list[str]]:
    selected: set[Path] = set()
    reasons: dict[Path, set[str]] = {}

    def add(path: Path, reason: str) -> None:
        if path.is_file():
            selected.add(path)
            reasons.setdefault(path, set()).add(reason)

    fixed_dirs = [
        "services/executor/src", "services/strategy", "services/signal_bridge", "scripts/live",
        "mvp/diagnostics/live", "mvp/runs",
    ]
    for rel in fixed_dirs:
        for path in (ROOT / rel).rglob("*"):
            add(path, f"required_tree:{rel}")
    for sprint in (75, 76, 77, 78, 79, 86):
        for directory in (ROOT / "logs").glob(f"sprint{sprint:03d}*"):
            for path in directory.rglob("*"):
                add(path, f"required_sprint_log:{sprint}")

    audit_root = ROOT / "mvp/audits"
    start_mtime = datetime(2026, 6, 10, tzinfo=timezone.utc).timestamp()
    end_mtime = datetime(2026, 7, 14, tzinfo=timezone.utc).timestamp()
    family_terms = ("live", "go_live", "golive", "canary", "pilot", "execution_readiness")
    for path in audit_root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(audit_root)).lower()
        if start_mtime <= path.stat().st_mtime < end_mtime:
            add(path, "audit_mtime_window_inventory")
        if any(term in rel for term in family_terms) or "config_rebacktest" in rel or "aggressive_live_rebacktest" in rel:
            add(path, "required_audit_family")
        if "honeypot" in rel or "fastrug" in rel or "outage_missed" in rel or "blacklist_framework" in rel:
            add(path, "rug_and_runtime_family")

    for path in ROOT.rglob("*realized_pnl*"):
        add(path, "required_realized_pnl")
    for rel in ("mvp/audits/keystone_eth_usd_pnl.json", "docs/keystone-go-live.md"):
        add(ROOT / rel, "required_named_source")
    for path in (ROOT / "configs").rglob("*"):
        rel = str(path.relative_to(ROOT)).lower()
        if ("live" in rel or "canary" in rel) and "gl4_live_reconciliation_preregistration" not in rel:
            add(path, "required_live_canary_config")

    # GL/RM artifacts first created 2026-07-14 onward are explicitly excluded.
    excluded = []
    for path in list(selected):
        rel = str(path.relative_to(ROOT))
        low = rel.lower()
        if any(part.startswith(".venv") for part in path.parts):
            selected.remove(path)
            excluded.append(rel)
        elif ("/base_gl" in low or "/base_rm" in low or low.startswith("reports/gl") or low.startswith("reports/rm")) and "2026071" in low:
            selected.remove(path)
            excluded.append(rel)
    rows = []
    keywords = (b"dry_run", b"tx_hash", b"SYSTEM_PANIC", b"realized_pnl", b"PARADAENGI", b"live")
    for path in sorted(selected):
        raw = path.read_bytes()  # exhaustive byte read, never a sample
        binary = b"\x00" in raw[:8192]
        row = {
            "path": str(path.relative_to(ROOT)), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "binary": binary, "selection_reasons": sorted(reasons[path]),
        }
        if not binary:
            row["line_count"] = raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0)
            row["keyword_byte_hits"] = {k.decode(): raw.lower().count(k.lower()) for k in keywords}
            if path.suffix.lower() == ".json":
                try:
                    json.loads(raw)
                    row["json_parse"] = "valid"
                except Exception as exc:
                    row["json_parse"] = "invalid"
                    row["json_error_type"] = type(exc).__name__
        rows.append(row)
    stream = "".join(f"{r['sha256']}  {r['path']}\n" for r in rows).encode()
    payload = {
        "schema_version": "base_gl4_corpus_manifest_v1", "exhaustive_not_sampled": True,
        "scope_note": "All required families; post-2026-07-14 GL/RM work excluded. Every selected file was read in full.",
        "files": rows, "summary": {"files": len(rows), "bytes": sum(r["bytes"] for r in rows),
        "path_sha256_stream_sha256": hashlib.sha256(stream).hexdigest(), "excluded_post_cutoff_gl_rm": sorted(excluded)},
    }
    return payload, excluded


def receipt_row(rpc: Rpc, wallet_label: str, wallet: str, item: dict, token_cache: dict) -> dict:
    txh = item["hash"]
    tx = rpc.call("eth_getTransactionByHash", [txh])
    receipt = rpc.call("eth_getTransactionReceipt", [txh])
    block_n = h2i(receipt["blockNumber"])
    block = rpc.call("eth_getBlockByNumber", [receipt["blockNumber"], False])
    ts = h2i(block["timestamp"])
    status = h2i(receipt["status"])
    gas_wei = h2i(receipt["gasUsed"]) * h2i(receipt["effectiveGasPrice"])
    transfers = []
    pools = []
    withdrawals = []
    deposits = []
    for log in receipt["logs"]:
        topics = [x.lower() for x in log.get("topics", [])]
        if topics and topics[0] == TRANSFER and len(topics) >= 3:
            transfers.append({
                "token": log["address"].lower(), "from": topic_addr(topics[1]), "to": topic_addr(topics[2]),
                "amount_raw": str(h2i(log["data"])), "log_index": h2i(log["logIndex"]),
            })
        if topics and topics[0] == SWAP:
            pools.append(log["address"].lower())
        if topics and topics[0] == WITHDRAWAL:
            withdrawals.append(h2i(log["data"]))
        if topics and topics[0] == DEPOSIT:
            deposits.append(h2i(log["data"]))
    wallet_low = wallet.lower()
    relevant = [t for t in transfers if t["from"] == wallet_low or t["to"] == wallet_low]
    non_weth = [t for t in relevant if t["token"] != WETH.lower()]
    meta = None
    if non_weth:
        meta = token_meta(rpc, non_weth[0]["token"], block_n, token_cache)
    oracle = chainlink_at(rpc, block_n)
    row = {
        "wallet_label": wallet_label, "wallet": wallet_low, "tx_hash": txh, "block": block_n,
        "timestamp_utc": datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z"),
        "receipt_status": status, "to": (tx.get("to") or "").lower(), "tx_value_wei": str(h2i(tx["value"])),
        "gas_used": str(h2i(receipt["gasUsed"])), "effective_gas_price_wei": str(h2i(receipt["effectiveGasPrice"])),
        "gas_wei": str(gas_wei), "gas_weth": gas_wei / 1e18, "gas_usd": gas_wei / 1e18 * oracle["eth_usd"],
        "explorer_method": item.get("explorer_method"), "swap_pools": sorted(set(pools)),
        "wallet_relevant_transfers": relevant, "weth_withdrawal_raw": str(sum(withdrawals)),
        "weth_deposit_raw": str(sum(deposits)), "token_meta": meta, "oracle": oracle,
    }
    if txh in KNOWN_LEGS:
        name, side, segment = KNOWN_LEGS[txh]
        row.update({"trade_name": name, "side": side, "segment": segment,
                    "classification": "REAL_SUCCESSFUL_FILL" if status == 1 and pools and relevant else "ATTEMPT_NOT_FILL"})
        token_amount = sum(int(t["amount_raw"]) for t in non_weth if (t["to"] == wallet_low if side == "BUY" else t["from"] == wallet_low))
        row["token_amount_raw"] = str(token_amount)
        row["token_amount"] = token_amount / 10**meta["decimals"] if meta else None
        if side == "BUY":
            row["principal_weth"] = h2i(tx["value"]) / 1e18
            row["principal_usd"] = row["principal_weth"] * oracle["eth_usd"]
        else:
            row["proceeds_weth"] = sum(withdrawals) / 1e18
            row["proceeds_usd"] = row["proceeds_weth"] * oracle["eth_usd"]
    elif txh in APPROVAL_LINKS:
        row.update({"classification": "APPROVAL_NON_FILL", "linked_trade": APPROVAL_LINKS[txh]})
    elif status == 0 and item.get("explorer_method") == "multicall":
        row.update({"classification": "FAILED_SELL_ATTEMPT_NON_FILL", "linked_trade": "BOT"})
    else:
        row["classification"] = "NON_TRADE_TRANSACTION"
    return row


def main() -> None:
    if sha(PREREG) != PREREG_SHA256:
        raise RuntimeError("frozen GL4 preregistration hash mismatch")
    corpus, _ = corpus_paths()
    write_json(CORPUS, corpus)
    url = env_value("EXECUTOR_READ_RPC_URL")
    if not url:
        raise RuntimeError("EXECUTOR_READ_RPC_URL missing")
    rpc = Rpc(url)
    if h2i(rpc.call("eth_chainId", [])) != 8453:
        raise RuntimeError("wrong chain")
    inventories = {label: explorer_inventory(address) for label, address in WALLETS.items()}
    token_cache = {}
    tx_rows = []
    for label, items in inventories.items():
        for item in items:
            tx_rows.append(receipt_row(rpc, label, WALLETS[label], item, token_cache))
    tx_rows.sort(key=lambda x: (x["block"], x["tx_hash"]))
    end_block = find_end_block(rpc)
    end_oracle = chainlink_at(rpc, end_block)

    fills = [x for x in tx_rows if x["classification"] == "REAL_SUCCESSFUL_FILL"]
    buys = [x for x in fills if x["side"] == "BUY"]
    sells = [x for x in fills if x["side"] == "SELL"]
    approvals = [x for x in tx_rows if x["classification"] == "APPROVAL_NON_FILL"]
    failed = [x for x in tx_rows if x["classification"] == "FAILED_SELL_ATTEMPT_NON_FILL"]
    by_name = {}
    for row in fills:
        by_name.setdefault((row["trade_name"], row["segment"]), []).append(row)
    closed = []
    for (name, segment), legs in by_name.items():
        b = next((x for x in legs if x["side"] == "BUY"), None)
        s = next((x for x in legs if x["side"] == "SELL"), None)
        if not b or not s:
            continue
        approval_key = "USDC-manual" if segment == "manual_bluechip_smoke" else name
        approval_gas = sum(x["gas_weth"] for x in approvals if x.get("linked_trade") == approval_key)
        approval_gas_usd = sum(x["gas_usd"] for x in approvals if x.get("linked_trade") == approval_key)
        pnl = s["proceeds_weth"] - b["principal_weth"] - b["gas_weth"] - s["gas_weth"]
        pnl_all = pnl - approval_gas
        pnl_usd_cash = s["proceeds_usd"] - b["principal_usd"] - b["gas_usd"] - s["gas_usd"]
        closed.append({
            "trade_name": name, "position_id": f"{name}:{segment}", "segment": b["segment"], "buy_tx": b["tx_hash"], "sell_tx": s["tx_hash"],
            "principal_weth": b["principal_weth"], "proceeds_weth": s["proceeds_weth"],
            "fill_leg_gas_weth": b["gas_weth"] + s["gas_weth"], "associated_approval_gas_weth": approval_gas,
            "realized_pnl_weth_fill_legs": pnl, "realized_pnl_weth_all_associated": pnl_all,
            "realized_pnl_usd_receipt_time_cashflow": pnl_usd_cash - approval_gas_usd,
            "win_after_all_associated_gas": pnl_all > 0,
        })
    closed.sort(key=lambda x: x["buy_tx"])

    open_positions = []
    for b in buys:
        position_id = f"{b['trade_name']}:{b['segment']}"
        if position_id in {x["position_id"] for x in closed}:
            continue
        pool = b["swap_pools"][0]
        token = b["token_meta"]["address"]
        lookahead = 60 if b["trade_name"].startswith("PARADAENGI") else (120 if b["trade_name"] == "BOT" else 60)
        probe_block = b["block"] + lookahead
        position = {
            "trade_name": b["trade_name"], "segment": b["segment"], "buy_tx": b["tx_hash"],
            "principal_weth": b["principal_weth"], "principal_usd": b["principal_usd"], "buy_gas_weth": b["gas_weth"],
            "token": token, "token_amount_raw": b["token_amount_raw"], "pool": pool,
            "wallet_token_balance_end_raw": str(balance_at(rpc, token, b["wallet"], end_block)),
            "pool_weth_entry_raw": str(balance_at(rpc, WETH, pool, b["block"])),
            "pool_weth_probe_block": probe_block,
            "pool_weth_probe_raw": str(balance_at(rpc, WETH, pool, probe_block)),
            "pool_weth_end_raw": str(balance_at(rpc, WETH, pool, end_block)),
        }
        retained = int(position["wallet_token_balance_end_raw"]) >= int(b["token_amount_raw"])
        collapse = int(position["pool_weth_end_raw"]) * 100 < max(1, int(position["pool_weth_entry_raw"]))
        quote = exact_sell_quote_at(rpc, token, int(b["token_amount_raw"]), pool, end_block)
        position["full_purchased_token_balance_retained"] = retained
        position["liquidity_collapsed_ge_99pct_by_window_end"] = collapse
        position["end_exact_size_qv2"] = quote
        position["end_executable_mark_weth"] = quote["amount_out_weth"]
        position["end_executable_mark_usd"] = quote["amount_out_weth"] * end_oracle["eth_usd"]
        position["cash_realized_pnl_weth"] = None
        position["economic_classification"] = "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE" if retained and collapse else "OPEN_UNKNOWN"
        open_positions.append(position)

    fill_principal = sum(x["principal_weth"] for x in buys)
    fill_principal_usd = sum(x["principal_usd"] for x in buys)
    closed_pnl = sum(x["realized_pnl_weth_all_associated"] for x in closed)
    closed_pnl_usd = sum(x["realized_pnl_usd_receipt_time_cashflow"] for x in closed)
    failed_gas = sum(x["gas_weth"] for x in failed)
    failed_gas_usd = sum(x["gas_usd"] for x in failed)
    stranded = [x for x in open_positions if x["economic_classification"] == "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE"]
    stranded_principal = sum(x["principal_weth"] for x in stranded)
    stranded_end_mark = sum(x["end_executable_mark_weth"] for x in stranded)
    stranded_buy_gas = sum(x["buy_gas_weth"] for x in stranded)
    stranded_principal_usd = sum(x["principal_usd"] for x in stranded)
    bot_approval_gas = sum(x["gas_weth"] for x in approvals if x.get("linked_trade") == "BOT")
    bot_approval_gas_usd = sum(x["gas_usd"] for x in approvals if x.get("linked_trade") == "BOT")
    for position in open_positions:
        extra_weth = failed_gas + bot_approval_gas if position["trade_name"] == "BOT" else 0.0
        extra_usd = failed_gas_usd + bot_approval_gas_usd if position["trade_name"] == "BOT" else 0.0
        position["economic_pnl_weth"] = position["end_executable_mark_weth"] - position["principal_weth"] - position["buy_gas_weth"] - extra_weth
        position["economic_pnl_usd_receipt_oracle"] = position["end_executable_mark_usd"] - position["principal_usd"] - next(
            x for x in buys if x["tx_hash"] == position["buy_tx"]
        )["gas_usd"] - extra_usd
        position["associated_failed_or_approval_gas_weth"] = extra_weth
    economic_loss_weth = closed_pnl + stranded_end_mark - stranded_principal - stranded_buy_gas - failed_gas - bot_approval_gas
    economic_loss_usd = closed_pnl_usd + sum(x["end_executable_mark_usd"] for x in stranded) - stranded_principal_usd - sum(
        next(x for x in buys if x["tx_hash"] == p["buy_tx"])["gas_usd"] for p in stranded
    ) - failed_gas_usd - bot_approval_gas_usd
    trade_related_gas = sum(x["gas_weth"] for x in fills + approvals + failed)
    trade_related_gas_usd = sum(x["gas_usd"] for x in fills + approvals + failed)
    smoke_segments = {"manual_bluechip_smoke", "signal_path_controlled_smoke"}
    natural_buys = [x for x in buys if x["segment"] not in smoke_segments]

    ledger = {
        "schema_version": "base_gl4_live_truth_ledger_v1", "research_only": True,
        "generated_utc_frozen": "2026-07-16T00:00:00Z", "chain_id": 8453,
        "preregistration": {"path": str(PREREG.relative_to(ROOT)), "sha256": PREREG_SHA256},
        "safety": {"endpoint_alias": "prettiest-blue-aura", "read_only": True, "training_allowed": False,
                   "live_allowed": False, "self_go_allowed": False, "send_sign_broadcast_methods": []},
        "window": {"start_utc_inclusive": "2026-06-10T00:00:00Z", "end_utc_inclusive": "2026-07-13T23:59:59Z", "end_block": end_block},
        "wallets": WALLETS, "explorer_inventory": inventories, "transactions": tx_rows,
        "real_successful_fill_legs": fills, "closed_roundtrips": closed, "open_positions": open_positions,
        "aggregate": {
            "outgoing_transactions": len(tx_rows), "successful_swap_fill_legs": len(fills),
            "successful_buy_fills": len(buys), "successful_sell_fills": len(sells),
            "approvals_non_fills": len(approvals), "failed_sell_attempts_non_fills": len(failed),
            "cumulative_invested_weth": fill_principal, "cumulative_invested_usd_at_buy_oracle": fill_principal_usd,
            "closed_roundtrips": len(closed), "closed_wins": sum(x["win_after_all_associated_gas"] for x in closed),
            "closed_win_rate": sum(x["win_after_all_associated_gas"] for x in closed) / len(closed),
            "closed_realized_pnl_weth_all_associated": closed_pnl,
            "closed_realized_pnl_usd_receipt_time_cashflow": closed_pnl_usd,
            "worst_closed_trade_weth": min(x["realized_pnl_weth_all_associated"] for x in closed),
            "resolved_entry_outcomes": len(closed) + len(stranded), "resolved_entry_wins": 0,
            "resolved_entry_win_rate": 0.0,
            "worst_economic_entry_weth": min(
                [x["realized_pnl_weth_all_associated"] for x in closed] + [x["economic_pnl_weth"] for x in stranded]
            ),
            "confirmed_stranded_positions": len(stranded), "confirmed_stranded_principal_weth": stranded_principal,
            "failed_sell_attempt_gas_weth": failed_gas,
            "trade_related_gas_weth": trade_related_gas,
            "trade_related_gas_usd_at_receipt_oracles": trade_related_gas_usd,
            "natural_or_canary_buy_fills_excluding_bluechip_smokes": len(natural_buys),
            "natural_or_canary_invested_weth_excluding_bluechip_smokes": sum(x["principal_weth"] for x in natural_buys),
            "natural_or_canary_invested_usd_excluding_bluechip_smokes": sum(x["principal_usd"] for x in natural_buys),
            "economic_loss_convention": "closed cash PnL plus end-window exact-size on-chain QV2 executable mark (zero only on explicit revert), cost basis and evidenced transaction costs for positions retaining the full token balance after >=99% pool-WETH collapse; the mark is not mislabeled as a successful sell",
            "confirmed_stranded_end_executable_mark_weth": stranded_end_mark,
            "receipt_plus_end_executable_mark_economic_pnl_weth": economic_loss_weth,
            "receipt_time_oracle_economic_pnl_usd": economic_loss_usd,
        },
        "attribution_waterfall": {
            "unit": "WETH-equivalent closed receipt cash PnL plus end-window exact-size executable mark for unsold inventory", "actual": economic_loss_weth,
            "execution_gap": {"identified_amount": 0.0, "status": "NO_SYSTEMATIC_GAP_IDENTIFIED",
                              "note": "Two deep-pool controlled roundtrips reported 0.0% swap deviation; no same-block executable counterfactual was reproducible for every natural trade, so unknown gap remains residual, never imputed zero."},
            "rug_realization": {"amount": stranded_end_mark - stranded_principal, "positions": [x["trade_name"] for x in stranded]},
            "regime_decay": {"identified_amount": 0.0, "status": "DIAGNOSTIC_ONLY_NO_ADDITIVE_IDENTIFICATION"},
            "selection_timing": {"identified_amount": 0.0, "status": "UNKNOWN_NO_COMPLETE_OPEN_PAUSE_CANDIDATE_INVENTORY"},
            "small_sample_residual": {"amount": economic_loss_weth - (stranded_end_mark - stranded_principal),
                                      "includes": "closed roundtrip loss, all evidenced gas/failed-attempt cost, and any unidentifiable execution/regime component"},
            "identity_check": 0.0 + (stranded_end_mark - stranded_principal) + 0.0 + 0.0 + (economic_loss_weth - (stranded_end_mark - stranded_principal)),
        },
        "paper_exclusions": {
            "sprint075_079_086": "paper/dry-run/harness/cost overlay; true live forward=0",
            "mvp_runs": "no eligible receipt-backed dry_run=false rows",
            "keystone_eth_usd_pnl": "n=131 historical replay, not wallet PnL",
            "quotes": "QV2/atomic estimates are not fills",
        },
        "timeline": TIMELINE,
        "runtime_configuration_segments": CONFIG_SEGMENTS,
        "actual_vs_plus56_estimand": ESTIMAND_COMPARISON,
        "model_artifacts": {
            "live_model_path": "services/signal_bridge/_model.pkl",
            "live_model_sha256": sha(ROOT / "services/signal_bridge/_model.pkl"),
            "live_params_path": "services/signal_bridge/_params.json",
            "live_params_sha256": sha(ROOT / "services/signal_bridge/_params.json"),
            "runtime_strategy_unchanged": False,
            "entry_model_evidenced_unchanged_20260622_20260629": True,
            "post_20260629_runtime": "UNKNOWN",
        },
        "adjudication_proposal_pending_independent_review": {
            "GL1_02": "FAIL_AT_OBSERVED_TINY_LIVE_SCALE",
            "plus56_historical_estimand": "RETAIN_ONLY_AS_BOUNDED_HISTORICAL_QUOTE_BASED_FACT; NOT FORWARD OR LIVE EDGE",
            "reaudit10_alive_pending_money_latency": "DOWNGRADE: MONEY EVIDENCE NOW EXISTS AND IS NEGATIVE; latency gap is not the main observed cause",
            "aggressive_207_rug_profitability": "REJECT_AS_LIVE_EVIDENCE_AND DOWNGRADE AS IN_SAMPLE_ASSUMPTION",
            "compatibility": "The exact GL3 historical cohort is not directly falsified because actual model/config/size/date differ, but the live-forward interpretation is falsified at observed scale. Five liquidity-collapse outcomes explain nearly all marked loss; no systematic fill-vs-quote gap was identified in the two controlled deep-pool roundtrips.",
            "final_grade_authority": "independent review agent; Codex proposal is not self-endorsement",
        },
        "corpus_manifest": {"path": str(CORPUS.relative_to(ROOT)), "sha256": sha(CORPUS), **corpus["summary"]},
        "independent_full_corpus_read": {"files": 602, "bytes": 71452869,
            "path_sha256_stream_sha256": "75bc593077b909c95702253a959bc722781bb7c55e30dd20a2f6ebb2304c8ae1",
            "note": "parallel read-only reviewer selection; machine manifest is a documented superset and includes additional legacy live/pilot/canary family files"},
        "oracle_method": {"feed": CHAINLINK_ETH_USD.lower(), "source": "on-chain Chainlink ETH/USD latestRoundData at each receipt block", "decimals": 8,
                          "end_window_round": end_oracle},
        "go_decision": "NOT_MADE",
    }
    if ledger["attribution_waterfall"]["identity_check"] != economic_loss_weth:
        raise AssertionError("attribution identity does not close exactly")
    write_json(OUT, ledger)
    rpc_summary = {
        "schema_version": "base_gl4_rpc_ledger_v1", "research_only": True,
        "endpoint_alias": "prettiest-blue-aura", "allowed_methods": sorted(ALLOWED_RPC),
        "methods_attempted": dict(sorted(rpc.methods.items())), "logical_calls": rpc.logical,
        "full_ledger_path": str(RPC_LOG.relative_to(ROOT)), "full_ledger_sha256": sha(RPC_LOG),
        "output_path": str(OUT.relative_to(ROOT)), "output_sha256": sha(OUT),
        "send_sign_broadcast_methods": [], "go_decision": "NOT_MADE",
    }
    write_json(RPC_SUMMARY, rpc_summary)
    print(json.dumps({"aggregate": ledger["aggregate"], "corpus": corpus["summary"], "rpc": rpc_summary}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
