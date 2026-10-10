#!/usr/bin/env bash
# Compare live crontab against infra/cron/crontab.sealed allowlist.
# Exit 0 when every active live line is in the sealed baseline; non-zero on drift.
#
# Usage:
#   scripts/maintenance/cron_guard.sh [--report PATH]
#
# Active lines in crontab.sealed are non-comment cron entries (schedule + command).
# Comment-only / DISABLED lines in the sealed file are NOT part of the allowlist.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SEALED="${REPO_ROOT}/infra/cron/crontab.sealed"
REPORT="${REPO_ROOT}/mvp/diagnostics/cron_drift.log"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report)
      REPORT="$2"
      shift 2
      ;;
    --sealed)
      SEALED="$2"
      shift 2
      ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

normalize_line() {
  # Collapse whitespace; strip trailing comments after command portion is hard —
  # we compare full line trimmed.
  echo "$1" | sed 's/[[:space:]]\+/ /g' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

extract_active_cron_lines() {
  local file="$1"
  if [[ ! -f "${file}" ]]; then
    return
  fi
  while IFS= read -r line || [[ -n "${line}" ]]; do
    local trimmed
    trimmed="$(echo "${line}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -z "${trimmed}" ]] && continue
    [[ "${trimmed}" =~ ^# ]] && continue
    normalize_line "${trimmed}"
  done < "${file}"
}

if [[ ! -f "${SEALED}" ]]; then
  echo "cron_guard: FAIL — sealed baseline missing: ${SEALED}" >&2
  exit 1
fi

mapfile -t ALLOWED < <(extract_active_cron_lines "${SEALED}" | sort -u)

LIVE_TMP="$(mktemp)"
trap 'rm -f "${LIVE_TMP}"' EXIT

if crontab -l 2>/dev/null > "${LIVE_TMP}"; then
  :
else
  # No crontab for this user — only OK if allowlist is empty.
  if [[ ${#ALLOWED[@]} -eq 0 ]]; then
    echo "cron_guard: OK — no live crontab and sealed allowlist is empty"
    exit 0
  fi
  echo "cron_guard: FAIL — live crontab empty but sealed allowlist has ${#ALLOWED[@]} entries" >&2
  mkdir -p "$(dirname "${REPORT}")"
  {
    echo "=== cron_guard $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    echo "FAIL: live crontab empty; expected allowlist entries:"
    printf '  %s\n' "${ALLOWED[@]}"
  } >> "${REPORT}"
  exit 1
fi

mapfile -t LIVE < <(extract_active_cron_lines "${LIVE_TMP}" | sort -u)

DRIFT=()
for live_line in "${LIVE[@]}"; do
  [[ -z "${live_line}" ]] && continue
  found=0
  for allowed in "${ALLOWED[@]}"; do
    if [[ "${live_line}" == "${allowed}" ]]; then
      found=1
      break
    fi
  done
  if [[ ${found} -eq 0 ]]; then
    DRIFT+=("${live_line}")
  fi
done

MISSING=()
for allowed in "${ALLOWED[@]}"; do
  [[ -z "${allowed}" ]] && continue
  found=0
  for live_line in "${LIVE[@]}"; do
    if [[ "${live_line}" == "${allowed}" ]]; then
      found=1
      break
    fi
  done
  if [[ ${found} -eq 0 ]]; then
    MISSING+=("${allowed}")
  fi
done

mkdir -p "$(dirname "${REPORT}")"
{
  echo "=== cron_guard $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo "live_active=${#LIVE[@]} allowed=${#ALLOWED[@]} drift=${#DRIFT[@]} missing=${#MISSING[@]}"
  if [[ ${#DRIFT[@]} -gt 0 ]]; then
    echo "--- UNAUTHORIZED (in live, not in sealed) ---"
    printf '%s\n' "${DRIFT[@]}"
  fi
  if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "--- MISSING (in sealed, not in live) ---"
    printf '%s\n' "${MISSING[@]}"
  fi
} >> "${REPORT}"

if [[ ${#DRIFT[@]} -gt 0 ]]; then
  echo "cron_guard: FAIL — ${#DRIFT[@]} unauthorized cron line(s)" >&2
  printf '  + %s\n' "${DRIFT[@]}" >&2
  echo "  report: ${REPORT}" >&2
  exit 1
fi

echo "cron_guard: OK — live crontab matches sealed allowlist (${#LIVE[@]} active lines)"
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "cron_guard: note — ${#MISSING[@]} sealed line(s) not installed (informational only)" >&2
fi
exit 0
