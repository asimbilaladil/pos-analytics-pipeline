#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/root/pos-analytics-pipeline"
LOG_DIR="/var/log/laynes"
VENV="$APP_DIR/venv"
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/run_$DATE.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "========================================="
log "Laynes pipeline starting — $DATE"
log "========================================="

set -a
source "$APP_DIR/.env"
set +a

log "Running pipeline.py (fetch + insert)..."
if "$VENV/bin/python3" "$APP_DIR/pipeline.py" >> "$LOG_FILE" 2>&1; then
    log "pipeline.py completed successfully"
else
    log "ERROR: pipeline.py failed — check $LOG_FILE"
    exit 1
fi

log "Running aggregate_features.py..."
if "$VENV/bin/python3" "$APP_DIR/aggregate_features.py" >> "$LOG_FILE" 2>&1; then
    log "aggregate_features.py completed successfully"
else
    log "ERROR: aggregate_features.py failed — check $LOG_FILE"
    exit 1
fi

# Refresh weather (Open-Meteo archive) for all locations. Best-effort — this
# feeds the tender drilldown's weather panel, not the forecast itself, so a
# failure here must not fail the nightly run. The archive lags ~5 days; the
# CLI's default window ends at today-5 and upserts, so re-running is idempotent.
log "Refreshing weather_daily..."
if (cd "$APP_DIR" && "$VENV/bin/python3" -m weather_analysis.cli backfill) >> "$LOG_FILE" 2>&1; then
    log "weather refresh completed successfully"
else
    log "WARN: weather refresh failed — drilldown weather may be stale (non-fatal)"
fi

log "Pipeline complete for $DATE"
find "$LOG_DIR" -name "run_*.log" -mtime +30 -delete 2>/dev/null || true
