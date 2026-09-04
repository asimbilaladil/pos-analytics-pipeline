#!/usr/bin/env bash
set -uo pipefail
P=/root/pos-analytics-pipeline
LOG=/var/log/laynes/backfill_jan_$(date +%Y%m%dT%H%M%S).log
S=2026-01-01
E=2026-02-10   # exclusive; existing data starts 2026-02-10
set -a; . $P/.env; set +a
cd $P || exit 1
log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

log "=== January backfill $S .. $E (exclusive) — all establishments ==="
for step in \
  "orders:backfill_orders_v2.py" \
  "payments:backfill_payments_v2.py" \
  "order_items:backfill_order_items_v2.py" \
  "modifier_items:backfill_modifier_items_v2.py" \
  "order_history:backfill_order_history_v2.py" ; do
    name=${step%%:*}; script=${step#*:}
    log ">>> $name ($script)"
    if $P/venv/bin/python "$P/$script" --start "$S" --end "$E" >> "$LOG" 2>&1; then
        log "<<< $name done"
    else
        log "!!! $name FAILED (see $LOG) — continuing; chunks are restartable"
    fi
done

log ">>> feature aggregation $S .. 2026-02-09"
if $P/venv/bin/python "$P/aggregate_features_v2.py" --start "$S" --end 2026-02-09 >> "$LOG" 2>&1; then
    log "<<< aggregation done"
else
    log "!!! aggregation FAILED (see $LOG)"
fi

log ">>> weather backfill"
$P/venv/bin/python -m weather_analysis.cli backfill --start "$S" >> "$LOG" 2>&1 \
  && log "<<< weather done" || log "!!! weather backfill failed (non-fatal)"

log "=== January backfill complete ==="
