#!/usr/bin/env bash
set -uo pipefail
P=/root/pos-analytics-pipeline
LOG=/var/log/laynes/backfill_jan2_$(date +%Y%m%dT%H%M%S).log
S=2026-01-01
E=2026-02-10
set -a; . $P/.env; set +a
cd $P || exit 1
log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# resilient runner: retry on non-zero exit OR hang (per-attempt timeout).
# every backfill_*_v2 chunk is checkpointed, so each retry makes forward progress.
run_resilient(){
  local name="$1" script="$2" tries=0 max=12
  while (( tries < max )); do
    tries=$((tries+1))
    log ">>> $name attempt $tries/$max"
    if timeout 3600 "$P/venv/bin/python" "$P/$script" --start "$S" --end "$E" >> "$LOG" 2>&1; then
      log "<<< $name completed cleanly"
      return 0
    fi
    log "!!! $name attempt $tries exited non-zero/timed out — checking if chunks remain"
    # if no failed/in_progress rows remain for this resource, treat as done
    local remain
    remain=$(sudo -u postgres psql -d laynes -tAc \
      "select count(*) from backfill_progress where resource like '${script%.py}%' and status <> 'success'" 2>/dev/null || echo 1)
    remain=$(echo "$remain" | tr -d '[:space:]')
    if [[ "$remain" == "0" ]]; then
      log "<<< $name — no non-success chunks remain, moving on"
      return 0
    fi
    log "    $remain chunk(s) still not success; retrying in 20s"
    pkill -f "$script" 2>/dev/null; sleep 20
  done
  log "!!! $name gave up after $max attempts"
  return 1
}

log "=== January backfill (resume) $S .. $E ==="
run_resilient order_items    backfill_order_items_v2.py
run_resilient modifier_items backfill_modifier_items_v2.py
run_resilient order_history  backfill_order_history_v2.py

log ">>> feature aggregation $S .. 2026-02-09"
"$P/venv/bin/python" "$P/aggregate_features_v2.py" --start "$S" --end 2026-02-09 >> "$LOG" 2>&1 \
  && log "<<< aggregation done" || log "!!! aggregation FAILED"

log ">>> weather backfill"
"$P/venv/bin/python" -m weather_analysis.cli backfill --start "$S" >> "$LOG" 2>&1 \
  && log "<<< weather done" || log "!!! weather backfill failed (non-fatal)"

log "=== January backfill (resume) complete ==="
