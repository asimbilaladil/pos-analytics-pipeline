#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/laynes"
VENV="$APP_DIR/venv"
LOG_DIR="/var/log/laynes"

set -a
source "$APP_DIR/.env"
set +a

START_DATE="${1:-2026-01-01}"
END_DATE="${2:-$(date -d 'yesterday' +%Y-%m-%d)}"

echo "Backfilling from $START_DATE to $END_DATE"
current="$START_DATE"
success=0; failed=0

while [[ "$current" < "$END_DATE" ]] || [[ "$current" == "$END_DATE" ]]; do
    echo -n "[$current] Fetching... "
    LOG="$LOG_DIR/backfill_$current.log"

    if "$VENV/bin/python3" "$APP_DIR/pipeline.py" --date "$current" > "$LOG" 2>&1; then
        echo "OK"
        "$VENV/bin/python3" "$APP_DIR/aggregate_features.py" --date "$current" >> "$LOG" 2>&1 || \
            echo "  WARN: aggregation failed for $current"
        success=$((success + 1))
    else
        echo "FAILED — see $LOG"
        failed=$((failed + 1))
    fi

    current=$(date -d "$current + 1 day" +%Y-%m-%d)
    sleep 2
done

echo "Backfill complete — Success: $success, Failed: $failed"
