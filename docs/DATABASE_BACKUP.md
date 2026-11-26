# Database backup: scheduled job, verification, restore test

**Status: NOT INSTALLED.** This change adds the job, units, tests and this runbook.
Nothing is scheduled, nothing has run against production, and the monitor's
production configuration is unchanged. Installing needs the approvals in
[Approvals required](#approvals-required).

It replaces the plan in `docs/BACKUP_PROPOSAL.md`.

## Facts this design is based on (read-only, 2026-11-26)

| | |
|---|---|
| Host | Hetzner Cloud vServer, nbg1-dc3, 2 vCPU, 3.7 GiB RAM, one 38 GB disk |
| PostgreSQL | 18.6, one cluster (`18/main`), application database `laynes`; `wal_level=replica`, no WAL archiving |
| Database size | 7.96 GB on disk: about 4.0 GB table data and 3.9 GB indexes. A dump contains no index data. |
| Full dump size | 852 MB (`pg_dump -Fc`, 2026-11-25), written in about 1.5 min |
| Disk `/` | 31.5 GB used of 40.0 GB, which is **78.9 %** as the monitor computes it (`used/total`). The monitor warns at 85 %, so **about 2.4 GB of headroom**. |
| Existing backups | None scheduled: no cron, no timer, no `pg_dump` job, no `/var/backups/laynes`. Only manual pre-deploy dumps in `/root/deploy-backups` (2.7 GB) and `/root/laynes_migrate.dump` (2026-09-02, 594 MB). |
| Provider backups | **Not verifiable from the server.** Hetzner's backup and snapshot settings are visible only in the Cloud Console or API, and no API token exists here. |
| Off-server copy | None |

**Root cause of the 2026-11-26 HIGH incident.** No scheduled backup exists. The
check takes the newest `*.dump` of at least 1 MiB under the globs as "the backup".
On 2026-11-26 that was `r365-financial-before.dump`, a 3.2 MB single-table
pre-deploy dump that had passed 26 h.

The check is also a **false-green risk**. Any manual dump of any table, at any
size of 1 MiB or more, resolves the HIGH incident, whether or not the database
can be restored.

## What is protected and how

| Data | Where | Covered by |
|---|---|---|
| Revel orders, items, payments, loyalty, labour, features | PostgreSQL `laynes` | **this job** (daily dump) |
| HME runs, store-day data, **goal history**, mapping | PostgreSQL `laynes` | **this job**, plus content fingerprints of goal history and mappings |
| R365 weekly and financial ingestion | PostgreSQL `laynes` | **this job** |
| AI Chat conversations, jobs, users, sessions | PostgreSQL `laynes` | **this job** |
| Roles (no passwords) | cluster globals | **this job** (`pg_dumpall --globals-only --no-role-passwords`) |
| Monitor incidents and run history | SQLite `/var/lib/laynes/monitor/monitor.db` (0.3 MB) | not yet: config/state archive, see below |
| Credentials and config: `/etc/laynes/*.env`, `/etc/laynes-mcp/mcp.env`, release `.env`, `/etc/laynes/monitor.toml`, systemd units and drop-ins, nginx | files | not yet: config/state archive (secrets, so off-server and encrypted only) |
| HME and R365 state and checkpoints | `/var/lib/laynes/{hme,r365,r365-financial}` (13 MB) | not yet: config/state archive |
| Raw Revel extracts | `/var/lib/laynes/raw_revel` (2.2 GB) | not covered; can be re-fetched from Revel |
| Code | GitHub | git (release SHA recorded in each deploy) |
| Whole server | disk | Hetzner Backups or Snapshots, **unverified**: owner to confirm |

## Components

| File | Installed as |
|---|---|
| `backup/laynes_db_backup.py` | `/usr/local/lib/laynes-backup/laynes_db_backup.py` (root, 0755) |
| `backup/backup.example.toml` | `/etc/laynes-backup/backup.toml` (root:postgres, 0640; contains no secrets) |
| `systemd/laynes-db-backup.{service,timer}` | `/etc/systemd/system/` |
| `systemd/laynes-db-restore-test.{service,timer}` | `/etc/systemd/system/` |
| `systemd/laynes-db-backup.tmpfiles.conf` | `/etc/tmpfiles.d/laynes-db-backup.conf` |

**No credentials.** The job runs as the OS user `postgres` and connects over the
local UNIX socket with peer authentication, the existing `pg_hba.conf` line
`local all postgres peer`. There is no password in the script, the config, the
unit, `systemctl show`, or the journal.

## What one backup does (`backup`)

1. **Lock.** Takes one lock shared with the restore test. If another job holds
   it, the backup waits up to 30 min, then fails with a status file.
2. **Disk guard.** Estimates the dump at 1.25 × the previous one (or 0.20 × the
   database size the first time). It refuses if the filesystem would pass
   **84 % used** (one point under the monitor's warning) or keep less than 2 GiB
   free.
   - If that fails, it first deletes the oldest of its own backups, but never
     below `max(1, keep_daily - 1)` complete backups.
3. **Consistent snapshot.**
   - One `REPEATABLE READ READ ONLY` transaction exports a snapshot.
   - `pg_dump --snapshot` dumps exactly that snapshot.
   - The manifest's exact row counts (every table), content fingerprints (goal
     history, mappings, establishments, periods), extension versions and view
     counts are measured **in the same snapshot**. A restore can therefore be
     compared exactly, even though ingestion keeps writing.
4. **No blocking.** `pg_dump` takes only ACCESS SHARE locks, which block DDL but
   not reads or writes. With `--lock-wait-timeout=120s` it gives up instead of
   queueing behind a migration.
5. **Format.** `-Fc --compress=zstd:3`. The custom format allows selective
   restores; zstd is faster than gzip at a similar size.
6. **Archive verification (every night):**
   - `pg_restore --list` must list a TABLE DATA entry for every counted table.
   - `pg_restore -f /dev/null` must **decode and decompress every data block**,
     which catches truncation and corruption.
   - The sha256 is recorded.
7. **Atomic promotion.**
   - Files are written as `*.partial`, made 0600 and fsynced, then renamed.
   - The **manifest is written last** and marks the backup complete.
   - A failure deletes this run's partial files.
8. **Status file.** `status/backup-<UTC>-<pid>.json` records `ok`, the dump path,
   size, sha256, the verification done, the duration and the disk numbers. On
   failure it records the `stage` and `error`.
9. **Retention.** Runs only after a successful backup. See below.

Exit codes: 0 means OK, 1 means failure (status file written), 2 means a
configuration error.

## What the restore test does (`restore-test`, weekly)

The restore test never touches production. It builds a **throwaway cluster** in
`/var/backups/laynes/restore-scratch`:
- `initdb` with the same encoding and locale as production;
- UNIX socket only (`listen_addresses=''`), on its own port (54399);
- inside the unit's memory and CPU caps.

Steps:
1. Re-check the sha256 of the dump and the globals file. Check the PostgreSQL
   major version matches.
2. Restore the roles from globals and check that every role in the manifest
   exists.
3. Restore the data:
   - **full** mode: `pg_restore --exit-on-error` of the whole database.
   - **critical** mode: the whole schema (pre-data); then the data of the
     critical tables and every FK parent they need (the FK graph is taken from
     the manifest); then post-data (indexes, constraints, FKs validated).
4. Compare with the manifest:
   - extensions and versions;
   - the full table set and the view count;
   - **exact row counts** of every loaded table;
   - **content fingerprints** of the critical small tables.
5. Stop and delete the scratch cluster. Write `status/restore-<UTC>-<pid>.json`
   with `ok`, `mode`, the tables, rows and fingerprints checked, and any
   mismatches by name.

**Which mode fits today:**
- **critical** (about 100 MB). It covers HME (including goal history), R365,
  chat, users, establishments, periods, sync state and reconciliation. It proves
  the archive restores and the schema is complete and valid. The large Revel
  tables are decoded nightly but not loaded.
- **full** needs about 1.15 × the database size free: **about 9.6 GB, which does
  not fit on this disk**. It becomes the default once more storage is attached
  (see Approvals). The status file and the monitor say which mode proved a
  backup, so a critical test is never presented as a full one.

## Schedule (America/Chicago, the business timezone)

| Time (CT) | Job |
|---|---|
| 02:30 daily | **laynes-db-backup** (a few minutes) |
| Sun 00:00 | **laynes-db-restore-test** (minutes in critical mode; full mode would need 30–60 min) |
| 04:15 | loyalty/labour sync |
| 05:30 | HME daily ingest |
| 07:00 | R365 financial ingest, monitor summary |
| 09:00 | Revel daily sync (about 20 min) |
| 10:15 | HME reconciliation retry |
| 11:30, 15:30 | Revel items self-heal |
| Thu 20:00 | R365 weekly ingest (Fri 02:00 UTC) |

- 02:30 CT is 08:30 UTC (CST) or 07:30 UTC (CDT). It is 6.5 h after the last
  self-heal and 1 h 45 min before the first sync.
- `Persistent=false`: after downtime the job does not catch up with a daytime
  full dump. The monitor raises the missed night after 26 h.
- `apt-daily` and `apt-daily-upgrade` have randomised delays and can overlap any
  window. The dump is MVCC-consistent regardless.

## Retention

**Local** copies are on the same disk as the database. They protect against
operator mistakes, bad deploys and corruption, **not against losing the server**.
- Default: `keep_daily = 2`, `keep_weekly = 0`.
- Steady state: 2 dumps (about 1.7 GB) and a peak of 2 during rotation, about
  83.7 % of disk. This is tight, and database growth will close the gap within
  weeks. More storage is needed (Approvals).
- With a 40–50 GB volume at `/var/backups/laynes`: 7 daily + 4 weekly locally, and
  full restore tests.

**Off-server** (proposed, needs approval): 7 daily + 4 weekly + 6 monthly. That
is about 17 × 0.9 GB ≈ 16 GB, growing with the database.

**What retention can delete.** Only regular files:
- directly inside `backup_dir`;
- named `laynes-<UTC>.{dump,globals.sql,manifest.json}[.partial]` or
  `status/{backup,restore}-<UTC>-<pid>.json`;
- owned by the job's user;
- belonging to a set whose manifest was written by this tool.

In addition:
- An incomplete set is deleted only after 24 h.
- Symlinks are never followed.
- `backup_dir` must be a real 0700 directory, never under `/root`.
- The unit has `ProtectHome=yes`, so `/root/deploy-backups` is invisible to it.
- Retention never runs after a failed backup.
- Tests cover foreign files, a symlink, another tool's manifest, and status
  history.

## Monitoring changes (`monitoring/checks/infra.py`)

New setting `infra.backup_status_glob`. When set:
- Only a **successful scheduled backup** recorded by the job satisfies
  `backup_freshness`. The threshold stays 26 h and the severity stays HIGH.
- A **failed attempt newer than the last success** is HIGH (`backup_failed`)
  **immediately**, not after 26 h. An unreadable status file counts as failed.
- The recorded dump must still exist with the recorded size, otherwise HIGH
  (`backup_missing`).
- The evidence shows:
  - the last success (time, age, size, sha256 prefix, verification level);
  - the last failure (stage, error);
  - the timer state and the **next scheduled run**;
  - restore-test status **with its mode**, and offsite status;
  - the newest manual dump, labelled "does NOT satisfy this check".
- When the setting is empty (the default), behaviour is unchanged, so deploying
  the code alone changes no incident.

**How today's HIGH incident resolves** after installation:
1. The approved first backup succeeds.
2. `backup_status_glob` is set in `/etc/laynes/monitor.toml`.
3. The next data-tier run sees a success under 26 h old, and the incident
   resolves by itself.

Nobody closes it by hand.

## Install (each step needs approval; see Approvals)

```bash
REL=/root/pos-analytics-pipeline-release            # at the merged SHA
# 0. preflight
df -h /; systemctl list-timers --all | grep -E 'laynes-db' || true   # must be empty
# 1. code + config (no secrets)
install -d -m 0755 /usr/local/lib/laynes-backup
install -m 0755 $REL/backup/laynes_db_backup.py /usr/local/lib/laynes-backup/
install -d -m 0750 -g postgres /etc/laynes-backup
install -m 0640 -g postgres $REL/backup/backup.example.toml /etc/laynes-backup/backup.toml
# 2. prerequisite directories (BEFORE the units; see the unit comments)
install -m 0644 $REL/systemd/laynes-db-backup.tmpfiles.conf /etc/tmpfiles.d/laynes-db-backup.conf
systemd-tmpfiles --create /etc/tmpfiles.d/laynes-db-backup.conf
stat -c '%n %U:%G %a' /var/backups/laynes /var/backups/laynes/status   # postgres:postgres 700
# 3. units, timers NOT enabled
install -m 0644 $REL/systemd/laynes-db-{backup,restore-test}.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/laynes-db-*.service /etc/systemd/system/laynes-db-*.timer
# 4. FIRST PRODUCTION BACKUP (separate approval; about 2–5 min; 02:30 CT preferred)
systemctl start laynes-db-backup.service; journalctl -u laynes-db-backup -n 50 --no-pager
runuser -u postgres -- /usr/bin/python3 -I /usr/local/lib/laynes-backup/laynes_db_backup.py \
    --config /etc/laynes-backup/backup.toml list
# 5. first restore test (critical), then read its status file
systemctl start laynes-db-restore-test.service; journalctl -u laynes-db-restore-test -n 50 --no-pager
ls /var/backups/laynes/status/
# 6. enable the timers (Persistent=false: enabling never starts a run)
systemctl enable --now laynes-db-backup.timer laynes-db-restore-test.timer
systemctl list-timers laynes-db-*
# 7. monitor: in /etc/laynes/monitor.toml [infra], add
#      backup_status_glob = "/var/backups/laynes/status/backup-*.json"
#      restore_test_status_glob = "/var/backups/laynes/status/restore-*.json"
#    (monitor code must be at this PR's merge; notifications stay dry_run)
```

The monitor change takes effect on the next timer run. No service restart is
needed: each run of the monitor is a new process.

## Restore procedure (disaster or bad change)

Never restore over the live `laynes` database. Restore into a new database or a
new server, check it, then switch over.

```bash
D=/var/backups/laynes; N=laynes-YYYYMMDDTHHMMSSZ        # from `list`
# 1. integrity
runuser -u postgres -- /usr/bin/python3 -I /usr/local/lib/laynes-backup/laynes_db_backup.py \
    --config /etc/laynes-backup/backup.toml verify $N
# 2. roles (no passwords in the backup; set them from the secret store / .env afterwards)
runuser -u postgres -- psql -X -f $D/$N.globals.sql postgres
#    ALTER ROLE laynes_user PASSWORD '...';  (and laynes_ro, laynes_monitor)
# 3. database: a NEW name, never over the live one
runuser -u postgres -- createdb -T template0 laynes_restore
runuser -u postgres -- pg_restore --exit-on-error -j 2 -d laynes_restore $D/$N.dump
# 4. compare with the manifest (row counts / fingerprints)
runuser -u postgres -- /usr/bin/python3 -I /usr/local/lib/laynes-backup/laynes_db_backup.py \
    --config /etc/laynes-backup/backup.toml restore-test --backup $N --mode full  # if space allows
# 5. switch over only with approval: stop timers + API/MCP with zero active jobs,
#    rename databases, start, verify, re-enable timers.
```

Rebuilding a whole server additionally needs the config/state archive (pending)
and the code at the recorded release SHA.

RTO estimate: about 30–60 min to restore 4 GB of data and rebuild 3.9 GB of
indexes on this VPS, plus switch-over. A full restore test will measure it.

## Failure handling

| `stage` in the status file | Meaning | Action |
|---|---|---|
| `preflight` / `insufficient_space` | The dump would pass 84 % disk or 2 GiB free | Free space (approval) or attach storage; never lower the guard to "make it fit" |
| `lock` | Another backup or restore test ran for more than 30 min | `systemctl status laynes-db-*`; check for a hung restore test |
| `snapshot` | Cannot connect to or query PostgreSQL | `systemctl status postgresql@18-main` |
| `pg_dump` | Dump failed: a lock timeout (DDL running) or an I/O error | Read the journal; rerun after the DDL finishes |
| `verify-toc` / `verify-decode` | The archive is unreadable or incomplete | Disk errors? `dmesg`; keep the previous backup |
| `checksum` (restore test) | The file changed after it was written | Bit rot or tampering: investigate before trusting any copy |
| `compare` mismatch | Restored rows or content differ from the snapshot | Treat as a failed backup; keep older copies |

## Rollback

```bash
systemctl disable --now laynes-db-backup.timer laynes-db-restore-test.timer
rm /etc/systemd/system/laynes-db-{backup,restore-test}.{service,timer}; systemctl daemon-reload
# monitor: remove backup_status_glob / restore_test_status_glob from monitor.toml
#   -> back to the legacy check (the HIGH stays open until scheduled backups exist)
```

Rollback does **not** delete `/var/backups/laynes`, the tmpfiles entry or the
installed script. Removing backups is a separate decision.

## Off-server copy (designed, NOT implemented: needs a provider and keys)

- **Destination (recommended):** a Hetzner Storage Box (BX11, 1 TB) in a
  **different location** (fsn1 or hel1).
  - Upload with `rsync` over SSH, using a restricted sub-account limited to one
    directory and an SSH key used only for this.
  - Turn on the box's own automatic snapshots, so a compromised server cannot
    erase the history.
  - Alternative for provider diversity: Backblaze B2 or AWS S3 with object lock
    (needs `rclone`; more cost and setup).
- **Encryption:**
  - The dump is encrypted on the server with a **public key only** (gpg 2.4 is
    installed; `age` would be a new package).
  - The private key is kept **offline** by the owner (password manager plus a
    paper copy), never on the server.
  - A compromised server can then neither read nor decrypt older off-server
    copies.
  - Checksums are recorded for the ciphertext and the plaintext.
- **Job:** `laynes-db-offsite.service` after each backup. It would:
  1. encrypt the dump to a `.partial` file;
  2. upload it;
  3. re-list the remote copy and compare its size and sha256;
  4. write `status/offsite-<UTC>.json` `{"ok", "completed_at", "dump", "object", "sha256"}`,
     which the monitor already reads (`offsite_status_glob`, 30 h → WARNING;
     failure → HIGH);
  5. apply remote retention of 7 daily, 4 weekly and 6 monthly.
- **Restore drill:** each quarter, the owner decrypts one copy with the offline
  key and runs a full restore test elsewhere. This is the only proof that the
  key and the off-server path work.

## Config/state archive (designed, NOT implemented: holds secrets)

- **Contents:** a daily `tar` of:
  - `/etc/laynes`, `/etc/laynes-mcp`, `/etc/laynes-backup`;
  - `/etc/systemd/system/{laynes,hme,r365,revel}-*` and their drop-ins;
  - `/etc/nginx/sites-*` and `/etc/tmpfiles.d/laynes-*`;
  - the release `.env`;
  - `/var/lib/laynes/{monitor,hme,r365,r365-financial}` (SQLite via
    `sqlite3 .backup`).
- **Size:** about 15 MB.
- **Handling:** it contains credentials, so it is only useful **encrypted and
  off-server**, under the same key and destination as above. It ships with the
  off-server job.

Separately: the release `.env` is currently mode **0777**. Its parent `/root` is
0700, which limits exposure, but it should be 0600. That is an owner-approved
change, outside this PR.

## Recovery targets

| | Target | Feasible now? |
|---|---|---|
| RPO (data loss) | 24 h | Yes, once installed (daily dump). Below 24 h would need WAL archiving, not proposed. |
| RTO (time to restore) | 4 h | Yes on the same server (about 1 h). For a new server it needs the off-server copy, the config archive and the runbook. |
| Local retention | 2 days | Yes; 7 daily + 4 weekly needs about 10 GB more storage |
| Off-server retention | 7 daily + 4 weekly + 6 monthly | Needs a destination and keys |
| Restore proof | weekly | critical: now; full: needs storage |

## Estimated costs (Hetzner list prices; confirm in the console)

| Item | Approx. €/month |
|---|---|
| Storage Box BX11 (1 TB), off-server copies | ~4 |
| Cloud Volume 50 GB for local backups and full restore tests | ~2.5 |
| *or* Hetzner Backups (whole server, 7 daily slots; 20 % of the server price) | ~1 |
| CPU and I/O on this VPS | a few minutes per night, plus a weekly test |

## Approvals required

1. **Merge** this PR (after independent review).
2. **Install** the files and units, steps 1–3 above. This is a production change.
3. **Run the first production backup** (step 4) and the first restore test
   (step 5).
4. **Enable the timers** (step 6).
5. **Change the monitor config** (step 7).
6. **Storage**, one of:
   - attach a Cloud Volume, which needs the owner's console access and cost
     approval;
   - or approve pruning old manual dumps in `/root/deploy-backups` or
     `/root/laynes_migrate.dump` (about 3.3 GB). This tool never deletes them.
7. **Off-server:** choose the provider, create the account and sub-account,
   install the SSH key, and generate the gpg key pair (private key held offline
   by the owner).
8. **Hetzner Backups or Snapshots:** the owner confirms in the Cloud Console
   whether they are on.
9. **`.env` permissions:** 0777 → 0600.
