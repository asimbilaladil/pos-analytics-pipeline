#!/usr/bin/env bash
set -uo pipefail
P=/root/pos-analytics-pipeline
LOG=/var/log/laynes/backfill_jan3_$(date +%Y%m%dT%H%M%S).log
set -a; . $P/.env; set +a
cd $P || exit 1
log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# Weekly windows 2026-01-01 .. 2026-02-10. Each backfill_*_v2 call chunks
# internally by (establishment x calendar-month) bounded by --end, so a
# ~7-day --end makes each committed chunk ~1 week of one location instead
# of a whole month. A hang now costs <=1 location-week of re-fetch.
WINDOWS=(
  "2026-01-01 2026-01-08" "2026-01-08 2026-01-15" "2026-01-15 2026-01-22"
  "2026-01-22 2026-01-29" "2026-01-29 2026-02-05" "2026-02-05 2026-02-10"
)

run_win(){  # resource_name script start end
  local name="$1" script="$2" s="$3" e="$4" tries=0 max=8
  while (( tries < max )); do
    tries=$((tries+1))
    if timeout 1500 "$P/venv/bin/python" "$P/$script" --start "$s" --end "$e" >> "$LOG" 2>&1; then
      return 0
    fi
    log "!!! $name $s..$e attempt $tries failed/timed out"
    pkill -f "$script" 2>/dev/null; sleep 15
  done
  log "!!! $name $s..$e GAVE UP after $max"; return 1
}

for step in "order_items:backfill_order_items_v2.py" \
            "modifier_items:backfill_modifier_items_v2.py" \
            "order_history:backfill_order_history_v2.py"; do
  name=${step%%:*}; script=${step#*:}
  log "===== $name ====="
  for w in "${WINDOWS[@]}"; do
    set -- $w
    log ">>> $name  $1 .. $2"
    run_win "$name" "$script" "$1" "$2" && log "<<< $name $1..$2 ok" || log "!!! $name $1..$2 FAILED"
  done
done

log ">>> feature aggregation 2026-01-01 .. 2026-02-09"
"$P/venv/bin/python" "$P/aggregate_features_v2.py" --start 2026-01-01 --end 2026-02-09 >> "$LOG" 2>&1 \
  && log "<<< aggregation done" || log "!!! aggregation FAILED"
log ">>> weather"
"$P/venv/bin/python" -m weather_analysis.cli backfill --start 2026-01-01 >> "$LOG" 2>&1 \
  && log "<<< weather done" || log "!!! weather failed (non-fatal)"
log "===== January backfill (weekly) COMPLETE ====="
