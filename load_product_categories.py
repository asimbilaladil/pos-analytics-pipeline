#!/usr/bin/env python3
"""Populate product_category_mapping from the newest archived Product snapshot.

    venv/bin/python load_product_categories.py [--run RUN_ID]

Reads the raw Revel archive rather than calling the API, so it is reproducible
and costs nothing: raw_archive already stores every Product page the nightly
sync fetched. Re-runnable -- it upserts by product_id and refreshes
category_stable_since from each product's updated_date.

This does NOT touch products.category_id: the nightly sync owns that table and
would overwrite it. It also does not read product_class, which is a different
namespace (see A3 and migration 31).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

ARCHIVE = os.getenv("RAW_ARCHIVE_DIR", "/var/lib/laynes/raw_revel")
_CAT_URI = re.compile(r"/ProductCategory/(\d+)/")


def _sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout


def newest_run(resource: str = "products") -> str:
    runs = sorted(
        d for d in _sh("sudo", "find", f"{ARCHIVE}/{resource}", "-type", "d",
                       "-name", "run_*").split()
    )
    if not runs:
        sys.exit(f"no archived {resource} runs under {ARCHIVE}")
    return runs[-1]


def read_products(run_dir: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for page in _sh("sudo", "find", run_dir, "-name", "*.json.gz").split():
        raw = subprocess.run(["sudo", "cat", page], capture_output=True).stdout
        for o in json.loads(gzip.decompress(raw)).get("objects", []):
            cat = o.get("category")
            m = _CAT_URI.search(cat) if cat else None
            est = o.get("establishment") or ""
            est_m = re.search(r"/Establishment/(\d+)/", est)
            out[o["id"]] = {
                "category_id": int(m.group(1)) if m else None,
                "establishment_id": int(est_m.group(1)) if est_m else None,
                "updated": (o.get("updated_date") or "")[:10] or None,
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="archive run directory (default: newest)")
    args = ap.parse_args()

    run_dir = args.run or newest_run()
    snapshot_at = datetime.now(timezone.utc)
    products = read_products(run_dir)
    print(f"run       : {run_dir}")
    print(f"products  : {len(products)}")
    print(f"with cat  : {sum(1 for v in products.values() if v['category_id'])}")

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", 5432),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.environ["DB_USER"], password=os.environ["DB_PASS"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, parent_id FROM product_categories")
            cats = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

            rows = []
            for pid, p in products.items():
                cid = p["category_id"]
                cname, parent_id = cats.get(cid, (None, None))
                pname = cats.get(parent_id, (None, None))[0] if parent_id else None
                rows.append((
                    pid, p["establishment_id"], cid, cname, parent_id, pname,
                    p["updated"], p["updated"], None,
                    "revel_product_api" if cid else "unresolved",
                    "verified_current" if cid else "unknown",
                    snapshot_at,
                    "Category read from Revel Product.category. Historical "
                    "validity is period-relative via category_stable_since; the "
                    "archive holds no snapshot older than 2026-09-02.",
                ))

            psycopg2.extras.execute_values(cur, """
                INSERT INTO product_category_mapping (
                    product_id, establishment_id, category_id,
                    category_name_snapshot, parent_category_id,
                    parent_category_name_snapshot, category_stable_since,
                    effective_from, effective_to, mapping_source,
                    mapping_confidence, snapshot_taken_at, review_note)
                VALUES %s
                ON CONFLICT (product_id) DO UPDATE SET
                    establishment_id = EXCLUDED.establishment_id,
                    category_id = EXCLUDED.category_id,
                    category_name_snapshot = EXCLUDED.category_name_snapshot,
                    parent_category_id = EXCLUDED.parent_category_id,
                    parent_category_name_snapshot = EXCLUDED.parent_category_name_snapshot,
                    category_stable_since = EXCLUDED.category_stable_since,
                    effective_from = EXCLUDED.effective_from,
                    mapping_source = EXCLUDED.mapping_source,
                    mapping_confidence = EXCLUDED.mapping_confidence,
                    snapshot_taken_at = EXCLUDED.snapshot_taken_at,
                    review_note = EXCLUDED.review_note
            """, rows, page_size=1000)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*), COUNT(category_id),
                                  COUNT(*) FILTER (WHERE category_name_snapshot IS NULL
                                                     AND category_id IS NOT NULL)
                           FROM product_category_mapping""")
            n, withcat, orphan = cur.fetchone()
        print(f"loaded    : {n} rows, {withcat} with a category, "
              f"{orphan} whose category_id is absent from product_categories")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
