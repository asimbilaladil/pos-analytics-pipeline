#!/usr/bin/env python3
"""Golden tests for the LLM-facing HME layer (migrations 43/44).

Run:  venv/bin/python tests/test_hme_llm_layer.py     (exit 0 = pass)

The boundary these tests defend is narrow and load-bearing: the assistant may
read four VERIFIED-gated views and nothing else. Everything that makes HME
dangerous to expose -- 23 OUT_OF_SCOPE stores belonging to the same tenant,
the HME-internal store number that invites a bogus cross-system join, and
per-day incompleteness that reads as zero if you are careless -- is excluded
structurally rather than by convention, and these tests say so out loud.

Read-only: every statement is a SELECT. Nothing here writes, grants or deploys.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)

import hme_load  # noqa: E402

LLM_VIEWS = ("v_hme_store_daily_llm", "v_hme_outliers_daily_llm",
             "v_hme_goal_history_llm", "v_hme_day_completeness_llm")
RAW_HME = ("hme_store_daily", "hme_outliers_daily", "hme_goal_history",
           "hme_ingest_run", "hme_store_mapping", "hme_goal_conflict",
           "v_hme_store_daily_verified")

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


conn = hme_load.connect()
cur = conn.cursor()

# ---------------------------------------------------------------------------
# 1 -- the views exist and expose exactly the agreed columns
# ---------------------------------------------------------------------------
EXPECTED_COLUMNS = {
    "v_hme_store_daily_llm": [
        "establishment_id", "store_name", "business_date", "drive_thru_cars",
        "regular_cars", "disastrous_cars", "disastrous_pct",
        "lane_total_avg_seconds", "lane_queue_avg_seconds",
        "lane_total_2_avg_seconds", "lane_total_goal_d_seconds",
        "trend_total_cars", "trend_avg_time_seconds"],
    "v_hme_outliers_daily_llm": [
        "establishment_id", "store_name", "business_date", "all_car_records",
        "car_departures", "total_outliers", "outlier_pct", "pull_in_pct",
        "pull_out_pct", "max_over_delete_pct", "discard_pct",
        "manual_delete_pct"],
    "v_hme_goal_history_llm": [
        "establishment_id", "store_name", "effective_from_date",
        "effective_to_date", "goal_a_seconds", "goal_b_seconds",
        "goal_c_seconds", "goal_d_seconds"],
    "v_hme_day_completeness_llm": [
        "business_date", "status", "source_complete", "reconciliation_ok",
        "observed_store_count", "expected_store_count",
        "verified_observed_count", "verified_expected_count",
        "counts_consistent"],
}

for v, cols in EXPECTED_COLUMNS.items():
    cur.execute("""SELECT column_name FROM information_schema.columns
                    WHERE table_name = %s ORDER BY ordinal_position""", (v,))
    got = [r[0] for r in cur.fetchall()]
    check(f"{v} exposes exactly the agreed columns", got == cols,
          f"got {got}" if got != cols else "")

# ---------------------------------------------------------------------------
# 2 -- store identity: hme_store_number must never cross the boundary
# ---------------------------------------------------------------------------
# It is text ('00111', '1158614'), it is NOT the establishment_id, and exposing
# it is what would make a numeric or name-based cross-system join expressible.
for v in LLM_VIEWS:
    cur.execute("""SELECT count(*) FROM information_schema.columns
                    WHERE table_name = %s AND column_name = 'hme_store_number'""", (v,))
    check(f"{v} does NOT expose hme_store_number", cur.fetchone()[0] == 0)

cur.execute("""SELECT count(*) FROM information_schema.columns
                WHERE table_name = ANY(%s)
                  AND (column_name LIKE '%%hme_store%%'
                       OR column_name IN ('run_id', 'ingested_at',
                                          'hme_store_name_seen', 'query_hash',
                                          'model_id', 'source_reports',
                                          'warnings', 'tenant_scope_detail',
                                          'freshness_detail', 'mapping_basis',
                                          'mapping_confidence', 'verified_by'))""",
            (list(LLM_VIEWS),))
check("no Power BI provenance or mapping-provenance column is exposed",
      cur.fetchone()[0] == 0)

# ---------------------------------------------------------------------------
# 3 -- only VERIFIED stores; OUT_OF_SCOPE is structurally impossible
# ---------------------------------------------------------------------------
cur.execute("""SELECT count(*) FROM hme_store_mapping
                WHERE active AND mapping_status = 'VERIFIED'""")
verified_n = cur.fetchone()[0]
cur.execute("""SELECT count(DISTINCT establishment_id) FROM v_hme_store_daily_llm""")
check("daily view exposes exactly the VERIFIED mapped stores",
      cur.fetchone()[0] == verified_n, f"expected {verified_n}")

for v in ("v_hme_store_daily_llm", "v_hme_outliers_daily_llm",
          "v_hme_goal_history_llm"):
    cur.execute(f"""SELECT count(*) FROM {v} x
                     WHERE NOT EXISTS (
                       SELECT 1 FROM hme_store_mapping m
                        WHERE m.revel_establishment_id = x.establishment_id
                          AND m.mapping_status = 'VERIFIED' AND m.active)""")
    check(f"{v} cannot surface a non-VERIFIED store", cur.fetchone()[0] == 0)

# Frisco (store 3) is OUT_OF_SCOPE and has been absent from the source since
# 2026-09-18. It must be invisible for BOTH reasons, independently.
cur.execute("""SELECT mapping_status, active, revel_establishment_id
                 FROM hme_store_mapping WHERE hme_store_number = '3'""")
frisco = cur.fetchone()
check("Frisco/store 3 is still OUT_OF_SCOPE and unmapped to Revel",
      frisco is not None and frisco[0] == 'OUT_OF_SCOPE' and frisco[2] is None,
      str(frisco))
cur.execute("""SELECT count(*) FROM v_hme_store_daily_llm
                WHERE store_name ILIKE '%%frisco%%'""")
check("Frisco cannot appear in the LLM daily view", cur.fetchone()[0] == 0)

cur.execute("""SELECT count(*) FROM hme_store_daily d
                 JOIN hme_store_mapping m USING (hme_store_number)
                WHERE m.mapping_status <> 'VERIFIED'""")
oos_rows = cur.fetchone()[0]
cur.execute("""SELECT count(*) FROM v_hme_store_daily_llm""")
llm_rows = cur.fetchone()[0]
cur.execute("""SELECT count(*) FROM hme_store_daily""")
all_rows = cur.fetchone()[0]
check("OUT_OF_SCOPE store-days exist but are excluded from the LLM view",
      oos_rows > 0 and llm_rows + oos_rows == all_rows,
      f"llm={llm_rows} oos={oos_rows} total={all_rows}")

# ---------------------------------------------------------------------------
# 4 -- Downtown Houston: absent, never synthesized
# ---------------------------------------------------------------------------
cur.execute("SELECT name FROM establishments WHERE id = 48")
row = cur.fetchone()
check("establishment 48 exists in Revel as Downtown Houston",
      row is not None and "Downtown Houston" in row[0], str(row))
cur.execute("SELECT count(*) FROM hme_store_mapping WHERE revel_establishment_id = 48")
check("Downtown Houston has NO HME mapping row", cur.fetchone()[0] == 0)
for v in ("v_hme_store_daily_llm", "v_hme_outliers_daily_llm",
          "v_hme_goal_history_llm"):
    cur.execute(f"SELECT count(*) FROM {v} WHERE establishment_id = 48")
    check(f"{v} returns no synthetic row for Downtown Houston",
          cur.fetchone()[0] == 0)

# ---------------------------------------------------------------------------
# 5 -- percent contract: stored 0-1, exposed 0-100
# ---------------------------------------------------------------------------
# Compare like with like: the stored max must be taken over the SAME
# VERIFIED-only population the view exposes, not over all stores.
cur.execute("""SELECT max(d.disastrous_pct)
                 FROM hme_store_daily d
                 JOIN hme_store_mapping m USING (hme_store_number)
                WHERE m.mapping_status = 'VERIFIED' AND m.active""")
stored_max = float(cur.fetchone()[0])
cur.execute("""SELECT max(disastrous_pct) FROM v_hme_store_daily_llm""")
exposed_max = float(cur.fetchone()[0])
check("disastrous_pct is converted from 0-1 to 0-100",
      stored_max <= 1.0 and 1.0 < exposed_max <= 100.0
      and abs(exposed_max - round(stored_max * 100, 2)) < 0.01,
      f"stored {stored_max} -> exposed {exposed_max}")
# And the whole stored column really is a 0-1 fraction everywhere.
cur.execute("""SELECT count(*) FROM hme_store_daily
                WHERE disastrous_pct IS NOT NULL AND disastrous_pct > 1""")
check("stored disastrous_pct is a 0-1 fraction for every row", cur.fetchone()[0] == 0)

for col in ("outlier_pct", "pull_in_pct", "pull_out_pct",
            "max_over_delete_pct", "discard_pct", "manual_delete_pct"):
    cur.execute(f"""SELECT count(*) FROM v_hme_outliers_daily_llm
                     WHERE {col} IS NOT NULL AND ({col} < 0 OR {col} > 100)""")
    check(f"{col} stays within 0-100", cur.fetchone()[0] == 0)

cur.execute("""SELECT max(outlier_pct) FROM v_hme_outliers_daily_llm""")
check("outlier_pct is on the 0-100 scale, not 0-1",
      float(cur.fetchone()[0]) > 1.0)

# Seconds must stay seconds -- an unconverted lane time is a silent 60x error.
cur.execute("""SELECT min(lane_total_avg_seconds), max(lane_total_avg_seconds)
                 FROM v_hme_store_daily_llm WHERE lane_total_avg_seconds IS NOT NULL""")
lo, hi = cur.fetchone()
check("lane_total_avg_seconds is plausible SECONDS, not minutes",
      lo is not None and lo > 20 and hi < 3600, f"{lo}..{hi}")

# ---------------------------------------------------------------------------
# 6 -- goal history: effective-dated selection, no backdating
# ---------------------------------------------------------------------------
cur.execute("""SELECT min(effective_from_date) FROM v_hme_goal_history_llm""")
first_goal = cur.fetchone()[0]
check("goal history has a first observed period", first_goal is not None,
      str(first_goal))

# The documented date-scoped join must return exactly one goal row per store.
cur.execute("""
    SELECT count(*) FROM (
      SELECT d.establishment_id, d.business_date, count(g.*) AS n
        FROM v_hme_store_daily_llm d
        LEFT JOIN v_hme_goal_history_llm g
               ON g.establishment_id = d.establishment_id
              AND d.business_date >= g.effective_from_date
              AND (g.effective_to_date IS NULL
                   OR d.business_date <= g.effective_to_date)
       GROUP BY 1, 2 HAVING count(g.*) <> 1) x""")
check("effective-dated goal join yields exactly one goal per store-day",
      cur.fetchone()[0] == 0)

# Goal D comparison for a real date must resolve and be usable.
cur.execute("""
    SELECT count(*) FROM v_hme_store_daily_llm d
      JOIN v_hme_goal_history_llm g
        ON g.establishment_id = d.establishment_id
       AND d.business_date >= g.effective_from_date
       AND (g.effective_to_date IS NULL OR d.business_date <= g.effective_to_date)
     WHERE d.business_date = '2026-09-19' AND g.goal_d_seconds IS NOT NULL""")
check("Goal D is resolvable for every VERIFIED store on 2026-09-19",
      cur.fetchone()[0] == verified_n)

# A date before the first observed goal period must return NOTHING, so the
# assistant answers "unavailable" rather than reusing the oldest known goal.
cur.execute("""
    SELECT count(*) FROM v_hme_goal_history_llm
     WHERE %s >= effective_from_date
       AND (effective_to_date IS NULL OR %s <= effective_to_date)""",
            (first_goal - __import__("datetime").timedelta(days=1),) * 2)
check("a date before the first observed goal period returns no goal (unavailable)",
      cur.fetchone()[0] == 0)

check("goal view is HISTORY, not current-only (effective_to_date is exposed)",
      "effective_to_date" in EXPECTED_COLUMNS["v_hme_goal_history_llm"])

# ---------------------------------------------------------------------------
# 7 -- completeness: independent axes, correct run selection
# ---------------------------------------------------------------------------
cur.execute("""SELECT business_date, status, source_complete, reconciliation_ok,
                      observed_store_count, expected_store_count,
                      verified_observed_count, verified_expected_count
                 FROM v_hme_day_completeness_llm WHERE business_date = '2026-09-19'""")
d19 = cur.fetchone()
check("2026-09-19 is exposed as partial, incomplete, but reconciled",
      d19 is not None and d19[1] == 'partial' and d19[2] is False
      and d19[3] is True and d19[4] == 33 and d19[5] == 34
      and d19[6] == 11 and d19[7] == 11, str(d19))

# 2026-09-19 has TWO analytical runs: 208 (unreconciled) and 209 (corrected).
# The view must reflect the latest, or the assistant reports a stale warning.
cur.execute("""SELECT count(*) FROM hme_ingest_run
                WHERE business_date = '2026-09-19' AND status IN ('succeeded','partial')""")
check("2026-09-19 genuinely has multiple analytical runs",
      cur.fetchone()[0] >= 2)
cur.execute("""SELECT count(*) FROM v_hme_day_completeness_llm
                WHERE business_date = '2026-09-19'""")
check("completeness exposes exactly one row per business_date",
      cur.fetchone()[0] == 1)

# Failed / security-aborted runs must never become analytical truth.
vdef = None
cur.execute("SELECT pg_get_viewdef('v_hme_day_completeness_llm'::regclass, true)")
vdef = cur.fetchone()[0]
check("completeness view selects on an explicit status IN list, not status <> 'failed'",
      "succeeded" in vdef and "partial" in vdef and "<> 'failed'" not in vdef
      and "<> ('failed'" not in vdef)
cur.execute("""SELECT count(*) FROM hme_ingest_run r
                WHERE r.status NOT IN ('succeeded','partial')
                  AND NOT EXISTS (SELECT 1 FROM hme_ingest_run r2
                                   WHERE r2.business_date = r.business_date
                                     AND r2.status IN ('succeeded','partial'))
                  AND r.business_date IN (SELECT business_date
                                            FROM v_hme_day_completeness_llm)""")
check("a date whose only run failed never appears in the completeness view",
      cur.fetchone()[0] == 0)

# The two axes must be able to disagree in both directions.
cur.execute("""SELECT count(*) FROM v_hme_day_completeness_llm
                WHERE reconciliation_ok IS TRUE AND source_complete IS FALSE""")
check("reconciliation_ok=true coexists with source_complete=false",
      cur.fetchone()[0] >= 1)
cur.execute("""SELECT count(*) FROM v_hme_day_completeness_llm
                WHERE source_complete IS NULL""")
check("source_complete is never NULL (legacy runs fall back to a derived value)",
      cur.fetchone()[0] == 0)
cur.execute("""SELECT count(*) FROM v_hme_day_completeness_llm
                WHERE counts_consistent IS NOT TRUE AND observed_store_count IS NOT NULL
                  AND business_date >= '2026-09-19'""")
check("observed counts add up on runs written by hme_load >= 1.4.0",
      cur.fetchone()[0] == 0)

# ---------------------------------------------------------------------------
# 8 -- missing row means UNKNOWN, never zero
# ---------------------------------------------------------------------------
cur.execute("""SELECT count(*) FROM v_hme_store_daily_llm
                WHERE business_date = '2026-09-19' AND drive_thru_cars = 0""")
check("no zero-car row was invented for 2026-09-19", cur.fetchone()[0] == 0)
cur.execute("""SELECT count(*) FROM v_hme_store_daily_llm d
                WHERE d.business_date = '2026-09-19'""")
check("2026-09-19 exposes only the VERIFIED stores actually observed",
      cur.fetchone()[0] == verified_n)

# ---------------------------------------------------------------------------
# 9 -- cross-system join grain
# ---------------------------------------------------------------------------
cur.execute("""SELECT count(*) FROM (
                 SELECT establishment_id, business_date
                   FROM v_hme_store_daily_llm
                  GROUP BY 1,2 HAVING count(*) > 1) x""")
check("establishment_id + business_date is a unique grain in the daily view",
      cur.fetchone()[0] == 0)
cur.execute("""SELECT count(*) FROM v_hme_store_daily_llm d
                 JOIN establishments e ON e.id = d.establishment_id
                WHERE d.business_date = '2026-09-19'""")
check("the only cross-system path (establishment_id) joins cleanly to Revel",
      cur.fetchone()[0] == verified_n)
# No store x hour HME fact exists -- an hourly cross-system claim is unsupported.
cur.execute("""SELECT count(*) FROM information_schema.columns
                WHERE table_name LIKE 'hme%%'
                  AND (column_name ILIKE '%%hour%%' OR column_name ILIKE '%%daypart%%')""")
check("no HME hour/daypart column exists anywhere (hourly analysis unsupported)",
      cur.fetchone()[0] == 0)

# ---------------------------------------------------------------------------
# 10 -- no PII anywhere on the exposed surface
# ---------------------------------------------------------------------------
cur.execute("""SELECT count(*) FROM information_schema.columns
                WHERE table_name = ANY(%s)
                  AND (column_name ILIKE '%%employee%%' OR column_name ILIKE '%%customer%%'
                       OR column_name ILIKE '%%email%%'  OR column_name ILIKE '%%phone%%'
                       OR column_name ILIKE '%%user%%'   OR column_name ILIKE '%%person%%')""",
            (list(LLM_VIEWS),))
check("no PII-shaped column on any LLM view", cur.fetchone()[0] == 0)
cur.execute("""SELECT count(*) FROM information_schema.columns
                WHERE table_name LIKE 'hme%%'
                  AND (column_name ILIKE '%%employee%%' OR column_name ILIKE '%%customer%%'
                       OR column_name ILIKE '%%email%%'  OR column_name ILIKE '%%phone%%')""")
check("no PII exists in the underlying HME tables either", cur.fetchone()[0] == 0)

# ---------------------------------------------------------------------------
# 11 -- the boundary is still CLOSED (migration 44 not applied)
# ---------------------------------------------------------------------------
cur.execute("""SELECT count(*) FROM information_schema.role_table_grants
                WHERE grantee = 'laynes_ro' AND table_name LIKE 'hme%%'""")
check("laynes_ro has NO grant on any raw hme_* relation", cur.fetchone()[0] == 0)

cur.execute("""SELECT table_name, privilege_type
                 FROM information_schema.role_table_grants
                WHERE grantee = 'laynes_ro' AND table_name LIKE 'v_hme%%'
                ORDER BY table_name""")
grants = cur.fetchall()
check("migration 44 IS applied: laynes_ro can read exactly the four LLM views",
      sorted(g[0] for g in grants) == sorted(LLM_VIEWS), str(grants))
check("the grant is SELECT only",
      all(g[1] == "SELECT" for g in grants), str(grants))

# Migration 44 must, when applied, touch only the four views.
mig44 = open("migrations/44_hme_llm_grants.sql").read()
check("migration 44 grants SELECT only", "GRANT SELECT" in mig44
      and not any(w in mig44.upper() for w in ("INSERT", "UPDATE", "DELETE", "ALL PRIVILEGES")))
for raw in RAW_HME:
    check(f"migration 44 does not grant {raw}",
          f"GRANT SELECT ON {raw} " not in mig44)
for v in LLM_VIEWS:
    check(f"migration 44 grants {v}", f"GRANT SELECT ON {v}" in mig44)

# ---------------------------------------------------------------------------
# 12 -- SQL validator: raw HME rejected, views not yet allowlisted
# ---------------------------------------------------------------------------
import chat_sql  # noqa: E402

for raw in RAW_HME:
    check(f"{raw} is NOT in the production allowlist",
          raw not in chat_sql._ALLOWED_RELATIONS)
for v in LLM_VIEWS:
    check(f"{v} IS allowlisted for the assistant", v in chat_sql._ALLOWED_RELATIONS)


# chat_sql._validate is the real entry point. An earlier version of this file
# called a non-existent _validate_sql, so every "rejected" assertion passed on
# the AttributeError instead of on the policy -- vacuously green. Bind to the
# attribute at import time so a rename fails loudly here rather than silently
# disarming the whole section.
_VALIDATE = chat_sql._validate


def _rejected(sql):
    """True when the validator refuses the statement on POLICY grounds."""
    try:
        _VALIDATE(sql)
        return False
    except chat_sql.SqlError:
        return True


for raw in RAW_HME:
    check(f"validator rejects a direct SELECT from {raw}",
          _rejected(f"SELECT * FROM {raw}"))

# Spelling variants: the validator is parse-tree based, so these must all fail
# for the same reason rather than needing individual patterns.
check("validator rejects a quoted raw HME relation",
      _rejected('SELECT * FROM "hme_store_daily"'))
check("validator rejects raw HME via implicit (SQL-89) join",
      _rejected("SELECT * FROM orders_v2 o, hme_store_daily h"))
check("validator rejects raw HME inside a CTE",
      _rejected("WITH x AS (TABLE hme_store_daily) SELECT * FROM x"))
check("validator rejects raw HME behind an alias",
      _rejected("SELECT * FROM hme_store_daily AS d"))
check("validator rejects a schema-qualified raw HME relation",
      _rejected("SELECT * FROM public.hme_store_daily"))
check("validator rejects the internal verified view too",
      _rejected("SELECT * FROM v_hme_store_daily_verified"))
for v in LLM_VIEWS:
    check(f"validator ACCEPTS {v}", not _rejected(f"SELECT * FROM {v}"))

# ---------------------------------------------------------------------------
# 13 -- the boundary as laynes_ro actually experiences it
#
# information_schema says what was granted; this proves what the role can
# really read. The two disagreeing is exactly the failure worth catching.
# ---------------------------------------------------------------------------
import psycopg2 as _pg                                # noqa: E402

_ro = _pg.connect(host=os.environ["DB_HOST"], port=os.environ["DB_PORT"],
                  user=os.environ["DB_RO_USER"], password=os.environ["DB_RO_PASS"],
                  dbname=os.environ["DB_NAME"])
try:
    for v in LLM_VIEWS:
        c = _ro.cursor()
        try:
            c.execute(f"SELECT count(*) FROM {v}")
            c.fetchone(); ok = True; detail = ""
        except Exception as e:
            ok = False; detail = type(e).__name__
        finally:
            _ro.rollback()
        check(f"laynes_ro CAN read {v}", ok, detail)

    for raw in RAW_HME:
        c = _ro.cursor()
        try:
            c.execute(f"SELECT count(*) FROM {raw}")
            c.fetchone(); denied = False
        except _pg.errors.InsufficientPrivilege:
            denied = True
        except Exception:
            denied = True
        finally:
            _ro.rollback()
        check(f"laynes_ro is DENIED {raw}", denied)

    # Store isolation, proven through the role rather than as the owner.
    c = _ro.cursor()
    c.execute("SELECT count(DISTINCT establishment_id) FROM v_hme_store_daily_llm")
    check("laynes_ro sees only the 11 VERIFIED establishments",
          c.fetchone()[0] == 11)
    c.execute("SELECT count(*) FROM v_hme_store_daily_llm WHERE store_name ILIKE '%%frisco%%'")
    check("laynes_ro cannot see Frisco (OUT_OF_SCOPE)", c.fetchone()[0] == 0)
    c.execute("SELECT count(*) FROM v_hme_store_daily_llm WHERE establishment_id = 48")
    check("laynes_ro sees no synthetic Downtown Houston row", c.fetchone()[0] == 0)
    try:
        c.execute("SELECT hme_store_number FROM v_hme_store_daily_llm LIMIT 1")
        no_col = False
    except Exception:
        no_col = True
    finally:
        _ro.rollback()
    check("hme_store_number cannot be selected through the safe layer", no_col)
finally:
    _ro.close()

conn.close()

# ---------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nHME LLM layer tests: {passed} passed, {failed} failed, {len(results)} total")
if failed:
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} -- {d}")
sys.exit(1 if failed else 0)
