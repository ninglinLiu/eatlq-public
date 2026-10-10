from __future__ import annotations

import csv, json, math, os, threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
IN_CSV = REPO / "logs/aero_vamm_60m_cost_sensitivity_5000.csv"
OUT_AUDIT_JSON = REPO / "mvp/diagnostics/aero_vamm_s5_feature_source_audit.json"
OUT_AUDIT_MD = REPO / "mvp/runs/aero-vamm-s5-feature-source-audit.md"
OUT_PILOT_CSV = REPO / "logs/aero_vamm_s5_entry_features_pilot_1000.csv"
OUT_FETCH_JSON = REPO / "mvp/diagnostics/aero_vamm_s5_entry_feature_fetch_report.json"
OUT_FETCH_MD = REPO / "mvp/runs/aero-vamm-s5-entry-feature-fetch-pilot.md"
OUT_TRAIN_JSON = REPO / "mvp/diagnostics/aero_vamm_s5_trainability_recheck.json"
OUT_TRAIN_MD = REPO / "mvp/runs/aero-vamm-s5-trainability-recheck.md"
SEL = {"token0": "0x0dfe1681", "token1": "0xd21220a7", "stable": "0x22be3de1", "reserves": "0x0902f1ac"}
WETH = "0x4200000000000000000000000000000000000006"


def naddr(x: str) -> str:
    x = (x or "").strip().lower()
    return x if x.startswith("0x") else ("0x" + x if x else "")


def to_i(x, d=0):
    try:
        return int(x)
    except Exception:
        return d


def to_f(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


class Abort429(RuntimeError):
    pass


class RPC:
    def __init__(self, url: str, max_calls: int, abort_on_429=True):
        self.url, self.max_calls, self.abort_on_429 = url, max_calls, abort_on_429
        self.lock = threading.Lock()
        self.i = 0
        self.counts = Counter()
        self.aborted = False
        self.reason = ""

    def _id(self):
        with self.lock:
            self.i += 1
            return self.i

    def call(self, method: str, params: list):
        with self.lock:
            if self.aborted:
                raise Abort429(self.reason or "aborted")
            if sum(self.counts.values()) >= self.max_calls:
                self.aborted, self.reason = True, "RPC_CALL_BUDGET_EXCEEDED"
                raise Abort429(self.reason)
            self.counts[method] += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id(), "method": method, "params": params}).encode()
        req = Request(self.url, data=body, method="POST")
        req.add_header("content-type", "application/json")
        try:
            with urlopen(req, timeout=30) as r:
                out = json.loads(r.read())
        except HTTPError as e:
            if e.code == 429 and self.abort_on_429:
                self.aborted, self.reason = True, "HTTP_429_ABORT"
                raise Abort429(self.reason)
            raise
        if out.get("error"):
            msg = str(out["error"].get("message", "rpc_error"))
            if "429" in msg and self.abort_on_429:
                self.aborted, self.reason = True, "HTTP_429_ABORT"
                raise Abort429(self.reason)
            raise RuntimeError(msg)
        return out["result"]


def eth_call(rpc: RPC, to: str, data: str, block: int | str = "latest") -> str:
    b = hex(block) if isinstance(block, int) else block
    r = rpc.call("eth_call", [{"to": naddr(to), "data": data}, b])
    if not isinstance(r, str) or r in ("", "0x"):
        raise ValueError("empty_call")
    return r


def d_addr(x: str) -> str:
    h = (x or "").replace("0x", "")
    return "0x" + h[-40:].lower()


def d_bool(x: str) -> bool:
    h = (x or "").replace("0x", "")
    return int(h[-64:], 16) != 0


def d_u(x: str, idx=0) -> int:
    h = (x or "").replace("0x", "")
    return int(h[idx * 64 : (idx + 1) * 64], 16)


def rpc_url() -> str:
    for k in ("LABEL_RPC_URL", "EVM_HTTP_URL"):
        if os.environ.get(k):
            return os.environ[k]
    p = REPO / "infra/compose/.env"
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1)
                if k.strip() in ("LABEL_RPC_URL", "EVM_HTTP_URL"):
                    return v.strip().strip('"').strip("'")
    return ""


def sample_1000(rows):
    b = defaultdict(list)
    for r in rows:
        if r.get("failure_reason") != "ok" or r.get("quote_status_60m") != "ok":
            continue
        b[(naddr(r.get("pool_address")), r.get("time_quartile", ""))].append(r)
    for k in b:
        b[k] = sorted(b[k], key=lambda x: to_i(x.get("timestamp")))
    pools, qs = sorted({k[0] for k in b}), ["Q1", "Q2", "Q3", "Q4"]
    out, seen = [], set()
    for q in qs:
        for p in pools[:2]:
            arr, k = b[(p, q)], 125
            step = (len(arr) - 1) / max(1, k - 1) if len(arr) > k else 0
            idxs = range(len(arr)) if len(arr) <= k else sorted({int(round(i * step)) for i in range(k)})
            for i in idxs:
                r = arr[i]
                key = (r["tx_hash"], r["log_index"], naddr(r["pool_address"]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(r)
    return sorted(out, key=lambda r: to_i(r.get("timestamp")))[:1000]


def main():
    rows = list(csv.DictReader(IN_CSV.open("r", encoding="utf-8")))
    cols = list(rows[0].keys())
    audit = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "input_csv": str(IN_CSV.relative_to(REPO)),
        "row_count": len(rows),
        "columns": cols,
        "allowed_model_feature_candidate": ["entry_notional_in", "entry_quote"],
        "diagnostics_only": ["pool_address", "token0", "token1", "tx_hash", "log_index", "pool_type", "stable_flag", "time_quartile", "quote_status_60m", "failure_reason"],
        "target_or_label": [c for c in cols if c.startswith("gross_return_") or c.startswith("net_return_cost_") or c.startswith("positive_cost_") or c.startswith("exit_quote_")],
        "forbidden_notes": ["future/exit fields forbidden as features", "label/return fields forbidden as features", "pool/token/tx identity forbidden", "timestamp clock features forbidden", "time_quartile forbidden as model input"],
    }
    OUT_AUDIT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_AUDIT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_AUDIT_JSON.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    OUT_AUDIT_MD.write_text("# Aero vAMM S5 Feature Source Audit\n\n- conclusion: current 5000 CSV features are too thin; run bounded entry-feature RPC pilot.\n", encoding="utf-8")

    url = rpc_url()
    if not url:
        raise RuntimeError("Missing LABEL_RPC_URL/EVM_HTTP_URL")
    rpc = RPC(url, int(os.environ.get("AERO_VAMM_RPC_MAX_CALLS", "6000")), os.environ.get("AERO_VAMM_ABORT_ON_429", "1") != "0")
    workers = max(1, min(2, int(os.environ.get("LABEL_WORKERS", "2"))))
    sample = sample_1000(rows)
    block_time = max(1, int(os.environ.get("AERO_VAMM_BLOCK_TIME_SEC", "2")))
    b7, b30 = int(7 * 24 * 3600 / block_time), int(30 * 24 * 3600 / block_time)
    pool_meta, block_cache, reserve_cache, age_cache = {}, {}, {}, {}
    failures = Counter()
    out_rows = []

    def enrich(r):
        pool, eblk = naddr(r["pool_address"]), to_i(r["entry_block"])
        try:
            if pool not in pool_meta:
                t0, t1 = d_addr(eth_call(rpc, pool, SEL["token0"])), d_addr(eth_call(rpc, pool, SEL["token1"]))
                st = d_bool(eth_call(rpc, pool, SEL["stable"]))
                pool_meta[pool] = (t0, t1, st)
            t0, t1, stable = pool_meta[pool]
            if stable:
                raise ValueError("stable_true")
            if (pool, eblk) not in reserve_cache:
                rv = eth_call(rpc, pool, SEL["reserves"], eblk).replace("0x", "")
                reserve_cache[(pool, eblk)] = (int(rv[0:64], 16), int(rv[64:128], 16))
            r0, r1 = reserve_cache[(pool, eblk)]
            if eblk not in block_cache:
                bh = rpc.call("eth_getBlockByNumber", [hex(eblk), True])
                block_cache[eblk] = bh
            bh = block_cache[eblk]
            if (pool, eblk) not in age_cache:
                p7, p30 = max(0, eblk - b7), max(0, eblk - b30)
                c7 = rpc.call("eth_getCode", [pool, hex(p7)])
                c30 = rpc.call("eth_getCode", [pool, hex(p30)])
                age_cache[(pool, eblk)] = (0 if c7 == "0x" else 1, 0 if c30 == "0x" else 1, eblk - p30 if c30 == "0x" else b30)
            ex7, ex30, ageb = age_cache[(pool, eblk)]
            token_in = naddr(r.get("quote_token_in") or r.get("token0"))
            q_in = to_f(r.get("entry_notional_in"))
            q_out = max(1e-12, to_f(r.get("entry_quote")))
            if token_in == t0:
                rin, rout = float(r0), float(r1)
            elif token_in == t1:
                rin, rout = float(r1), float(r0)
            else:
                rin, rout = float(r0), float(r1)
            spot = (q_in * rout / rin) if rin > 0 else 0.0
            pi = max(0.0, (spot - q_out) / spot) if spot > 0 else None
            rw, rt = (float(r0), float(r1)) if t0 == WETH else ((float(r1), float(r0)) if t1 == WETH else (None, None))
            row = dict(r)
            row.update({
                "reserve0_at_entry": r0, "reserve1_at_entry": r1, "reserve_weth_at_entry": rw, "reserve_token_at_entry": rt,
                "log_reserve_weth": (math.log(max(rw, 1.0)) if rw else None), "reserve_ratio": (rw / rt if rw and rt else None),
                "liquidity_to_trade_ratio": (rw / q_in if rw and q_in > 0 else None), "swap_size_vs_reserve": (q_in / rw if rw and rw > 0 else None),
                "price_impact_estimate_at_notional": pi, "quote_to_notional_ratio": (q_out / q_in if q_in > 0 else None),
                "log_entry_quote": math.log(max(q_out, 1e-12)), "log_entry_notional": math.log(max(q_in, 1e-12)),
                "notional_vs_entry_quote": (q_in / q_out if q_out > 0 else None), "block_gas_used": int(bh["gasUsed"], 16),
                "block_gas_limit": int(bh["gasLimit"], 16), "block_gas_fill_ratio": (int(bh["gasUsed"], 16) / max(1, int(bh["gasLimit"], 16))),
                "block_tx_count": len(bh.get("transactions", [])), "is_new_pool_7d": 1 - ex7, "is_new_pool_30d": 1 - ex30, "pool_age_blocks_at_entry": ageb,
            })
            return row, "ok"
        except Abort429 as e:
            return None, f"fail_{str(e).lower()}"
        except Exception as e:
            return None, "fail_feature_fetch_error"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        fut = {ex.submit(enrich, r): r for r in sample}
        for f in as_completed(fut):
            row, st = f.result()
            if st == "ok":
                out_rows.append(row)
            else:
                failures[st] += 1
                if "429" in st:
                    break

    out_rows = sorted(out_rows, key=lambda r: to_i(r.get("timestamp")))
    if out_rows:
        fn = list(out_rows[0].keys())
        OUT_PILOT_CSV.parent.mkdir(parents=True, exist_ok=True)
        with OUT_PILOT_CSV.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fn); w.writeheader(); w.writerows(out_rows)

    # Trainability recheck (lightweight)
    data = [r for r in out_rows if r.get("failure_reason") == "ok" and r.get("quote_status_60m") == "ok"]
    data = sorted(data, key=lambda r: to_i(r.get("timestamp")))
    cut = int(round(len(data) * 0.75))
    tr, ho = data[:cut], data[cut:]
    feats = ["entry_notional_in", "entry_quote", "quote_to_notional_ratio", "log_entry_quote", "log_entry_notional", "notional_vs_entry_quote", "log_reserve_weth", "reserve_ratio", "liquidity_to_trade_ratio", "swap_size_vs_reserve", "price_impact_estimate_at_notional", "block_gas_fill_ratio", "block_tx_count", "is_new_pool_7d", "is_new_pool_30d", "pool_age_blocks_at_entry"]
    use = [c for c in feats if any(to_f(r.get(c), float("nan")) == to_f(r.get(c), float("nan")) for r in tr)]
    def X(rows): return [[to_f(r.get(c), 0.0) for c in use] for r in rows]
    Xtr, Xho = X(tr), X(ho)
    ytr = [1 if r.get("positive_cost_004_60m") == "1" else 0 for r in tr]; yho = [1 if r.get("positive_cost_004_60m") == "1" else 0 for r in ho]
    rtr = [to_f(r.get("net_return_cost_004_60m")) for r in tr]; rho = [to_f(r.get("net_return_cost_004_60m")) for r in ho]
    mu = [sum(v[i] for v in Xtr) / max(1, len(Xtr)) for i in range(len(use))]
    sd = []
    for i in range(len(use)):
        v = sum((x[i] - mu[i]) ** 2 for x in Xtr) / max(1, len(Xtr))
        sd.append(math.sqrt(v) if v > 1e-12 else 1.0)
    Ztr = [[(x[i] - mu[i]) / sd[i] for i in range(len(use))] for x in Xtr]
    Zho = [[(x[i] - mu[i]) / sd[i] for i in range(len(use))] for x in Xho]
    w, b = [0.0] * len(use), 0.0
    for _ in range(180):
        gw, gb = [0.0] * len(use), 0.0
        for x, y in zip(Ztr, ytr):
            z = sum(wi * xi for wi, xi in zip(w, x)) + b; p = 1 / (1 + math.exp(-max(-30, min(30, z)))); e = p - y
            for i in range(len(use)): gw[i] += e * x[i]
            gb += e
        n = max(1, len(Ztr))
        for i in range(len(use)): w[i] -= 0.08 * gw[i] / n
        b -= 0.08 * gb / n
    ph = [1 / (1 + math.exp(-max(-30, min(30, sum(wi * xi for wi, xi in zip(w, x)) + b)))) for x in Zho]
    base = sum(yho) / max(1, len(yho))
    k = max(1, int(len(ph) * 0.1))
    idx = sorted(range(len(ph)), key=lambda i: ph[i], reverse=True)[:k]
    top_pr = sum(yho[i] for i in idx) / max(1, k)
    top_med = sorted([rho[i] for i in idx])[k // 2] if idx else 0.0
    med_all = sorted(rho)[len(rho) // 2] if rho else 0.0
    by_pool = defaultdict(list); by_q = defaultdict(list)
    for i, rr in enumerate(ho):
        by_pool[naddr(rr.get("pool_address"))].append((ph[i], yho[i]))
        by_q[rr.get("time_quartile", "")].append((ph[i], yho[i]))
    diag_pool = {}
    for p, arr in by_pool.items():
        kk = max(1, int(len(arr) * 0.1)); aa = sorted(arr, key=lambda t: t[0], reverse=True)[:kk]
        diag_pool[p] = sum(y for _, y in aa) / max(1, len(aa))
    diag_q = {}
    for q, arr in by_q.items():
        kk = max(1, int(len(arr) * 0.1)); aa = sorted(arr, key=lambda t: t[0], reverse=True)[:kk]
        diag_q[q] = sum(y for _, y in aa) / max(1, len(aa))

    group_keys = {
        "group_a": ["reserve0_at_entry", "reserve1_at_entry", "reserve_weth_at_entry", "reserve_token_at_entry", "price_impact_estimate_at_notional"],
        "group_b": ["entry_quote", "entry_notional_in", "quote_to_notional_ratio", "log_entry_quote", "log_entry_notional"],
        "group_c": ["block_gas_used", "block_gas_limit", "block_gas_fill_ratio", "block_tx_count"],
        "group_d": ["is_new_pool_7d", "is_new_pool_30d", "pool_age_blocks_at_entry"],
    }
    null_rates = {}
    for g, ks in group_keys.items():
        miss = 0
        for r in out_rows:
            if any(r.get(k) in (None, "", "None") for k in ks):
                miss += 1
        null_rates[g] = round(miss / max(1, len(out_rows)), 6)

    fetch = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "attempted_rows": len(sample), "success_rows": len(out_rows), "success_rate": round(len(out_rows) / max(1, len(sample)), 6),
        "failure_reason_distribution": dict(failures), "rpc_call_count_by_method": dict(rpc.counts), "total_rpc_calls": sum(rpc.counts.values()),
        "calls_per_success": round(sum(rpc.counts.values()) / max(1, len(out_rows)), 6), "feature_group_null_rates": null_rates,
        "pool_distribution": dict(Counter(naddr(r.get("pool_address")) for r in out_rows)), "quartile_distribution": dict(Counter(r.get("time_quartile") for r in out_rows)),
    }
    train = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "rows_for_modeling": len(data), "holdout_rows": len(ho), "base_positive_rate_holdout_cost004": round(base, 6),
        "top_decile_positive_rate_cost004": round(top_pr, 6), "top_decile_lift_vs_base": round(top_pr - base, 6),
        "top_decile_median_net_return_cost004": round(top_med, 6), "overall_median_net_return_cost004_holdout": round(med_all, 6),
        "pool_top_decile_positive_rate_cost004": {k: round(v, 6) for k, v in diag_pool.items()},
        "quartile_top_decile_positive_rate_cost004": {k: round(v, 6) for k, v in diag_q.items()},
        "allowed_features_used": use,
    }
    ok = (
        fetch["success_rate"] >= 0.95
        and fetch["total_rpc_calls"] <= 6000
        and max(null_rates.values()) <= 0.03
        and (train["top_decile_positive_rate_cost004"] >= train["base_positive_rate_holdout_cost004"] + 0.05)
        and (train["top_decile_median_net_return_cost004"] > train["overall_median_net_return_cost004_holdout"])
    )
    conclusion = "AERO_S5_FEATURES_IMPROVE_RANKING_GO_TO_5000_FEATURE_FETCH" if ok else "AERO_S5_NEEDS_MORE_FEATURES_OR_REGIME_MODEL"
    if any("429" in k for k in failures): conclusion = "HOLD_DUE_TO_RPC_LIMIT"
    if fetch["success_rate"] < 0.95: conclusion = "HOLD_DUE_TO_FEATURE_FETCH_FAILURE"
    fetch["conclusion_enum"], train["conclusion_enum"] = conclusion, conclusion

    OUT_FETCH_JSON.write_text(json.dumps(fetch, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    OUT_TRAIN_JSON.write_text(json.dumps(train, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    OUT_FETCH_MD.write_text("# Aero vAMM S5 Entry Feature Fetch Pilot\n\n- see JSON diagnostics for full metrics.\n", encoding="utf-8")
    OUT_TRAIN_MD.write_text("# Aero vAMM S5 Trainability Recheck\n\n- see JSON diagnostics for full metrics.\n", encoding="utf-8")
    print(json.dumps({"audit_json": str(OUT_AUDIT_JSON.relative_to(REPO)), "feature_csv": str(OUT_PILOT_CSV.relative_to(REPO)), "fetch_json": str(OUT_FETCH_JSON.relative_to(REPO)), "train_json": str(OUT_TRAIN_JSON.relative_to(REPO)), "conclusion": conclusion}, indent=2))


if __name__ == "__main__":
    main()
