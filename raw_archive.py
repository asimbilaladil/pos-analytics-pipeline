"""
raw_archive.py
===============
Task 06 — archive untouched Revel API responses to .json.gz before parsing.

    API -> raw .json.gz -> parser -> Postgres

Every archive call writes two files:
  page_NNNNNN.json.gz   the exact, untouched response body Revel returned
  page_NNNNNN.meta.json plain JSON: everything needed to reproduce the request

Layout:
    {RAW_ARCHIVE_DIR}/{resource}/{year}/{month}/[establishment_{id}/]run_{run_id}/
        page_000001.json.gz
        page_000001.meta.json
        ...

run_{run_id} (one per pipeline invocation, see new_run_id()) means two
different runs can never collide/overwrite each other's archives, even if
they target the same resource/establishment/month/page number. Within a run,
writes are atomic (tmp file + os.replace) so an interrupted write can never
leave a corrupt file where a caller might mistake it for a complete archive.

Storage root is configurable via RAW_ARCHIVE_DIR (default: an external data
directory, NOT inside the git repo — these files are large binary data dumps).
"""

import os
import json
import gzip
import shutil
import logging
from datetime import datetime, date, timezone
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_ARCHIVE_ROOT = "/var/lib/laynes/raw_revel"
RAW_ARCHIVE_DIR = os.getenv("RAW_ARCHIVE_DIR", DEFAULT_ARCHIVE_ROOT)

VALID_RESOURCES = {
    "orders", "order_items", "modifier_items", "payments",
    "order_history", "products", "product_categories",
}

_verified_roots: set[str] = set()  # avoid re-checking writability/disk space every call


class ArchiveError(RuntimeError):
    """Raised on any archive failure. Callers must not treat archiving as
    optional — a failed archive write must not be silently swallowed as if
    the response were safely persisted."""


def new_run_id() -> str:
    """One of these per pipeline invocation — pass the same value to every
    archive_response() call within a single run."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ensure_archive_root(base_dir: str) -> None:
    """
    Verify the archive root exists and is writable, and log available disk
    space. Runs once per (process, base_dir) — not on every archive call.
    Raises ArchiveError rather than letting a write fail later and silently.
    """
    if base_dir in _verified_roots:
        return

    try:
        os.makedirs(base_dir, exist_ok=True)
    except OSError as exc:
        raise ArchiveError(f"cannot create archive root {base_dir}: {exc}") from exc

    if not os.access(base_dir, os.W_OK):
        raise ArchiveError(f"archive root {base_dir} exists but is not writable")

    try:
        usage = shutil.disk_usage(base_dir)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        log.info(
            "Raw archive root %s ready — %.1f GB free of %.1f GB",
            base_dir, free_gb, total_gb,
        )
        if free_gb < 1.0:
            log.warning(
                "Raw archive root %s has under 1 GB free (%.2f GB) — "
                "archiving may fail soon", base_dir, free_gb,
            )
    except OSError as exc:
        # Disk-space check failing is a warning, not fatal — writability is
        # what actually matters and was already confirmed above.
        log.warning("Could not stat disk usage for %s: %s", base_dir, exc)

    _verified_roots.add(base_dir)


def _atomic_write_bytes(path: str, data: bytes) -> None:
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)  # atomic on the same filesystem
    except OSError as exc:
        # Clean up a half-written tmp file rather than leaving debris behind.
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise ArchiveError(f"atomic write failed for {path}: {exc}") from exc


def archive_response(
    resource: str,
    run_id: str,
    raw_text: str,
    *,
    endpoint: str,
    query_params: dict,
    page: int,
    offset: int,
    archive_date: date,
    attempt: int = 1,
    batch: Optional[int] = None,
    window_key: Optional[str] = None,
    fetch_time: Optional[datetime] = None,
    establishment_id: Optional[int] = None,
    date_window: Optional[tuple] = None,
    pipeline_version: str = "unknown",
    base_dir: str = None,
) -> dict:
    """
    Archive one untouched Revel API page response. Call this BEFORE parsing
    the response for insertion — including malformed/truncated responses:
    archiving is a storage operation on raw bytes, not a validity check, so
    a response that later fails json.loads() still gets archived here as a
    record of that failed attempt (see pipeline.py's fetch_all_pages).

    raw_text must be the literal response body text (e.g. Playwright's
    response.text()), not a re-serialized dict — "untouched" means untouched.

    `attempt` must be unique per (resource, establishment, run, page) retry
    so retries never overwrite each other's archive — pass the 1-based
    attempt number from the caller's retry loop.

    `batch` (optional): for fetches that page over multiple independent
    requests within one run (e.g. order__in batches of order IDs), `page`
    alone is only unique WITHIN a single batch's own pagination — two
    different batches both start at page 1. Pass the 1-based batch number
    to get its own path segment (run_{run_id}/batch_{batch}/...) and its
    own "batch_number" field in the metadata, so archives from different
    batches can never collide even if their internal page/offset match.

    `window_key` (optional, Task 09): same idea as `batch`, for a different
    collision shape. A historical backfill fetches multiple independent
    (establishment, created_date window) chunks that can all share the same
    run_id, archive_date (today's real date — irrelevant to which
    HISTORICAL period each chunk covers), resource, and establishment, and
    each chunk's own pagination restarts at page 1 — so March's page 1 would
    otherwise try to overwrite February's. Pass a deterministic string
    derived from the chunk's own window_start/window_end (e.g.
    "20260210_20260301"), not a sequential counter — it needs to be stable
    and self-documenting across separate script invocations covering the
    same chunk, not just unique within one run. Gets its own path segment
    (run_{run_id}/window_{window_key}/...) and its own "window_key" field
    in the metadata. Independent of and composable with `batch`.

    Returns {"json_path", "meta_path", "object_count", "total_count"}.
    object_count/total_count are best-effort (None if raw_text isn't valid
    JSON) — that is expected and not a failure of archiving itself.
    Raises ArchiveError only for actual storage failures (can't create the
    directory, disk full, an existing file for this exact attempt already
    present, etc.) — callers must let this propagate rather than continuing
    as if the data were safely archived, and must NOT retry the Revel
    request in response to it (see with_retries' hard-coded non_retryable).
    """
    if resource not in VALID_RESOURCES:
        raise ArchiveError(f"unknown resource {resource!r}, expected one of {sorted(VALID_RESOURCES)}")

    base_dir = base_dir or RAW_ARCHIVE_DIR
    _ensure_archive_root(base_dir)
    fetch_time = fetch_time or datetime.now(timezone.utc)

    # Best-effort only — a parse failure here is exactly the case this
    # function must still successfully archive, not reject.
    try:
        parsed = json.loads(raw_text)
        object_count = len(parsed.get("objects", [])) if isinstance(parsed, dict) else None
        total_count = parsed.get("meta", {}).get("total_count") if isinstance(parsed, dict) else None
        parse_error = None
    except json.JSONDecodeError as exc:
        object_count = None
        total_count = None
        parse_error = str(exc)

    parts = [base_dir, resource, f"{archive_date.year:04d}", f"{archive_date.month:02d}"]
    if establishment_id is not None:
        parts.append(f"establishment_{establishment_id}")
    parts.append(f"run_{run_id}")
    if window_key is not None:
        parts.append(f"window_{window_key}")
    if batch is not None:
        parts.append(f"batch_{batch:04d}")
    dir_path = os.path.join(*parts)

    try:
        os.makedirs(dir_path, exist_ok=True)
    except OSError as exc:
        raise ArchiveError(f"cannot create archive directory {dir_path}: {exc}") from exc

    filename_base = f"page_{page:06d}_attempt_{attempt:02d}"
    json_path = os.path.join(dir_path, filename_base + ".json.gz")
    meta_path = os.path.join(dir_path, filename_base + ".meta.json")

    # Never silently overwrite another request/run/window/attempt's archive.
    # Different runs already can't collide (separate run_{run_id} directory);
    # different windows already can't collide (separate window_{window_key}
    # directory when window_key= is passed — Task 09: two historical chunks
    # for the same establishment sharing a run_id and today's real archive_
    # date would otherwise both start their own pagination at page 1);
    # different batches already can't collide (separate batch_{batch}
    # directory when batch= is passed); attempt is part of the filename so
    # retries can't collide either — this check is a defensive backstop
    # against a caller bug re-archiving the exact same attempt, not the
    # primary uniqueness mechanism.
    if os.path.exists(json_path):
        raise ArchiveError(
            f"refusing to overwrite existing archive {json_path} "
            f"(same resource/establishment/run/window/batch/page/attempt already archived)"
        )

    compressed = gzip.compress(raw_text.encode("utf-8"))
    _atomic_write_bytes(json_path, compressed)

    meta = {
        "resource": resource,
        "endpoint": endpoint,
        "query_params": query_params,
        "establishment_id": establishment_id,
        "date_window": list(date_window) if date_window else None,
        "page": page,
        "offset": offset,
        "attempt": attempt,
        "batch_number": batch,
        "window_key": window_key,
        "fetch_time": fetch_time.isoformat(),
        "pipeline_version": pipeline_version,
        "run_id": run_id,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "object_count": object_count,
        "total_count": total_count,
        "parse_error": parse_error,
        "compressed_bytes": len(compressed),
        "raw_bytes": len(raw_text.encode("utf-8")),
    }
    try:
        _atomic_write_bytes(meta_path, json.dumps(meta, indent=2).encode("utf-8"))
    except ArchiveError:
        # The .json.gz already landed — remove it rather than leave an
        # archive with no metadata (unreproducible request, half the point
        # of archiving at all).
        try:
            os.remove(json_path)
        except OSError:
            pass
        raise

    return {
        "json_path": json_path,
        "meta_path": meta_path,
        "object_count": object_count,
        "total_count": total_count,
    }


def read_archived_response(json_path: str) -> dict:
    """Decompress and parse an archived .json.gz back into the original dict.
    Used for validation/testing and, later, for resumable backfills that
    re-parse from disk instead of re-fetching from Revel."""
    with gzip.open(json_path, "rt", encoding="utf-8") as f:
        return json.load(f)
