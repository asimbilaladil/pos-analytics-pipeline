"""
sanitize_order_archives.py
==========================
One-off remediation: remove gift_reward_data from EXISTING Order raw archives.

Migration 35's work established that Revel's Order.gift_reward_data embeds
plaintext customerName, firstName, lastName, phoneNumber and birthday. Archives
written before that field was stripped at the archive boundary (raw_archive.
PII_FIELDS_BY_RESOURCE) still contain it on disk. This script removes it.

SAFETY PROPERTIES
-----------------
* ONLY the gift_reward_data key is deleted. Every other field, the meta block,
  key order and the objects list are preserved exactly.
* Rewrites are atomic: temp file in the same directory -> flush -> fsync ->
  os.replace. A crash mid-write leaves the original intact.
* No persistent unsanitized backup is created. A temp copy exists only for the
  duration of one file's rewrite and is removed on failure.
* No gift_reward_data value is ever printed, logged or returned -- not even in
  an error path. Counts only.
* Per-file verification BEFORE the replace: the rewritten body must parse, must
  contain exactly the same number of objects, must carry the identical set of
  object ids, and must contain zero gift_reward_data keys. If any check fails
  the file is left untouched and reported.
* --dry-run reports what would change without writing.

Touches ONLY the `orders` resource tree. Non-Order archives are never opened.
"""

import argparse
import glob
import gzip
import hashlib
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import raw_archive

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PII_FIELD = "gift_reward_data"
FILE_MODE = raw_archive.ARCHIVE_FILE_MODE


def order_archive_files(base_dir):
    root = os.path.join(base_dir, "orders")
    return sorted(glob.glob(os.path.join(root, "**", "*.json.gz"), recursive=True))


def _object_ids(objects):
    return [o.get("id") for o in objects if isinstance(o, dict)]


def sanitize_file(path, dry_run=False):
    """Returns a per-file result dict. Never includes any payload value."""
    result = {"path": path, "status": None, "records_before": None,
              "records_after": None, "pii_removed": 0,
              "sha256_before": None, "sha256_after": None}

    with open(path, "rb") as fh:
        original_bytes = fh.read()
    result["sha256_before"] = hashlib.sha256(original_bytes).hexdigest()

    try:
        body = gzip.decompress(original_bytes).decode("utf-8")
        parsed = json.loads(body)
    except (OSError, EOFError, ValueError) as exc:
        # Cannot parse => cannot surgically remove the field. Do not guess and
        # do not delete the file; report it for a human decision.
        result["status"] = "unparseable"
        result["error"] = type(exc).__name__
        return result

    objects = parsed.get("objects") if isinstance(parsed, dict) else parsed
    if not isinstance(objects, list):
        result["status"] = "no_objects_list"
        return result

    result["records_before"] = len(objects)
    ids_before = _object_ids(objects)

    removed = 0
    for obj in objects:
        if isinstance(obj, dict) and PII_FIELD in obj:
            del obj[PII_FIELD]
            removed += 1
    result["pii_removed"] = removed

    if removed == 0:
        result["status"] = "clean"
        result["records_after"] = len(objects)
        result["sha256_after"] = result["sha256_before"]
        return result

    new_body = json.dumps(parsed)

    # ---- verify the replacement BEFORE it touches the real path ----
    check = json.loads(new_body)
    check_objects = check.get("objects") if isinstance(check, dict) else check
    if len(check_objects) != result["records_before"]:
        result["status"] = "verify_failed_record_count"
        return result
    if _object_ids(check_objects) != ids_before:
        result["status"] = "verify_failed_ids_changed"
        return result
    if PII_FIELD in new_body:
        result["status"] = "verify_failed_pii_remains"
        return result
    result["records_after"] = len(check_objects)

    if dry_run:
        result["status"] = "would_sanitize"
        return result

    new_bytes = gzip.compress(new_body.encode("utf-8"))
    tmp_path = path + ".sanitize.tmp"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(new_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, FILE_MODE)
        # Confirm the temp file reads back correctly before replacing the
        # original -- the original is the only copy at this moment.
        with gzip.open(tmp_path, "rt") as fh:
            reread = json.load(fh)
        reread_objects = reread.get("objects") if isinstance(reread, dict) else reread
        if len(reread_objects) != result["records_before"]:
            raise RuntimeError("re-read record count mismatch")
        os.replace(tmp_path, path)
        # fsync the directory so the rename itself is durable.
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception as exc:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        result["status"] = "write_failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["sha256_after"] = hashlib.sha256(new_bytes).hexdigest()
    result["status"] = "sanitized"
    return result


def update_meta(path, records, removed, dry_run=False):
    """Record the retroactive sanitation in the sidecar, preserving all
    existing keys. Best-effort: a sidecar failure never undoes the data fix."""
    meta_path = path.replace(".json.gz", ".meta.json")
    if not os.path.exists(meta_path) or dry_run:
        return
    try:
        with open(meta_path) as fh:
            meta = json.load(fh)
        meta["pii_sanitized_retroactively"] = {
            "field": PII_FIELD,
            "records_with_field_removed": removed,
            "record_count_preserved": records,
            "tool": "sanitize_order_archives.py",
        }
        tmp = meta_path + ".sanitize.tmp"
        with open(tmp, "w") as fh:
            json.dump(meta, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, meta_path)
    except Exception as exc:
        log.warning("  sidecar update failed for %s: %s", os.path.basename(meta_path), type(exc).__name__)


def main():
    ap = argparse.ArgumentParser(description="Strip gift_reward_data from existing Order archives")
    ap.add_argument("--base-dir", default=raw_archive.RAW_ARCHIVE_DIR)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    files = order_archive_files(a.base_dir)
    log.info("Order archive files found: %d (%s)", len(files), "DRY RUN" if a.dry_run else "LIVE")

    tally = {}
    records_before = records_after = pii_removed = 0
    problems = []

    for path in files:
        r = sanitize_file(path, dry_run=a.dry_run)
        tally[r["status"]] = tally.get(r["status"], 0) + 1
        if r["records_before"]:
            records_before += r["records_before"]
        if r["records_after"]:
            records_after += r["records_after"]
        pii_removed += r["pii_removed"]
        if r["status"] == "sanitized":
            update_meta(path, r["records_before"], r["pii_removed"], dry_run=a.dry_run)
        elif r["status"] not in ("clean", "would_sanitize"):
            problems.append(r)

    log.info("--- summary ---")
    for status, n in sorted(tally.items()):
        log.info("  %-32s %d", status, n)
    log.info("  records before                    %d", records_before)
    log.info("  records after                     %d", records_after)
    log.info("  %s occurrences removed  %d", PII_FIELD, pii_removed)
    log.info("  record counts match:              %s", records_before == records_after)

    if problems:
        log.error("  FILES NEEDING ATTENTION: %d", len(problems))
        for p in problems:
            log.error("    %s -> %s %s", os.path.basename(p["path"]), p["status"], p.get("error", ""))

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
