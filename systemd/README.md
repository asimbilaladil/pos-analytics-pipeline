# systemd units — loyalty/labour daily sync

Tracked copies of the units that are live in `/etc/systemd/system/`. They are
byte-for-byte identical to production; edit here, then reinstall.

## Install

    sudo cp systemd/laynes-loyalty-labor-sync.* /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now laynes-loyalty-labor-sync.timer

## Verify the schedule after ANY change

    systemctl list-timers laynes-loyalty-labor-sync.timer

The next elapse is printed in the system timezone, which is UTC on this host.
It must read **09:15 UTC during CDT** and **10:15 UTC during CST** — both are
04:15 America/Chicago. If it reads 04:15 UTC the timezone is being ignored and
the job is running five or six hours early.

## Why the timezone lives in the OnCalendar expression

`OnCalendar=*-*-* 04:15:00 America/Chicago` (systemd 252+).

A `Timezone=` key in `[Timer]` **does not exist**. It is accepted silently and
ignored — verified on this host, where it produced a next elapse of 04:15 UTC.
This is the same failure as the crontab's bare `TZ=America/Chicago`, which is
likewise not honoured here (its `0 9 * * *` entry fires at 09:00 UTC = 04:00
Chicago).

## Secrets

Neither unit carries credentials, and there is no `EnvironmentFile=`.
`sync_loyalty_labor.py` loads `.env` itself via python-dotenv. Keep it that way:
`.env` is gitignored and mode 600, and unit files are world-readable.

## What the job does

Incremental sync of `order_loyalty_v2` and `timesheet_entries_v2` from Revel on
an `updated_date` window with 48h overlap. The overlap absorbs DST movement and
the host's unreliable cron timezone handling, and matters for labour especially:
`clock_out` is NULL while a shift is open, so an overnight shift is stored with
NULL hours and corrected once it closes.

Loyalty runs with raw archiving disabled (`resource=None`) because
`Order.gift_reward_data` embeds plaintext PII. That must not change.

Daily runs checkpoint under `order_loyalty_v2_daily` and
`timesheet_entries_v2_daily`, separate from the historical backfill resources.

---

# revel-items-selfheal (service + timer)

Re-fetches OrderItems (+ nested ModifierItems) and OrderHistory for paid
orders in the last 7 days that have **no item rows**, then drains the
feature recompute queue. Added after 2026-10-25/26, when OOM kills during the
items fetch left thousands of paid orders at two stores without items (the orders
watermark had already advanced, so the next sync never revisited them).

    sudo cp systemd/revel-items-selfheal.* /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now revel-items-selfheal.timer
    systemctl list-timers revel-items-selfheal.timer   # 16:30/20:30 UTC in CDT, 17:30/21:30 in CST

* Runs from `/root/pos-analytics-pipeline-release`; writes only
  order_items_v2 / modifier_items_v2 / order_history_v2 (idempotent upserts)
  and feature_recompute_queue — never orders or payments.
* Exit 75 = skipped (daily sync running, another repair holds the lock, or
  low memory) and counts as success; the next elapse retries.
* A run with nothing missing is a single read-only query and never opens a
  browser.
* Manual repair of a dated range (checkpointed, resumable):

      venv/bin/python repair_missing_items.py --establishments 15,54 \
          --start 2026-10-18 --end 2026-10-23 --checkpoint /path/ck.json

# revel-daily-sync from the release checkout

`systemd/revel-daily-sync-release.conf` is installed as
`/etc/systemd/system/revel-daily-sync.service.d/10-release.conf`. `run.sh`
now resolves `APP_DIR` from its own location, so the release checkout runs
its own merged code; the drop-in supplies the shared venv and the optional
step 1b script. Delete the drop-in and daemon-reload to roll back.
