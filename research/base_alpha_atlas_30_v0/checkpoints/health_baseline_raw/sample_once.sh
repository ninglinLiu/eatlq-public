#!/bin/bash
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# CPU idle from /proc/stat delta
read -r _ u1 n1 s1 i1 w1 irq1 sirq1 st1 _ < /proc/stat
sleep 1
read -r _ u2 n2 s2 i2 w2 irq2 sirq2 st2 _ < /proc/stat
total=$(( (u2+n2+s2+i2+w2+irq2+sirq2+st2) - (u1+n1+s1+i1+w1+irq1+sirq1+st1) ))
idle=$(( i2 - i1 ))
iowait=$(( w2 - w1 ))
idle_pct=$(awk -v i=$idle -v t=$total 'BEGIN{ if(t>0) printf "%.2f", 100*i/t; else print "na"}')
iowait_pct=$(awk -v i=$iowait -v t=$total 'BEGIN{ if(t>0) printf "%.2f", 100*i/t; else print "na"}')
mem_avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
swap_used=$(free -b | awk '/Swap:/{print $3}')
disk_avail_k=$(df -Pk / | awk 'NR==2{print $4}')
load=$(awk '{print $1","$2","$3}' /proc/loadavg)
# docker restart counts + status for protected containers
docker_line=$(docker ps -a --filter name=eatlq- --format '{{.Names}}|{{.Status}}|{{.RunningFor}}' 2>/dev/null | tr '\n' ';')
# redis stream pending/lag (read-only)
mt_info=$(docker exec eatlq-redis-1 redis-cli XINFO GROUPS market_ticks 2>/dev/null | awk '/^name$/{getline n} /^pending$/{getline p} /^lag$/{getline l; printf "%s:p=%s:lag=%s ", n,p,l}')
sig_info=$(docker exec eatlq-redis-1 redis-cli XINFO GROUPS signals 2>/dev/null | awk '/^name$/{getline n} /^pending$/{getline p} /^lag$/{getline l; printf "%s:p=%s:lag=%s ", n,p,l}')
# CH query count (read-only)
ch_q=$(docker exec eatlq-clickhouse-1 clickhouse-client --query "SELECT count() FROM system.processes WHERE is_cancelled=0" 2>/dev/null || echo na)
ch_cpu=$(docker stats --no-stream --format '{{.CPUPerc}}' eatlq-clickhouse-1 2>/dev/null | tr -d '%')
ex_cpu=$(docker stats --no-stream --format '{{.CPUPerc}}' eatlq-executor-1 2>/dev/null | tr -d '%')
ex_status=$(docker inspect -f '{{.State.Status}}|{{.RestartCount}}|{{.State.StartedAt}}' eatlq-executor-1 2>/dev/null)
sb_status=$(docker inspect -f '{{.State.Status}}|{{.RestartCount}}|{{.State.StartedAt}}' eatlq-signal-bridge-1 2>/dev/null)
ing_status=$(docker inspect -f '{{.State.Status}}|{{.RestartCount}}|{{.State.StartedAt}}' eatlq-ingestor-1 2>/dev/null)
panic=$(docker exec eatlq-redis-1 redis-cli EXISTS SYSTEM_PANIC 2>/dev/null)
# market_ticks_discovery tip age
tip=$(docker exec eatlq-redis-1 redis-cli XREVRANGE market_ticks_discovery + - COUNT 1 2>/dev/null | head -1)
tip_ms=${tip%-*}
now_ms=$(date +%s%3N)
if [[ "$tip_ms" =~ ^[0-9]+$ ]]; then tip_age=$(( (now_ms - tip_ms)/1000 )); else tip_age=na; fi
echo "$ts idle=$idle_pct iowait=$iowait_pct mem_avail_kb=$mem_avail swap_used_b=$swap_used disk_avail_k=$disk_avail_k load=$load ch_queries=$ch_q ch_cpu=$ch_cpu ex_cpu=$ex_cpu ex=$ex_status sb=$sb_status ing=$ing_status panic_exists=$panic disc_tip_age_s=$tip_age sig=[$sig_info] mt=[$mt_info]"
