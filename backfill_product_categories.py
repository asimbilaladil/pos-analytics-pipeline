"""
backfill_product_categories.py
===============================
Task 08 — backfill product_categories from the complete Revel ProductCategory
resource. Scope: product_categories ONLY (per explicit instruction) — does
NOT touch products.category_id.

    API (/products/ProductCategory/) -> raw .json.gz (Task 06) -> parser -> Postgres

ProductCategory is small, establishment-independent reference data, so this
is a full fetch (no date windowing, no chunking) — every run pulls the
complete resource and UPSERTs it. That also makes reruns naturally
idempotent: same input, same UPSERT, no accumulating state to resume from.

"The complete resource" is actually TWO fetches, not one: Revel's list
endpoint defaults to active=True with no active__in filter available, so
an unfiltered/default fetch silently excludes inactive (soft-deleted)
categories — discovered when 521 rows referenced a parent_id absent from
a 2,595-row default fetch; all 164 unique missing parent IDs turned out
to be individually retrievable and present in an explicit active=False
fetch (763 rows, disjoint from the 2,595). See CATEGORY_FETCH_SETS.

Uses pipeline.fetch_all_pages (the one canonical pager/archiver),
sync_updated.fetch_all_categories (the one canonical active+inactive
fetch, also used by the production REVEL_SYNC_MODE=updated path — see
sync_updated.sync_reference_data), and pipeline.upsert_product_categories
(the one canonical UPSERT) — no duplicate fetch or parsing logic here.
"""

import os
import sys
import json
import glob
import logging
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

import pipeline as P
import raw_archive
import sync_updated as S
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def archived_meta_files(run_id: str) -> list:
    """All .meta.json files Task06 archived for this run (product_categories
    has no establishment_id segment)."""
    today = date.today()
    pattern = os.path.join(
        raw_archive.RAW_ARCHIVE_DIR, "product_categories",
        f"{today.year:04d}", f"{today.month:02d}",
        f"run_{run_id}", "*.meta.json",
    )
    return sorted(glob.glob(pattern))


def fetch_categories(run_id_prefix: str) -> tuple:
    """Thin wrapper: opens the Playwright session, delegates the actual
    active+inactive fetch to sync_updated.fetch_all_categories (shared with
    the production sync path). Returns (records, {"active": run_id,
    "inactive": run_id})."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state="/tmp/revel_session.json")
        records, run_ids = S.fetch_all_categories(context, run_id_prefix)
        browser.close()
    return records, run_ids


def validate_and_report(records: list, run_ids: dict) -> dict:
    report = {}

    # ── archive files + per-set meta.total_count (read back from Task06) ──
    meta_files_by_set = {label: archived_meta_files(rid) for label, rid in run_ids.items()}
    all_meta_files = [f for files in meta_files_by_set.values() for f in files]
    report["archive_files"] = len(all_meta_files) * 2  # .json.gz + .meta.json per page
    report["api_pages"] = len(all_meta_files)

    meta_total_by_set = {}
    for label, files in meta_files_by_set.items():
        totals = set()
        for mf in files:
            with open(mf) as f:
                m = json.load(f)
            if m.get("total_count") is not None:
                totals.add(m["total_count"])
        meta_total_by_set[label] = sorted(totals)
    report["meta_total_count_by_set"] = meta_total_by_set
    report["fetched_count"] = len(records)

    # ── duplicate ID check across the COMBINED (active+inactive) set —
    # confirmed live the two sets are disjoint, so any duplicate here would
    # be a real anomaly, not just double-counting the same category ──
    ids = [r.get("id") for r in records if r.get("id")]
    dupes = {i for i in ids if ids.count(i) > 1}
    report["duplicate_ids"] = sorted(dupes)

    # ── row count validation: deduplicated union of fetched IDs, NOT
    # meta_total_count_by_set["active"] alone (that count excludes inactive
    # categories by construction — see CATEGORY_FETCH_SETS comment) ──
    id_set = set(ids)
    report["unique_fetched_ids"] = len(id_set)

    # ── hierarchy validation: every non-null parent_id resolves to a
    # category present in the COMBINED fetch ──
    orphans = []
    for r in records:
        parent = P.extract_id(r.get("parent"))
        if parent is not None and parent not in id_set:
            orphans.append({"id": r.get("id"), "name": r.get("name"), "missing_parent_id": parent})
    report["orphan_parent_refs"] = orphans

    return report


def run_backfill():
    conn = P.db_connect()
    run_id_prefix = raw_archive.new_run_id()
    log.info("=== Task 08 ProductCategory backfill — run_id_prefix=%s ===", run_id_prefix)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM product_categories")
        before_count = cur.fetchone()[0]

    records, run_ids = fetch_categories(run_id_prefix)
    log.info("Fetched %d ProductCategory records total (active+inactive)", len(records))

    report = validate_and_report(records, run_ids)
    if report["duplicate_ids"]:
        raise RuntimeError(f"duplicate IDs across active+inactive fetch: {report['duplicate_ids']}")
    if report["orphan_parent_refs"]:
        raise RuntimeError(f"{len(report['orphan_parent_refs'])} unresolved parent refs after combining active+inactive")

    rows = [S.build_product_category_row(c) for c in records if c.get("id")]

    with conn.cursor() as cur:
        stats = P.upsert_product_categories(cur, rows)  # raises + rolls back if its own validation finds a dangling parent_id
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM product_categories")
        after_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM product_categories WHERE parent_id IS NOT NULL")
        with_parent = cur.fetchone()[0]
        cur.execute("""
            SELECT c.id, c.name, c.parent_id FROM product_categories c
            LEFT JOIN product_categories p ON p.id = c.parent_id
            WHERE c.parent_id IS NOT NULL AND p.id IS NULL
        """)
        db_orphans = cur.fetchall()

    conn.close()

    report.update({
        "run_id_prefix": run_id_prefix,
        "run_ids": run_ids,
        "before_count": before_count,
        "after_count": after_count,
        # the validation the user specified: DB count must equal the
        # deduplicated union of IDs fetched from BOTH sets, not the
        # active-only endpoint's meta.total_count (2,595) which by
        # construction excludes the 763 inactive categories.
        "db_count_matches_unique_fetched_ids": after_count == report["unique_fetched_ids"],
        "upsert_stats": stats,
        "with_parent": with_parent,
        "db_orphan_parent_refs": db_orphans,
    })
    return report


if __name__ == "__main__":
    r = run_backfill()
    print(json.dumps(r, indent=2, default=str))
