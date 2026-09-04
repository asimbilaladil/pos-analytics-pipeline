"""
seed_establishments.py
Fetches establishment metadata from Revel API and seeds the establishments table.
Run once before pipeline.py.
"""

import os
import psycopg2
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

BASE_URL   = "https://laynes.revelup.com"
STATE_FILE = "/tmp/revel_session.json"

REVEL_USER = os.getenv("REVEL_USER")
REVEL_PASS = os.getenv("REVEL_PASS")
EST_IDS    = [int(x.strip()) for x in os.getenv("ESTABLISHMENTS", "32,14,48,7,6,25,36,26,20,40,15").split(",")]

def db_connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS"),
    )

def login_and_save(context, page):
    print("Logging into Revel...")
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")
    if "login" in page.url or "authentication" in page.url:
        page.wait_for_selector('input[name="username"]', timeout=60000)
        page.fill('input[name="username"]', REVEL_USER)
        page.click('button[type="submit"]')
        page.wait_for_selector('input[name="password"]', timeout=60000)
        page.fill('input[name="password"]', REVEL_PASS)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
    context.storage_state(path=STATE_FILE)
    print("Session ready")

with sync_playwright() as pw:
    storage = STATE_FILE if os.path.exists(STATE_FILE) else None
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(storage_state=storage)
    page    = context.new_page()

    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")
    if "login" in page.url or "authentication" in page.url:
        print("Session expired — re-logging in")
        login_and_save(context, page)
    else:
        print("Session valid")

    page.close()

    # Try fetching each establishment by ID directly
    print("Fetching establishments from Revel...")
    our_ests = []
    for est_id in EST_IDS:
        resp = context.request.get(
            f"{BASE_URL}/resources/Establishment/{est_id}/",
            params={"format": "json"}
        )
        print(f"  [{est_id}] HTTP {resp.status}")
        if resp.status == 200:
            try:
                e = resp.json()
                our_ests.append(e)
                print(f"       → {e.get('name', '?')} — {e.get('city', '')} {e.get('state', '')}")
            except Exception as ex:
                print(f"       → JSON parse error: {ex} | body: {resp.text()[:100]}")
        else:
            print(f"       → {resp.text()[:100]}")

    browser.close()

# Seed the DB
conn = db_connect()
with conn:
    with conn.cursor() as cur:
        found_ids = set()
        for e in our_ests:
            est_id   = int(e["id"])
            name     = e.get("name") or f"Location {est_id}"
            city     = e.get("city") or ""
            state    = e.get("state") or "TX"
            timezone = e.get("timezone") or "US/Central"
            found_ids.add(est_id)

            cur.execute("""
                INSERT INTO establishments (id, name, city, state, timezone, active)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (id) DO UPDATE SET
                    name     = EXCLUDED.name,
                    city     = EXCLUDED.city,
                    state    = EXCLUDED.state,
                    timezone = EXCLUDED.timezone
            """, (est_id, name, city, state, timezone))
            print(f"  Seeded: [{est_id}] {name} — {city}, {state}")

        for missing_id in EST_IDS:
            if missing_id not in found_ids:
                cur.execute("""
                    INSERT INTO establishments (id, name, city, state, timezone, active)
                    VALUES (%s, %s, 'Unknown', 'TX', 'US/Central', TRUE)
                    ON CONFLICT (id) DO NOTHING
                """, (missing_id, f"Location {missing_id}"))
                print(f"  Placeholder: [{missing_id}]")

conn.close()
print("\nDone.")
