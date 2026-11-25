# Backup monitoring: provider VPS backups and local PostgreSQL dumps

`infra.backups` reports two kinds of backup **separately**. Neither is ever
called "restore verified" without a recorded restore test.

| | Provider VPS backups | Local PostgreSQL dumps |
|---|---|---|
| What | Hetzner Cloud automatic backups: whole-disk images of the `revel-system` VPS, taken and stored by Hetzner (7 rolling, daily) | `pg_dump` files on this host (today only the manual pre-deploy dumps under `/root/deploy-backups/`) |
| Restore unit | the whole server (crash-consistent disk image) | the database, logically |
| Monitor check key | `infrastructure:provider_backup` | `infrastructure:backup_freshness` |
| Evidence | Hetzner Cloud API (read-only) | file modification time and the pg_dump header |
| Proves | a backup image exists, is `available`, and how old it is | a dump file was written recently and looks like a dump |
| Does **not** prove | that the image restores, or that PostgreSQL inside it is consistent | that the dump restores |

**Owner evidence (2026-11-25, Hetzner Cloud Console, screenshot):** automatic
backups are enabled on `revel-system` (CAX11). There are 7 available backups;
the newest is 2026-11-24 22:29 UTC, 10.65 GB. This is recorded here as context.
It is **not** used to resolve any incident: only the automated check can do that.

## Verdicts

### Provider (`provider_backup`)

| Situation | Result |
|---|---|
| Newest `available` backup ≤ `provider_max_age_hours` (30 h) old | **OK** ("current, not restore-verified") |
| That, but fewer than `provider_min_available` available | WARNING `provider_backup_few` (a recent backup still exists) |
| `server.backup_window` is null | HIGH `provider_backups_disabled` |
| No image with status `available` | HIGH `provider_backup_none_available` |
| Newest available backup older than the limit | HIGH `provider_backup_stale` |
| API unreachable, 401/403/429/5xx, invalid response, metadata service unreachable | **UNKNOWN**. Any open incident is left exactly as it was: not resolved, not escalated. |
| `provider_backup = "hetzner"` but no token in the environment | **UNKNOWN** ("not monitored (no API token)") |
| UNKNOWN continuously for `provider_unknown_alert_hours` (24 h) with a token configured | WARNING `provider_backup_status_unknown`: "status could not be verified", which explicitly does *not* mean backups are missing. It clears when the API answers again. |
| `provider_backup = ""` (the default) | not evaluated; the dashboard says "not monitored" |

A newer image that is still `creating` is ignored for age; the newest
**available** one decides.

### Local dump (`backup_freshness`)

| Provider verdict this run | Local dump missing or older than 26 h |
|---|---|
| OK | `local_dump_severity_when_provider_ok` (default **WARNING**): "Local PostgreSQL dump is stale (provider VPS backup is current)" |
| FAIL | `backup_severity` (HIGH): "...(provider VPS backup is NOT current)" |
| UNKNOWN or not monitored | `backup_severity` (HIGH): "...(provider VPS backup status unknown)" |

The local incident is about local dumps only. A current provider backup
**de-escalates** it (no alert) but never resolves it; only a fresh local dump
does. Why it stays open: a disk image restores the whole server to the
snapshot time, while a logical dump allows a database-only restore. They cover
different failure modes. If the owner decides local dumps are not wanted, set
`local_dump_severity_when_provider_ok` and/or `backup_globs` accordingly. That
is a policy decision, not a monitor default.

**Restore verification** stays as before. `newest_verified_backup` and "restore
verified" come **only** from a restore-test status file that records `ok: true`
(docs/BACKUP_PROPOSAL.md). No provider or local check sets it.

## How the status is read (read-only)

```
GET https://api.hetzner.cloud/v1/servers/{id}                                   -> server.backup_window
GET https://api.hetzner.cloud/v1/images?type=backup&bound_to={id}&sort=created:desc&per_page=50
                                                                                -> id, status, created, image_size
```

- **GET only.** The client has no other HTTP method and calls no `/actions` endpoint,
  so it cannot create, enable, disable or delete backups. A test asserts this from
  the source.
- **Server id.** `provider_server_id = "auto"` reads
  `http://169.254.169.254/hetzner/v1/metadata/instance-id`, Hetzner's link-local
  metadata service. That call needs no credential and sends no token.
- **Calls.** 2 per hourly backup check, about 48 a day, against Hetzner's limit
  of 3,600 per hour.
- **Timeouts.** 10 s for the API and 3 s for the metadata service. Any failure
  gives UNKNOWN, never a crash: the check stays healthy, and so does the run.

## Credential

- Create a **project API token with the Read permission**: Hetzner Console →
  project → Security → API Tokens → Generate, permission **Read**. Never
  Read & Write.
  - Hetzner tokens are project-scoped. A Read token can list, but not change,
    every resource in the project. That is the minimum Hetzner offers; a token
    scoped to one server does not exist.
  - Use a project that contains only this server if possible.
- Store it as `MONITOR_HCLOUD_TOKEN=…` in `/etc/laynes/monitor.env` (0600 root),
  next to the database credential. Never put it in Git, `monitor.toml`,
  arguments or logs.
- The client sends it only in the `Authorization` header to the configured https
  API URL (https is enforced by config validation).
- Error messages contain only the HTTP status and Hetzner's error code, never
  the token or response bodies. Tests check that a token echoed by the API does
  not leak.
- Rotation: generate a new Read token, replace the line, then revoke the old one
  in the Console. No restart is needed; the next hourly check reads it.
- Revocation shows up as 401, then UNKNOWN, then after 24 h a WARNING. It never
  shows as "no backups".

## Enabling it (production change, approval required)

1. The owner creates the Read token in the Hetzner Console.
2. Append `MONITOR_HCLOUD_TOKEN=…` to `/etc/laynes/monitor.env` (keep it 0600).
3. In `/etc/laynes/monitor.toml`, under `[infra]`: `provider_backup = "hetzner"`.
4. The next data run (within 15 min; the check is hourly) records
   `provider_backup`. Read it on the dashboard, or with
   `python -m monitoring status`.

Rollback: remove the `provider_backup` line (or set it to `""`) and the token
line. The provider result then stops being produced; the local-dump wording
reverts to "status unknown".

No duplicate backup scheduler is installed. Hetzner's own schedule is the
provider backup.
