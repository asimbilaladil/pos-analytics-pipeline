#!/usr/bin/env bash
# Smart 3-month backfill with auto-resume, rate limiting, and failure tracking.
#
# Usage:
#   bash backfill3m.sh                              # last 3 months → yesterday, 30 min sleep
#   bash backfill3m.sh 2026-01-01                   # custom start → yesterday
#   bash backfill3m.sh 2026-01-01 2026-03-31        # custom range
#   bash backfill3m.sh 2026-01-01 2026-03-31 1800   # custom range + 30 min sleep (seconds)
#
# Run in background (survives logout):
#   nohup bash /opt/laynes/backfill3m.sh > /var/log/laynes/backfill3m.log 2>&1 &
#
# Watch live progress:
#   tail -f /var/log/laynes/backfill3m.log
#
# Check failed dates anytime:
#   bash /opt/laynes/backfill3m.sh --status
#
# Retry only failed dates (re-run same command — completed dates are auto-skipped):
#   nohup bash /opt/laynes/backfill3m.sh > /var/log/laynes/backfill3m_retry.log 2>&1 &

set -euo pipefail

APP_DIR="/opt/laynes"
VENV="$APP_DIR/venv"
LOG_DIR="/var/log/laynes"
FAILED_DATES_FILE="$LOG_DIR/backfill_failed_dates.txt"
EXPECTED_ESTS=11

set -a; source "$APP_DIR/.env"; set +a

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── DB helpers ────────────────────────────────────────────────────────────────
db() {
    PGPASSWORD="$DB_PASS" psql \
        -h "${DB_HOST:-localhost}" -p "${DB_PORT:-5432}" \
        -U "${DB_USER:-laynes_user}" -d "${DB_NAME:-laynes}" \
        -tAc "$1" 2>/dev/null || echo 0
}

is_done() {
    local d="$1"
    local n
    n=$(db "SELECT COUNT(DISTINCT establishment_id)
            FROM ingestion_log
            WHERE run_date = '$d' AND status = 'success'")
    [[ "${n:-0}" -ge "$EXPECTED_ESTS" ]]
}

# ── Status mode ───────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--status" ]]; then
    START_DATE="${2:-$(date -d '3 months ago' +%Y-%m-%d)}"
    END_DATE="${3:-$(date -d 'yesterday' +%Y-%m-%d)}"

    echo ""
    echo "=== Backfill Status: $START_DATE → $END_DATE ==="
    echo ""

    db "SELECT
            run_date,
            COUNT(DISTINCT establishment_id) FILTER (WHERE status = 'success') AS ok,
            COUNT(DISTINCT establishment_id) FILTER (WHERE status = 'failed')  AS failed,
            SUM(orders_inserted)                                                AS orders,
            SUM(items_inserted)                                                 AS items
        FROM ingestion_log
        WHERE run_date BETWEEN '$START_DATE' AND '$END_DATE'
        GROUP BY run_date
        ORDER BY run_date" | column -t -s '|'

    echo ""
    echo "=== Incomplete / Missing Dates ==="
    cur="$START_DATE"
    missing=0
    while [[ "$cur" < "$END_DATE" || "$cur" == "$END_DATE" ]]; do
        if ! is_done "$cur"; then
            echo "  $cur"
            missing=$((missing + 1))
        fi
        cur=$(date -d "$cur + 1 day" +%Y-%m-%d)
    done
    [[ "$missing" -eq 0 ]] && echo "  None — all dates complete!" || echo ""
    echo "  Total incomplete: $missing"
    exit 0
fi

# ── Main backfill ─────────────────────────────────────────────────────────────
START_DATE="${1:-$(date -d '3 months ago' +%Y-%m-%d)}"
END_DATE="${2:-$(date -d 'yesterday' +%Y-%m-%d)}"
SLEEP_SECS="${3:-1800}"    # default: 30 minutes between dates

# Count total days
total=0; cur="$START_DATE"
while [[ "$cur" < "$END_DATE" || "$cur" == "$END_DATE" ]]; do
    total=$((total + 1))
    cur=$(date -d "$cur + 1 day" +%Y-%m-%d)
done

est_hours=$(( total * (SLEEP_SECS + 480) / 3600 ))

log "================================================="
log "Backfill: $START_DATE → $END_DATE ($total days)"
log "Sleep between dates: ${SLEEP_SECS}s ($(( SLEEP_SECS / 60 )) min)"
log "Estimated max time: ~${est_hours}h (skipped dates are instant)"
log "Failed dates log: $FAILED_DATES_FILE"
log "================================================="

# Clear failed dates file for this run
> "$FAILED_DATES_FILE"

done_count=0; skip_count=0; fail_count=0
cur="$START_DATE"

while [[ "$cur" < "$END_DATE" || "$cur" == "$END_DATE" ]]; do
    idx=$(( done_count + skip_count + fail_count + 1 ))

    if is_done "$cur"; then
        log "[$cur] ($idx/$total) Already complete — skipping"
        skip_count=$((skip_count + 1))
        cur=$(date -d "$cur + 1 day" +%Y-%m-%d)
        continue
    fi

    log "[$cur] ($idx/$total) Starting fetch..."
    BFLOG="$LOG_DIR/backfill_$cur.log"

    if "$VENV/bin/python3" "$APP_DIR/pipeline.py" --date "$cur" > "$BFLOG" 2>&1; then
        log "[$cur] Pipeline OK — aggregating..."

        if "$VENV/bin/python3" "$APP_DIR/aggregate_features.py" --date "$cur" >> "$BFLOG" 2>&1; then
            log "[$cur] Done ✓  (fetched=$done_count skipped=$skip_count failed=$fail_count)"
        else
            log "[$cur] WARN: aggregation failed — order data saved, see $BFLOG"
        fi
        done_count=$((done_count + 1))
    else
        log "[$cur] FAILED — recording to $FAILED_DATES_FILE"
        log "[$cur] See detail: $BFLOG"
        echo "$cur" >> "$FAILED_DATES_FILE"
        fail_count=$((fail_count + 1))
    fi

    # Sleep before next date (skip on last date)
    if [[ "$cur" < "$END_DATE" ]]; then
        next_time=$(date -d "$SLEEP_SECS seconds" '+%H:%M:%S')
        log "Sleeping $(( SLEEP_SECS / 60 ))m — next date starts around $next_time..."
        sleep "$SLEEP_SECS"
    fi

    cur=$(date -d "$cur + 1 day" +%Y-%m-%d)
done

log "================================================="
log "Backfill complete"
log "  Fetched:  $done_count days"
log "  Skipped:  $skip_count days (already in DB)"
log "  Failed:   $fail_count days"
log "================================================="

if [[ "$fail_count" -gt 0 ]]; then
    log ""
    log "Failed dates saved to: $FAILED_DATES_FILE"
    log "Contents:"
    cat "$FAILED_DATES_FILE" | while read -r d; do log "  $d — see $LOG_DIR/backfill_$d.log"; done
    log ""
    log "To investigate a failure:"
    log "  cat $LOG_DIR/backfill_<date>.log | grep -i error"
    log ""
    log "To retry all failed dates (completed ones auto-skipped):"
    log "  nohup bash $APP_DIR/backfill3m.sh $START_DATE $END_DATE $SLEEP_SECS > $LOG_DIR/backfill3m_retry.log 2>&1 &"
    exit 1
fi
