"""
generate_deliverables.py — writes all Nederland June 2026 deliverable files
from the in-memory rows produced by build_export.main().

Writes, all under this directory:
  nederland_orders_2026-06.parquet / .csv.gz   (File A)
  nederland_order_items_2026-06.parquet / .csv.gz (File B)
  nederland_june_2026_modifiers.parquet        (nested modifier rows)
  sample_orders_200.csv / sample_order_items_200.csv
  field_mapping.csv / field_mapping.md
  README.md
  email_to_masroor.txt
"""
import csv
import gzip
import io
import json
import time
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

import build_export as B

OUT = Path(__file__).parent


def rows_to_table(rows):
    if not rows:
        raise ValueError("no rows")
    cols = list(rows[0].keys())
    data = {c: [r.get(c) for r in rows] for c in cols}
    return pa.table(data)


def write_parquet(rows, path: Path):
    table = rows_to_table(rows)
    pq.write_table(table, path, compression="zstd")
    return path.stat().st_size


def _csv_stringify(v):
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def write_csv_gz(rows, path: Path):
    cols = list(rows[0].keys())
    with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: _csv_stringify(r.get(c)) for c in cols})
    return path.stat().st_size


def write_csv(rows, path: Path):
    cols = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: _csv_stringify(r.get(c)) for c in cols})
    return path.stat().st_size


def deterministic_sample(rows, key_fn, n=200):
    ordered = sorted(rows, key=key_fn)
    if len(ordered) <= n:
        return ordered
    step = len(ordered) / n
    idx = sorted(set(int(i * step) for i in range(n)))
    return [ordered[i] for i in idx[:n]]


def main():
    t0 = time.time()
    orders, items, mods, report, est_name = B.main()
    report["timing"]["current_export_generation_total_seconds"] = None  # filled at end

    sizes = {}

    print("\nWriting File A (orders)...")
    sizes["nederland_orders_2026-06.parquet"] = write_parquet(
        orders, OUT / "nederland_orders_2026-06.parquet")
    sizes["nederland_orders_2026-06.csv.gz"] = write_csv_gz(
        orders, OUT / "nederland_orders_2026-06.csv.gz")

    print("Writing File B (order items)...")
    sizes["nederland_order_items_2026-06.parquet"] = write_parquet(
        items, OUT / "nederland_order_items_2026-06.parquet")
    sizes["nederland_order_items_2026-06.csv.gz"] = write_csv_gz(
        items, OUT / "nederland_order_items_2026-06.csv.gz")

    print("Writing modifiers file (nested raw modifier rows)...")
    sizes["nederland_june_2026_modifiers.parquet"] = write_parquet(
        mods, OUT / "nederland_june_2026_modifiers.parquet")

    print("Writing 200-row deterministic samples...")
    order_sample = deterministic_sample(
        orders, key_fn=lambda r: (r["created_at_chicago"] or B.JUNE_START_CHI, r["order_id"]))
    item_sample = deterministic_sample(
        items, key_fn=lambda r: (r["created_at_chicago"] or B.JUNE_START_CHI, r["order_item_id"]))
    sizes["sample_orders_200.csv"] = write_csv(order_sample, OUT / "sample_orders_200.csv")
    sizes["sample_order_items_200.csv"] = write_csv(item_sample, OUT / "sample_order_items_200.csv")

    report["timing"]["current_export_generation_total_seconds"] = round(time.time() - t0, 2)

    with open(OUT / "_run_report.json", "w") as f:
        json.dump({"report": report, "file_sizes_bytes": sizes,
                   "order_sample_rows": len(order_sample),
                   "item_sample_rows": len(item_sample)}, f, indent=2, default=str)

    print("\n=== DONE ===")
    for k, v in sizes.items():
        print(f"  {k}: {v:,} bytes")
    print(f"Generation time: {report['timing']['current_export_generation_total_seconds']}s")

    return orders, items, mods, report, sizes, order_sample, item_sample


if __name__ == "__main__":
    main()
