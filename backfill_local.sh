#!/usr/bin/env bash
# Backfill runner adapted for /root/pos-analytics-pipeline
# Usage:
#   bash backfill_local.sh                        # Feb 10 2026 → yesterday
#   bash backfill_local.sh 2026-03-01 2026-04-30  # custom range
#   bash backfill_local.sh --status               # check progress

set -euo pipefail

APP_DIR="/root/pos-analytics-pipeline"
VENV="$APP_DIR/venv"
LOG_DIR="/var/log/laynes"
FAILED_FILE="$LOG_DIR/backfill_failed.txt"
EXPECTED_ESTS=11
SLEEP_SECS=5   # 5 sec between dates — fast but polite to Revel API

mkdir -p "$LOG_DIR"

set -a; source "$APP_DIR/.env"; set +a

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

db() {
    PGPASSWORD="$DB_PASS" psql \
        -h "${DB_HOST:-localhost}" -p "${DB_PORT:-5432}" \
        -U "${DB_USER:-laynes_user}" -d "${DB_NAME:-laynes}" \
        -tAc "$1" 2>/dev/null || echo 0
}

is_done() {
    local n
    n=$(db "SELECT COUNT(DISTINCT establishment_id)
            FROM ingestion_log
            WHERE run_date = '$1' AND status = 'success'")
    [[ "${n:-0}" -ge "$EXPECTED_ESTS" ]]
}

# ── Status mode ───────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--status" ]]; then
    START="${2:-2026-02-10}"; END="${3:-$(date -d 'yesterday' +%Y-%m-%d)}"
    echo ""
    echo "=== Backfill Status: $START → $END ==="
    db "SELECT run_date,
               COUNT(DISTINCT establishment_id) FILTER (WHERE status='success') AS ok,
               COUNT(DISTINCT establishment_id) FILTER (WHERE status='failed')  AS failed,
               SUM(orders_inserted) AS orders,
               SUM(items_inserted)  AS items
        FROM ingestion_log
        WHERE run_date BETWEEN '$START' AND '$END'
        GROUP BY run_date ORDER BY run_date" | column -t -s '|'
    echo ""
    echo "=== Incomplete / Missing Dates ==="
    cur="$START"; missing=0
    while [[ "$cur" < "$END" || "$cur" == "$END" ]]; do
        is_done "$cur" || { echo "  $cur"; missing=$((missing+1)); }
        cur=$(date -d "$cur + 1 day" +%Y-%m-%d)
    done
    [[ "$missing" -eq 0 ]] && echo "  None — all dates complete!" || true
    echo "  Total incomplete: $missing"
    exit 0
fi

# ── Main backfill ─────────────────────────────────────────────────────────────
START_DATE="${1:-2026-02-10}"
END_DATE="${2:-$(date -d 'yesterday' +%Y-%m-%d)}"

total=0; cur="$START_DATE"
while [[ "$cur" < "$END_DATE" || "$cur" == "$END_DATE" ]]; do
    total=$((total+1)); cur=$(date -d "$cur + 1 day" +%Y-%m-%d)
done

log "================================================="
log "Backfill: $START_DATE → $END_DATE ($total days)"
log "Log dir:  $LOG_DIR"
log "================================================="

> "$FAILED_FILE"
done_count=0; skip_count=0; fail_count=0; cur="$START_DATE"

while [[ "$cur" < "$END_DATE" || "$cur" == "$END_DATE" ]]; do
    idx=$(( done_count + skip_count + fail_count + 1 ))

    if is_done "$cur"; then
        log "[$cur] ($idx/$total) Already complete — skipping"
        skip_count=$((skip_count+1))
        cur=$(date -d "$cur + 1 day" +%Y-%m-%d)
        continue
    fi

    log "[$cur] ($idx/$total) Fetching..."
    BFLOG="$LOG_DIR/backfill_$cur.log"

    if "$VENV/bin/python3" "$APP_DIR/pipeline.py" --date "$cur" --skip-reference > "$BFLOG" 2>&1; then
        log "[$cur] Pipeline OK — aggregating features..."
        "$VENV/bin/python3" "$APP_DIR/aggregate_features.py" --date "$cur" >> "$BFLOG" 2>&1 \
            && log "[$cur] Done ✓ (done=$done_count skipped=$skip_count failed=$fail_count)" \
            || log "[$cur] WARN: aggregation failed — raw data saved"
        done_count=$((done_count+1))
    else
        log "[$cur] FAILED — see $BFLOG"
        echo "$cur" >> "$FAILED_FILE"
        fail_count=$((fail_count+1))
    fi

    [[ "$cur" < "$END_DATE" ]] && sleep "$SLEEP_SECS"
    cur=$(date -d "$cur + 1 day" +%Y-%m-%d)
done

log "================================================="
log "Backfill complete — Fetched: $done_count  Skipped: $skip_count  Failed: $fail_count"
[[ "$fail_count" -gt 0 ]] && log "Failed dates: $FAILED_FILE" && cat "$FAILED_FILE"
log "================================================="
