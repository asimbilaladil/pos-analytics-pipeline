from playwright.sync_api import sync_playwright
import json
from datetime import datetime, timedelta
import os
import time

USERNAME = os.getenv("REVEL_USER")
PASSWORD = os.getenv("REVEL_PASS")

BASE_URL = "https://laynes.revelup.com"

ESTABLISHMENTS = [32, 14, 48, 7, 6, 25, 36, 26, 20, 40, 15]

STATE_FILE = "state.json"

# OrderItem establishment filter does NOT work on Revel API.
# We fetch items by order ID batches instead.
# Max safe batch size to keep URL length reasonable.
BATCH_SIZE = 200


# ---------- DATE RANGE ----------
def get_date_range():
    yesterday = datetime.now() - timedelta(days=1)
    range_from = yesterday.strftime("%Y-%m-%dT00:00:00")
    range_to   = yesterday.strftime("%Y-%m-%dT23:59:59")
    return range_from, range_to


# ---------- LOGIN ----------
def login_and_save_state(context, page):
    print("Logging in...")
    page.goto(BASE_URL)
    page.wait_for_url("**authentication.revelup.com**")
    page.wait_for_selector('input[name="username"]', timeout=60000)
    page.fill('input[name="username"]', USERNAME)
    page.click('button[type="submit"]')
    page.wait_for_selector('input[name="password"]', timeout=60000)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")
    print("Logged in successfully")
    context.storage_state(path=STATE_FILE)
    print("Session saved")


# ---------- UTILS ----------
def extract_order_id(order_field):
    if isinstance(order_field, int):
        return order_field
    if isinstance(order_field, str):
        return int(order_field.strip("/").split("/")[-1])
    return None


def fetch_all_pages(context, endpoint, params, label="records"):
    """
    Fetches all pages from a Revel endpoint.
    Params passed as dict so Playwright URL-encodes colons in datetimes correctly.
    """
    all_records = []
    offset = 0

    while True:
        paged_params = {**params, "limit": 1000, "offset": offset, "format": "json"}
        response = context.request.get(endpoint, params=paged_params)

        if response.status != 200:
            print(f"  ERROR {response.status} at offset {offset}: {response.text()[:300]}")
            break

        data = response.json()
        records = data.get("objects", [])
        total_count = data.get("meta", {}).get("total_count", 0)

        if not records:
            break

        all_records.extend(records)
        print(f"  Fetched {len(all_records)}/{total_count} {label}")

        if len(all_records) >= total_count:
            break

        offset += 1000
        time.sleep(0.3)

    return all_records


def fetch_items_for_orders(context, order_ids):
    """
    Fetches OrderItems by batching order IDs using order__in=id1,id2,...
    Each batch is fully paginated to ensure no items are missed.

    Why batches of 200:
    - OrderItem establishment filter is broken (returns all locations)
    - Fetching by order ID is the only reliable way to get correct items
    - 200 IDs per batch keeps URL length safe while minimising API calls
    - Each batch is paginated so even large orders get all their items
    """
    all_items = []
    total_batches = (len(order_ids) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(order_ids), BATCH_SIZE):
        batch = order_ids[i: i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        ids_str = ",".join(map(str, batch))

        # Paginate within each batch
        offset = 0
        while True:
            response = context.request.get(
                f"{BASE_URL}/resources/OrderItem/",
                params={
                    "format": "json",
                    "order__in": ids_str,
                    "limit": 1000,
                    "offset": offset,
                }
            )

            if response.status != 200:
                print(f"  ERROR {response.status} on batch {batch_num}/{total_batches}")
                break

            data = response.json()
            records = data.get("objects", [])
            total_count = data.get("meta", {}).get("total_count", 0)

            if not records:
                break

            all_items.extend(records)
            offset += 1000

            if offset >= total_count:
                break

        print(f"  Items batch {batch_num}/{total_batches} done — {len(all_items)} items so far")
        time.sleep(0.2)

    return all_items


# ---------- MAIN ----------
def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        if os.path.exists(STATE_FILE):
            print("Using saved session...")
            context = browser.new_context(storage_state=STATE_FILE)
        else:
            context = browser.new_context()

        page = context.new_page()

        if not os.path.exists(STATE_FILE):
            login_and_save_state(context, page)
        else:
            page.goto(BASE_URL)
            if "login" in page.url or "authentication" in page.url:
                print("Session expired, re-logging in...")
                login_and_save_state(context, page)

        range_from, range_to = get_date_range()
        print(f"Date range: {range_from} to {range_to}")

        for est in ESTABLISHMENTS:
            print(f"\n--- Establishment {est} ---")

            try:
                # -------------------- FETCH ORDERS --------------------
                all_orders = fetch_all_pages(
                    context,
                    endpoint=f"{BASE_URL}/resources/Order/",
                    params={
                        "establishment": est,
                        "created_date__gte": range_from,
                        "created_date__lte": range_to,
                    },
                    label="orders"
                )
                print(f"Total orders: {len(all_orders)}")

                if not all_orders:
                    print("No orders found, skipping.")
                    continue

                # -------------------- FETCH ITEMS BY ORDER IDs --------------------
                # establishment filter is broken on OrderItem endpoint — use order__in instead
                order_ids = [o["id"] for o in all_orders]
                print(f"Fetching items for {len(order_ids)} orders in batches of {BATCH_SIZE}...")
                all_items = fetch_items_for_orders(context, order_ids)
                print(f"Total items fetched: {len(all_items)}")

                # -------------------- JOIN --------------------
                items_map = {}
                for item in all_items:
                    oid = extract_order_id(item.get("order"))
                    if oid:
                        items_map.setdefault(oid, []).append(item)

                results = []
                orders_with_items = 0
                orders_without_items = 0

                for order in all_orders:
                    oid = order["id"]
                    items = items_map.get(oid, [])
                    if items:
                        orders_with_items += 1
                    else:
                        orders_without_items += 1
                    results.append({"order": order, "items": items})

                pct = orders_with_items / len(all_orders) * 100
                print(f"Orders WITH items:    {orders_with_items} ({pct:.1f}%)")
                print(f"Orders without items: {orders_without_items} (open/voided — expected)")

                # -------------------- SAVE --------------------
                filename = f"orders_with_items_{est}.json"
                with open(filename, "w") as f:
                    json.dump(results, f, indent=2)

                print(f"Saved: {filename}")

            except Exception as e:
                print(f"ERROR on establishment {est}: {e}")
                import traceback
                traceback.print_exc()

        browser.close()
        print("\nDone.")


run()
