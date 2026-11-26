# Proposal: scheduled PostgreSQL backups with verification

> **Superseded by `docs/DATABASE_BACKUP.md`** (2026-11-26), which implements this
> proposal with these corrections: it adds snapshot-consistent row counts and
> fingerprints, a disk guard, a critical-table restore mode, and local retention
> sized for the current disk. It also records that the monitor's file-age check
> could be satisfied by a partial pre-deploy dump.
> This file is kept as the record of the original proposal.

**Status: PROPOSAL ONLY.** Nothing here has been installed, scheduled or run. PR
#38 adds no backup job. It does not create, delete or modify backups.

## What exists today (checked read-only, 2026-11-25)

- There is no scheduled `pg_dump`, `pg_basebackup` or WAL archiving on the host.
- The only dumps are manual pre-deploy archives under `/root/deploy-backups/*/`.
  They are pg_dump custom format, and the newest is from 2026-11-17.
- It is unknown whether the hosting provider takes disk snapshots. Someone with
  access to the provider console needs to confirm.

## What the monitor can and cannot say

`infra.backups` reports:

- **Freshness.** The newest local dump (≥ 1 MiB) and its modification time. It
  is stale after 26 h.
- **Format.** The first bytes must be a pg_dump header (custom `PGDMP` or plain
  SQL). That proves the file looks like a dump, not that it restores.
- **Offsite copy.** Only if a status file exists. Otherwise the dashboard says
  "not monitored".
- **Restore test.** Only if a status file exists. Otherwise "not monitored".
- **Newest verified backup.** Reported only from a successful restore test.

It never calls a backup recoverable based on file time or size.

## Proposed automation (needs approval; separate change)

1. **Nightly dump at 02:30 Chicago.** This avoids the 04:15 to 11:30 ingestion
   window.
   - Command:
     `pg_dump -Fc -Z 6 --no-owner -d laynes -f /var/backups/laynes/laynes-<UTC>.dump`.
   - Run it as a dedicated role with read access, or as postgres locally.
   - Use `ionice -c3` and `nice 10`.
   - Keep 7 daily and 4 weekly copies.
   - Disk budget: check the dump size first. The disk alert at 85% guards the
     host.
2. **Offsite copy.** Copy the dump to object storage in another provider or
   region, encrypted client-side. Write the status file
   `/var/backups/laynes/status/offsite-<UTC>.json` with
   `{"ok": true, "completed_at": ISO, "dump": path, "object": key, "sha256": …}`.
3. **Weekly restore test.**
   - Restore the newest dump into a throwaway local cluster on a non-production
     port, the same way the isolated test databases are built.
   - Run row-count and checksum queries on key tables: orders, payments, HME
     store-day, R365 daily.
   - Write `/var/backups/laynes/status/restore-<UTC>.json` with
     `{"ok": bool, "completed_at": ISO, "dump": path, "detail": …}`.
   - Drop the cluster afterwards.
4. **Enable the monitor's evidence checks:**

   ```toml
   [infra]
   backup_globs = ["/var/backups/laynes/*.dump"]
   offsite_status_glob = "/var/backups/laynes/status/offsite-*.json"
   restore_test_status_glob = "/var/backups/laynes/status/restore-*.json"
   ```

   The thresholds would be: local stale after 26 h; offsite stale after 30 h;
   restore test stale after 8 days; a failed restore is HIGH.

Rollback: disable the timers. The dumps are plain files and can be deleted if
space is needed.

Open decisions for the owner:

- the offsite destination and who holds its credentials;
- the retention period;
- whether provider snapshots exist and should count as the offsite copy.
