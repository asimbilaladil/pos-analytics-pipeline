#!/usr/bin/env python3
"""Data-monitoring dashboard: grouping, counts, filters, store health, states,
acknowledgement, auth and failure handling -- in a real browser.

Run:  venv/bin/python tests/test_monitoring_dashboard_frontend.py     (exit 0 = pass)
Needs: frontend/node_modules (npm ci) and Playwright's Chromium.

The REAL built frontend runs in Chromium against a mocked API. The fixture
mirrors the production incident mix of 2026-11-25 (17 unresolved incidents, one
HME day with 9 store-level failures) with synthetic IDs and no business figures.
Screenshots go to $DASHBOARD_SHOTS when set.
"""
import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
os.chdir(ROOT)
from playwright.async_api import async_playwright  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not ok else ""))


DIST = tempfile.mkdtemp(prefix="laynes-dash-")
b = subprocess.run(["npx", "vite", "build", "--outDir", DIST, "--emptyOutDir"],
                   cwd=os.path.join(ROOT, "frontend"), capture_output=True, text=True)
if b.returncode != 0:
    print("frontend build failed:\n" + (b.stderr or b.stdout)[-2000:])
    sys.exit(1)


class Static(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if not os.path.isfile(os.path.join(DIST, self.path.split("?")[0].lstrip("/"))):
            self.path = "/index.html"
        return super().do_GET()


httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Static, directory=DIST))
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
SHOTS = os.environ.get("DASHBOARD_SHOTS")

# ── fixture ───────────────────────────────────────────────────────────────
STORES = {6: "LCF Katy", 7: "LCF Ella", 14: "LCF Beaumont", 15: "LCF Shepherd", 20: "LCF Pasadena",
          25: "LCF Mission Bend", 26: "LCF Cypress", 32: "LCF Airtex", 36: "LCF Missouri City",
          40: "LCF Rosenberg", 54: "LCF Nederland", 61: "LCF Downtown Houston"}
HME_MAPPED = [s for s in STORES if s != 61]
HME_FAILED = [14, 15, 20, 25, 32, 36, 40, 6, 7]
T = "2026-11-25T08:30:12+00:00"
_n = [0]


def inc(source, check_name, category, severity, title, store=None, date=None, qualifier=None,
        status="OPEN", difference=None, first=T, actions=("Check the source.",)):
    _n[0] += 1
    return {"id": f"INC-T{_n[0]:03d}", "dedup_key": f"{source}:{check_name}:{store or '-'}:{date or '-'}:{qualifier or ''}",
            "source": source, "check_name": check_name, "store_id": store,
            "store_name": STORES.get(store), "business_date": date, "qualifier": qualifier,
            "severity": severity, "status": status, "title": title, "category": category,
            "expected": "store-day VERIFIED" if source == "hme" else "within tolerance",
            "actual": "UNVERIFIED" if source == "hme" else "outside tolerance",
            "difference": difference, "evidence": {"run_id": 598, "note": "synthetic"},
            "actions": list(actions), "fingerprint": "f", "first_detected_at": first,
            "last_checked_at": "2026-11-25T08:47:13+00:00", "last_changed_at": first,
            "check_count": 4, "acknowledged_at": None, "acknowledged_by": None, "ack_note": None,
            "resolved_at": None, "resolution": None, "notified": 1}


def incidents():
    _n[0] = 0
    xs = [inc("hme", "day_reconciliation", "goal_threshold_mismatch", "HIGH",
              "HME day unreconciled after scheduled retry", date="2026-11-23",
              difference="goal_d_matches_pa_threshold: observed 9 vs expected 0")]
    xs += [inc("hme", "store_day_trust", "store_reconciliation_mismatch", "HIGH",
               "HME store-day did not reconcile", store=s, date="2026-11-23") for s in HME_FAILED]
    xs += [inc("hme", "store_day_trust", "no_access", "HIGH",
               "HME store not visible to the account (NO_ACCESS)", store=40, date="2026-11-19"),
           inc("infrastructure", "job", "job_failed", "HIGH", "hme-reconciliation-retry last run failed",
               qualifier="hme-reconciliation-retry"),
           inc("infrastructure", "job", "job_failed", "WARNING", "hme-daily-ingest last run failed",
               qualifier="hme-daily-ingest", status="ACKNOWLEDGED"),
           inc("r365_financial", "period_coverage", "stale_after_close", "WARNING",
               "R365 period not refreshed after close", date="2026-11-04", qualifier="FY2026 P11"),
           inc("revel", "sales_reconciliation", "open_unpaid_tickets", "WARNING",
               "Sales gap explained by open tickets", store=15, date="2026-11-22", difference="+1.20%"),
           inc("revel", "sales_reconciliation", "open_unpaid_tickets", "WARNING",
               "Sales gap explained by open tickets", store=15, date="2026-11-23", difference="+0.90%"),
           inc("revel", "sales_reconciliation", "payment_timing", "WARNING",
               "Payment dated on another business day", store=36, date="2026-11-22", difference="-2.10%")]
    xs[-4]["acknowledged_by"] = "ops@example.com"
    xs[-4]["acknowledged_at"] = T
    return xs


def cell(state, date, title="", sev="WARNING", diff=None):
    return {"business_date": date, "state": state, "severity": sev, "title": title, "difference": diff,
            "check": "x"}


def overview(*, monitor_state="HEALTHY", data_tier="HEALTHY", hme_mon="HEALTHY", incs=None, coverage="mapping",
             extra_store=False):
    incs = incidents() if incs is None else incs
    stores = []
    for sid, name in STORES.items():
        srcs = {"revel": cell("OK", "2026-11-23", "Reconciled"), "r365": cell("OK", "2026-11-18", "Week complete")}
        if sid in HME_MAPPED:
            srcs["hme"] = cell("FAIL" if sid in HME_FAILED else "OK", "2026-11-23", sev="HIGH")
        stores.append({"store_id": sid, "store_name": name, "sources": srcs})
    if extra_store:   # mapped for HME, no results anywhere yet (row added by the backend from coverage)
        stores.append({"store_id": 99, "store_name": "LCF New Store", "sources": {}})
    srcs = []
    for src, label in [("revel", "Revel POS"), ("hme", "HME drive-thru"), ("r365", "R365 operations"),
                       ("r365_financial", "R365 financials"), ("ai_chat", "AI Chat"), ("mcp", "MCP server"),
                       ("infrastructure", "Infrastructure"), ("monitor", "Data monitor")]:
        mine = [i for i in incs if i["source"] == src and i["status"] != "RESOLVED"]
        worst = "HIGH" if any(i["severity"] == "HIGH" for i in mine) else ("WARNING" if mine else None)
        mon = hme_mon if src == "hme" else "HEALTHY"
        data = "FAILED" if worst == "HIGH" else "DEGRADED" if worst else "HEALTHY"
        srcs.append({"source": src, "label": label, "data_state": data, "monitoring_state": mon,
                     "state": "FAILED" if data == "FAILED" else (mon if mon in ("UNKNOWN", "STALE") else data),
                     "worst_open_severity": worst, "open_incidents": len(mine), "counts": {"OK": 10},
                     "last_success_at": "2026-11-24T15:18:36+00:00", "detail": None,
                     "last_successful_check_at": "2026-11-25T08:47:13+00:00",
                     "oldest_successful_check_at": None, "last_attempted_check_at": "2026-11-25T08:47:13+00:00",
                     "checks": []})
    tier = lambda st: {"state": st, "disabled": False, "max_minutes": 45, "last_success_at": "2026-11-25T08:47:13+00:00",  # noqa: E731
                       "last_attempt_at": "2026-11-25T08:47:13+00:00", "last_attempt_status": "SUCCESS",
                       "last_attempt_error": None, "last_run_id": 7}
    open_ = [i for i in incs if i["status"] != "RESOLVED"]
    counts = {}
    for i in open_:
        counts[i["severity"]] = counts.get(i["severity"], 0) + 1
    fr = lambda src, label, verified, retry=False: {  # noqa: E731
        "source": src, "label": label, "last_sync_at": "2026-11-24T15:18:36+00:00",
        "last_check_at": "2026-11-25T08:47:13+00:00", "monitoring_state": hme_mon if src == "hme" else "HEALTHY",
        "expected_date": "2026-11-23", "expected_rule": "each day is due 09:45 Chicago the next day",
        "expected_counts": {} if src == "revel" else {"OK": 10, "FAIL": 2},
        "latest_evaluated_date": "2026-11-24", "latest_evaluated_label": None,
        "latest_evaluated_counts": {"PENDING": 12}, "latest_checked_date": "2026-11-23", "latest_checked_label": None,
        "latest_checked_counts": {"OK": 10, "FAIL": 2}, "latest_verified_date": verified,
        "latest_verified_label": None, "retry_pending": retry, "pending": ["Awaiting daily sync"]}
    return {"generated_at": "2026-11-25T08:50:00+00:00", "sources": srcs, "stores": stores,
            "open_incidents": open_, "open_counts": counts, "reconciliation_differences": [],
            "last_runs": {}, "checks": [],
            "monitor": {"state": monitor_state,
                        "tiers": {"health": tier("HEALTHY"), "data": tier(data_tier), "summary": tier("UNKNOWN")},
                        "notifications": {"state": "HEALTHY", "QUEUED": 0, "RETRYING": 0, "SENT": 0, "DRY_RUN": 4,
                                          "FAILED": 0, "failed_24h": 0, "oldest_undelivered_at": None}},
            "states": ["HEALTHY", "PENDING", "DEGRADED", "UNKNOWN", "STALE", "FAILED"],
            "freshness": [fr("revel", "Revel POS", None), fr("hme", "HME drive-thru", "2026-11-22", True),
                          fr("r365", "R365 operations", "2026-11-18")],
            "coverage": None if coverage is None else {
                k: {"basis": "mapping", "as_of": T, "store_ids": ids, "stores": [{"id": i, "name": STORES.get(i, "LCF New Store")} for i in ids]}
                for k, ids in (("revel", list(STORES)), ("hme", HME_MAPPED + ([99] if extra_store else [])),
                               ("r365", list(STORES)))}}


RESOLVED = []


class Api:
    def __init__(self, ov=None, overview_status=200, fail_next=0, role="super_admin"):
        self.ov = ov or overview()
        self.overview_status = overview_status
        self.fail_next = fail_next
        self.role = role
        self.calls = []
        self.acks = []

    async def handle(self, route):
        req = route.request
        path = req.url.split("/api", 1)[1]
        p = path.split("?")[0]
        self.calls.append((req.method, path))

        def ok(body, status=200):
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        if p == "/auth/me":
            return await ok({"id": 1, "email": "admin@example.com", "full_name": "Admin", "role": self.role,
                             "is_admin": True})
        if p == "/models":
            return await ok({"models": ["m1"], "default": "m1"})
        if p == "/conversations":
            return await ok([])
        if p == "/admin/monitoring/overview":
            if self.fail_next:
                self.fail_next -= 1
                return await ok({"detail": "Monitor store unavailable (OperationalError)."}, 503)
            if self.overview_status != 200:
                return await ok({"detail": "nope"}, self.overview_status)
            return await ok(self.ov)
        if p == "/admin/monitoring/incidents":
            return await ok(copy.deepcopy(RESOLVED) if "RESOLVED" in path else self.ov["open_incidents"])
        if p.startswith("/admin/monitoring/incidents/") and p.endswith("/ack") and req.method == "POST":
            iid = p.split("/")[4]
            self.acks.append((iid, json.loads(req.post_data or "{}")))
            for i in self.ov["open_incidents"]:
                if i["id"] == iid:
                    i["status"], i["acknowledged_by"], i["acknowledged_at"] = "ACKNOWLEDGED", "admin@example.com", T
                    i["ack_note"] = json.loads(req.post_data or "{}").get("note")
            return await ok({"acknowledged": iid})
        if p.startswith("/admin/monitoring/incidents/"):
            iid = p.split("/")[4]
            i = next((x for x in self.ov["open_incidents"] + RESOLVED if x["id"] == iid), None)
            if not i:
                return await ok({"detail": "Incident not found"}, 404)
            ev = [{"id": 1, "at": i["first_detected_at"], "event": "OPENED", "actor": "monitor", "detail": {}}]
            if i["status"] == "ACKNOWLEDGED":
                ev.append({"id": 2, "at": T, "event": "ACKNOWLEDGED", "actor": i["acknowledged_by"], "detail": {}})
            return await ok({"incident": i, "events": ev})
        if p == "/admin/monitoring/notifications":
            return await ok([])
        return await ok({"detail": "unmocked " + p}, 404)


async def text(p, sel):
    return (await p.locator(sel).first.inner_text()).strip()


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        async def open_dash(api, mobile=False):
            ctx = await browser.new_context(**({"viewport": {"width": 390, "height": 844}, "is_mobile": True,
                                                 "has_touch": True} if mobile else {"viewport": {"width": 1440, "height": 900}}))
            await ctx.route("**/api/**", api.handle)
            p = await ctx.new_page()
            await p.goto(BASE + "/")
            await p.get_by_label("Account menu").click()
            await p.get_by_role("menuitem", name="Data monitoring").or_(p.get_by_text("Data monitoring")).first.click()
            return ctx, p

        # 1. counts + grouping ------------------------------------------------
        print("1. summary counts and incident grouping")
        api = Api()
        ctx, p = await open_dash(api)
        await p.wait_for_selector("[data-testid=summary]")
        check("unresolved incidents = 17 (every record counted)", await text(p, "[data-testid=unresolved-count]") == "17")
        check("issues = 8 after grouping; 16 new, 1 acknowledged",
              await text(p, "[data-testid=issue-count]") == "8 issues · 16 new · 1 acknowledged",
              await text(p, "[data-testid=issue-count]"))
        check("affected locations = 9 distinct stores (+4 network-level)",
              await text(p, "[data-testid=affected-count]") == "9")
        check("severity counts: 12 HIGH, 5 WARNING",
              await p.locator("[data-sev=HIGH]").inner_text() == "12" and await p.locator("[data-sev=WARNING]").inner_text() == "5")
        members = await p.eval_on_selector_all("[data-group]", "els => els.map(e => +e.dataset.members)")
        check("no record lost: group member counts sum to 17", sum(members) == 17, members)
        hme = p.locator('[data-group="hme|reconciliation|2026-11-23"]')
        check("HME Nov 23 network + 9 stores shown as ONE group of 10", await hme.get_attribute("data-members") == "10")
        t = await hme.inner_text()
        check("group titled 'HME — November 23: Goal reconciliation failure' with HIGH and 9 stores",
              "HME — November 23: Goal reconciliation failure" in t and "HIGH" in t and "9 stores" in t, t)
        check("Rosenberg NO_ACCESS on Nov 19 stays a separate issue",
              await p.locator('[data-group*="no_access|2026-11-19"]').count() == 1)
        sections = await p.eval_on_selector_all("[data-section]", "els => els.map(e => e.dataset.section)")
        check("priority sort: sections with unresolved HIGH first (HME, Infrastructure), then Revel, R365",
              sections == ["hme", "infrastructure", "revel", "r365"], sections)
        infra = await p.locator('[data-section=infrastructure] [data-group]').all_inner_texts()
        check("within a section unresolved HIGH comes before WARNING", "HIGH" in infra[0] and "WARNING" in infra[1], infra)
        check("acknowledged incident visibly marked ACKNOWLEDGED, new ones NEW",
              "ACKNOWLEDGED" in infra[1] and "NEW" in infra[0])
        # expand
        btn = hme.get_by_role("button", name="Expand 10 incidents")
        await btn.click()
        ids = await hme.locator("[data-testid=group-members] [data-incident]").evaluate_all("els => els.map(e => e.dataset.incident)")
        check("expanding shows all 10 underlying incident IDs", len(ids) == 10 and len(set(ids)) == 10, ids)
        check("expand control reports aria-expanded=true",
              await hme.get_by_role("button", name="Collapse 10 incidents").get_attribute("aria-expanded") == "true")
        await hme.locator("[data-testid=group-open]").click()
        gd = await text(p, "[data-testid=group-detail]")
        check("group detail: 10 records, 9 affected stores, November 23, first/last times",
              "10 incident records" in gd and "Affected stores\n9" in gd and "November 23" in gd
              and "First detected" in gd and "Last checked" in gd, gd[:300])
        check("group detail explains grouping is display-only", "keeps its own ID, status and history" in gd)
        await p.locator("[data-testid=group-detail] [data-incident]").nth(3).click()
        await p.wait_for_selector("[data-testid=incident-detail]")
        d = await text(p, "[data-testid=incident-detail]")
        check("member detail: plain summary, location, business date, expected/actual",
              "did not pass reconciliation" in d and "Location" in d and "November 23" in d
              and "Expected" in d and "Actual" in d, d[:300])
        check("'Back to group' offered from a member", await p.get_by_role("button", name="Back to group").count() == 1)
        check("technical evidence collapsed by default", await p.locator("[data-testid=technical]").count() == 0)
        await p.get_by_role("button", name="Technical evidence").click()
        check("technical evidence expands (check, key, JSON)",
              "dedup_key" not in await text(p, "[data-testid=technical]") and "key=hme:" in await text(p, "[data-testid=technical]"))
        # likely cause only from evidence
        await p.get_by_role("button", name="Back to group").click()
        check("likely cause shown for goal mismatch (evidence-backed category)", "Likely cause" in await text(p, "[data-testid=group-detail]"))
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            await p.screenshot(path=os.path.join(SHOTS, "desktop-full.png"), full_page=True)
            await p.locator("main").evaluate("m => m.scrollTo(0, 0)")
            await p.screenshot(path=os.path.join(SHOTS, "desktop-top.png"))

        # 2. filters / search / sort -----------------------------------------
        print("2. filtering, search and sorting")
        cnt = lambda: text(p, "[data-testid=list-count]")  # noqa: E731
        await p.get_by_label("Filter by severity").select_option("WARNING")
        check("severity WARNING -> 5 issues / 5 records", await cnt() == "5 issues · 5 matching incident records", await cnt())
        await p.get_by_label("Filter by severity").select_option("")
        await p.get_by_label("Filter by source").select_option("revel")
        check("source Revel -> 3 issues", await cnt() == "3 issues · 3 matching incident records", await cnt())
        await p.get_by_label("Filter by source").select_option("")
        await p.get_by_label("Filter by store").select_option("15")
        c = await cnt()
        check("store Shepherd -> 3 issues, 3 MATCHING records; the network parent is counted separately as context",
              c == "3 issues · 3 matching incident records (+1 network-level shown for context)", c)
        hg = p.locator('[data-group="hme|reconciliation|2026-11-23"]')
        check("filtered group says '1 of 10 incidents match' and '1 of 9 stores match'",
              await hg.locator("[data-testid=group-count]").inner_text() == "1 of 10 incidents match"
              and "1 of 9 stores match" in await hg.inner_text(), await hg.inner_text())
        if await hg.locator("[aria-controls]").get_attribute("aria-expanded") != "true":
            await hg.locator("[aria-controls]").click()
        ctxs = await hg.locator("[data-testid=group-members] [data-context=true]").count()
        rows_ = await hg.locator("[data-testid=group-members] [data-incident]").count()
        check("expanded filtered group: Shepherd + the network parent marked CONTEXT (2 rows, 1 context)",
              rows_ == 2 and ctxs == 1 and "context" in (await hg.locator("[data-context=true]").inner_text()).lower())
        await hg.locator("[data-testid=group-open]").click()
        check("group detail: '1 of 10 match the current filters'",
              "1 of 10 match the current filters" in await text(p, "[data-testid=group-detail-matches]"))
        await p.get_by_label("Filter by store").select_option("")
        await p.get_by_label("Filter by business date").select_option("2026-11-22")
        check("business date Nov 22 -> 2 Revel issues", await cnt() == "2 issues · 2 matching incident records", await cnt())
        await p.get_by_label("Filter by business date").select_option("")
        await p.get_by_label("Search incidents").fill("rosenberg")
        check("search 'rosenberg' -> NO_ACCESS issue + HME group: 2 matching (+1 context)",
              await cnt() == "2 issues · 2 matching incident records (+1 network-level shown for context)", await cnt())
        await p.get_by_label("Search incidents").fill("")
        await p.get_by_label("Filter by status").select_option("acknowledged")
        check("status Acknowledged -> only the acknowledged record", await cnt() == "1 issue · 1 matching incident record", await cnt())
        await p.get_by_label("Filter by status").select_option("new")
        check("status New -> 7 issues / 16 matching records (the acknowledged job drops out), no context needed",
              (await cnt()) == "7 issues · 16 matching incident records", await cnt())
        RESOLVED.append({**incidents()[0], "id": "INC-R001", "status": "RESOLVED", "business_date": "2026-11-18",
                         "dedup_key": "hme:day:-:2026-11-18", "resolved_at": T, "resolution": "cleared: check passed"})
        await p.get_by_label("Filter by status").select_option("resolved")
        await p.wait_for_function("document.querySelector('[data-testid=list-count]').innerText.startsWith('1 issue')")
        check("status Resolved -> fetched from the incidents API (1)", await cnt() == "1 issue · 1 matching incident record")
        check("resolved incidents requested only when needed",
              sum(1 for m, u in api.calls if "status=RESOLVED" in u) >= 1)
        await p.get_by_label("Filter by status").select_option("unresolved")
        await p.get_by_label("Search incidents").fill("zzzz-no-match")
        check("no matches -> empty state with clear-filters", "No incidents match these filters" in await text(p, "[data-testid=empty]"))
        await p.get_by_role("button", name="Clear filters").first.click()
        check("clear filters restores all 8 issues", (await cnt()).startswith("8 issues"))
        await p.get_by_label("Sort incidents").select_option("business_date")
        first = await p.locator("[data-section=hme] [data-group]").first.get_attribute("data-group")
        check("sort by business date puts Nov 23 before Nov 19 in HME", "2026-11-23" in first, first)
        sections = await p.eval_on_selector_all("[data-section]", "els => els.map(e => e.dataset.section)")
        check("non-priority sorts keep the fixed source order", sections == ["revel", "hme", "r365", "infrastructure"], sections)
        await p.get_by_label("Sort incidents").select_option("priority")

        # 3. store matrix ----------------------------------------------------
        print("3. per-store health")
        st = lambda sid, src: p.locator(f'[data-cell="{sid}:{src}"]').get_attribute("data-state")  # noqa: E731
        check("12 stores listed", await p.locator("[data-testid=store-matrix] tbody tr").count() == 12)
        check("HME-unmapped Downtown Houston -> NOT_APPLICABLE (not healthy, not failed)", await st(61, "hme") == "NOT_APPLICABLE")
        check("Katy HME with open HIGH incident -> FAILED", await st(6, "hme") == "FAILED")
        check("Cypress HME verified -> HEALTHY", await st(26, "hme") == "HEALTHY")
        check("Missouri City Revel with WARNING incident -> DEGRADED", await st(36, "revel") == "DEGRADED")
        check("Rosenberg HME (two HIGH incidents, two dates) -> FAILED", await st(40, "hme") == "FAILED")
        await p.locator('[data-store="36"] th button').click()
        check("clicking a store filters incidents to it (2 matching + 1 context)", await p.get_by_label("Filter by store").input_value() == "36"
              and (await cnt()) == "2 issues · 2 matching incident records (+1 network-level shown for context)", await cnt())
        check("store button reports aria-pressed", await p.locator('[data-store="36"] th button').get_attribute("aria-pressed") == "true")
        await p.locator('[data-store="36"] th button').click()
        check("clicking again clears the store filter", await p.get_by_label("Filter by store").input_value() == "")

        # 4. freshness -------------------------------------------------------
        print("4. data-source freshness")
        ft = await text(p, "[data-testid=freshness-table]")
        check("freshness table shows loaded vs verified columns",
              all(x.lower() in ft.lower() for x in ("Last sync", "Expected by now", "Latest evaluated", "Latest checked", "Latest verified", "Monitor check", "Retry")))
        check("expected date not evaluated -> says 'not evaluated yet' (an older/newer evaluated date never stands in)",
              await text(p, "[data-testid=expected-revel]") == "Nov 23\nnot evaluated yet"
              and (await text(p, "[data-testid=evaluated-revel]")).startswith("Nov 24"), await text(p, "[data-testid=expected-revel]"))
        check("expected date evaluated -> its own counts", "10 ok, 2 failing" in await text(p, "[data-testid=expected-hme]"))
        check("no 'Latest expected' label anywhere", "latest expected" not in (await p.locator("main").inner_text()).lower())
        check("Revel with no fully verified date says so (never blank or healthy)",
              await text(p, "[data-testid=verified-revel]") == "None in the monitored window")
        check("HME retry pending shown", "pending" in await p.locator('[data-source=hme]').inner_text())
        check("HME latest verified Nov 22", await text(p, "[data-testid=verified-hme]") == "Nov 22")

        # 5. acknowledgement -------------------------------------------------
        print("5. acknowledgement")
        await p.locator('[data-group*="no_access|2026-11-19"] [data-testid=group-open]').click()
        await p.wait_for_selector("[data-testid=incident-detail]")
        sev_before = await p.locator("[data-testid=incident-detail] [data-badge]").first.get_attribute("data-badge")
        await p.get_by_label("Acknowledge", exact=True).fill("Checking HME portal access")
        await p.get_by_role("button", name="Acknowledge", exact=True).click()
        await p.wait_for_function("document.querySelector('[data-testid=incident-detail]').innerText.includes('ACKNOWLEDGED')")
        d = await text(p, "[data-testid=incident-detail]")
        check("ack POSTs once with the note", len(api.acks) == 1 and api.acks[0][1] == {"note": "Checking HME portal access"}, api.acks)
        check("after ack: ACKNOWLEDGED, severity unchanged, not resolved",
              "ACKNOWLEDGED" in d and sev_before == "HIGH"
              and await p.locator("[data-testid=incident-detail] [data-badge]").first.get_attribute("data-badge") == "HIGH"
              and "Resolved" not in d, d[:200])
        check("ack explains it never changes the verdict or resolves", "does not change the" in d and "data-quality verdict" in d)
        check("unresolved count unchanged by ack (still 17)", await text(p, "[data-testid=unresolved-count]") == "17")
        check("acknowledge button gone once acknowledged", await p.get_by_role("button", name="Acknowledge", exact=True).count() == 0)
        writes = [(m, u) for m, u in api.calls if m != "GET"]
        check("the only write the page made was the ack", writes == [("POST", f"/admin/monitoring/incidents/{api.acks[0][0]}/ack")], writes)
        # keyboard
        g = p.locator('[data-group="hme|reconciliation|2026-11-23"]')
        tog = g.locator("[aria-controls]")
        before = await tog.get_attribute("aria-expanded")
        await tog.focus()
        await p.keyboard.press("Enter")
        mid = await tog.get_attribute("aria-expanded")
        await p.keyboard.press("Enter")
        check("keyboard: Enter on the focused expand control toggles the group both ways",
              before != mid and await tog.get_attribute("aria-expanded") == before, (before, mid))
        await ctx.close()

        # 5b. mixed-status / mixed-severity groups under filters -------------
        print("5b. filtered groups show matched status/severity, never the group's")
        incs = incidents()
        katy = next(i for i in incs if i["store_id"] == 6 and i["business_date"] == "2026-11-23")
        katy.update(status="ACKNOWLEDGED", acknowledged_by="ops@example.com", acknowledged_at=T)
        ella = next(i for i in incs if i["store_id"] == 7 and i["business_date"] == "2026-11-23")
        ella["severity"] = "WARNING"
        api = Api(overview(incs=incs))
        ctx, p = await open_dash(api)
        await p.wait_for_selector("[data-testid=summary]")
        cnt = lambda: text(p, "[data-testid=list-count]")  # noqa: E731
        hg = p.locator('[data-group="hme|reconciliation|2026-11-23"]')
        check("unfiltered mixed group: PARTLY ACK, HIGH, 10 incidents",
              "PARTLY ACK" in await hg.inner_text() and await hg.locator("[data-badge]").first.get_attribute("data-badge") == "HIGH")
        await p.get_by_label("Filter by status").select_option("acknowledged")
        check("status Acknowledged: group shows ACKNOWLEDGED (its matched member), 1 of 10, parent as context",
              await hg.locator("[data-badge]").nth(1).get_attribute("data-badge") == "ACKNOWLEDGED"
              and await hg.locator("[data-testid=group-count]").inner_text() == "1 of 10 incidents match"
              and (await cnt()).startswith("2 issues · 2 matching incident records (+1"), await cnt())
        await p.get_by_label("Filter by status").select_option("unresolved")
        await p.get_by_label("Filter by severity").select_option("WARNING")
        check("severity WARNING: the HIGH group appears as WARNING (only its WARNING member matches), parent is context",
              await hg.locator("[data-badge]").first.get_attribute("data-badge") == "WARNING"
              and await hg.locator("[data-testid=group-count]").inner_text() == "1 of 10 incidents match")
        await p.get_by_label("Filter by severity").select_option("HIGH")
        c = await cnt()
        check("severity HIGH: 8 of 10 match in the group; parent itself HIGH so it is a match, not context",
              await hg.locator("[data-testid=group-count]").inner_text() == "9 of 10 incidents match" and "context" not in c, c)
        await ctx.close()

        # 5c. coverage -----------------------------------------------------
        print("5c. N/A only from a known mapping; missing evidence is UNKNOWN")
        api = Api(overview(coverage=None))
        ctx, p = await open_dash(api)
        await p.wait_for_selector("[data-testid=summary]")
        check("mapping unknown -> Downtown Houston HME is UNKNOWN, not N/A",
              await p.locator('[data-cell="61:hme"]').get_attribute("data-state") == "UNKNOWN")
        check("header says the mapping is not yet known", "mapping not yet known" in await p.locator("section[aria-labelledby=stores-h]").inner_text())
        await ctx.close()
        api = Api(overview(extra_store=True))
        ctx, p = await open_dash(api)
        await p.wait_for_selector("[data-testid=summary]")
        check("store mapped for HME with no evidence -> UNKNOWN (not N/A, not healthy)",
              await p.locator('[data-cell="99:hme"]').get_attribute("data-state") == "UNKNOWN")
        check("same store not in the Revel mapping -> N/A", await p.locator('[data-cell="99:revel"]').get_attribute("data-state") == "NOT_APPLICABLE")
        check("known mapping excluding Downtown Houston -> HME N/A",
              await p.locator('[data-cell="61:hme"]').get_attribute("data-state") == "NOT_APPLICABLE")
        await ctx.close()

        # 6. UNKNOWN / STALE --------------------------------------------------
        print("6. UNKNOWN and STALE are never shown as HEALTHY")
        api = Api(overview(monitor_state="STALE", data_tier="STALE", hme_mon="STALE", incs=[]))
        ctx, p = await open_dash(api)
        await p.wait_for_selector("[data-testid=summary]")
        check("no incidents but stale monitoring -> overall STALE, monitoring STALE",
              await p.locator("[data-testid=overall] [data-badge]").get_attribute("data-badge") == "STALE"
              and await p.locator("[data-testid=monitoring-health] [data-badge]").get_attribute("data-badge") == "STALE")
        check("business-data health reported separately (HEALTHY here)",
              await p.locator("[data-testid=data-health] [data-badge]").get_attribute("data-badge") == "HEALTHY")
        check("empty list says 'not an all-clear' while the monitor is STALE",
              "not an all-clear" in await text(p, "[data-testid=empty]"))
        check("store cells of a STALE source show STALE, not HEALTHY",
              await p.locator('[data-cell="26:hme"]').get_attribute("data-state") == "STALE")
        await ctx.close()
        api = Api(overview(monitor_state="UNKNOWN", incs=[]))
        ctx, p = await open_dash(api)
        await p.wait_for_selector("[data-testid=summary]")
        check("monitor UNKNOWN -> overall UNKNOWN (never HEALTHY)",
              await p.locator("[data-testid=overall] [data-badge]").get_attribute("data-badge") == "UNKNOWN")
        await ctx.close()

        # 7. empty, healthy --------------------------------------------------
        print("7. empty state")
        api = Api(overview(incs=[]))
        ctx, p = await open_dash(api)
        await p.wait_for_selector("[data-testid=summary]")
        check("no incidents + healthy monitor -> 'No unresolved incidents', overall HEALTHY",
              (await text(p, "[data-testid=empty]")).startswith("No unresolved incidents")
              and "all-clear" not in await text(p, "[data-testid=empty]")
              and await p.locator("[data-testid=overall] [data-badge]").get_attribute("data-badge") == "HEALTHY")
        check("counts are zero", await text(p, "[data-testid=unresolved-count]") == "0" and await text(p, "[data-testid=affected-count]") == "0")
        await ctx.close()

        # 8. auth + failures -------------------------------------------------
        print("8. unauthorized and failed API requests")
        api = Api(overview_status=403)
        ctx, p = await open_dash(api)
        await p.wait_for_selector("[data-testid=load-error]")
        e = await text(p, "[data-testid=load-error]")
        check("403 -> 'does not have access', and not an all-clear", "does not have access" in e and "not an all-clear" in e, e)
        await ctx.close()
        api = Api(overview_status=401)
        ctx, p = await open_dash(api)
        await p.wait_for_function("!document.querySelector('[data-testid=summary]') && !document.querySelector('[data-testid=loading]')")
        check("401 -> page leaves the dashboard (signed out)", await p.locator("[data-testid=summary]").count() == 0
              and await p.get_by_text("Data monitoring").count() == 0)
        await ctx.close()
        api = Api(fail_next=1)
        ctx, p = await open_dash(api)
        await p.wait_for_selector("[data-testid=load-error]")
        check("first load 503 -> error with retry, no data drawn", "Monitor store unavailable" in await text(p, "[data-testid=load-error]")
              and await p.locator("[data-testid=summary]").count() == 0)
        await p.get_by_role("button", name="Try again").click()
        await p.wait_for_selector("[data-testid=summary]")
        check("retry recovers", await text(p, "[data-testid=unresolved-count]") == "17")
        api.fail_next = 1
        await p.get_by_role("button", name="Refresh", exact=True).click()
        await p.wait_for_selector("[data-testid=refresh-error]")
        check("refresh failure keeps last data and warns it may be out of date",
              "may be out of date" in await text(p, "[data-testid=refresh-error]")
              and await text(p, "[data-testid=unresolved-count]") == "17")
        await ctx.close()

        # 9. mobile ----------------------------------------------------------
        print("9. mobile layout")
        api = Api()
        ctx, p = await open_dash(api, mobile=True)
        await p.wait_for_selector("[data-testid=summary]")
        ov = await p.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        check("no horizontal page overflow at 390px", ov[0] <= ov[1], ov)
        wide = await p.evaluate("""() => [...document.querySelectorAll('main *')].filter(e => {
            const r = e.getBoundingClientRect(); let a = e.parentElement, clipped = false;
            while (a) { const s = getComputedStyle(a); if (/(auto|scroll|hidden)/.test(s.overflowX)) { clipped = true; break } a = a.parentElement }
            return !clipped && r.right > window.innerWidth + 1 }).length""")
        check("no element sticks out of the viewport (tables scroll inside their card)", wide == 0, wide)
        check("mobile shows freshness as stacked cards, not the wide table",
              await p.locator("[data-testid=freshness-cards]").is_visible()
              and not await p.locator("[data-testid=freshness-table]").is_visible())
        if SHOTS:
            await p.screenshot(path=os.path.join(SHOTS, "mobile-top.png"))
            await p.screenshot(path=os.path.join(SHOTS, "mobile-full.png"), full_page=True)
        await p.locator('[data-group="hme|reconciliation|2026-11-23"] [data-testid=group-open]').click()
        check("selecting an issue on mobile shows detail and hides the list",
              await p.locator("[data-testid=group-detail]").is_visible() and not await p.locator("[data-testid=incident-list]").is_visible())
        if SHOTS:
            await p.locator("[data-testid=detail-pane]").scroll_into_view_if_needed()
            await p.screenshot(path=os.path.join(SHOTS, "mobile-detail.png"))
        await p.get_by_test_id("back-to-list").click()
        check("'All incidents' returns to the list", await p.locator("[data-testid=incident-list]").is_visible())
        await p.keyboard.press("Escape")
        await ctx.close()
        await browser.close()


asyncio.run(main())
httpd.shutdown()
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
