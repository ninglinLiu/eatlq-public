#!/usr/bin/env bash
# LASZLO / EATLQ single-entrypoint operator CLI.
#
# Mirrors the laszlo.ps1 surface described in Home.md / Operations.md but
# implemented in POSIX bash for Linux VPS operators. Wraps docker compose and
# the weekly retraining cycle so operators only need to remember one command.
#
# Usage:
#   scripts/laszlo.sh up             Start the full stack (strategy + executor + dashboard)
#   scripts/laszlo.sh soak             Paper soak: core + strategy + dashboard (no executor)
#   scripts/laszlo.sh paper            Soak + executor in dry-run profile
#   scripts/laszlo.sh down             Stop and remove the stack
#   scripts/laszlo.sh restart          Restart all services
#   scripts/laszlo.sh status           Show docker compose ps + ingestion mode
#   scripts/laszlo.sh logs [svc]       Tail logs for all services or a specific one
#   scripts/laszlo.sh dashboard        Print dashboard URL (--open to launch browser)
#   scripts/laszlo.sh phase6           Run the weekly retraining cycle
#   scripts/laszlo.sh cron-audit       Diff live crontab vs infra/cron/crontab.sealed
#   scripts/laszlo.sh maintain         Prune dangling images + vacuum redis
#   scripts/laszlo.sh doctor [--compose-config]  Static preflight; optional docker compose config -q
#   scripts/laszlo.sh help             Show this help
#
# Environment overrides:
#   COMPOSE_FILE (default: infra/compose/compose.yaml)
#   ENV_FILE     (default: infra/compose/.env; auto-created from .env.example)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_DIR="${REPO_ROOT}/infra/compose"
COMPOSE_FILE="${COMPOSE_FILE:-${COMPOSE_DIR}/compose.yaml}"
ENV_FILE="${ENV_FILE:-${COMPOSE_DIR}/.env}"

# Default profiles for a full production stack.
PROFILES_FULL=(--profile strategy --profile executor --profile dashboard)
PROFILES_SOAK=(--profile strategy --profile dashboard)
PROFILES_PAPER=(--profile strategy --profile executor --profile dashboard)

ensure_env() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${COMPOSE_DIR}/.env.example" "${ENV_FILE}"
    echo "Created ${ENV_FILE} from .env.example -- edit EVM_WS_URL before running against a real provider."
  fi
}

ingestion_mode() {
  grep -E '^EATLQ_INGESTION_MODE=' "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2- || echo "unknown"
}

compose() {
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

compose_full() {
  compose "${PROFILES_FULL[@]}" "$@"
}

compose_soak() {
  compose "${PROFILES_SOAK[@]}" "$@"
}

compose_paper() {
  compose "${PROFILES_PAPER[@]}" "$@"
}

maybe_start_ingestion() {
  local mode
  mode="$(ingestion_mode | tr -d '[:space:]')"
  if [[ "${mode}" == "live" ]]; then
    echo "== ingestion mode=live — starting ingestor (profile ingestion) =="
    compose --profile ingestion up -d ingestor
  else
    echo "== ingestion mode=${mode:-sealed} — ingestor NOT started (opt-in: EATLQ_INGESTION_MODE=live) =="
  fi
}

cmd_up() {
  ensure_env
  compose_full up -d --build
  maybe_start_ingestion
}

cmd_soak() {
  ensure_env
  echo "== soak: strategy + dashboard only (no executor write path) =="
  compose_soak up -d --build
  maybe_start_ingestion
}

cmd_paper() {
  ensure_env
  echo "== paper: soak stack + executor profile (set EXECUTOR_DRY_RUN=1 in .env) =="
  compose_paper up -d --build
  maybe_start_ingestion
}

cmd_down() {
  compose "${PROFILES_FULL[@]}" --profile ingestion down --remove-orphans
}

cmd_restart() {
  ensure_env
  compose_full restart
}

cmd_status() {
  echo "== ingestion mode: $(ingestion_mode) =="
  compose_full ps
  echo
  echo "== optional ingestion profile =="
  compose --profile ingestion ps ingestor 2>/dev/null || true
}

cmd_logs() {
  if [[ $# -gt 0 ]]; then
    compose_full logs -f --tail=200 "$@"
  else
    compose_full logs -f --tail=200
  fi
}

cmd_dashboard() {
  ensure_env
  local open_browser=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --open|-o) open_browser=1; shift ;;
      *) echo "Unknown dashboard arg: $1" >&2; exit 2 ;;
    esac
  done
  local host port
  host="$(grep -E '^DASHBOARD_HOST_BIND=' "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2- || echo "127.0.0.1")"
  port="$(grep -E '^DASHBOARD_PORT=' "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2- || echo "8080")"
  host="${host:-127.0.0.1}"
  port="${port:-8080}"
  local url="http://${host}:${port}/"
  echo "Dashboard (authoritative ops surface): ${url}"
  echo "Open in browser: laszlo.sh dashboard --open"
  if [[ ${open_browser} -eq 1 ]]; then
    if command -v xdg-open >/dev/null 2>&1; then
      xdg-open "${url}" >/dev/null 2>&1 &
    elif command -v sensible-browser >/dev/null 2>&1; then
      sensible-browser "${url}" >/dev/null 2>&1 &
    else
      echo "No browser opener found (xdg-open / sensible-browser)." >&2
    fi
  fi
  # Ensure dashboard container is up when operator asks for URL.
  compose "${PROFILES_SOAK[@]}" up -d dashboard 2>/dev/null || true
}

cmd_cron_audit() {
  local guard="${REPO_ROOT}/scripts/maintenance/cron_guard.sh"
  if [[ ! -x "${guard}" ]]; then
    chmod +x "${guard}"
  fi
  "${guard}"
}

cmd_phase6() {
  local cycle="${REPO_ROOT}/scripts/phase6_weekly_cycle.sh"
  if [[ ! -x "${cycle}" ]]; then
    echo "ERROR: ${cycle} not found or not executable" >&2
    exit 1
  fi
  "${cycle}" "$@"
}

cmd_maintain() {
  echo "== docker image prune =="
  docker image prune -f || true
  echo "== redis MEMORY PURGE =="
  compose_full exec -T redis redis-cli MEMORY PURGE || true
  echo "== redis INFO keyspace =="
  compose_full exec -T redis redis-cli INFO keyspace || true
}

cmd_doctor() {
  python3 "${REPO_ROOT}/scripts/paper_dryrun_readiness.py" "$@"
}

cmd_help() {
  sed -n '2,30p' "$0"
}

main() {
  local sub="${1:-help}"
  shift || true
  case "${sub}" in
    up)           cmd_up "$@" ;;
    soak)         cmd_soak "$@" ;;
    paper)        cmd_paper "$@" ;;
    down)         cmd_down "$@" ;;
    restart)      cmd_restart "$@" ;;
    status|ps)    cmd_status "$@" ;;
    logs)         cmd_logs "$@" ;;
    dashboard)    cmd_dashboard "$@" ;;
    cron-audit)   cmd_cron_audit "$@" ;;
    phase6)       cmd_phase6 "$@" ;;
    maintain)     cmd_maintain "$@" ;;
    doctor)       cmd_doctor "$@" ;;
    help|-h|--help) cmd_help ;;
    *)
      echo "Unknown subcommand: ${sub}" >&2
      cmd_help
      exit 2
      ;;
  esac
}

main "$@"
