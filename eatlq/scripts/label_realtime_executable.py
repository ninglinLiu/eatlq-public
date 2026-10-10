"""Re-label the author's pool_realtime_training_v1 dataset with real
executable-quote P&L instead of the `future_buy_pressure > 0.5` heuristic.

Outputs a CSV compatible with train_model.py --features realtime:
    pool_address, chain_id, window_start,
    price_velocity_5m, buy_pressure_ratio, token_volatility_5m, activity_heat,
    unique_buyer_entropy_5m,
    label,          # executable: 1 iff 0.0005 ETH roundtrip > 0
    net_return,     # signed return (cost-adjusted, clipped ±10)
    mv_label,       # author's original label, kept for comparison
    label_type      # "executable" | "quote_failed"

Reuses the RPC + quote core from label_dashcam_executable.py.

Usage:
    LABEL_RPC_URL=<archive>  python scripts/label_realtime_executable.py

Env knobs:
    LABELED_OUTPUT (default models/realtime_dataset_v2.csv)
    LABEL_WORKERS (default 10)
    LABEL_EXIT_HORIZON_SEC (default 1800)
    LABEL_NOTIONAL_WEI (default 5e14 = 0.0005 ETH)
    LABEL_MIN_WETH_WEI (default 1e17 = 0.1 ETH reserve floor)
    LABEL_MAX_ABS_NET (default 10.0)
    LABEL_LIMIT (0 = all; >0 for smoke tests)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# reuse the quote core we wrote for dashcam executable labeling
from label_dashcam_executable import (  # noqa: E402
    RPC,
    WETH_BASE,
    build_ts_grid,
    label_one,
    load_pool_meta,
    ts_to_block,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("label_realtime_exec")


def fetch_mv_samples(ch_url: str, ch_db: str, ch_user: str, ch_pw: str,
                     chain_id: int, view: str = "eatlq.pool_realtime_training_v1") -> list[dict]:
    """Pull the full training view into memory.

    72k × ~200 bytes = ~14 MB, trivial.
    """
    sql = f"""
        SELECT
            lower(pool_address) AS pool,
            chain_id,
            toUnixTimestamp(window_start) AS window_ts_sec,
            price_velocity_5m,
            buy_pressure_ratio,
            token_volatility_5m,
            activity_heat,
            label AS mv_label
        FROM {view}
        WHERE chain_id = {chain_id}
        ORDER BY window_start ASC
        SETTINGS
            max_memory_usage = 5500000000,
            max_bytes_before_external_group_by = 1500000000,
            max_bytes_before_external_sort = 1500000000
    """
    params = urlencode({
        "database": ch_db, "user": ch_user, "password": ch_pw,
        "default_format": "JSONEachRow",
    })
    req = Request(f"{ch_url}/?{params}", data=sql.encode(), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    log.info("querying pool_realtime_training_v1 ...")
    with urlopen(req, timeout=180) as resp:
        body = resp.read().decode("utf-8").strip()
    rows: list[dict] = []
    for line in body.split("\n") if body else []:
        if not line.strip():
            continue
        rows.append(json.loads(line))
    log.info("MV rows loaded: %d", len(rows))
    return rows


def main() -> None:
    output_path = Path(os.environ.get("LABELED_OUTPUT", "models/realtime_dataset_v2.csv"))
    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pw = os.environ.get("CLICKHOUSE_PASSWORD", "")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    rpc_url = os.environ.get("LABEL_RPC_URL", "") or os.environ.get("EVM_HTTP_URL", "")
    workers = int(os.environ.get("LABEL_WORKERS", "10"))
    exit_horizon_sec = int(os.environ.get("LABEL_EXIT_HORIZON_SEC", "1800"))
    notional_wei = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))
    limit = int(os.environ.get("LABEL_LIMIT", "0"))

    if not rpc_url:
        log.error("LABEL_RPC_URL required")
        sys.exit(1)

    rpc = RPC(rpc_url)
    r = rpc.call("eth_chainId", [])
    chid = int(r["result"], 16)
    if chid != chain_id:
        log.error("RPC chain_id=%d != expected %d", chid, chain_id)
        sys.exit(1)
    log.info("RPC ok, chain_id=%d, workers=%d", chid, workers)

    view = os.environ.get("LABEL_SOURCE_VIEW", "eatlq.pool_realtime_training_v1")
    log.info("source view: %s", view)
    samples = fetch_mv_samples(ch_url, ch_db, ch_user, ch_pw, chain_id, view=view)
    if not samples:
        log.error("no samples")
        sys.exit(1)

    # drop samples that are too young for exit horizon
    now_sec = int(time.time())
    min_age_sec = exit_horizon_sec + 60
    samples = [s for s in samples if now_sec - int(s["window_ts_sec"]) >= min_age_sec]
    log.info("after age filter (>= %ds old): %d", exit_horizon_sec, len(samples))

    if limit and len(samples) > limit:
        # take the most recent N (most liquid / most representative); oldest
        # samples often predate pool maturity and always quote_fail.
        samples = samples[-limit:]
        log.info("LIMIT applied (tail) → %d", len(samples))

    # load pool metadata
    pools = {s["pool"] for s in samples}
    log.info("loading pool metadata for %d pools ...", len(pools))
    meta = load_pool_meta(ch_url, ch_db, ch_user, ch_pw, pools)
    log.info("meta resolved: %d/%d", len(meta), len(pools))

    # ts→block grid
    ts_min = min(int(s["window_ts_sec"]) for s in samples)
    ts_max = max(int(s["window_ts_sec"]) for s in samples) + exit_horizon_sec
    log.info("building ts→block grid for [%d … %d]", ts_min, ts_max)
    bn_arr, ts_arr = build_ts_grid(rpc, ts_min, ts_max)

    # resume logic: skip (pool, window_ts) already in output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    done_keys: set[tuple[str, int]] = set()
    if output_path.exists():
        try:
            with output_path.open("r", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    try:
                        pool = r["pool_address"].lower()
                        ts_str = r.get("window_start", "")
                        # convert YYYY-MM-DD HH:MM:SS → unix sec
                        from datetime import datetime, timezone
                        dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        done_keys.add((pool, int(dt.timestamp())))
                    except Exception:
                        continue
            log.info("resuming: %d rows already labeled", len(done_keys))
        except Exception as e:
            log.warning("existing output unreadable (%s), starting fresh", e)
            output_path.unlink(missing_ok=True)
            done_keys.clear()

    todo = [s for s in samples if (s["pool"], int(s["window_ts_sec"])) not in done_keys]
    log.info("todo: %d samples", len(todo))

    from datetime import datetime, timezone
    append = output_path.exists()
    f_out = output_path.open("a" if append else "w", newline="", encoding="utf-8")
    w = csv.writer(f_out)
    if not append:
        w.writerow([
            "pool_address", "chain_id", "window_start",
            "price_velocity_5m", "buy_pressure_ratio",
            "token_volatility_5m", "activity_heat",
            "unique_buyer_entropy_5m",
            "label", "net_return", "mv_label", "label_type",
        ])
        f_out.flush()
    write_lock = Lock()

    stats = {"labeled": 0, "pos": 0, "skipped": 0, "quote_failed": 0,
             "mv_agree": 0, "mv_disagree": 0}
    stats_lock = Lock()
    t_start = time.time()

    def do_one(sample: dict):
        pool = sample["pool"]
        ts_sec = int(sample["window_ts_sec"])
        exit_ts = ts_sec + exit_horizon_sec
        entry_block = ts_to_block(bn_arr, ts_arr, ts_sec)
        exit_block = ts_to_block(bn_arr, ts_arr, exit_ts)
        m = meta.get(pool, {"pool_type": "", "t0": "", "t1": "", "fee_bps": 30})
        label, net, lt = label_one(rpc, pool, m, WETH_BASE, notional_wei,
                                   entry_block, exit_block)
        if label < 0:
            with stats_lock:
                stats["skipped"] += 1
                stats["quote_failed"] += 1
            return
        window_dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        mv_lab = int(sample["mv_label"])
        with write_lock:
            w.writerow([
                pool, sample["chain_id"], window_dt,
                sample["price_velocity_5m"],
                sample["buy_pressure_ratio"],
                sample["token_volatility_5m"],
                sample["activity_heat"],
                0.0,  # unique_buyer_entropy_5m not in MV, pad 0
                label, round(net, 6), mv_lab, lt,
            ])
            f_out.flush()
        with stats_lock:
            stats["labeled"] += 1
            if label == 1:
                stats["pos"] += 1
            if label == mv_lab:
                stats["mv_agree"] += 1
            else:
                stats["mv_disagree"] += 1

    log.info("launching %d workers over %d samples", workers, len(todo))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(do_one, s) for s in todo]
        done = 0
        last_log = time.time()
        for _ in as_completed(futures):
            done += 1
            if time.time() - last_log > 30:
                elapsed = time.time() - t_start
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(todo) - done) / rate if rate > 0 else -1
                with stats_lock:
                    agree_rate = (stats["mv_agree"] /
                                  max(stats["mv_agree"] + stats["mv_disagree"], 1) * 100)
                    log.info(
                        "progress %d/%d (%.1f%%) labeled=%d pos=%d quote_failed=%d "
                        "mv_agree=%.1f%% rate=%.1f/s eta=%.1fmin",
                        done, len(todo), 100.0 * done / len(todo),
                        stats["labeled"], stats["pos"], stats["quote_failed"],
                        agree_rate, rate, eta / 60,
                    )
                last_log = time.time()

    f_out.close()
    with stats_lock:
        pos_rate = stats["pos"] / max(stats["labeled"], 1) * 100
        agree_rate = (stats["mv_agree"] /
                      max(stats["mv_agree"] + stats["mv_disagree"], 1) * 100)
        log.info(
            "DONE labeled=%d pos=%d (%.1f%%) quote_failed=%d "
            "mv_agree=%.1f%% elapsed=%.1fs",
            stats["labeled"], stats["pos"], pos_rate,
            stats["quote_failed"], agree_rate, time.time() - t_start,
        )
    log.info("output=%s", output_path)


if __name__ == "__main__":
    main()
