#!/usr/bin/env python3
"""
Static paper / dry-run preflight — no Redis, RPC, or container **start**.

Reads only infra/compose/.env.example (names + non-secret guard vars) and
optional --env-file limited to LIVE_TRADING_MODE / EXECUTOR_DRY_RUN lines.

With --compose-config, runs `docker compose config -q` (validates interpolation;
does not run containers). Requires Docker CLI; skips with a message if missing.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Tuple

# Keys that must be declared in infra/compose/.env.example for a documented
# paper/dry-run + compose baseline (ingestor / strategy / executor / dashboard).
PAPER_READINESS_REQUIRED_ENV_EXAMPLE_KEYS: FrozenSet[str] = frozenset(
    {
        "ARCHIVER_BATCH_SIZE",
        "ARCHIVER_BATCH_TIMEOUT_MS",
        "ARCHIVER_BLOCK_MS",
        "ARCHIVER_CLICKHOUSE_TABLE",
        "ARCHIVER_CONSUMER",
        "ARCHIVER_GROUP",
        "ARCHIVER_HEALTHCHECK_PATH",
        "ARCHIVER_PENDING_MIN_IDLE_MS",
        "ARCHIVER_PENDING_RECLAIM_COUNT",
        "ARCHIVER_READ_COUNT",
        "BACKFILL_AUTO_CLAIM_PENDING_GAP",
        "BACKFILL_CHUNK_SIZE",
        "BACKFILL_CONFIRMATIONS",
        "BACKFILL_DISCOVERY_CLICKHOUSE_DB",
        "BACKFILL_DISCOVERY_CLICKHOUSE_PASSWORD",
        "BACKFILL_DISCOVERY_CLICKHOUSE_TABLE",
        "BACKFILL_DISCOVERY_CLICKHOUSE_URL",
        "BACKFILL_DISCOVERY_CLICKHOUSE_USER",
        "BACKFILL_DISCOVERY_FROM_BLOCK",
        "BACKFILL_DISCOVERY_INCLUDE_EDGE_GAPS",
        "BACKFILL_DISCOVERY_MAX_GAP_SIZE",
        "BACKFILL_DISCOVERY_MIN_GAP_SIZE",
        "BACKFILL_DISCOVERY_TO_BLOCK",
        "BACKFILL_FROM_BLOCK",
        "BACKFILL_JOB_NAME",
        "BACKFILL_PAUSE_MS",
        "BACKFILL_SCHEDULER_POLL_INTERVAL_MS",
        "BACKFILL_SOURCE_SUFFIX",
        "BACKFILL_STATE_POSTGRES_URL",
        "BACKFILL_TO_BLOCK",
        "BASE_CAPITAL",
        "CHAIN_ID",
        "CLICKHOUSE_DB",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_URL",
        "CLICKHOUSE_USER",
        "COMPOSE_PROJECT_NAME",
        "EATLQ_INGESTION_MODE",
        "EATLQ_RPC_BUDGET_ENABLED",
        "EATLQ_RPC_DAILY_CALL_LIMIT",
        "DASHBOARD_ACTION_TOKEN",
        "DASHBOARD_ALLOW_PANIC_ACTIONS",
        "DASHBOARD_ARCHIVE_STALE_SEC",
        "DASHBOARD_BASIC_PASS",
        "DASHBOARD_BASIC_USER",
        "DASHBOARD_BIND",
        "DASHBOARD_HOST_BIND",
        "DASHBOARD_INGEST_STALE_SEC",
        "DASHBOARD_MAX_MARKET_CANDLES",
        "DASHBOARD_MAX_MARKET_TICKS",
        "DASHBOARD_MAX_SHADOW",
        "DASHBOARD_MAX_SIGNALS",
        "DASHBOARD_PORT",
        "DASHBOARD_READONLY",
        "DASHCAM_PATH",
        "EVM_HTTP_URL",
        "EVM_HTTP_URLS_POOL",
        "EVM_WS_URL",
        "EVM_WS_URLS_POOL",
        "EXECUTOR_CONSUMER_GROUP",
        "EXECUTOR_CONSUMER_NAME",
        "EXECUTOR_DAILY_LOSS_PCT",
        "EXECUTOR_DRY_RUN",
        "EXECUTOR_ETH_PRICE_USD",
        "EXECUTOR_FRICTION_FAIL_CLOSED",
        "EXECUTOR_FRICTION_MIN",
        "EXECUTOR_MAX_BALANCE_FRACTION",
        "EXECUTOR_MAX_CONCURRENT_POSITIONS",
        "EXECUTOR_MAX_GAS_USD",
        "EXECUTOR_MAX_POSITION_USD",
        "EXECUTOR_MEV_PROTECTED",
        "EXECUTOR_MONITOR_INTERVAL_SEC",
        "EXECUTOR_PRIVATE_KEY",
        "EXECUTOR_READ_RPC_URL",
        "EXECUTOR_RPC_URL",
        "EXECUTOR_SEND_RPC_URL",
        "EXECUTOR_SEND_RPC_URL_FALLBACK",
        "EXECUTOR_STOP_LOSS_PCT",
        "EXECUTOR_TAKE_PROFIT_PCT",
        "EXECUTOR_TIME_DECAY_MIN",
        "EXECUTOR_TRAILING_STOP_PCT",
        "BRIDGE_POLL_SEC",
        "BRIDGE_P_CUT_MAX",
        "BRIDGE_POSITION_SIZE_USD",
        "FREE_RPC_URL",
        "INGESTOR_HEALTHCHECK_PATH",
        "INGESTOR_POOL_BAN_SECS",
        "INGESTOR_POOL_COOLDOWN_SECS",
        "INGESTOR_POOL_MAX_CONSECUTIVE_FAILS",
        "INGESTOR_WS_IDLE_SECS",
        "LIVE_TRADING_MODE",
        "LIVE_AUTHORIZED_BY",
        "LOG_FILTER_JSON",
        "MODEL_DIR",
        "POOL_DISCOVERY_CLICKHOUSE_TABLE",
        "POOL_DISCOVERY_ENABLE_CLICKHOUSE",
        "POOL_DISCOVERY_RPC_URL",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "RAW_CHANNEL_CAPACITY",
        "RECORD_CHANNEL_CAPACITY",
        "REDIS_BATCH_SIZE",
        "REDIS_BATCH_TIMEOUT_MS",
        "REDIS_URL",
        "RPC_REQUEST_TIMEOUT_SECS",
        "RUST_LOG",
        "SIGNAL_MIN_CONFIDENCE",
        "SIGNALS_STREAM",
        "SMART_MONEY_BUY_THRESHOLD",
        "SMART_MONEY_POOL_MAX_AGE_H",
        "STRATEGY_CONSUMER_GROUP",
        "STRATEGY_CONSUMER_NAME",
        "STRATEGY_READ_COUNT",
        "STRATEGY_REQUIRE_MANIFEST",
        "STRATEGY_SYMBOL_BLACKLIST",
        "STRATEGY_TOKEN_BLACKLIST",
        "SWAP_POLLER_INTERVAL_SEC",
        "SWAP_POLLER_SKIP_TX_FROM",
        "SYSTEM_PANIC_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "WHALE_FLOW_THRESHOLD",
        "MARKET_TICKS_MAXLEN",
        "MARKET_TICKS_STREAM",
        "STRATEGY_BLOCK_MS",
        "WS_READ_TIMEOUT_SECS",
        "WS_RECONNECT_INITIAL_BACKOFF_MS",
        "WS_RECONNECT_MAX_BACKOFF_MS",
        "WS_SOURCE",
        "WS_SUBSCRIBE_METHOD",
        "WS_SUBSCRIBE_PARAMS_JSON",
        "TZ",
    }
)

_SAFE_ENV_FILE_KEYS: FrozenSet[str] = frozenset({"LIVE_TRADING_MODE", "EXECUTOR_DRY_RUN"})

_DOTENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def repo_root_from_here() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_dotenv_keys(lines: Iterable[str]) -> FrozenSet[str]:
    """Return declared KEY names from KEY=value lines (no values)."""
    keys: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _sep, _val = line.partition("=")
        key = key.strip()
        if _DOTENV_KEY_RE.match(key):
            keys.add(key)
    return frozenset(keys)


def parse_dotenv_file_keys(path: Path) -> FrozenSet[str]:
    text = path.read_text(encoding="utf-8")
    return parse_dotenv_keys(text.splitlines())


def parse_truthy(s: str | None) -> bool:
    if s is None:
        return False
    return s.strip().lower() in ("1", "true", "yes", "on")


def live_trading_and_dry_run_conflict(live: str | None, dry_run: str | None) -> bool:
    return parse_truthy(live) and parse_truthy(dry_run)


def read_safe_env_pairs(path: Path) -> Dict[str, str]:
    """Only LIVE_TRADING_MODE / EXECUTOR_DRY_RUN — no secrets."""
    out: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _sep, val = line.partition("=")
        key = key.strip()
        if key in _SAFE_ENV_FILE_KEYS:
            out[key] = val.strip()
    return out


def expected_gitignore_runtime_patterns() -> Tuple[str, ...]:
    return (
        "logs/*.csv",
        "mvp/diagnostics/*.csv",
    )


def gitignore_covers_runtime_outputs(gitignore_text: str) -> List[str]:
    missing = []
    for pat in expected_gitignore_runtime_patterns():
        if pat not in gitignore_text:
            missing.append(pat)
    return missing


def validate_env_example(repo: Path) -> Tuple[bool, List[str]]:
    env_ex = repo / "infra" / "compose" / ".env.example"
    if not env_ex.is_file():
        return False, [f"missing {env_ex.relative_to(repo)}"]
    declared = parse_dotenv_file_keys(env_ex)
    missing = sorted(PAPER_READINESS_REQUIRED_ENV_EXAMPLE_KEYS - declared)
    extra = sorted(declared - PAPER_READINESS_REQUIRED_ENV_EXAMPLE_KEYS)
    errs: List[str] = []
    if missing:
        errs.append(f".env.example missing keys: {', '.join(missing)}")
    if extra:
        errs.append(
            ".env.example has keys not listed in scripts/paper_dryrun_readiness.py "
            f"PAPER_READINESS_REQUIRED_ENV_EXAMPLE_KEYS: {', '.join(extra)}"
        )
    return not errs, errs


def validate_compose_profiles(compose_text: str) -> Tuple[bool, List[str]]:
    errs: List[str] = []
    if 'profiles: ["dashboard"]' not in compose_text and "profiles: ['dashboard']" not in compose_text:
        errs.append("dashboard service must use profiles: [\"dashboard\"] (opt-in profile)")
    if 'profiles: ["strategy"]' not in compose_text and "profiles: ['strategy']" not in compose_text:
        errs.append("strategy service must use profiles: [\"strategy\"]")
    if 'profiles: ["executor"]' not in compose_text and "profiles: ['executor']" not in compose_text:
        errs.append("executor service must use profiles: [\"executor\"]")
    return not errs, errs


def validate_laszlo_profiles(laszlo_text: str) -> Tuple[bool, List[str]]:
    errs: List[str] = []
    for needle in ("--profile strategy", "--profile executor", "--profile dashboard"):
        if needle not in laszlo_text:
            errs.append(f"scripts/laszlo.sh must pass {needle} for full stack parity")
    for subcmd in ("soak", "paper", "dashboard", "cron-audit"):
        if f"cmd_{subcmd.replace('-', '_')}" not in laszlo_text and f"{subcmd})" not in laszlo_text:
            errs.append(f"scripts/laszlo.sh must expose subcommand: {subcmd}")
    return not errs, errs


def run_cron_guard(repo: Path) -> Tuple[bool, List[str]]:
    guard = repo / "scripts" / "maintenance" / "cron_guard.sh"
    if not guard.is_file():
        return False, [f"missing cron guard script: {guard.relative_to(repo)}"]
    if not shutil.which("crontab"):
        return True, ["cron-audit: SKIP — crontab CLI not in PATH"]
    try:
        proc = subprocess.run(
            [str(guard.resolve())],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, [f"cron-audit: FAIL — {exc}"]
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return False, [f"cron-audit: FAIL — {detail[:600]}"]
    return True, ["cron-audit: OK — live crontab matches infra/cron/crontab.sealed"]


def run_dual_ingestion_guard(repo: Path) -> Tuple[bool, List[str]]:
    """Warn when ingestor and swap-poller are both running (P0-3 double-ingest footgun)."""
    if not shutil.which("docker"):
        return True, ["dual-ingestion: SKIP — docker CLI not in PATH"]
    compose_file = repo / "infra" / "compose" / ".env.example"
    env_file = repo / "infra" / "compose" / ".env"
    env_for = env_file if env_file.is_file() else compose_file
    names = ("ingestor", "swap-poller")
    running: List[str] = []
    for svc in names:
        cmd = [
            "docker",
            "compose",
            "--env-file",
            str(env_for.resolve()),
            "-f",
            str((repo / "infra" / "compose" / "compose.yaml").resolve()),
            "ps",
            svc,
            "--format",
            "{{.State}}",
        ]
        try:
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=20)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return True, ["dual-ingestion: SKIP — docker compose ps failed"]
        state = (proc.stdout or "").strip().lower()
        if state.startswith("running"):
            running.append(svc)
    if len(running) >= 2:
        return False, [
            "dual-ingestion: FAIL — ingestor and swap-poller are BOTH running. "
            "This duplicates RPC load (P0-3). Stop one path or keep EATLQ_INGESTION_MODE=sealed."
        ]
    if running:
        return True, [f"dual-ingestion: OK — only {running[0]} running"]
    return True, ["dual-ingestion: OK — neither ingestor nor swap-poller running"]


def validate_core_paths(repo: Path) -> Tuple[bool, List[str]]:
    must = [
        repo / "infra" / "compose" / "compose.yaml",
        repo / "infra" / "compose" / ".env.example",
        repo / "scripts" / "laszlo.sh",
        repo / "Makefile",
        repo / "Operations.md",
    ]
    missing = [str(p.relative_to(repo)) for p in must if not p.is_file()]
    return not missing, [f"missing files: {', '.join(missing)}"] if missing else []


def validate_gitignore(repo: Path) -> Tuple[bool, List[str]]:
    gi = repo / ".gitignore"
    if not gi.is_file():
        return False, ["missing .gitignore"]
    miss = gitignore_covers_runtime_outputs(gi.read_text(encoding="utf-8"))
    return not miss, [f".gitignore missing patterns: {', '.join(miss)}"] if miss else []


def validate_optional_env_safety(repo: Path, env_file: Path | None) -> Tuple[bool, List[str]]:
    if env_file is None:
        return True, []
    if not env_file.is_file():
        return False, [f"env file not found: {env_file}"]
    try:
        rel = env_file.resolve().relative_to(repo.resolve())
    except ValueError:
        rel = env_file
    pairs = read_safe_env_pairs(env_file)
    live = pairs.get("LIVE_TRADING_MODE")
    dry = pairs.get("EXECUTOR_DRY_RUN")
    if live_trading_and_dry_run_conflict(live, dry):
        return False, [
            f"{rel}: LIVE_TRADING_MODE and EXECUTOR_DRY_RUN are both truthy — "
            "executor refuses this mix (ambiguous live vs simulation)."
        ]
    if parse_truthy(live):
        return False, [
            f"{rel}: LIVE_TRADING_MODE is truthy — refuse stack preflight for Sprint010 "
            "(paper/dry-run only; human must not set live mode for this path)."
        ]
    return True, []


def run_compose_config_validate(repo: Path, env_file: Path) -> Tuple[bool, List[str], bool]:
    """
    Run `docker compose config -q`. Returns (ok, messages, skipped).
    skipped=True when Docker CLI is missing (not a hard fail).
    """
    if not env_file.is_file():
        return False, [f"compose-config: env file missing: {env_file}"], False
    if not shutil.which("docker"):
        return True, [
            "compose-config: SKIP — docker CLI not in PATH (install Docker to validate compose interpolation)"
        ], True
    compose_file = repo / "infra" / "compose" / "compose.yaml"
    cmd = [
        "docker",
        "compose",
        "--env-file",
        str(env_file.resolve()),
        "-f",
        str(compose_file.resolve()),
        "--profile",
        "strategy",
        "--profile",
        "executor",
        "--profile",
        "dashboard",
        "config",
        "-q",
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        return True, ["compose-config: SKIP — docker executable vanished mid-check"], True
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return False, [f"compose-config: FAIL — docker compose config: {err[:800]}"], False
    return True, [
        "compose-config: OK — docker compose config -q (validated interpolation; no containers started)"
    ], False


def run_preflight(repo: Path, env_file: Path | None) -> int:
    all_errs: List[str] = []
    for fn in (
        validate_core_paths,
        lambda r: validate_env_example(r),
        lambda r: validate_gitignore(r),
    ):
        ok, errs = fn(repo)
        all_errs.extend(errs)

    compose_path = repo / "infra" / "compose" / "compose.yaml"
    ok_c, errs_c = validate_compose_profiles(compose_path.read_text(encoding="utf-8"))
    if not ok_c:
        all_errs.extend(errs_c)

    laszlo_path = repo / "scripts" / "laszlo.sh"
    ok_l, errs_l = validate_laszlo_profiles(laszlo_path.read_text(encoding="utf-8"))
    if not ok_l:
        all_errs.extend(errs_l)

    ok_e, errs_e = validate_optional_env_safety(repo, env_file)
    if not ok_e:
        all_errs.extend(errs_e)

    ok_cron, msgs_cron = run_cron_guard(repo)
    for m in msgs_cron:
        print(f"  · {m}", file=sys.stderr if not ok_cron else sys.stdout)
    ok_dual, msgs_dual = run_dual_ingestion_guard(repo)
    for m in msgs_dual:
        if not ok_dual:
            all_errs.append(m)
        else:
            print(f"  · {m}")

    if all_errs:
        print("paper_dryrun_readiness: FAIL", file=sys.stderr)
        for e in all_errs:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("paper_dryrun_readiness: OK — static checks (no containers / no Redis / no RPC)")
    print("  · core paths and docs present")
    print("  · infra/compose/.env.example key parity")
    print("  · .gitignore covers logs/*.csv and mvp/diagnostics/*.csv")
    print("  · compose profiles (strategy / executor / dashboard) + laszlo.sh --profile lines")
    for m in msgs_dual:
        if m not in all_errs:
            print(f"  · {m}")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: parent of scripts/)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional local .env to scan for LIVE_TRADING_MODE / EXECUTOR_DRY_RUN only",
    )
    parser.add_argument(
        "--compose-config",
        action="store_true",
        help="After static OK, run docker compose config -q (requires Docker CLI; no up)",
    )
    parser.add_argument(
        "--compose-env-file",
        type=Path,
        default=None,
        help="Env file for docker compose config (default: --env-file if set, else .env.example)",
    )
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve() if args.repo_root else repo_root_from_here()
    rc = run_preflight(repo, args.env_file)
    if rc != 0:
        return rc
    if not args.compose_config:
        return 0
    env_for = args.compose_env_file or args.env_file or (repo / "infra" / "compose" / ".env.example")
    ok, msgs, _skipped = run_compose_config_validate(repo, env_for)
    for m in msgs:
        print(m, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
