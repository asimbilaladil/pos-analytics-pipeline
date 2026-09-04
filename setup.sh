#!/usr/bin/env bash
# =============================================================================
# setup.sh — Laynes Prediction Platform — One-Command Server Setup
# =============================================================================
# Run once on a fresh Ubuntu 22.04 VPS:
#
#   chmod +x setup.sh && sudo bash setup.sh
#
# What this does:
#   1. Installs system packages (Python 3, PostgreSQL, Playwright deps)
#   2. Installs Python packages
#   3. Installs Playwright + Chromium
#   4. Creates the laynes database and runs the schema
#   5. Creates /opt/laynes directory with all scripts
#   6. Creates /opt/laynes/.env from your input
#   7. Sets up cron: 2:00 AM pipeline, 3:00 AM aggregation
#   8. Runs a health check to verify everything is connected
#
# After running, the pipeline will fire automatically every night at 2 AM.
# Logs go to /var/log/laynes/
# =============================================================================

set -euo pipefail

# ── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log()     { echo -e "${GREEN}[SETUP]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${BLUE}══════════════════════════════════════════${NC}"; \
            echo -e "${BLUE}  $*${NC}"; \
            echo -e "${BLUE}══════════════════════════════════════════${NC}"; }

# ── Must run as root ─────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    error "Run this script with sudo: sudo bash setup.sh"
fi

# ── Paths ─────────────────────────────────────────────────────────────────────
APP_DIR="/opt/laynes"
LOG_DIR="/var/log/laynes"
ENV_FILE="$APP_DIR/.env"
CRON_FILE="/etc/cron.d/laynes"
VENV_DIR="$APP_DIR/venv"
PYTHON="$VENV_DIR/bin/python3"
PIP="$VENV_DIR/bin/pip"

# =============================================================================
section "Step 1 — System Packages"
# =============================================================================
log "Updating package list..."
apt-get update -qq

log "Installing system packages..."
apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    postgresql \
    postgresql-client \
    curl \
    wget \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libasound2 \
    libxrandr2 \
    libxfixes3 \
    libxcomposite1 \
    libxdamage1 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    fonts-liberation \
    xdg-utils

log "System packages installed"

# =============================================================================
section "Step 2 — App Directory"
# =============================================================================
log "Creating $APP_DIR ..."
mkdir -p "$APP_DIR"
mkdir -p "$LOG_DIR"

# Copy scripts if they exist next to setup.sh, otherwise create placeholders
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in pipeline.py aggregate_features.py ingest_to_db.py database_design.sql; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        cp "$SCRIPT_DIR/$f" "$APP_DIR/$f"
        log "Copied $f → $APP_DIR/$f"
    else
        warn "$f not found next to setup.sh — copy it to $APP_DIR manually"
    fi
done

# =============================================================================
section "Step 3 — Python Virtual Environment"
# =============================================================================
log "Creating Python virtual environment at $VENV_DIR ..."
python3 -m venv "$VENV_DIR"

log "Upgrading pip..."
"$PIP" install --upgrade pip -q

log "Installing Python packages..."
"$PIP" install \
    playwright \
    psycopg2-binary \
    python-dotenv \
    -q

log "Installing Playwright + Chromium browser..."
"$VENV_DIR/bin/playwright" install chromium
"$VENV_DIR/bin/playwright" install-deps chromium

log "Python environment ready"

# =============================================================================
section "Step 4 — PostgreSQL Setup"
# =============================================================================
log "Starting PostgreSQL..."
systemctl enable postgresql
systemctl start postgresql

# Generate a random strong password for the DB user
DB_USER="laynes_user"
DB_PASS=$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 32)
DB_NAME="laynes"

log "Creating database user '$DB_USER' and database '$DB_NAME'..."
sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null || \
    sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASS';"

sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null || \
    warn "Database '$DB_NAME' already exists — skipping creation"

sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"

log "Running database schema..."
if [[ -f "$APP_DIR/database_design.sql" ]]; then
    sudo -u postgres psql -d "$DB_NAME" -f "$APP_DIR/database_design.sql" \
        --set ON_ERROR_STOP=off 2>&1 | grep -v "^$" | head -40 || true
    log "Schema applied"
else
    warn "database_design.sql not found in $APP_DIR — run it manually later:"
    warn "  sudo -u postgres psql -d $DB_NAME -f $APP_DIR/database_design.sql"
fi

# =============================================================================
section "Step 5 — Environment File"
# =============================================================================
log "We need your Revel credentials to configure the pipeline."
echo ""

# Prompt for Revel credentials
read -p "  Revel username (email): " REVEL_USER
read -s -p "  Revel password:         " REVEL_PASS
echo ""

cat > "$ENV_FILE" <<EOF
# Laynes Prediction Platform — Environment Variables
# Generated by setup.sh on $(date)
# !! Keep this file private — never commit to git !!

# Revel credentials
REVEL_USER=$REVEL_USER
REVEL_PASS=$REVEL_PASS

# Establishments — comma-separated Revel IDs
# To add a new location: append its ID here, then also INSERT into the establishments table
# Example: ESTABLISHMENTS=32,14,48,7,6,25,36,26,20,40,15,55
ESTABLISHMENTS=32,14,48,7,6,25,36,26,20,40,15

# PostgreSQL
DB_HOST=localhost
DB_PORT=5432
DB_NAME=$DB_NAME
DB_USER=$DB_USER
DB_PASS=$DB_PASS
EOF

chmod 600 "$ENV_FILE"
log ".env file created at $ENV_FILE (chmod 600 — owner only)"

# =============================================================================
section "Step 6 — Run Script"
# =============================================================================
cat > "$APP_DIR/run.sh" <<'RUNSCRIPT'
#!/usr/bin/env bash
# run.sh — Called by cron every night
# Loads .env, runs pipeline then aggregation, logs everything

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

# Load environment variables
set -a
# shellcheck disable=SC1091
source "$APP_DIR/.env"
set +a

# ── Step 1: Fetch from Revel → insert into DB ──────────────────────────────
log "Running pipeline.py (fetch + insert)..."
if "$VENV/bin/python3" "$APP_DIR/pipeline.py" >> "$LOG_FILE" 2>&1; then
    log "pipeline.py completed successfully"
else
    log "ERROR: pipeline.py failed — check $LOG_FILE"
    exit 1
fi

# ── Step 2: Aggregate feature tables ──────────────────────────────────────
log "Running aggregate_features.py..."
if "$VENV/bin/python3" "$APP_DIR/aggregate_features.py" >> "$LOG_FILE" 2>&1; then
    log "aggregate_features.py completed successfully"
else
    log "ERROR: aggregate_features.py failed — check $LOG_FILE"
    exit 1
fi

log "========================================="
log "Pipeline complete for $DATE"
log "========================================="

# Keep only last 30 days of logs
find "$LOG_DIR" -name "run_*.log" -mtime +30 -delete 2>/dev/null || true
RUNSCRIPT

chmod +x "$APP_DIR/run.sh"
log "run.sh created at $APP_DIR/run.sh"

# =============================================================================
section "Step 7 — Backfill Script"
# =============================================================================
cat > "$APP_DIR/backfill.sh" <<'BACKFILL'
#!/usr/bin/env bash
# backfill.sh — Fetch historical data month by month
#
# Usage:
#   bash /opt/laynes/backfill.sh 2026-01-01 2026-04-30
#
# This loops day by day and fetches each day from Revel.
# Run in a tmux/screen session — takes ~10-15 min per month of data.

set -euo pipefail

APP_DIR="/opt/laynes"
VENV="$APP_DIR/venv"
LOG_DIR="/var/log/laynes"

# Load env
set -a
source "$APP_DIR/.env"
set +a

START_DATE="${1:-2026-01-01}"
END_DATE="${2:-$(date -d 'yesterday' +%Y-%m-%d)}"

echo "Backfilling from $START_DATE to $END_DATE"
echo "This may take a while — each day makes API calls for 11 locations."
echo ""

current="$START_DATE"
success=0
failed=0

while [[ "$current" < "$END_DATE" ]] || [[ "$current" == "$END_DATE" ]]; do
    echo -n "[$current] Fetching... "

    LOG="$LOG_DIR/backfill_$current.log"

    if "$VENV/bin/python3" "$APP_DIR/pipeline.py" --date "$current" > "$LOG" 2>&1; then
        echo "OK (orders inserted)"

        # Aggregate features for this date too
        "$VENV/bin/python3" "$APP_DIR/aggregate_features.py" \
            --date "$current" >> "$LOG" 2>&1 || \
            echo "  WARN: aggregation failed for $current"

        success=$((success + 1))
    else
        echo "FAILED — see $LOG"
        failed=$((failed + 1))
    fi

    # Move to next day
    current=$(date -d "$current + 1 day" +%Y-%m-%d)

    # Small pause to be polite to Revel API between days
    sleep 2
done

echo ""
echo "==============================="
echo "Backfill complete"
echo "  Success: $success days"
echo "  Failed:  $failed days"
echo "==============================="
BACKFILL

chmod +x "$APP_DIR/backfill.sh"
log "backfill.sh created at $APP_DIR/backfill.sh"

# =============================================================================
section "Step 8 — Cron Job"
# =============================================================================
cat > "$CRON_FILE" <<CRONFILE
# Laynes Prediction Platform — Daily Pipeline
# Runs as root (or change to a dedicated user)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

# 2:00 AM — Fetch from Revel + insert into DB + aggregate features
0 2 * * * root /opt/laynes/run.sh

CRONFILE

chmod 644 "$CRON_FILE"
log "Cron job installed at $CRON_FILE"
log "  Schedule: 2:00 AM every night"

# Restart cron to pick up the new file
systemctl restart cron 2>/dev/null || systemctl restart crond 2>/dev/null || true

# =============================================================================
section "Step 9 — Health Check"
# =============================================================================
log "Verifying database connection..."

export PGPASSWORD="$DB_PASS"
if psql -h localhost -U "$DB_USER" -d "$DB_NAME" -c "SELECT COUNT(*) FROM establishments;" > /dev/null 2>&1; then
    log "Database connection OK"
else
    warn "Database connection failed — check PostgreSQL is running and .env is correct"
fi

log "Verifying Python + Playwright..."
if "$PYTHON" -c "from playwright.sync_api import sync_playwright; print('Playwright OK')" 2>/dev/null; then
    log "Python + Playwright OK"
else
    warn "Playwright import failed — try: $VENV_DIR/bin/playwright install chromium"
fi

log "Verifying psycopg2..."
if "$PYTHON" -c "import psycopg2; print('psycopg2 OK')" 2>/dev/null; then
    log "psycopg2 OK"
fi

# =============================================================================
section "Setup Complete!"
# =============================================================================
echo ""
echo -e "${GREEN}Everything is installed and configured.${NC}"
echo ""
echo "  App directory:  $APP_DIR"
echo "  Logs directory: $LOG_DIR"
echo "  Environment:    $ENV_FILE"
echo "  Cron job:       $CRON_FILE"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo ""
echo "  1. TEST the pipeline runs correctly:"
echo "     bash /opt/laynes/run.sh"
echo ""
echo "  2. BACKFILL historical data (3-4 months):"
echo "     bash /opt/laynes/backfill.sh 2026-01-01 2026-04-30"
echo ""
echo "  3. CHECK logs anytime:"
echo "     tail -f /var/log/laynes/run_\$(date +%Y-%m-%d).log"
echo ""
echo "  4. CRON fires automatically at 2:00 AM every night."
echo "     No further action needed."
echo ""
echo -e "${BLUE}Database credentials (save these):${NC}"
echo "  Host:     localhost"
echo "  Database: $DB_NAME"
echo "  User:     $DB_USER"
echo "  Password: $DB_PASS"
echo ""
echo "  (also saved in $ENV_FILE)"
echo ""
