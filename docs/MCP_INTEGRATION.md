# Laynes Intelligence read-only MCP server

Status: implemented on branch `feat/read-only-mcp`, **not deployed**. Production
(`laynes-api.service`, nginx, DB, env files) is untouched by this change.

## 1. What it does

A separate, loopback-only service (`127.0.0.1:8710`) exposing five **read-only**
tools over **MCP Streamable HTTP** at `https://intelligence.aygchicken.com/mcp`
(after nginx is configured). Built on the official Python SDK `mcp==1.30.0`
(protocol `2025-11-25`; also negotiates `2025-06-18`, `2025-03-26`, `2024-11-05`).

Transport choices: stateless, JSON responses (`application/json`), **POST only**.
`GET`/`DELETE`/others return `405` (spec-permitted; no idle SSE streams behind
the proxy). No WebSocket. Origin header is validated (browser origins must be in
`MCP_ALLOWED_ORIGINS`; non-browser clients send none), Host is validated by the SDK.

## 2. Authentication

* Dedicated bearer tokens `lmcp_<key_id>_<secret>` (256-bit secret from
  `secrets.token_urlsafe(32)`). Only a SHA-256 digest is stored, in a 0600 SQLite
  file under `MCP_DATA_DIR` (not the Laynes PostgreSQL database).
* Each key: id, label, created, optional expiry, revoked status, **tool list**,
  **store list** (explicit ids, or `*`), per-minute rate, daily question cap.
* `tools/list` shows only the tools the key may call.
* Default deny: a key with no tool grant can do nothing; store ids outside the
  grant are refused (never silently dropped, never rolled into a network total).
* Missing / malformed / wrong / unknown / revoked / expired tokens all return the
  identical `401` (no oracle). Personal logins, session cookies and admin JWTs are
  not accepted and never consulted.
* No HTTP route creates or manages keys. Administration is a server-side CLI.

## 3. Generate, list, revoke, rotate

Run on the server as the service user (needs only the data dir):

```bash
A='sudo -u laynes-mcp env MCP_DATA_DIR=/var/lib/laynes-mcp /opt/laynes-mcp/venv/bin/python -m mcp_server.admin'
$A create --label "Masroor Grok Bot" --tools all --stores 6,7,14 \
          --expires 2027-06-30T00:00:00+00:00 --write-secret-file /root/masroor.key
$A list                       # metadata only; never secrets or hashes
$A usage KEY_ID               # sanitised audit rows
$A rotate KEY_ID --write-secret-file /root/masroor2.key [--grace-hours 24]
$A revoke KEY_ID --reason "left the project"
```

The secret is shown once: in a terminal, or written to a new 0600 file
(refuses to overwrite; refuses to print into a pipe — the key is revoked if it
could not be delivered). Revocation is effective on the next request.
Use `--stores '*'` only when the bot is meant to see every store.

## 4. Tools

All dates `YYYY-MM-DD`, America/Chicago business dates, inclusive; max
range 62 days (`MCP_MAX_RANGE_DAYS`); max 12 stores per call. Every result has a
`status` (`ok` | `partial` | `unavailable`), per-store coverage/freshness, and
`definitions`. **null = no verified data, never zero.**

| Tool | Inputs | Sources & behaviour |
|---|---|---|
| `get_sales_guest_counts` | `start_date`, `end_date`, `group_by` day\|store, `store_ids?` | Revel `v_orders_classified` (REAL tickets: sales, real transactions, comp/empty tickets) and R365 daily (net/gross sales, guest count, check count). Revel and R365 are reported side by side, never blended. Revel has **no verified guest count**; guests come from R365 (loaded weekly, so recent days are null). Revel A12 reconciliation gate is run per store; a failed gate withholds that store's Revel figures. |
| `get_labor_metrics` | `start_date`, `end_date`, `source?` r365\|revel, `store_ids?` | One source per result. R365: labour cost/hours ÷ R365 net sales. Revel: *estimated* cost (hours × wage, not payroll) ÷ Revel REAL sales; hours include unpaid breaks. `source` omitted → R365 if it covers every store-day, else Revel, stated in `source_selection`. % is null unless numerator and denominator cover the same days. |
| `get_pnl_food_cost` | `fiscal_year`, `period` (1-13 or "P10"), `report_type` summary\|pnl\|food_cost, `store_ids?` | AYG fiscal calendar (13 × 28 days; FY2026/FY2027 only). R365 net/gross sales and labour per period; the five **verified** GL categories (electricity, gas, water, rent, repairs) from Approved lines, `NO_POSTING` ≠ $0; subtotal is labelled "not total operating expense". Detail↔summary reconciliation reported. **Food cost/COGS and operating profit are explicitly `unavailable`** (see §6). No cross-store totals. |
| `get_drive_thru_speed` | `start_date`, `end_date`, `daypart?`, `store_ids?` | HME `v_hme_store_daily_llm`: car-weighted average lane total, queue time, total-2, cars. Days that failed HME reconciliation are excluded and listed. Stores without HME mapping (Downtown Houston) → `unavailable`, never 0 s. `daypart` → `unavailable` (no hourly HME data). |
| `ask_laynes_question` | `question`, `store_ids?`, `start_date?/end_date?`, `idempotency_key?`, `job_id?` | Existing chat engine (`backend.chat.ask` → `chat_sql.answer_question`) with its default provider/model. Waits up to 90 s; if longer returns `status: "pending"` + `job_id` — call again with only `job_id` (same credential) to collect it. Same `idempotency_key` = same job; jobs are never restarted or duplicated. Returns `answer`, `evidence` (relations used, trust-gate verdicts, model; no SQL text). |

## 5. Connecting Grok

xAI's remote-MCP feature (docs.x.ai → *Remote MCP*) is configured **in the xAI
API (Responses API) or xAI SDK**, in the request's `tools` array; xAI's servers
make the connection, so the endpoint must be public HTTPS. Documented fields:
`server_url`, `server_label`, `authorization`, `headers`, `allowed_tools`.
Only Streamable HTTP and SSE are supported (this server uses Streamable HTTP).
This is *not* a consumer-app connector; whether a given Grok product (grok.com,
Grok CLI) exposes custom MCP servers must be checked per product — only the API/SDK
path is documented. Field names have varied between doc revisions
(`authorization` vs `headers`); confirm against the live page when wiring it.

```python
from openai import OpenAI
client = OpenAI(base_url="https://api.x.ai/v1", api_key="<XAI_API_KEY>")
resp = client.responses.create(
    model="<grok model>",
    input="What was LCF Katy's labor % last week?",
    tools=[{
        "type": "mcp",
        "server_url": "https://intelligence.aygchicken.com/mcp",
        "server_label": "laynes",
        "authorization": "<LAYNES_MCP_TOKEN>",   # or headers={"Authorization": "Bearer <token>"}
        "allowed_tools": ["get_sales_guest_counts", "get_labor_metrics",
                          "get_pnl_food_cost", "get_drive_thru_speed",
                          "ask_laynes_question"],
    }])
```
Check whether xAI sends `authorization` verbatim or prefixed with `Bearer`; this server
requires the `Bearer <token>` form. If `authorization` is sent raw, use `headers`.

Raw protocol check (curl; placeholders only):
```bash
curl -s https://intelligence.aygchicken.com/mcp \
  -H 'Authorization: Bearer <LAYNES_MCP_TOKEN>' \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# -> get_sales_guest_counts, get_labor_metrics, get_pnl_food_cost,
#    get_drive_thru_speed, ask_laynes_question
```

Tested locally (tests/mcp/test_e2e_tls.py): real TLS, official MCP client, bearer
header, discovery, call, concurrent clients, revoked/invalid keys, aborted
connection. Not tested: xAI's actual servers (requires deployment).

## 6. Data freshness and limitations

* Revel: nightly; gate = payments reconciliation, integrity, freshness (reported per store).
* R365 weekly report: loaded Thursdays 20:00 CT (Thu→Wed weeks) from 2026-08-13; recent days absent.
* R365 financial: daily, **period grain only**; only 5 verified categories.
* HME: daily ~05:30 CT, store×day only, 11 of 12 stores mapped. **At time of writing
  HME data ends 2026-10-13 and `hme-daily-ingest.service` is in failed state** — the
  tool reports this as `freshness_status`.
* **Food cost/COGS, operating profit: unavailable** — no verified COGS GL category,
  inventory-usage or purchase feed exists. Missing dependency: a verified COGS
  account mapping in R365 financial (and the full expense set for profit).
* Business day = Chicago calendar day (no verified rollover rule).

## 7. Security model

* Tools 1-4: fixed, parameterised SELECTs on allowlisted views as `laynes_ro` in a
  read-only transaction, 15 s statement timeout; store ids always from the
  credential's grant. No tool accepts SQL, table names, paths, URLs or commands.
* Tool 5 store isolation is enforced **in the data layer** (`store_scope.py`): for a
  restricted credential every relation in every SQL statement the model writes is
  AST-rewritten into a store-filtered subselect (joins, CTEs, subqueries, unions);
  relations with no store column and network-aggregate views are denied; null/other
  declared scopes are refused in `enforce_scope`/`meta_profile`. Verified live
  against the read-only DB. Existing validator, allowlist, trust gates, 3-query
  waste-review budget and never-empty handling are unchanged (the waste review is
  refused for store-restricted keys because it is a network comparison).
* Deterministic pre-screen rejects admin/credential/write/destructive requests.
* Own job store (SQLite); website `chat_jobs`/conversations are never touched.
* Logs/audit: key id, tool, outcome, duration, counts only — no tokens, arguments or
  question text; errors are generic and redacted.

## 8. Recommended limits

62-day ranges, ≤12 stores, 30 requests/min/key, 40 questions/day/key, 2 concurrent
questions/key and 3 process-wide, 1500-char questions, 64 KB bodies. Each question
is one existing-chat run (typically 10–60 s of provider time); budget accordingly.
Tool 1/2 on 12 stores × 62 days runs the full Revel gate per store (~6 s each, 4 in
parallel) — prefer ≤ 31 days or fewer stores for routine use.

## 9–10. Service, nginx and production installation

Files: `deploy/mcp/laynes-mcp.service`, `deploy/mcp/nginx-mcp.location.conf`,
`deploy/mcp/mcp.env.example`, `requirements-mcp.txt` / `requirements-mcp.lock`.

**Dependency strategy:** the production API imports from the pricing checkout's venv.
The MCP service does not join it: it uses its own `/opt/laynes-mcp/venv` built from
`requirements-mcp.lock` and an exported copy of the merged commit in `/opt/laynes-mcp/app`.
Nothing is installed into any existing venv.

### Exact deployment commands (run only after approval, merge and a release SHA)
```bash
SHA=<merged commit sha>
# 1. user + directories
useradd --system --home /opt/laynes-mcp --shell /usr/sbin/nologin laynes-mcp
install -d -o root -g laynes-mcp -m 0750 /etc/laynes-mcp
install -d -o laynes-mcp -g laynes-mcp -m 0755 /opt/laynes-mcp
# 2. code (export of the merged commit; NOT the pricing checkout)
git -C /root/pos-analytics-pipeline-release fetch origin   # read-only for the checkout's files
mkdir -p /opt/laynes-mcp/app && git -C /root/pos-analytics-pipeline-release archive $SHA | tar -x -C /opt/laynes-mcp/app
chown -R root:root /opt/laynes-mcp/app
# 3. dedicated venv from the pinned lock file
python3.12 -m venv /opt/laynes-mcp/venv    # or: uv venv --python 3.12 /opt/laynes-mcp/venv
/opt/laynes-mcp/venv/bin/pip install -r /opt/laynes-mcp/app/requirements-mcp.lock
# 4. env file: RO DB role + provider key only (edit values by hand; never commit)
install -m 0640 -o root -g laynes-mcp /opt/laynes-mcp/app/deploy/mcp/mcp.env.example /etc/laynes-mcp/mcp.env
$EDITOR /etc/laynes-mcp/mcp.env
# 5. service (new unit; laynes-api is NOT restarted)
install -m 0644 /opt/laynes-mcp/app/deploy/mcp/laynes-mcp.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now laynes-mcp
curl -s 127.0.0.1:8710/healthz                                   # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' -X POST 127.0.0.1:8710/mcp   # 401
# 6. Masroor's key (secret goes to a 0600 file once; hand over securely, then shred)
A='sudo -u laynes-mcp env MCP_DATA_DIR=/var/lib/laynes-mcp /opt/laynes-mcp/venv/bin/python -m mcp_server.admin'
$A create --label "Masroor Grok Bot" --tools <approved tools> --stores <approved ids> \
          --expires <ISO date> --write-secret-file /root/masroor.key
# 7. nginx: add the rate-limit zone and the exact-match location, test, reload
cp /etc/nginx/sites-enabled/intelligence /root/deploy-backups/nginx-intelligence.pre-mcp
install -m 0644 /opt/laynes-mcp/app/deploy/mcp/nginx-mcp-limit.conf /etc/nginx/conf.d/mcp-limit.conf
#   insert deploy/mcp/nginx-mcp.location.conf above `location / {` in sites-enabled/intelligence
nginx -t && systemctl reload nginx
# 8. smoke test (placeholders): see section 5; confirm /api/status and the website still respond.
```
The nginx change was dry-run validated (`nginx -t`) on a scratch copy of the live
config: `location = /mcp` matches only that exact path, so `/api/`, `/assets/`, the SPA
and the Streamlit locations are unaffected.

### Rollback
```bash
# fastest: cut Grok off only
$A revoke KEY_ID
# remove the route
cp /root/deploy-backups/nginx-intelligence.pre-mcp /etc/nginx/sites-enabled/intelligence
rm -f /etc/nginx/conf.d/mcp-limit.conf && nginx -t && systemctl reload nginx
# stop the service
systemctl disable --now laynes-mcp && rm /etc/systemd/system/laynes-mcp.service && systemctl daemon-reload
# optional: rm -rf /opt/laynes-mcp /var/lib/laynes-mcp /etc/laynes-mcp
```
No database, schema or existing service is changed, so rollback needs nothing else.

## Separate data-quality issues (out of scope here)
1. **COGS / food cost**: no verified COGS GL mapping, inventory-usage or purchase feed.
2. **HME ingestion**: `hme-daily-ingest.service` is failed; latest HME date is 2026-10-13
   and 3 days in 2026-10 failed reconciliation.

## 11. Troubleshooting

| Symptom | Cause |
|---|---|
| 401 | wrong/revoked/expired token; header must be `Authorization: Bearer lmcp_…` |
| 403 | `Origin` not in `MCP_ALLOWED_ORIGINS` or Host not in `MCP_ALLOWED_HOSTS` |
| 405 | non-POST request (by design) |
| 406/415 | needs `Accept: application/json, text/event-stream` and JSON body |
| 413 / 429 | body > 64 KB / per-key or edge rate limit |
| tool `isError` "not permitted" | key lacks that tool or store |
| status `unavailable`/`partial` | read `coverage`, `blocking_reasons`, `freshness` — usually a source gate or stale ingest |
| pending job | call `ask_laynes_question` with only `job_id` |
| 502 from nginx | `systemctl status laynes-mcp`; `journalctl -u laynes-mcp` |
