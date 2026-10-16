#!/usr/bin/env python3
"""Validate and load R365 FINANCIAL API responses into the local tables.

The fetch, archive, lock and schedule live in r365_financial_ingest.py; this
module decides what is true:

  * plan_accounts() / plan_period() / plan_history() -- PURE. Parse source
    payloads into rows, enforce the proven contract, map stores, reconcile the
    category summaries against the approved GL detail. No I/O.
  * write_*() -- replace-by-target UPSERTs inside the caller's transaction.
  * start_run() / fail_run() -- r365_financial_ingest_run provenance.

Rules enforced here (see migrations/56_r365_financial.sql for the evidence):
  * Values are stored EXACTLY (parse_float=Decimal). Nothing is rounded.
  * A store-period with no posting (locationsWithNoActivity) becomes a
    NO_ACTIVITY row with expense_amount NULL. Missing is never $0.
  * Store identity comes only from r365_store_mapping (ACTIVE + VERIFIED, by
    R365 locationId). Never by name. Unknown locations are kept with a NULL
    establishment_id and are never exposed.
  * A fiscal period must match the official AYG calendar exactly; the source's
    periodStart/periodEnd are checked against it. Detail lines without a fiscal
    period are kept per calendar month and never given one.
  * Category totals are the SOURCE's: VERIFIED accounts only. CANDIDATE and
    UNMAPPED activity is kept as unverified accounting data.
  * Every GL detail line (Approved and Unapproved) is kept in
    r365_financial_transaction, keyed by the source detailId and located by the
    LINE's own location. expense_amount must equal debit - credit. Summaries
    and account roll-ups use Approved lines only, as the source does.
  * Nothing token-shaped is read from a payload or written anywhere.
"""
import datetime
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r365_load as R  # noqa: E402  (shared: connect, load_mapping, secrets guard)
import psycopg2.extras  # noqa: E402

LOADER_VERSION = "r365_financial_load/1.0.0"
STATUS_COMPLETE, STATUS_PARTIAL, STATUS_FAILED = "COMPLETE", "PARTIAL", "FAILED"
ROW_ACTIVITY, ROW_NO_ACTIVITY = "ACTIVITY", "NO_ACTIVITY"
UNMAPPED_BUCKET = "UNMAPPED"
MAPPING_STATUSES = {"VERIFIED", "CANDIDATE", "UNMAPPED"}
MONEY_TOLERANCE = Decimal("0.005")

ACCOUNT_KEYS = {"accountId", "accountNumber", "accountName", "accountType",
                "parentAccountId", "parentAccountNumber", "analyticsCategory",
                "mappingStatus"}
SUMMARY_TOP_KEYS = {"filters", "includeUnapproved", "dataStatus", "statusReasons",
                    "sync", "data", "pagination"}
SUMMARY_ROW_KEYS = {"locationId", "locationName", "fiscalYear", "periodNumber",
                    "periodStart", "periodEnd", "analyticsCategory", "expenseAmount",
                    "approvedAmount", "sourceQuality", "transactionDetailAmount",
                    "migratedSummaryAmount", "transactionCount", "accountCount",
                    "dataStatus", "coverageStart", "coverageEnd",
                    "transactionDetailStart"}
NO_ACTIVITY_KEYS = {"locationId", "locationName", "fiscalYear", "periodNumber"}
CANDIDATE_KEYS = {"locationId", "fiscalYear", "periodNumber", "accountNumber",
                  "accountName", "candidateCategory", "expenseAmount"}
DETAIL_KEYS = {"detailId", "transactionId", "transactionNumber", "transactionType",
               "status", "locationId", "locationName", "accountingDate", "fiscalYear",
               "periodNumber", "glAccountId", "accountNumber", "accountName",
               "accountType", "analyticsCategory", "mappingStatus", "debitAmount",
               "creditAmount", "expenseAmount", "sourceClass"}
TXN_STATUSES = {"Approved", "Unapproved"}
SOURCE_CLASSES = {"TRANSACTION_DETAIL", "MIGRATED_SUMMARY"}


class SourceSchemaError(Exception):
    """The payload does not have the proven shape. Fail closed: load nothing."""


class ReconciliationError(Exception):
    """Summary and GL detail disagree. Fail closed: load nothing."""


def parse_payload(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    doc = json.loads(raw, parse_float=Decimal)
    try:
        R.assert_no_secrets(doc)
    except R.SourceSchemaError as e:
        raise SourceSchemaError(str(e)) from None
    return doc


def _date(v, where, nullable=False):
    if v is None and nullable:
        return None
    try:
        return datetime.date.fromisoformat(v)
    except (TypeError, ValueError):
        raise SourceSchemaError(f"{where}: expected YYYY-MM-DD, got {v!r}") from None


def _money(v, where, nullable=False):
    if v is None:
        if nullable:
            return None
        raise SourceSchemaError(f"{where}: null where an amount is required")
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
        raise SourceSchemaError(f"{where}: expected a number, got {type(v).__name__}")
    return Decimal(v)


def _count(v, where):
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)) or v != int(v) or v < 0:
        raise SourceSchemaError(f"{where}: expected a non-negative integer, got {v!r}")
    return int(v)


def _keys(obj, required, where):
    if not isinstance(obj, dict):
        raise SourceSchemaError(f"{where}: expected an object")
    missing = sorted(required - obj.keys())
    if missing:
        raise SourceSchemaError(f"{where}: missing keys {missing}")


def _mapped(mapping, location_id):
    m = mapping.get(location_id)
    if m and m["status"] == "VERIFIED" and m["active"] and m["establishment_id"]:
        return m["establishment_id"]
    return None


def verified_store_ids(mapping):
    return {k for k, m in mapping.items()
            if m["status"] == "VERIFIED" and m["active"] and m["establishment_id"]}


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
def plan_accounts(payload):
    _keys(payload, {"data", "pagination"}, "accounts")
    data = payload["data"]
    total = payload["pagination"].get("total")
    if not isinstance(data, list) or total != len(data):
        raise SourceSchemaError(f"accounts: got {len(data) if isinstance(data, list) else '?'} "
                                f"rows but pagination.total = {total}")
    rows, seen = [], set()
    for i, a in enumerate(data):
        w = f"accounts[{i}]"
        _keys(a, ACCOUNT_KEYS, w)
        if a["mappingStatus"] not in MAPPING_STATUSES:
            raise SourceSchemaError(f"{w}: unknown mappingStatus {a['mappingStatus']!r}")
        if a["mappingStatus"] == "VERIFIED" and not a["analyticsCategory"]:
            raise SourceSchemaError(f"{w}: VERIFIED account without a category")
        if a["accountId"] in seen:
            raise SourceSchemaError(f"{w}: duplicate accountId")
        seen.add(a["accountId"])
        rows.append({"account_id": a["accountId"], "account_number": a["accountNumber"],
                     "account_name": a["accountName"], "account_type": a["accountType"],
                     "parent_account_id": a["parentAccountId"],
                     "parent_account_number": a["parentAccountNumber"],
                     "analytics_category": a["analyticsCategory"],
                     "mapping_status": a["mappingStatus"]})
    verified = sorted({r["analytics_category"] for r in rows
                       if r["mapping_status"] == "VERIFIED"})
    return {"rows": rows, "verified_categories": verified}


# ---------------------------------------------------------------------------
# One fiscal period
# ---------------------------------------------------------------------------
def _check_summary(payload, fy, pn, category, where):
    _keys(payload, SUMMARY_TOP_KEYS, where)
    f = payload["filters"] or {}
    if f.get("fiscal_year") != fy or f.get("period_number") != pn:
        raise SourceSchemaError(f"{where}: source echoed filters {f}, expected FY{fy} P{pn}")
    if category is not None and f.get("analytics_category") != category:
        raise SourceSchemaError(f"{where}: source echoed category "
                                f"{f.get('analytics_category')!r}, expected {category!r}")
    if category is None and "analytics_category" in f:
        raise SourceSchemaError(f"{where}: unfiltered call came back filtered")
    if payload["includeUnapproved"] is not False:
        raise SourceSchemaError(f"{where}: includeUnapproved is not false")
    pag = payload["pagination"] or {}
    if pag.get("returned") != len(payload["data"]) or len(payload["data"]) >= pag.get("limit", 0):
        raise SourceSchemaError(f"{where}: summary is paginated or truncated ({pag})")


def _summary_row(r, fy, pn, cal, where):
    _keys(r, SUMMARY_ROW_KEYS, where)
    if r["fiscalYear"] != fy or r["periodNumber"] != pn:
        raise SourceSchemaError(f"{where}: row is FY{r['fiscalYear']} P{r['periodNumber']}")
    ps, pe = _date(r["periodStart"], where), _date(r["periodEnd"], where)
    if (ps, pe) != cal:
        raise SourceSchemaError(f"{where}: source period {ps}..{pe} differs from the "
                                f"official calendar {cal[0]}..{cal[1]}")
    return {
        "r365_location_id": r["locationId"], "store_name": r["locationName"],
        "fiscal_year": fy, "period_number": pn,
        "analytics_category": r["analyticsCategory"],
        "period_start": ps, "period_end": pe, "row_kind": ROW_ACTIVITY,
        "expense_amount": _money(r["expenseAmount"], where),
        "approved_amount": _money(r["approvedAmount"], where, nullable=True),
        "transaction_count": _count(r["transactionCount"], where),
        "account_count": _count(r["accountCount"], where),
        "source_quality": r["sourceQuality"],
        "transaction_detail_amount": _money(r["transactionDetailAmount"], where, True),
        "migrated_summary_amount": _money(r["migratedSummaryAmount"], where, True),
        "data_status": r["dataStatus"],
        "coverage_start": _date(r["coverageStart"], where, True),
        "coverage_end": _date(r["coverageEnd"], where, True),
        "transaction_detail_start": _date(r["transactionDetailStart"], where, True),
    }


def _sync_at(payload):
    s = (payload.get("sync") or {}).get("lastSuccessfulSyncAt")
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def aggregate_detail(lines, bucket_of, where="detail"):
    """Roll approved GL detail lines up per (location, account, bucket). PURE.

    bucket_of(line, accounting_date) -> (bucket_key, kind, fy, pn, start, end)
    or raises SourceSchemaError. Returns (rows, per-location category sums).
    """
    agg = {}
    tx = defaultdict(set)
    seen = set()
    cat_sums = defaultdict(Decimal)          # (location, category, verified) -> amount
    for i, ln in enumerate(lines):
        w = f"{where}[{i}]"
        _keys(ln, DETAIL_KEYS, w)
        if ln["detailId"] in seen:
            raise SourceSchemaError(f"{w}: duplicate detailId across pages")
        seen.add(ln["detailId"])
        if ln["status"] != "Approved":
            raise SourceSchemaError(f"{w}: non-approved line in an approved-only feed")
        d = _date(ln["accountingDate"], w)
        bkey, kind, fy, pn, bs, be = bucket_of(ln, d)
        amt = _money(ln["expenseAmount"], w)
        deb, cred = _money(ln["debitAmount"], w), _money(ln["creditAmount"], w)
        key = (ln["locationId"], ln["glAccountId"], bkey)
        a = agg.get(key)
        if a is None:
            a = agg[key] = {
                "r365_location_id": ln["locationId"], "gl_account_id": ln["glAccountId"],
                "bucket_key": bkey, "bucket_kind": kind, "fiscal_year": fy,
                "period_number": pn, "bucket_start": bs, "bucket_end": be,
                "location_name": ln["locationName"], "account_number": ln["accountNumber"],
                "analytics_category": ln["analyticsCategory"],
                "mapping_status": ln["mappingStatus"],
                "expense_amount": Decimal(0), "debit_amount": Decimal(0),
                "credit_amount": Decimal(0), "migrated_summary_amount": Decimal(0),
                "line_count": 0, "min_accounting_date": d, "max_accounting_date": d}
        a["expense_amount"] += amt
        a["debit_amount"] += deb
        a["credit_amount"] += cred
        if ln["sourceClass"] == "MIGRATED_SUMMARY":
            a["migrated_summary_amount"] += amt
        a["line_count"] += 1
        a["min_accounting_date"] = min(a["min_accounting_date"], d)
        a["max_accounting_date"] = max(a["max_accounting_date"], d)
        tx[key].add(ln["transactionId"])
        verified = ln["mappingStatus"] == "VERIFIED"
        cat_sums[(ln["locationId"], ln["analyticsCategory"] if verified else UNMAPPED_BUCKET)] += amt
    rows = []
    for key, a in agg.items():
        a["transaction_count"] = len(tx[key])
        rows.append(a)
    return rows, cat_sums


def parse_transaction(ln, where, fiscal=None):
    """One source detail line -> one r365_financial_transaction row. PURE.

    fiscal = (fy, pn) the line must carry, or "NONE" for a pre-calendar line.
    The amount rule is checked, not trusted: expense = debit - credit.
    """
    _keys(ln, DETAIL_KEYS, where)
    if ln["status"] not in TXN_STATUSES:
        raise SourceSchemaError(f"{where}: unknown status {ln['status']!r}")
    if ln["mappingStatus"] not in MAPPING_STATUSES:
        raise SourceSchemaError(f"{where}: unknown mappingStatus {ln['mappingStatus']!r}")
    if ln["sourceClass"] not in SOURCE_CLASSES:
        raise SourceSchemaError(f"{where}: unknown sourceClass {ln['sourceClass']!r}")
    if not ln["detailId"] or not ln["transactionId"] or not ln["locationId"]:
        raise SourceSchemaError(f"{where}: missing detail, transaction or location id")
    deb, cred = _money(ln["debitAmount"], where), _money(ln["creditAmount"], where)
    amt = _money(ln["expenseAmount"], where)
    if amt != deb - cred:
        raise SourceSchemaError(f"{where}: expenseAmount {amt} != debit {deb} - credit {cred}")
    fy, pn = ln["fiscalYear"], ln["periodNumber"]
    if (fy is None) != (pn is None):
        raise SourceSchemaError(f"{where}: half a fiscal period ({fy}, {pn})")
    if fiscal == "NONE" and fy is not None:
        raise SourceSchemaError(f"{where}: line carries FY{fy} P{pn} but lies before the "
                                "official calendar")
    if fiscal not in (None, "NONE") and (fy, pn) != fiscal:
        raise SourceSchemaError(f"{where}: line is FY{fy} P{pn}, requested "
                                f"FY{fiscal[0]} P{fiscal[1]}")
    return {
        "source_transaction_detail_id": ln["detailId"],
        "source_transaction_id": ln["transactionId"],
        "transaction_number": ln["transactionNumber"],
        "transaction_type": ln["transactionType"],
        "transaction_status": ln["status"],
        "r365_location_id": ln["locationId"],
        "location_name": ln["locationName"],
        "accounting_date": _date(ln["accountingDate"], where),
        "fiscal_year": fy, "period_number": pn,
        "gl_account_id": ln["glAccountId"],
        "gl_account_number": ln["accountNumber"],
        "gl_account_name": ln["accountName"],
        "gl_account_type": ln["accountType"],
        "analytics_category": ln["analyticsCategory"],
        "mapping_status": ln["mappingStatus"],
        "debit_amount": deb, "credit_amount": cred, "expense_amount": amt,
        "source_class": ln["sourceClass"],
    }


def plan_transactions(lines, mapping, fiscal):
    """Every detail line as a transaction row (both statuses). PURE."""
    rows, seen = [], set()
    for i, ln in enumerate(lines):
        r = parse_transaction(ln, f"detail[{i}]", fiscal)
        if r["source_transaction_detail_id"] in seen:
            raise SourceSchemaError(f"detail[{i}]: duplicate detailId across pages")
        seen.add(r["source_transaction_detail_id"])
        r["establishment_id"] = _mapped(mapping, r["r365_location_id"])
        rows.append(r)
    return rows


def _txn_stats(rows):
    return {"lines": len(rows),
            "headers": len({r["source_transaction_id"] for r in rows}),
            "approved": sum(r["transaction_status"] == "Approved" for r in rows),
            "unapproved": sum(r["transaction_status"] == "Unapproved" for r in rows),
            "mapped_store_lines": sum(r["establishment_id"] is not None for r in rows),
            "unmapped_location_lines": sum(r["establishment_id"] is None for r in rows),
            "verified_lines": sum(r["mapping_status"] == "VERIFIED" for r in rows),
            "unverified_lines": sum(r["mapping_status"] != "VERIFIED" for r in rows)}


def plan_period(fy, pn, calendar, cat_payloads, all_payload, detail_lines, mapping,
                verified_categories):
    """Plan one fiscal period. PURE.

    calendar            (start, end) of FY/P from ayg_business_periods
    cat_payloads        {category: summary payload filtered to that category}
                        for every VERIFIED category
    all_payload         the unfiltered summary for the period
    detail_lines        every GL detail line of the period, Approved AND
                        Unapproved (summaries reconcile to the Approved ones)
    """
    transactions = plan_transactions(detail_lines, mapping, (fy, pn))
    for t in transactions:
        if not calendar[0] <= t["accounting_date"] <= calendar[1]:
            raise SourceSchemaError(f"detail line dated {t['accounting_date']} is outside "
                                    f"FY{fy} P{pn}")
    detail_lines = [ln for ln in detail_lines if ln["status"] == "Approved"]
    warnings = []
    if set(cat_payloads) != set(verified_categories):
        raise SourceSchemaError("category payloads do not match the VERIFIED categories")
    expense, keys = [], set()

    def add(row, where):
        k = (row["r365_location_id"], row["analytics_category"])
        if k in keys:
            raise SourceSchemaError(f"{where}: duplicate store/category row {k}")
        keys.add(k)
        row["establishment_id"] = _mapped(mapping, row["r365_location_id"])
        expense.append(row)

    source_status, reasons, no_activity, unaccounted = {}, {}, 0, {}
    verified_ids = verified_store_ids(mapping)
    for cat, p in sorted(cat_payloads.items()):
        w = f"summary[{cat}]"
        _check_summary(p, fy, pn, cat, w)
        source_status[cat] = p["dataStatus"]
        reasons[cat] = p.get("statusReasons") or []
        seen_ids = set()
        for i, r in enumerate(p["data"]):
            row = _summary_row(r, fy, pn, calendar, f"{w}.data[{i}]")
            if row["analytics_category"] != cat:
                raise SourceSchemaError(f"{w}.data[{i}]: category {row['analytics_category']!r}")
            add(row, f"{w}.data[{i}]")
            seen_ids.add(row["r365_location_id"])
        for i, n in enumerate(p.get("locationsWithNoActivity") or []):
            ww = f"{w}.locationsWithNoActivity[{i}]"
            _keys(n, NO_ACTIVITY_KEYS, ww)
            if n["fiscalYear"] != fy or n["periodNumber"] != pn:
                raise SourceSchemaError(f"{ww}: entry is for another period")
            if n["locationId"] in seen_ids:
                raise SourceSchemaError(f"{ww}: store has both activity and no activity")
            add({"r365_location_id": n["locationId"], "store_name": n["locationName"],
                 "fiscal_year": fy, "period_number": pn, "analytics_category": cat,
                 "period_start": calendar[0], "period_end": calendar[1],
                 "row_kind": ROW_NO_ACTIVITY, "expense_amount": None,
                 "approved_amount": None, "transaction_count": None,
                 "account_count": None, "source_quality": None,
                 "transaction_detail_amount": None, "migrated_summary_amount": None,
                 "data_status": ROW_NO_ACTIVITY, "coverage_start": None,
                 "coverage_end": None, "transaction_detail_start": None}, ww)
            seen_ids.add(n["locationId"])
            no_activity += 1
        missing = sorted(verified_ids - seen_ids)
        if missing:
            # Neither a posting nor a no-activity entry: the source said nothing.
            unaccounted[cat] = [mapping[m]["r365_store_name"] for m in missing]

    # Unfiltered summary: keep every non-VERIFIED bucket; cross-check the
    # VERIFIED rows against the category-filtered call.
    _check_summary(all_payload, fy, pn, None, "summary[ALL]")
    by_key = {(r["r365_location_id"], r["analytics_category"]): r for r in expense}
    for i, r in enumerate(all_payload["data"]):
        row = _summary_row(r, fy, pn, calendar, f"summary[ALL].data[{i}]")
        k = (row["r365_location_id"], row["analytics_category"])
        if row["analytics_category"] in verified_categories:
            mine = by_key.get(k)
            if mine is None or abs(mine["expense_amount"] - row["expense_amount"]) > MONEY_TOLERANCE:
                raise ReconciliationError(
                    f"unfiltered summary {k} = {row['expense_amount']} but the "
                    f"{row['analytics_category']} summary has "
                    f"{mine['expense_amount'] if mine else 'no row'}")
            continue
        add(row, f"summary[ALL].data[{i}]")

    candidates, cseen = [], set()
    for i, c in enumerate(all_payload.get("unverifiedCandidates") or []):
        w = f"summary[ALL].unverifiedCandidates[{i}]"
        _keys(c, CANDIDATE_KEYS, w)
        if c["fiscalYear"] != fy or c["periodNumber"] != pn:
            raise SourceSchemaError(f"{w}: candidate is for another period")
        k = (c["locationId"], c["accountNumber"])
        if k in cseen:
            raise SourceSchemaError(f"{w}: duplicate candidate {k}")
        cseen.add(k)
        candidates.append({"r365_location_id": c["locationId"], "fiscal_year": fy,
                           "period_number": pn, "account_number": c["accountNumber"],
                           "account_name": c["accountName"],
                           "candidate_category": c["candidateCategory"],
                           "establishment_id": _mapped(mapping, c["locationId"]),
                           "expense_amount": _money(c["expenseAmount"], w)})

    def bucket_of(ln, d):
        if ln["fiscalYear"] != fy or ln["periodNumber"] != pn:
            raise SourceSchemaError(f"detail line {ln['detailId']} is FY{ln['fiscalYear']} "
                                    f"P{ln['periodNumber']}, requested FY{fy} P{pn}")
        if not calendar[0] <= d <= calendar[1]:
            raise SourceSchemaError(f"detail line dated {d} is outside FY{fy} P{pn}")
        return f"FY{fy}-P{pn}", "FISCAL_PERIOD", fy, pn, calendar[0], calendar[1]

    acct_rows, cat_sums = aggregate_detail(detail_lines, bucket_of)
    for r in acct_rows:
        r["establishment_id"] = _mapped(mapping, r["r365_location_id"])

    # Reconcile every summary bucket (VERIFIED categories and the UNMAPPED
    # bucket) against the sum of its approved detail lines, per location.
    recon = []
    summary_sums = defaultdict(Decimal)
    for r in expense:
        if r["row_kind"] == ROW_ACTIVITY:
            cat = r["analytics_category"] if r["analytics_category"] in verified_categories \
                else UNMAPPED_BUCKET
            summary_sums[(r["r365_location_id"], cat)] += r["expense_amount"]
    for k in sorted(set(summary_sums) | set(cat_sums)):
        s, d = summary_sums.get(k, Decimal(0)), cat_sums.get(k, Decimal(0))
        if abs(s - d) > MONEY_TOLERANCE:
            recon.append({"location_id": k[0], "category": k[1],
                          "summary": str(s), "detail": str(d)})
    if recon:
        raise ReconciliationError(f"{len(recon)} summary bucket(s) differ from GL detail: "
                                  + json.dumps(recon[:5]))

    unmapped_locs = sorted({(r["r365_location_id"], r["store_name"]) for r in expense
                            if r["establishment_id"] is None})
    for loc, name in unmapped_locs:
        if loc in mapping:
            warnings.append(f"{name} has a mapping that is not ACTIVE VERIFIED")
    complete = (all(s == "COMPLETE" for s in source_status.values()) and not unaccounted)
    dates = [r["min_accounting_date"] for r in acct_rows] + \
            [r["max_accounting_date"] for r in acct_rows]
    return {
        "fiscal_year": fy, "period_number": pn, "calendar": calendar,
        "status": STATUS_COMPLETE if complete else STATUS_PARTIAL,
        "expense_rows": expense, "candidate_rows": candidates,
        "account_period_rows": acct_rows,
        "transaction_rows": transactions,
        "transaction_stats": _txn_stats(transactions),
        "rows_received": len(all_payload["data"]) + sum(len(p["data"]) for p in cat_payloads.values()),
        "no_activity_rows": no_activity,
        "detail_lines": len(detail_lines),
        "unmapped_locations": [{"r365_location_id": a, "name": b} for a, b in unmapped_locs],
        "unaccounted_stores": unaccounted,
        "reconciliation": {"buckets_checked": len(set(summary_sums) | set(cat_sums)),
                           "mismatches": 0},
        "source_data_status": source_status, "source_status_reasons": reasons,
        "source_last_sync_at": _sync_at(all_payload),
        "source_sync_stale": (all_payload.get("sync") or {}).get("stale"),
        "source_min_date": min(dates) if dates else None,
        "source_max_date": max(dates) if dates else None,
        "schema_warnings": warnings,
    }


# ---------------------------------------------------------------------------
# History with no fiscal period (pre-FY2026)
# ---------------------------------------------------------------------------
def month_bounds(d):
    start = d.replace(day=1)
    nxt = (start + datetime.timedelta(days=32)).replace(day=1)
    return start, nxt - datetime.timedelta(days=1)


def plan_history(lines, mapping, range_start, range_end):
    """Plan the pre-calendar GL detail. PURE. Every line must lack a fiscal
    period: the source assigns one only inside the official calendar, and this
    loader never invents one."""
    def bucket_of(ln, d):
        if ln["fiscalYear"] is not None or ln["periodNumber"] is not None:
            raise SourceSchemaError(f"detail line {ln['detailId']} dated {d} carries "
                                    f"FY{ln['fiscalYear']} P{ln['periodNumber']} but lies "
                                    "before the official calendar")
        if not range_start <= d <= range_end:
            raise SourceSchemaError(f"detail line dated {d} is outside {range_start}..{range_end}")
        ms, me = month_bounds(d)
        return f"CM-{d:%Y-%m}", "CALENDAR_MONTH", None, None, ms, me

    transactions = plan_transactions(lines, mapping, "NONE")
    for t in transactions:
        if not range_start <= t["accounting_date"] <= range_end:
            raise SourceSchemaError(f"detail line dated {t['accounting_date']} is outside "
                                    f"{range_start}..{range_end}")
    rows, _ = aggregate_detail([ln for ln in lines if ln["status"] == "Approved"],
                               bucket_of, "history")
    for r in rows:
        r["establishment_id"] = _mapped(mapping, r["r365_location_id"])
    dates = [r["min_accounting_date"] for r in rows] + [r["max_accounting_date"] for r in rows]
    return {"status": STATUS_COMPLETE, "account_period_rows": rows,
            "transaction_rows": transactions,
            "transaction_stats": _txn_stats(transactions),
            "range": (range_start, range_end),
            "detail_lines": len(lines),
            "source_min_date": min(dates) if dates else None,
            "source_max_date": max(dates) if dates else None}


# ---------------------------------------------------------------------------
# Database side
# ---------------------------------------------------------------------------
connect = R.connect
load_mapping = R.load_mapping
_json = R._json


def load_calendar(cur):
    cur.execute("SELECT fiscal_year, period_number, start_date, end_date "
                "FROM ayg_business_periods ORDER BY start_date")
    return {(fy, pn): (s, e) for fy, pn, s, e in cur.fetchall()}


def start_run(conn, batch_id, trigger, target_kind, fy=None, pn=None, start=None, end=None):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO r365_financial_ingest_run
                         (batch_id, trigger, target_kind, requested_fiscal_year,
                          requested_period_number, requested_start, requested_end,
                          loader_version)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (batch_id, trigger, target_kind, fy, pn, start, end, LOADER_VERSION))
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def fail_run(conn, run_id, code, message, fetch=None):
    conn.rollback()
    fetch = fetch or {}
    with conn.cursor() as cur:
        cur.execute("""UPDATE r365_financial_ingest_run
                          SET status = 'FAILED', completed_at = now(),
                              error_code = %s, error_message = %s,
                              http_requests = %s, fetch_seconds = %s,
                              payload_sha256 = %s, raw_archive_path = %s
                        WHERE id = %s""",
                    (code, str(message)[:2000], fetch.get("http_requests"),
                     fetch.get("fetch_seconds"), fetch.get("payload_sha256"),
                     fetch.get("raw_archive_path"), run_id))
    conn.commit()


def _replace(cur, table, pk_cols, value_cols, rows, run_id, scope_sql, scope_params,
             stamp=None):
    """Make `table` hold exactly `rows` within one scope. Returns
    (upserted, changed, deleted). Rows the source no longer reports for the
    scope (a re-classified or reversed posting) are deleted, so a refresh is a
    faithful replacement, and re-running the same payload changes nothing.
    stamp = {column: value} written on every row but NOT compared (e.g. the
    fetch time), so it never counts as a change."""
    stamp = stamp or {}
    cur.execute(f"SELECT {', '.join(pk_cols)}, {', '.join(value_cols)} FROM {table} "
                f"WHERE {scope_sql}", scope_params)
    before = {tuple(r[:len(pk_cols)]): tuple(r[len(pk_cols):]) for r in cur.fetchall()}
    new_keys = {tuple(r[c] for c in pk_cols) for r in rows}
    gone = [k for k in before if k not in new_keys]
    for k in gone:
        cur.execute(f"DELETE FROM {table} WHERE ({', '.join(pk_cols)}) = %s", (k,))
    changed = sum(1 for r in rows
                  if before.get(tuple(r[c] for c in pk_cols)) != tuple(r[c] for c in value_cols))
    if rows:
        extra = list(stamp)
        cols = pk_cols + value_cols + extra + ["ingest_run_id"]
        upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in value_cols + extra + ["ingest_run_id"])
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s "
            f"ON CONFLICT ({', '.join(pk_cols)}) DO UPDATE SET {upd}, updated_at = now()",
            [tuple(r[c] for c in pk_cols + value_cols) + tuple(stamp.values()) + (run_id,)
             for r in rows],
            page_size=1000)
    return len(rows), changed, len(gone)


_ACCOUNT_VALS = ["account_number", "account_name", "account_type", "parent_account_id",
                 "parent_account_number", "analytics_category", "mapping_status"]
_EXPENSE_PK = ["r365_location_id", "fiscal_year", "period_number", "analytics_category"]
_EXPENSE_VALS = ["establishment_id", "store_name", "period_start", "period_end", "row_kind",
                 "expense_amount", "approved_amount", "transaction_count", "account_count",
                 "source_quality", "transaction_detail_amount", "migrated_summary_amount",
                 "data_status", "coverage_start", "coverage_end", "transaction_detail_start",
                 "source_updated_at"]
_CAND_PK = ["r365_location_id", "fiscal_year", "period_number", "account_number"]
_CAND_VALS = ["account_name", "candidate_category", "establishment_id", "expense_amount"]
_ACCT_PERIOD_PK = ["r365_location_id", "gl_account_id", "bucket_key"]
_ACCT_PERIOD_VALS = ["bucket_kind", "fiscal_year", "period_number", "bucket_start",
                     "bucket_end", "establishment_id", "location_name", "account_number",
                     "analytics_category", "mapping_status", "expense_amount",
                     "debit_amount", "credit_amount", "migrated_summary_amount",
                     "line_count", "transaction_count", "min_accounting_date",
                     "max_accounting_date"]


_TXN_PK = ["source_transaction_detail_id"]
_TXN_VALS = ["source_transaction_id", "transaction_number", "transaction_type",
             "transaction_status", "r365_location_id", "location_name", "establishment_id",
             "accounting_date", "fiscal_year", "period_number", "gl_account_id",
             "gl_account_number", "gl_account_name", "gl_account_type", "analytics_category",
             "mapping_status", "debit_amount", "credit_amount", "expense_amount",
             "source_class"]


def _write_transactions(cur, run_id, plan, fetch, scope_sql, scope_params):
    fetched_at = fetch.get("fetched_at") or datetime.datetime.now(datetime.timezone.utc)
    return _replace(cur, "r365_financial_transaction", _TXN_PK, _TXN_VALS,
                    plan["transaction_rows"], run_id, scope_sql, scope_params,
                    stamp={"source_fetched_at": fetched_at})


def _finish(cur, run_id, status, **f):
    sets = ", ".join(f"{k} = %s" for k in f)
    cur.execute(f"UPDATE r365_financial_ingest_run SET status = %s, completed_at = now(), "
                f"{sets} WHERE id = %s", [status] + list(f.values()) + [run_id])


def write_accounts(conn, run_id, plan, fetch):
    """Upsert the chart of accounts. Accounts are never deleted: a GL account
    that disappears from the listing keeps its last published mapping."""
    with conn.cursor() as cur:
        rows = plan["rows"]
        keys = [r["account_id"] for r in rows]
        cur.execute(f"SELECT account_id, {', '.join(_ACCOUNT_VALS)} FROM r365_financial_account")
        before = {r[0]: tuple(r[1:]) for r in cur.fetchall()}
        changed = sum(1 for r in rows
                      if before.get(r["account_id"]) != tuple(r[c] for c in _ACCOUNT_VALS))
        upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in _ACCOUNT_VALS + ["ingest_run_id"])
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO r365_financial_account (account_id, {', '.join(_ACCOUNT_VALS)}, "
            f"ingest_run_id) VALUES %s ON CONFLICT (account_id) DO UPDATE SET {upd}, "
            "updated_at = now()",
            [(r["account_id"],) + tuple(r[c] for c in _ACCOUNT_VALS) + (run_id,) for r in rows])
        _finish(cur, run_id, STATUS_COMPLETE, rows_received=len(rows),
                rows_upserted=len(keys), rows_changed=changed, rows_deleted=0,
                source_data_status=_json({c: "VERIFIED" for c in plan["verified_categories"]}),
                **_fetch_cols(fetch))
    return {"upserted": len(keys), "changed": changed}


def _fetch_cols(fetch):
    return {"http_requests": fetch.get("http_requests"),
            "fetch_seconds": fetch.get("fetch_seconds"),
            "payload_sha256": fetch.get("payload_sha256"),
            "raw_archive_path": fetch.get("raw_archive_path")}


def write_period(conn, run_id, plan, fetch):
    """Replace one fiscal period's rows in all three period tables. Caller commits."""
    fy, pn = plan["fiscal_year"], plan["period_number"]
    sync_at = plan["source_last_sync_at"]
    for r in plan["expense_rows"]:
        r["source_updated_at"] = sync_at
    with conn.cursor() as cur:
        e = _replace(cur, "r365_financial_period_expense", _EXPENSE_PK, _EXPENSE_VALS,
                     plan["expense_rows"], run_id,
                     "fiscal_year = %s AND period_number = %s", (fy, pn))
        c = _replace(cur, "r365_financial_candidate_expense", _CAND_PK, _CAND_VALS,
                     plan["candidate_rows"], run_id,
                     "fiscal_year = %s AND period_number = %s", (fy, pn))
        a = _replace(cur, "r365_financial_account_period", _ACCT_PERIOD_PK, _ACCT_PERIOD_VALS,
                     plan["account_period_rows"], run_id,
                     "bucket_kind = 'FISCAL_PERIOD' AND fiscal_year = %s AND period_number = %s",
                     (fy, pn))
        t = _write_transactions(cur, run_id, plan, fetch,
                                "fiscal_year = %s AND period_number = %s", (fy, pn))
        _finish(cur, run_id, plan["status"],
                rows_received=plan["rows_received"], rows_upserted=e[0] + c[0],
                rows_changed=e[1] + c[1], rows_deleted=e[2] + c[2] + a[2],
                no_activity_rows=plan["no_activity_rows"],
                candidate_rows=len(plan["candidate_rows"]),
                detail_lines_received=plan["detail_lines"],
                account_period_rows=a[0],
                unmapped_locations=_json(plan["unmapped_locations"]),
                unaccounted_stores=_json(plan["unaccounted_stores"]),
                reconciliation_ok=True, reconciliation=_json(plan["reconciliation"]),
                source_data_status=_json(plan["source_data_status"]),
                source_status_reasons=_json(plan["source_status_reasons"]),
                source_last_sync_at=sync_at, source_sync_stale=plan["source_sync_stale"],
                source_min_date=plan["source_min_date"], source_max_date=plan["source_max_date"],
                schema_warnings=_json(plan["schema_warnings"]),
                transaction_rows_upserted=t[0], transaction_rows_changed=t[1],
                transaction_rows_deleted=t[2],
                transaction_stats=_json(plan["transaction_stats"]), **_fetch_cols(fetch))
    return {"expense": e, "candidates": c, "account_period": a, "transactions": t}


def write_history(conn, run_id, plan, fetch):
    with conn.cursor() as cur:
        a = _replace(cur, "r365_financial_account_period", _ACCT_PERIOD_PK, _ACCT_PERIOD_VALS,
                     plan["account_period_rows"], run_id,
                     "bucket_kind = 'CALENDAR_MONTH'", ())
        t = _write_transactions(cur, run_id, plan, fetch,
                                "fiscal_year IS NULL AND accounting_date BETWEEN %s AND %s",
                                plan["range"])
        _finish(cur, run_id, plan["status"], rows_received=plan["detail_lines"],
                rows_upserted=a[0], rows_changed=a[1], rows_deleted=a[2],
                detail_lines_received=plan["detail_lines"], account_period_rows=a[0],
                source_min_date=plan["source_min_date"],
                source_max_date=plan["source_max_date"],
                transaction_rows_upserted=t[0], transaction_rows_changed=t[1],
                transaction_rows_deleted=t[2],
                transaction_stats=_json(plan["transaction_stats"]), **_fetch_cols(fetch))
    return {"account_period": a, "transactions": t}
