#!/usr/bin/env bash
# Production-adjacent capacity observer — FAIL CLOSED by default.
# Ambient ENABLE_*/ALLOW_* environment variables are ignored.
# Requires BOTH:
#   1) CLI flag: --authorize-redis-health-observe
#   2) token file: $ROOT/var/auth_tokens/redis_health_observe.authorized
#      containing exactly: AUTHORIZED:redis_health_observe
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AUTHORIZE=0
ARGS=()
for arg in "$@"; do
  if [[ "$arg" == "--authorize-redis-health-observe" ]]; then
    AUTHORIZE=1
  else
    ARGS+=("$arg")
  fi
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

if [[ "$AUTHORIZE" -ne 1 ]]; then
  echo "DENIED: missing --authorize-redis-health-observe (default fail-closed; no Redis/network connect)" >&2
  exit 3
fi
CAPABILITY_DIR="$ROOT/var/auth_tokens"
CAPABILITY_FILE="$CAPABILITY_DIR/redis_health_observe.authorized"
# Reject symlink token, symlink token dir, or any symlink ancestor inside workspace.
_check="$CAPABILITY_FILE"
while [[ "$_check" != "/" && "$_check" != "$ROOT" ]]; do
  if [[ -L "$_check" ]]; then
    echo "DENIED: symlink in capability path: $_check" >&2
    exit 3
  fi
  _check="$(dirname "$_check")"
done
if [[ -L "$ROOT" ]]; then
  echo "DENIED: workspace root must not be a symlink" >&2
  exit 3
fi
if [[ ! -f "$CAPABILITY_FILE" || -L "$CAPABILITY_FILE" ]]; then
  echo "DENIED: missing regular capability token $CAPABILITY_FILE" >&2
  exit 3
fi
EXPECTED='AUTHORIZED:redis_health_observe'
GOT="$(tr -d '\n' < "$CAPABILITY_FILE")"
if [[ "$GOT" != "$EXPECTED" ]]; then
  echo "DENIED: invalid capability token contents" >&2
  exit 3
fi
# Explicitly ignore ambient env attempts
: "${ENABLE_REDIS_HEALTH_OBSERVE:=}"
: "${ALLOW_REDIS_HEALTH_OBSERVE:=}"


out_dir=${1:?output directory required}
duration_sec=${2:-595}
interval_sec=5
sample_file="$out_dir/capacity_samples.tsv"
pidstat_file="$out_dir/pidstat_samples.txt"
redis_file="$out_dir/executor_consumer_samples.txt"
event_file="$out_dir/guard_events.txt"

mkdir -p "$out_dir"
: > "$sample_file"
: > "$pidstat_file"
: > "$redis_file"
: > "$event_file"

ulimit -v 393216

read_cpu() {
  read -r _ cpu_user cpu_nice cpu_system cpu_idle cpu_iowait cpu_irq cpu_softirq cpu_steal _ < /proc/stat
  cpu_total=$((cpu_user + cpu_nice + cpu_system + cpu_idle + cpu_iowait + cpu_irq + cpu_softirq + cpu_steal))
}

read_memory() {
  read -r mem_available_kb swap_total_kb swap_free_kb < <(
    awk '
      /^MemAvailable:/ {m=$2}
      /^SwapTotal:/ {st=$2}
      /^SwapFree:/ {sf=$2}
      END {print m, st, sf}
    ' /proc/meminfo
  )
  swap_used_kb=$((swap_total_kb - swap_free_kb))
}

read_disk() {
  read -r sectors_read sectors_written io_ms < <(
    awk '$3=="sda" {print $6, $10, $13}' /proc/diskstats
  )
}

read_vm() {
  read -r pswpin pswpout pgmajfault < <(
    awk '
      $1=="pswpin" {si=$2}
      $1=="pswpout" {so=$2}
      $1=="pgmajfault" {pf=$2}
      END {print si+0, so+0, pf+0}
    ' /proc/vmstat
  )
}

health_mtime() {
  stat -c %Y "$1" 2>/dev/null || printf '0\n'
}

sample_executor_consumer() {
  local line
  if ! exec 3<>/dev/tcp/172.18.0.2/6379; then
    printf 'redis_connect_failed\n'
    return 0
  fi
  printf '*4\r\n$5\r\nXINFO\r\n$9\r\nCONSUMERS\r\n$7\r\nsignals\r\n$15\r\nexecution_group\r\n' >&3
  while IFS= read -r -t 1 line <&3; do
    printf '%s\n' "${line%$'\r'}"
  done
  exec 3<&-
  exec 3>&-
}

printf 'timestamp\telapsed_sec\tcpu_idle_bp\tcpu_iowait_bp\tmem_available_kb\tswap_used_kb\tpswpin_delta\tpswpout_delta\tpgmajfault_delta\tsectors_read_delta\tsectors_written_delta\tio_ms_delta\tingestor_health_age_sec\tarchiver_health_age_sec\n' > "$sample_file"

start_epoch=$(date +%s)
end_epoch=$((start_epoch + duration_sec))
read_cpu
prev_total=$cpu_total
prev_idle=$cpu_idle
prev_iowait=$cpu_iowait
read_memory
initial_swap_used_kb=$swap_used_kb
read_disk
prev_sectors_read=$sectors_read
prev_sectors_written=$sectors_written
prev_io_ms=$io_ms
read_vm
prev_pswpin=$pswpin
prev_pswpout=$pswpout
prev_pgmajfault=$pgmajfault
low_idle_streak=0
high_iowait_streak=0
missing_health_streak=0
sample_n=0

printf 'monitor_start=%s duration_sec=%s initial_swap_used_kb=%s\n' "$(date -u +%FT%TZ)" "$duration_sec" "$initial_swap_used_kb"

while :; do
  target_epoch=$((start_epoch + (sample_n + 1) * interval_sec))
  now_epoch=$(date +%s)
  if (( target_epoch > end_epoch )); then
    break
  fi
  if (( target_epoch > now_epoch )); then
    sleep $((target_epoch - now_epoch))
  fi

  sample_n=$((sample_n + 1))
  now_epoch=$(date +%s)
  read_cpu
  delta_total=$((cpu_total - prev_total))
  delta_idle=$((cpu_idle - prev_idle))
  delta_iowait=$((cpu_iowait - prev_iowait))
  if (( delta_total > 0 )); then
    idle_bp=$((10000 * delta_idle / delta_total))
    iowait_bp=$((10000 * delta_iowait / delta_total))
  else
    idle_bp=0
    iowait_bp=0
  fi
  prev_total=$cpu_total
  prev_idle=$cpu_idle
  prev_iowait=$cpu_iowait

  read_memory
  read_disk
  read_vm
  delta_read=$((sectors_read - prev_sectors_read))
  delta_written=$((sectors_written - prev_sectors_written))
  delta_io_ms=$((io_ms - prev_io_ms))
  delta_pswpin=$((pswpin - prev_pswpin))
  delta_pswpout=$((pswpout - prev_pswpout))
  delta_pgmajfault=$((pgmajfault - prev_pgmajfault))
  prev_sectors_read=$sectors_read
  prev_sectors_written=$sectors_written
  prev_io_ms=$io_ms
  prev_pswpin=$pswpin
  prev_pswpout=$pswpout
  prev_pgmajfault=$pgmajfault

  ingestor_mtime=$(health_mtime /proc/1139890/root/tmp/ingestor_healthy)
  archiver_mtime=$(health_mtime /proc/2600375/root/tmp/archiver_healthy)
  ingestor_age=$((ingestor_mtime > 0 ? now_epoch - ingestor_mtime : -1))
  archiver_age=$((archiver_mtime > 0 ? now_epoch - archiver_mtime : -1))

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$(date -u +%FT%TZ)" "$((now_epoch - start_epoch))" "$idle_bp" "$iowait_bp" \
    "$mem_available_kb" "$swap_used_kb" "$delta_pswpin" "$delta_pswpout" \
    "$delta_pgmajfault" "$delta_read" "$delta_written" "$delta_io_ms" \
    "$ingestor_age" "$archiver_age" >> "$sample_file"

  if (( idle_bp < 1000 )); then
    low_idle_streak=$((low_idle_streak + 1))
  else
    low_idle_streak=0
  fi
  if (( iowait_bp >= 1000 )); then
    high_iowait_streak=$((high_iowait_streak + 1))
  else
    high_iowait_streak=0
  fi
  if (( ingestor_age < 0 || ingestor_age > 120 || archiver_age < 0 || archiver_age > 120 )); then
    missing_health_streak=$((missing_health_streak + 1))
  else
    missing_health_streak=0
  fi

  if (( swap_used_kb > initial_swap_used_kb )); then
    printf '%s GUARD_SWAP_GROWTH initial_kb=%s current_kb=%s\n' "$(date -u +%FT%TZ)" "$initial_swap_used_kb" "$swap_used_kb" | tee -a "$event_file"
    break
  fi
  if (( low_idle_streak >= 6 )); then
    printf '%s GUARD_CPU_IDLE_LOW_30S idle_bp=%s\n' "$(date -u +%FT%TZ)" "$idle_bp" | tee -a "$event_file"
    break
  fi
  if (( high_iowait_streak >= 6 )); then
    printf '%s GUARD_IOWAIT_HIGH_30S iowait_bp=%s\n' "$(date -u +%FT%TZ)" "$iowait_bp" | tee -a "$event_file"
    break
  fi
  if (( missing_health_streak >= 6 )); then
    printf '%s GUARD_MARKET_DATA_HEALTH_STALE ingestor_age=%s archiver_age=%s\n' "$(date -u +%FT%TZ)" "$ingestor_age" "$archiver_age" | tee -a "$event_file"
    break
  fi

  if (( sample_n % 6 == 0 )); then
    printf '\n### %s\n' "$(date -u +%FT%TZ)" >> "$pidstat_file"
    pidstat -urd -p ALL 1 1 >> "$pidstat_file" 2>&1 || true
    printf '\n### %s\n' "$(date -u +%FT%TZ)" >> "$redis_file"
    sample_executor_consumer >> "$redis_file" 2>&1 || true
    printf 'progress elapsed_sec=%s idle_bp=%s iowait_bp=%s swap_used_kb=%s ingestor_age=%s archiver_age=%s\n' \
      "$((now_epoch - start_epoch))" "$idle_bp" "$iowait_bp" "$swap_used_kb" "$ingestor_age" "$archiver_age"
  fi
done

finish_epoch=$(date +%s)
if [[ ! -s "$event_file" ]]; then
  printf '%s GUARD_NOT_TRIGGERED\n' "$(date -u +%FT%TZ)" >> "$event_file"
fi
printf 'monitor_end=%s elapsed_sec=%s samples=%s\n' "$(date -u +%FT%TZ)" "$((finish_epoch - start_epoch))" "$sample_n"
