#!/usr/bin/env bash
# ==============================================================================
# run_live_pipeline.sh — EATLQ Live-Forward Dry-Run Pipeline Manager
# ==============================================================================
# Manages three long-running processes with flock, heartbeat, and auto-restart:
#   1. sprint083_realtime_sender_enricher  (every 2 min, enriches CSV)
#   2. sprint082_signal_bridge            (watch mode, publishes to Redis)
#   3. sprint083_accumulation_monitor     (every 5 min, writes n_accounting)
#
# Usage:
#   ./run_live_pipeline.sh [start|stop|status|restart|monitor|enricher|bridge]
#
# Safety: Aborts if LIVE_TRADING_MODE=true.
# ==============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$REPO/scripts"
LOGS="$REPO/logs/pipeline"
LOCK_DIR="/tmp/eatlq_locks"
HEARTBEAT_DIR="/tmp"

export REDIS_URL="${REDIS_URL:-redis://172.18.0.10:6379/0}"
export LIVE_TRADING_MODE="${LIVE_TRADING_MODE:-false}"
export SPRINT055_CSV="${SPRINT055_CSV:-$REPO/logs/sprint055_paper_shadow.csv}"

# ── Safety check ────────────────────────────────────────────────────────────
if [[ "$LIVE_TRADING_MODE" == "true" || "$LIVE_TRADING_MODE" == "1" ]]; then
    echo "[ABORT] LIVE_TRADING_MODE=true is forbidden in this pipeline manager."
    exit 1
fi

mkdir -p "$LOGS" "$LOCK_DIR"

# ── Component configs ────────────────────────────────────────────────────────
ENRICHER_LOCK="$LOCK_DIR/sprint083_enricher.lock"
BRIDGE_LOCK="$LOCK_DIR/sprint082_bridge.lock"
MONITOR_LOCK="$LOCK_DIR/sprint083_monitor.lock"

ENRICHER_HB="$HEARTBEAT_DIR/sprint083_enricher.heartbeat"
BRIDGE_HB="$HEARTBEAT_DIR/sprint082_bridge.heartbeat"

ENRICHER_LOG="$LOGS/enricher.log"
BRIDGE_LOG="$LOGS/bridge.log"
MONITOR_LOG="$LOGS/monitor.log"

# Stale threshold in seconds
STALE_BRIDGE=600
STALE_ENRICHER=300

# ── Helpers ──────────────────────────────────────────────────────────────────
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

hb_age() {
    local hb_file="$1"
    if [[ ! -f "$hb_file" ]]; then echo "∞"; return; fi
    echo $(( $(date +%s) - $(cat "$hb_file") ))
}

is_stale() {
    local hb_file="$1" threshold="$2"
    local age; age=$(hb_age "$hb_file")
    [[ "$age" == "∞" ]] && return 0
    (( age > threshold ))
}

start_enricher() {
    log "Starting realtime sender enricher..."
    flock -n "$ENRICHER_LOCK" -c "
        python3 $SCRIPTS/sprint083_realtime_sender_enricher.py --watch \
            >> $ENRICHER_LOG 2>&1
    " &
    echo $! > "$LOCK_DIR/enricher.pid"
    log "Enricher started (pid=$!)"
}

start_bridge() {
    log "Starting signal bridge (watch mode)..."
    flock -n "$BRIDGE_LOCK" -c "
        REDIS_URL=$REDIS_URL SPRINT055_CSV=$SPRINT055_CSV \
        python3 $SCRIPTS/sprint082_signal_bridge.py --watch \
            >> $BRIDGE_LOG 2>&1
    " &
    echo $! > "$LOCK_DIR/bridge.pid"
    log "Bridge started (pid=$!)"
}

start_monitor_cron() {
    # Installs a 5-minute cron for accumulation monitor if not already present
    CRON_CMD="*/5 * * * * REDIS_URL=$REDIS_URL flock -n $MONITOR_LOCK python3 $SCRIPTS/sprint083_accumulation_monitor.py --once >> $MONITOR_LOG 2>&1"
    if crontab -l 2>/dev/null | grep -qF "sprint083_accumulation_monitor"; then
        log "Monitor cron already installed."
    else
        (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
        log "Monitor cron installed."
    fi
}

install_restart_cron() {
    # Watchdog cron: check heartbeats every 10 min, restart if stale
    WATCHDOG_CMD="*/10 * * * * $SCRIPTS/run_live_pipeline.sh watchdog >> $LOGS/watchdog.log 2>&1"
    if crontab -l 2>/dev/null | grep -qF "run_live_pipeline.sh watchdog"; then
        log "Watchdog cron already installed."
    else
        (crontab -l 2>/dev/null; echo "$WATCHDOG_CMD") | crontab -
        log "Watchdog cron installed."
    fi
}

watchdog() {
    log "=== Watchdog check ==="

    # Bridge heartbeat check
    if is_stale "$BRIDGE_HB" "$STALE_BRIDGE"; then
        log "WARN: bridge heartbeat stale (age=$(hb_age "$BRIDGE_HB")s > ${STALE_BRIDGE}s)"
        if flock -n "$BRIDGE_LOCK" true 2>/dev/null; then
            log "Bridge lock is free — restarting bridge..."
            start_bridge
        else
            log "Bridge lock held — process may be running slowly (not restarting)"
        fi
    else
        log "Bridge heartbeat OK (age=$(hb_age "$BRIDGE_HB")s)"
    fi

    # Enricher heartbeat check
    if is_stale "$ENRICHER_HB" "$STALE_ENRICHER"; then
        log "WARN: enricher heartbeat stale (age=$(hb_age "$ENRICHER_HB")s > ${STALE_ENRICHER}s)"
        if flock -n "$ENRICHER_LOCK" true 2>/dev/null; then
            log "Enricher lock is free — restarting enricher..."
            start_enricher
        else
            log "Enricher lock held — may be running slowly"
        fi
    else
        log "Enricher heartbeat OK (age=$(hb_age "$ENRICHER_HB")s)"
    fi

    log "=== Watchdog done ==="
}

show_status() {
    log "=== Pipeline Status ==="
    echo "Bridge  heartbeat: $(hb_age "$BRIDGE_HB")s ago (stale if > ${STALE_BRIDGE}s)"
    echo "Enricher heartbeat: $(hb_age "$ENRICHER_HB")s ago (stale if > ${STALE_ENRICHER}s)"
    echo ""

    # Show running processes
    echo "Running processes:"
    pgrep -a python3 | grep -E "sprint08[23]" || echo "  (none found)"
    echo ""

    # Quick Redis check
    echo "Redis signals lag:"
    redis-cli -u "$REDIS_URL" xinfo groups signals 2>/dev/null | grep -A1 lag || echo "  (unavailable)"

    echo "Latest bridge log:"
    tail -5 "$BRIDGE_LOG" 2>/dev/null || echo "  (no log yet)"
    echo ""
    echo "Latest enricher log:"
    tail -5 "$ENRICHER_LOG" 2>/dev/null || echo "  (no log yet)"
}

start_all() {
    log "Starting full live-forward dry-run pipeline..."
    # Start enricher first so bridge has enriched rows to pick up
    start_enricher
    sleep 3
    start_bridge
    start_monitor_cron
    install_restart_cron
    log "Pipeline started. Use './run_live_pipeline.sh status' to monitor."
}

stop_all() {
    log "Stopping pipeline..."
    for pidfile in "$LOCK_DIR/enricher.pid" "$LOCK_DIR/bridge.pid"; do
        if [[ -f "$pidfile" ]]; then
            pid=$(cat "$pidfile" 2>/dev/null)
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                kill "$pid" && log "Killed pid=$pid"
            fi
            rm -f "$pidfile"
        fi
    done
    # Kill by name as fallback
    pkill -f "sprint082_signal_bridge.py --watch" 2>/dev/null || true
    pkill -f "sprint083_realtime_sender_enricher.py --watch" 2>/dev/null || true
    log "Pipeline stopped."
}

# ── Main ──────────────────────────────────────────────────────────────────────
CMD="${1:-help}"

case "$CMD" in
    start)    start_all ;;
    stop)     stop_all ;;
    restart)  stop_all; sleep 2; start_all ;;
    status)   show_status ;;
    watchdog) watchdog ;;
    monitor)  python3 "$SCRIPTS/sprint083_accumulation_monitor.py" --once ;;
    enricher) python3 "$SCRIPTS/sprint083_realtime_sender_enricher.py" --once ;;
    bridge)   REDIS_URL="$REDIS_URL" SPRINT055_CSV="$SPRINT055_CSV" \
              python3 "$SCRIPTS/sprint082_signal_bridge.py" --once ;;
    help|*)
        echo "Usage: $0 [start|stop|restart|status|watchdog|monitor|enricher|bridge]"
        echo "  start    — start enricher + bridge + cron monitor + watchdog"
        echo "  stop     — stop enricher and bridge"
        echo "  restart  — stop + start"
        echo "  status   — show heartbeats and process list"
        echo "  watchdog — run heartbeat check (normally called by cron)"
        echo "  monitor  — run accumulation monitor once"
        echo "  enricher — run sender enricher once"
        echo "  bridge   — run signal bridge once"
        ;;
esac
