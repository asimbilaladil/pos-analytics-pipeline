#!/usr/bin/env bash
set -euo pipefail

# Task 16 post-cutover daily sequence. ACTIVE as of the 2026-08-13 cutover —
# this is what crontab's existing `run.sh` entry now runs, unchanged schedule
# (0 9 * * *). run.sh.pre-task16 is the byte-for-byte pre-cutover version of
# this file, kept for fast rollback (`cp run.sh.pre-task16 run.sh`, see Task
# 16 plan §8) -- restore it and the legacy created-mode/aggregate_features.py
# path resumes exactly as it ran before cutover.
#
# Sequence (avoids ever running the legacy writer and the v2 writer in the
# same nightly run):
#   1. updated-mode Revel sync -> shadow_v2 (orders_v2/order_items_v2/
#      modifier_items_v2/order_history_v2/payments_v2 -- all 5, via
#      run_establishment_updated(target="shadow_v2"); dirty dates are
#      queued as a side effect of this same step, inside the same
#      transaction as each UPSERT -- not a separate script call)
#   2. yesterday v2 feature aggregation (aggregate_features_v2.py, full
#      network, unchanged formula/behavior from every prior validation run)
#   3. historical dirty-date recomputation (reprocess_dirty_features_v2.py
#      drains feature_recompute_queue -- corrections to dates other than
#      yesterday, discovered by step 1)
#   4. weather/other existing jobs (weather_analysis.cli backfill --
#      already repointed to orders_v2/order_items_v2, Phase 3)
#
# The old aggregate_features.py call is intentionally absent -- it would be
# a second writer to a now-frozen legacy table, serving no one post-cutover.

APP_DIR="/root/pos-analytics-pipeline"
LOG_DIR="/var/log/laynes"
VENV="$APP_DIR/venv"
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/run_$DATE.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "========================================="
log "Laynes pipeline starting (Task 16 v2 path) — $DATE"
log "========================================="

set -a
source "$APP_DIR/.env"
set +a

# Explicit overrides, not left to .env: activating cutover is "point cron at
# this file," not "also remember to edit .env" -- keeps the two rollback
# paths (restore run.sh, or just stop invoking this file) independent of
# .env state, and matches every other opt-in flag in this project
# (REVEL_SYNC_MODE was already env-driven; REVEL_WRITE_TARGET is new and
# follows the same convention) defaulting safely if unset elsewhere.
export REVEL_SYNC_MODE=updated
export REVEL_WRITE_TARGET=shadow_v2

log "Step 1/4: Revel sync (updated-mode, target=shadow_v2)..."
if "$VENV/bin/python3" "$APP_DIR/pipeline.py" >> "$LOG_FILE" 2>&1; then
    log "Revel sync completed successfully"
else
    log "ERROR: Revel sync failed — check $LOG_FILE"
    exit 1
fi

YESTERDAY=$(date -d "yesterday" +%Y-%m-%d)
log "Step 2/4: yesterday ($YESTERDAY) v2 feature aggregation..."
if "$VENV/bin/python3" "$APP_DIR/aggregate_features_v2.py" --start "$YESTERDAY" --end "$YESTERDAY" >> "$LOG_FILE" 2>&1; then
    log "yesterday aggregation completed successfully"
else
    log "ERROR: yesterday aggregation failed — check $LOG_FILE"
    exit 1
fi

log "Step 3/4: draining feature_recompute_queue (dirty-date recomputation)..."
if "$VENV/bin/python3" "$APP_DIR/reprocess_dirty_features_v2.py" >> "$LOG_FILE" 2>&1; then
    log "dirty-date recomputation completed successfully"
else
    log "WARN: dirty-date recomputation reported an error — check $LOG_FILE (individual failed rows stay visible in feature_recompute_queue for retry, this does not fail the whole run)"
fi

# Refresh weather (Open-Meteo archive) for all locations. Best-effort — this
# feeds the tender drilldown's weather panel, not the forecast itself, so a
# failure here must not fail the nightly run. The archive lags ~5 days; the
# CLI's default window ends at today-5 and upserts, so re-running is idempotent.
log "Step 4/4: refreshing weather_daily..."
if (cd "$APP_DIR" && "$VENV/bin/python3" -m weather_analysis.cli backfill) >> "$LOG_FILE" 2>&1; then
    log "weather refresh completed successfully"
else
    log "WARN: weather refresh failed — drilldown weather may be stale (non-fatal)"
fi

log "Pipeline complete for $DATE (Task 16 v2 path)"
find "$LOG_DIR" -name "run_*.log" -mtime +30 -delete 2>/dev/null || true
