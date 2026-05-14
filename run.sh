#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/laynes"
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

log "Running predict_daily.py..."
if "$VENV/bin/python3" "$APP_DIR/predict_daily.py" >> "$LOG_FILE" 2>&1; then
    log "predict_daily.py completed successfully"
else
    log "ERROR: predict_daily.py failed — check $LOG_FILE"
    exit 1
fi

log "Pipeline complete for $DATE"
find "$LOG_DIR" -name "run_*.log" -mtime +30 -delete 2>/dev/null || true
