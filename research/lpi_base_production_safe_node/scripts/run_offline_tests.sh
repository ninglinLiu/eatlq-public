#!/usr/bin/env bash
# Single-process, single-thread, low-priority offline test runner.
# Does NOT reset HOME. Writes to a new timestamped, non-overwritable directory.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${ROOT}/runtime/venv"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${ROOT}/reports/raw/offline_tests_${TS}"

# Fail if directory already exists (never overwrite sealed evidence).
if [[ -e "${OUT_DIR}" ]]; then
  echo "ERROR: output dir already exists: ${OUT_DIR}" >&2
  exit 1
fi
mkdir -p "${OUT_DIR}" "${ROOT}/var/cache" "${ROOT}/var/tmp"

SOURCE_COMMIT="$(git -C "${ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
CREATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "${OUT_DIR}/run_metadata.json" <<EOF
{
  "round_id": "offline_unittest_run",
  "artifact_class": "OFFLINE_UNITTEST_RUN",
  "source_commit": "${SOURCE_COMMIT}",
  "created_at": "${CREATED_AT}"
}
EOF

export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="${ROOT}/tests/offline_guard:${ROOT}"
export TMPDIR="${ROOT}/var/tmp"
export XDG_CACHE_HOME="${ROOT}/var/cache"
export XDG_RUNTIME_DIR="${ROOT}/var/tmp"
# Intentionally do NOT set HOME.
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

CMD=( "${VENV}/bin/python" -m unittest discover -s "${ROOT}/tests" -p 'test_*.py' -v )

COMBINED="${OUT_DIR}/combined.txt"
SUMMARY="${OUT_DIR}/summary.txt"

set +e
/usr/bin/time -v \
  nice -n 19 \
  ionice -c 3 \
  env \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="${ROOT}/tests/offline_guard:${ROOT}" \
    TMPDIR="${ROOT}/var/tmp" \
    XDG_CACHE_HOME="${ROOT}/var/cache" \
    XDG_RUNTIME_DIR="${ROOT}/var/tmp" \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
  "${CMD[@]}" > "${OUT_DIR}/unittest_stdout.txt" 2> "${OUT_DIR}/unittest_stderr_time.txt"
rc=$?
set -e

{
  echo "start_note=see_file_mtimes"
  echo "out_dir=${OUT_DIR}"
  echo "unittest_exit=${rc}"
  echo "home_was_reset=false"
  echo "end_utc=$(date -u +%FT%TZ)"
} > "${SUMMARY}"

cat "${OUT_DIR}/unittest_stdout.txt" "${OUT_DIR}/unittest_stderr_time.txt" > "${COMBINED}"

# Pointer file for operators (does not overwrite historical sealed evidence names).
echo "${OUT_DIR}" > "${ROOT}/reports/raw/LATEST_OFFLINE_TEST_DIR.txt"

tail -n 50 "${COMBINED}" || true
echo "unittest_exit=${rc}"
echo "out_dir=${OUT_DIR}"

MAX_RSS_KB="$(awk -F: '/Maximum resident set size/{gsub(/ /,"",$2); print $2}' "${OUT_DIR}/unittest_stderr_time.txt" || true)"
if [[ -n "${MAX_RSS_KB}" && "${MAX_RSS_KB}" -gt 393216 ]]; then
  echo "ERROR: max RSS ${MAX_RSS_KB} KiB exceeds 384 MiB" >&2
  exit 1
fi
exit "${rc}"
