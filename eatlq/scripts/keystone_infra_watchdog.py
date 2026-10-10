#!/usr/bin/env python3
"""Keystone infra watchdog — proactive Telegram alerts + optional auto-restart.

Checks Docker health, ingestor/archiver heartbeats, ClickHouse swap freshness,
and watchdog self-liveness (detects cron/scheduler gaps).

Target: first Telegram alert within ~2 minutes of any stall.

Run every 30s via dual cron (see keystone_infra_watchdog.sh / crontab):
  * * * * * /opt/EATLQ/scripts/keystone_infra_watchdog.sh
  * * * * * sleep 30; /opt/EATLQ/scripts/keystone_infra_watchdog.sh
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path("/opt/EATLQ")
ENV_FILE = ROOT / "infra/compose/.env"
REDIS_CONTAINER = os.environ.get("INFRA_WATCHDOG_REDIS_CONTAINER", "eatlq-redis-1")
CH_CONTAINER = os.environ.get("INFRA_WATCHDOG_CH_CONTAINER", "eatlq-clickhouse-1")
ARCHIVER_CONTAINER = os.environ.get("INFRA_WATCHDOG_ARCHIVER_CONTAINER", "eatlq-archiver-1")
SWAP_POLLER_CONTAINER = os.environ.get("INFRA_WATCHDOG_SWAP_POLLER_CONTAINER", "eatlq-swap-poller-1")
ALERT_TTL_SEC = int(os.environ.get("INFRA_WATCHDOG_ALERT_TTL_SEC", "600"))
# Layered detection. The <2min SLA for the real failure modes (archiver hang,
# ingestor stop, container death) is carried by the HEARTBEAT/container checks
# below at 90s (90s + 30s cron = ~120s worst case). CH_SWAP_LAG is a *backstop*
# for the rare "both heartbeats alive but data not landing" case and is set with
# headroom above the swap-poller's natural cadence.
#
# IMPORTANT: V3 swap freshness is bounded by the swap-poller (source=
# discovery_poller), NOT the archiver. The poller sleeps SWAP_POLLER_INTERVAL_SEC
# (30s) between cycles and each cycle sweeps ~1200 pools in 0.5s-throttled batches,
# so block_time lands ~35-110s behind wall clock even when everything is healthy.
# Measured over 2h (2026-07-06): ingest-delay avg 68s / p95 99s / max 108s, plus
# the archiver's ~10s flush. Transient RPC slowness on a single poll cycle can push
# the end-to-end lag past 150s with no real outage — this is exactly what produced
# the 2026-07-06 false page. 210s gives real headroom above that oscillation while
# still catching a genuine multi-minute stall (which grows unbounded) well before
# it matters; the true <2min SLA is still carried by the container/heartbeat checks.
INGESTOR_STALE_SEC = int(os.environ.get("INFRA_WATCHDOG_INGESTOR_STALE_SEC", "90"))
CH_SWAP_LAG_ALERT_SEC = int(os.environ.get("INFRA_WATCHDOG_CH_SWAP_LAG_SEC", "210"))
# docker exec timeout for the lag query. The query hits the MergeTree base table
# (not the decoded/deduped views) and is normally <1s; 30s means CH is wedged
# or the container cannot exec, not "the view is slow".
CH_QUERY_TIMEOUT_SEC = int(os.environ.get("INFRA_WATCHDOG_CH_QUERY_TIMEOUT_SEC", "30"))
# Page "新鲜度查询失败" only after this many consecutive failed readings.
# Dual cron is ~2 runs/min; default 3 ≈ 90s. A single 30s timeout during CPU
# contention is not an outage — a real CH/docker failure stays failed.
CH_QUERY_FAIL_CONFIRM_CHECKS = int(os.environ.get("INFRA_WATCHDOG_CH_QUERY_FAIL_CONFIRM", "3"))
ARCHIVER_HEALTH_STALE_SEC = int(os.environ.get("INFRA_WATCHDOG_ARCHIVER_STALE_SEC", "90"))
# If watchdog itself hasn't run for this long, cron/scheduler likely stopped.
WATCHDOG_GAP_ALERT_SEC = int(os.environ.get("INFRA_WATCHDOG_GAP_ALERT_SEC", "120"))
LAST_RUN_KEY = "infra_watchdog:last_run_ts"
AUTO_RESTART_INGESTOR = os.environ.get("INFRA_WATCHDOG_AUTO_RESTART", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
AUTO_RESTART_ARCHIVER = os.environ.get("INFRA_WATCHDOG_AUTO_RESTART_ARCHIVER", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
AUTO_RESTART_SWAP_POLLER = os.environ.get("INFRA_WATCHDOG_AUTO_RESTART_SWAP_POLLER", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
RESTART_COOLDOWN_SEC = int(os.environ.get("INFRA_WATCHDOG_RESTART_COOLDOWN_SEC", "1800"))
# Recovery notifications. A "已恢复健康/无需干预" message must NEVER fire unless the
# condition is genuinely, durably healthy — a false recovery would lull us during a
# real outage. So we require RECOVERY_CONFIRM_CHECKS *consecutive* healthy
# observations before announcing recovery, and any single bad reading resets the
# streak to zero (see _open_incident). We count observations rather than wall-clock
# time on purpose: if the watchdog/cron stalls, the gap simply stops accruing
# confidence instead of being wrongly credited as "healthy the whole time". At the
# dual-cron cadence (~2 runs/min) the default 4 ≈ 2 minutes of sustained health.
RECOVERY_CONFIRM_CHECKS = int(os.environ.get("INFRA_WATCHDOG_RECOVERY_CONFIRM_CHECKS", "4"))
INCIDENT_TTL_SEC = int(os.environ.get("INFRA_WATCHDOG_INCIDENT_TTL_SEC", "86400"))

WATCH_CONTAINERS = [
    "eatlq-ingestor-1",
    "eatlq-archiver-1",
    "eatlq-executor-1",
    "eatlq-signal-bridge-1",
    "eatlq-pool-discovery-1",
    # Sole producer of decoded V3 swaps (source=discovery_poller). If it dies or
    # wedges, ch_swap_lag climbs while archiver/ingestor stay green — so it must be
    # watched directly, not inferred via the archiver.
    "eatlq-swap-poller-1",
    "eatlq-redis-1",
    "eatlq-clickhouse-1",
]


def _load_env() -> None:
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        os.environ.setdefault(k, v)


def _send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("WARN: TELEGRAM_BOT_TOKEN/CHAT_ID missing — alert not sent", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:3900]}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:
        print(f"WARN: telegram send failed: {exc}", file=sys.stderr)
        return False


class _RedisCli:
    """Minimal Redis client via docker exec (host cron has no docker-network access)."""

    def __init__(self, container: str) -> None:
        self.container = container

    def _run(self, *args: str) -> str:
        cmd = ["docker", "exec", self.container, "redis-cli", *args]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=True)
        return (out.stdout or "").strip()

    def ping(self) -> None:
        if self._run("ping") != "PONG":
            raise RuntimeError("redis ping failed")

    def get(self, key: str) -> str | None:
        val = self._run("GET", key)
        return None if val in ("", "(nil)") else val

    def set(self, key: str, val: str, ex: int | None = None, nx: bool = False) -> bool:
        args = ["SET", key, val]
        if nx:
            args.append("NX")
        if ex is not None:
            args.extend(["EX", str(ex)])
        result = self._run(*args)
        return result == "OK"

    def hget(self, key: str, field: str) -> str | None:
        val = self._run("HGET", key, field)
        return None if val in ("", "(nil)") else val

    def delete(self, *keys: str) -> None:
        if keys:
            self._run("DEL", *keys)


def _redis() -> _RedisCli:
    return _RedisCli(REDIS_CONTAINER)


def _docker_health(name: str) -> str:
    """Return running|healthy|unhealthy|missing|no-healthcheck."""
    try:
        insp = subprocess.run(
            ["docker", "inspect", name, "--format", "{{json .State}}"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        state = json.loads(insp.stdout.strip())
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        return "missing"
    if not state.get("Running"):
        return "stopped"
    health = (state.get("Health") or {}).get("Status")
    if health:
        return health  # healthy|unhealthy|starting
    return "running"


def _ingestor_pool_health_age_sec(r) -> int | None:
    try:
        raw = r.hget("ingestor:pool_health", "updated_at_ms")
        if not raw:
            return None
        ts_ms = int(raw)
        return max(0, int(time.time()) - ts_ms // 1000)
    except Exception:
        return None


def _clickhouse_swap_lag_sec() -> int | None:
    """Seconds since the newest Base swap landed in ClickHouse.

    Query the MergeTree base table, not uniswap_v3_swaps_decoded. The decoded
    path is a dedup+hex view over the full table (~4.5 GiB / 8–20s) and was
    the source of Telegram flaps: the client timed out at 30s while CH was
    still healthy and swaps were still landing. block_time on the base table
    is the same ingest timestamp; freshness does not need decoded columns.

    Returns None if the query itself fails (caller pages only after a confirm
    streak — a single timeout is not an outage).
    """
    sql = (
        "SELECT toUnixTimestamp(now()) - toUnixTimestamp(max(block_time)) "
        "FROM eatlq.uniswap_v3_swaps WHERE chain_id=8453"
    )
    try:
        out = subprocess.run(
            ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--query", sql],
            capture_output=True, text=True, timeout=CH_QUERY_TIMEOUT_SEC, check=True,
        )
        val = (out.stdout or "").strip()
        if not val or val.lower() in ("nan", "\\n", "null"):
            return None
        return max(0, int(float(val)))
    except Exception as exc:
        print(f"WARN: clickhouse swap-lag query failed: {exc}", file=sys.stderr)
        return None


R3_ARM = Path("/root/keystone_r3_shadow/HUMAN_ARM.json")
R3_HEALTH = Path("/root/keystone_r3_shadow/ledger/observer_health.json")
R3_PID = Path("/root/keystone_r3_shadow/ledger/observer.pid")
R3_HEALTH_STALE_SEC = int(os.environ.get("INFRA_WATCHDOG_R3_STALE_SEC", "180"))


def _r3_observer_alive() -> bool:
    if not R3_PID.is_file():
        return False
    try:
        pid = int(R3_PID.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _r3_shadow_probe(r) -> None:
    if not R3_ARM.is_file():
        return
    try:
        if not json.loads(R3_ARM.read_text()).get("clock_started"):
            return
    except Exception:
        return
    if not _r3_observer_alive():
        _open_incident(r, "r3-observer-dead")
        _alert_once(
            r, "r3-observer-dead",
            "⚠️ r3 shadow 观察器进程不在。\n"
            "前瞻账本不再写入。生产 Keystone / 执行器不受影响。",
        )
    else:
        _note_healthy(
            r, "r3-observer-dead",
            "✅ r3 shadow 观察器进程已恢复。生产交易不受影响。",
        )
    if not R3_HEALTH.is_file():
        _open_incident(r, "r3-observer-stale")
        _alert_once(
            r, "r3-observer-stale",
            "⚠️ r3 shadow 健康文件缺失。\n观察器可能没在写心跳。生产交易不受影响。",
        )
        return
    age = max(0, int(time.time() - R3_HEALTH.stat().st_mtime))
    if age > R3_HEALTH_STALE_SEC:
        _open_incident(r, "r3-observer-stale")
        _alert_once(
            r, "r3-observer-stale",
            f"⚠️ r3 shadow 观察器心跳过期 {age}s。\n"
            "进程可能卡住。生产 Keystone / 执行器不受影响。",
        )
    else:
        _note_healthy(
            r, "r3-observer-stale",
            f"✅ r3 shadow 观察器心跳已恢复（{age}s）。",
        )
        try:
            health = json.loads(R3_HEALTH.read_text())
        except Exception:
            health = {}
        if health.get("instrument_status") == "WRITE_STALL":
            _open_incident(r, "r3-write-stall")
            _alert_once(
                r, "r3-write-stall",
                "⚠️ r3 shadow 账本写入停滞：扫到池但记不进去。\n"
                "生产 Keystone 不受影响。看 ledger/observer.log",
            )
        else:
            _note_healthy(
                r, "r3-write-stall",
                "✅ r3 shadow 账本写入已恢复。此前空窗不回填。",
            )


def _archiver_health_age_sec() -> int | None:
    """Age (seconds) of the archiver heartbeat file inside its container."""
    path = os.environ.get("ARCHIVER_HEALTHCHECK_PATH", "/tmp/archiver_healthy")
    try:
        out = subprocess.run(
            ["docker", "exec", ARCHIVER_CONTAINER, "stat", "-c", "%Y", path],
            capture_output=True, text=True, timeout=15, check=True,
        )
        mtime = int((out.stdout or "0").strip())
        return max(0, int(time.time()) - mtime)
    except Exception:
        return None


def _alert_once(r, alert_id: str, message: str) -> None:
    key = f"infra_watchdog:sent:{alert_id}"
    if r.set(key, str(int(time.time())), nx=True, ex=ALERT_TTL_SEC):
        _send_telegram(message)
        print(f"ALERT sent: {alert_id}")
    else:
        print(f"ALERT suppressed (dedup): {alert_id}")


def _open_incident(r, incident_id: str) -> None:
    """Record that an alert condition is CURRENTLY firing, and reset any recovery
    progress. Call this on every cycle the condition is bad so that a relapse wipes
    the healthy streak and the recovery message can never fire on stale confidence.
    """
    r.set(f"infra_watchdog:active:{incident_id}", str(int(time.time())), ex=INCIDENT_TTL_SEC)
    r.delete(f"infra_watchdog:healthy_streak:{incident_id}")


def _note_healthy(r, incident_id: str, recovery_message: str, clear_extra: list[str] | None = None) -> None:
    """Call this ONLY on a cycle where the condition returned a concrete healthy
    reading. If an incident was previously open, require RECOVERY_CONFIRM_CHECKS
    consecutive healthy observations before sending a single recovery notice.

    Safety: because this is only ever invoked when the metric is verified healthy
    NOW, and any bad cycle resets the streak via _open_incident, the "已恢复" message
    cannot be sent while the underlying condition is still (or intermittently) bad.
    """
    active_key = f"infra_watchdog:active:{incident_id}"
    if not r.get(active_key):
        return  # no incident was ever opened → stay silent (avoids spurious notices)

    streak_key = f"infra_watchdog:healthy_streak:{incident_id}"
    streak = int(r.get(streak_key) or "0") + 1
    if streak < RECOVERY_CONFIRM_CHECKS:
        r.set(streak_key, str(streak), ex=INCIDENT_TTL_SEC)
        print(f"RECOVERY pending: {incident_id} ({streak}/{RECOVERY_CONFIRM_CHECKS})")
        return

    _send_telegram(recovery_message)
    # Clear incident state and re-arm the alert dedup so a *future* recurrence pages
    # immediately instead of being suppressed by the old ALERT_TTL window.
    keys_to_clear = [active_key, streak_key, f"infra_watchdog:sent:{incident_id}"]
    if clear_extra:
        keys_to_clear.extend(f"infra_watchdog:sent:{k}" for k in clear_extra)
    r.delete(*keys_to_clear)
    print(f"RECOVERY sent: {incident_id}")


def _maybe_restart_service(r, service: str, reasons: list[str], enabled: bool) -> None:
    """Restart a compose service (with per-service cooldown) and alert on Telegram.

    `service` is the compose service name (e.g. "ingestor", "archiver").
    """
    if not enabled:
        return
    cooldown_key = f"infra_watchdog:{service}_restart_ts"
    last = r.get(cooldown_key)
    if last and int(time.time()) - int(last) < RESTART_COOLDOWN_SEC:
        print(f"{service} restart skipped (cooldown)")
        _alert_once(
            r,
            f"{service}-stuck-in-cooldown",
            f"🚨 <b>{service} 仍异常但处于重启冷却期</b>\n"
            f"原因：{'; '.join(reasons)}\n"
            f"自动重启已在 {RESTART_COOLDOWN_SEC}s 内执行过仍未恢复，需人工介入。",
        )
        return
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(ROOT / "infra/compose/compose.yaml"), "restart", service],
            cwd=str(ROOT / "infra/compose"),
            timeout=120, check=True,
        )
        r.set(cooldown_key, str(int(time.time())), ex=RESTART_COOLDOWN_SEC)
        _send_telegram(
            f"🔧 <b>{service} 已自动重启</b>\n"
            f"原因：{'; '.join(reasons)}\n"
            "请确认 5 分钟内 health 恢复为 healthy。"
        )
        print(f"{service} restarted")
    except Exception as exc:
        _alert_once(r, f"{service}-restart-failed", f"❌ {service} 自动重启失败: {exc}\n请立即人工检查。")


def main() -> int:
    _load_env()
    now = int(time.time())
    try:
        r = _redis()
        r.ping()
    except Exception as exc:
        print(f"redis unavailable: {exc}", file=sys.stderr)
        _send_telegram(f"❌ Infra watchdog: Redis 不可达 ({exc})")
        return 1

    # --- Watchdog / cron self-liveness ----------------------------------------
    last_run_raw = r.get(LAST_RUN_KEY)
    if last_run_raw:
        gap = now - int(last_run_raw)
        if gap > WATCHDOG_GAP_ALERT_SEC:
            _alert_once(
                r,
                "watchdog-scheduler-gap",
                f"🚨 <b>监控 cron 疑似停止</b>\n"
                f"watchdog 上次成功运行距今 {gap}s（阈值 {WATCHDOG_GAP_ALERT_SEC}s）。\n"
                f"请检查 crontab / keystone_infra_watchdog.sh 是否在跑。",
            )

    issues: list[str] = []
    container_status: dict[str, str] = {}

    for name in WATCH_CONTAINERS:
        status = _docker_health(name)
        container_status[name] = status
        prev = r.get(f"infra_watchdog:status:{name}") or ""
        r.set(f"infra_watchdog:status:{name}", status, ex=86400)

        if status in ("unhealthy", "stopped", "missing"):
            issues.append(f"{name}={status}")
            _open_incident(r, f"container-{name}")
            if status != prev:
                _alert_once(
                    r,
                    f"container-{name}-{status}",
                    f"⚠️ <b>容器异常</b>\n<code>{name}</code> → {status}\n"
                    f"请检查 docker logs {name}",
                )
        elif status in ("healthy", "running"):
            _note_healthy(
                r, f"container-{name}",
                f"✅ <b>容器已恢复</b>\n<code>{name}</code> → {status}\n"
                f"已连续 {RECOVERY_CONFIRM_CHECKS} 次检查正常，无需人工干预。",
            )

    ingest_age = _ingestor_pool_health_age_sec(r)
    if ingest_age is not None and ingest_age > INGESTOR_STALE_SEC:
        issues.append(f"ingestor_pool_health_stale={ingest_age}s")
        _open_incident(r, "ingestor-pool-health-stale")
        _alert_once(
            r,
            "ingestor-pool-health-stale",
            f"⚠️ <b>Ingestor 心跳过期</b>\n"
            f"ingestor:pool_health 已 {ingest_age}s 未更新 (阈值 {INGESTOR_STALE_SEC}s)\n"
            f"ingestor 可能已僵死（RPC/WS 断连后进程未恢复）",
        )
    elif ingest_age is not None:
        # Concrete fresh reading (not "couldn't measure") → eligible for recovery.
        _note_healthy(
            r, "ingestor-pool-health-stale",
            f"✅ <b>Ingestor 心跳已恢复</b>\n"
            f"ingestor:pool_health 已重新更新（当前 {ingest_age}s，阈值 {INGESTOR_STALE_SEC}s），"
            f"已连续 {RECOVERY_CONFIRM_CHECKS} 次正常，无需人工干预。",
        )

    ingestor_bad = (
        container_status.get("eatlq-ingestor-1") == "unhealthy"
        or (ingest_age is not None and ingest_age > INGESTOR_STALE_SEC)
    )
    if ingestor_bad:
        _maybe_restart_service(
            r, "ingestor",
            [i for i in issues if "ingestor" in i or "eatlq-ingestor" in i],
            AUTO_RESTART_INGESTOR,
        )

    # --- Data pipeline freshness (ingestor → archiver → ClickHouse) ----------
    # The authoritative end-to-end signal. Decoupled from container health so a
    # "fake alive" archiver zombie cannot hide a stalled pipeline.
    ch_lag = _clickhouse_swap_lag_sec()
    arch_health_age = _archiver_health_age_sec()
    archiver_status = container_status.get("eatlq-archiver-1", "missing")

    archiver_reasons: list[str] = []
    if archiver_status in ("unhealthy", "stopped", "missing"):
        archiver_reasons.append(f"container={archiver_status}")
    if arch_health_age is not None and arch_health_age > ARCHIVER_HEALTH_STALE_SEC:
        archiver_reasons.append(f"heartbeat_stale={arch_health_age}s")
    if ch_lag is not None and ch_lag > CH_SWAP_LAG_ALERT_SEC:
        archiver_reasons.append(f"ch_swap_lag={ch_lag}s")

    if ch_lag is None:
        issues.append("ch_swap_lag_query_failed")
        fail_key = "infra_watchdog:fail_streak:ch-swap-lag-query-failed"
        fail_streak = int(r.get(fail_key) or "0") + 1
        r.set(fail_key, str(fail_streak), ex=INCIDENT_TTL_SEC)
        if fail_streak < CH_QUERY_FAIL_CONFIRM_CHECKS:
            print(
                f"QUERY-FAIL pending: ch-swap-lag-query-failed "
                f"({fail_streak}/{CH_QUERY_FAIL_CONFIRM_CHECKS})"
            )
        else:
            _open_incident(r, "ch-swap-lag-query-failed")
            _alert_once(
                r, "ch-swap-lag-query-failed",
                "⚠️ <b>ClickHouse 新鲜度查询失败</b>\n"
                f"连续 {fail_streak} 次无法读取最新 swap 时间，"
                "CH 可能不可用，请检查 eatlq-clickhouse-1。",
            )
    elif ch_lag > CH_SWAP_LAG_ALERT_SEC:
        # The query succeeded (so query-failed has recovered), but data is stale.
        r.delete("infra_watchdog:fail_streak:ch-swap-lag-query-failed")
        _note_healthy(
            r, "ch-swap-lag-query-failed",
            "✅ <b>ClickHouse 新鲜度查询已恢复</b>\n可正常读取最新 swap 时间。",
        )
        issues.append(f"ch_swap_lag={ch_lag}s")
        _open_incident(r, "ch-swap-data-stale")
        swap_poller_status = container_status.get("eatlq-swap-poller-1", "missing")
        _alert_once(
            r, "ch-swap-data-stale",
            f"🚨 <b>数据管道停滞</b>\n"
            f"ClickHouse 最新 swap 已 {ch_lag}s 未更新（阈值 {CH_SWAP_LAG_ALERT_SEC}s）。\n"
            f"信号桥将收不到新池→无信号→无成交。\n"
            f"V3 swap 唯一来源是 swap-poller（source=discovery_poller），"
            f"archiver 仅搬运不产数据。\n"
            f"swap-poller={swap_poller_status} archiver={archiver_status} "
            f"心跳年龄={arch_health_age}s ingestor心跳={ingest_age}s",
        )
    else:
        # Query succeeded AND lag is within threshold: the pipeline is truly fresh.
        # Recover both the query-failed and the data-stale incidents. On data-stale
        # recovery also clear the swap-poller/archiver "stuck-in-cooldown" pages so
        # the earlier "需人工介入" escalation is explicitly stood down.
        r.delete("infra_watchdog:fail_streak:ch-swap-lag-query-failed")
        _note_healthy(
            r, "ch-swap-lag-query-failed",
            "✅ <b>ClickHouse 新鲜度查询已恢复</b>\n可正常读取最新 swap 时间。",
        )
        _note_healthy(
            r, "ch-swap-data-stale",
            f"✅ <b>数据管道已恢复健康</b>\n"
            f"ClickHouse 最新 swap 落后仅 {ch_lag}s（阈值 {CH_SWAP_LAG_ALERT_SEC}s），"
            f"已连续 {RECOVERY_CONFIRM_CHECKS} 次检查正常。\n"
            f"此前的『数据管道停滞 / 需人工介入』告警可忽略，无需干预。",
            clear_extra=["swap-poller-stuck-in-cooldown", "archiver-stuck-in-cooldown"],
        )

    if arch_health_age is not None and arch_health_age > ARCHIVER_HEALTH_STALE_SEC:
        issues.append(f"archiver_heartbeat_stale={arch_health_age}s")
        _open_incident(r, "archiver-heartbeat-stale")
        _alert_once(
            r, "archiver-heartbeat-stale",
            f"⚠️ <b>Archiver 心跳过期</b>\n"
            f"archiver 心跳文件已 {arch_health_age}s 未更新（阈值 {ARCHIVER_HEALTH_STALE_SEC}s）。\n"
            f"archiver 可能已僵死/卡住，正在尝试自动重启。",
        )
    elif arch_health_age is not None:
        _note_healthy(
            r, "archiver-heartbeat-stale",
            f"✅ <b>Archiver 心跳已恢复</b>\n"
            f"archiver 心跳文件已重新刷新（当前 {arch_health_age}s，阈值 {ARCHIVER_HEALTH_STALE_SEC}s），"
            f"已连续 {RECOVERY_CONFIRM_CHECKS} 次正常，无需人工干预。",
        )

    # --- Targeted auto-remediation ------------------------------------------
    # Restart the service that is ACTUALLY at fault, not a scapegoat.
    #
    #   archiver_self_bad  → the archiver's own container/heartbeat is broken:
    #                        it can no longer move rows Redis→CH. Restart archiver.
    #   swaps_stale only   → archiver + ingestor are green but decoded swaps are
    #                        stale. V3 swaps are produced ONLY by the swap-poller
    #                        (source=discovery_poller); the archiver merely relays
    #                        already-received rows (~10s flush latency), so it can
    #                        NOT be the cause. Restarting it is a no-op that burns
    #                        the cooldown and escalates to a human — exactly the
    #                        2026-07-06 failure (3 archiver restarts, lag stuck
    #                        152→156→154s). Restart the real data source instead.
    ingestor_fresh = ingest_age is None or ingest_age <= INGESTOR_STALE_SEC
    swaps_stale = ch_lag is not None and ch_lag > CH_SWAP_LAG_ALERT_SEC
    archiver_self_bad = (
        archiver_status in ("unhealthy", "stopped", "missing")
        or (arch_health_age is not None and arch_health_age > ARCHIVER_HEALTH_STALE_SEC)
    )

    if archiver_self_bad:
        _maybe_restart_service(r, "archiver", archiver_reasons, AUTO_RESTART_ARCHIVER)
    elif swaps_stale:
        _maybe_restart_service(
            r, "swap-poller",
            [
                f"ch_swap_lag={ch_lag}s",
                f"archiver={archiver_status}/heartbeat_age={arch_health_age}s (ok)",
                f"ingestor心跳={ingest_age}s ({'fresh' if ingestor_fresh else 'stale'})",
            ],
            AUTO_RESTART_SWAP_POLLER,
        )

    # r3 shadow is not production trading, but a silent write stall hid a
    # 3-week empty confirmatory ledger. Page process death and WRITE_STALL.
    _r3_shadow_probe(r)

    # Heartbeat: record successful run (used for cron-gap detection above).
    r.set(LAST_RUN_KEY, str(now), ex=86400)

    if not issues:
        print("OK: all watched services healthy")
    else:
        print("ISSUES:", "; ".join(issues))
    return 0 if not issues else 2


if __name__ == "__main__":
    raise SystemExit(main())
