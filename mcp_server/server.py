"""ASGI application: MCP Streamable HTTP + bearer authentication.

Built on the official SDK (mcp.server.fastmcp). This module adds only:
  * an outer ASGI gate (path allowlist, Origin check, body cap, bearer auth,
    per-key rate limit, redacted logging), and
  * five thin tool wrappers over analytics.py / asker.py.

Run (loopback only; nginx is the sole way in):
    uvicorn mcp_server.server:app --host 127.0.0.1 --port 8710 --no-access-log
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Annotated, Any, Literal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

# The service needs ONLY the read-only DB role and provider keys. It loads the
# env file named by MCP_ENV_FILE (a dedicated, 0600 file); it never reads the
# application's .env implicitly.
if os.environ.get("MCP_ENV_FILE"):
    load_dotenv(os.environ["MCP_ENV_FILE"])

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from . import analytics, asker, keystore, settings
from .security import (ToolDenied, ToolInputError, parse_range, redact, require_tool,
                       resolve_stores)

log = logging.getLogger("laynes.mcp")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)
log.propagate = False

INSTRUCTIONS = (
    "Read-only AYG Foods / Laynes Intelligence analytics. Five tools: Revel sales and "
    "counts, labor, R365 P&L (partial: no verified food cost), HME drive-thru speed, and "
    "a natural-language question tool. All figures are per permitted store. A null or "
    "status 'unavailable'/'partial' means data is missing or untrusted -- never zero. "
    "Always read each result's `status`, `limitations` and `definitions`.")

StoreIds = Annotated[list[int] | None, Field(
    description="Establishment ids to include. Omit for every store this credential may read.")]
IsoDate = Annotated[str, Field(description="YYYY-MM-DD (America/Chicago business date)",
                               pattern=r"^\d{4}-\d{2}-\d{2}$")]


def _principal(ctx: Context):
    req = ctx.request_context.request
    p = None
    if req is not None:
        p = req.scope.get("mcp_principal")
    if p is None:
        raise ToolError("authentication required")
    return p


async def _run(ctx: Context, tool: str, fn, *, store_count=None, range_days=None):
    p = _principal(ctx)
    t0 = time.monotonic()
    outcome = "ok"
    try:
        require_tool(p, tool)
        result = await asyncio.wait_for(asyncio.to_thread(fn, p),
                                        timeout=settings.TOOL_TIMEOUT_SECONDS)
        outcome = result.get("status", "ok") if isinstance(result, dict) else "ok"
        return result
    except ToolDenied as e:
        outcome = "denied"
        raise ToolError(str(e)) from None
    except ToolInputError as e:
        outcome = "invalid"
        raise ToolError(str(e)) from None
    except asyncio.TimeoutError:
        outcome = "timeout"
        raise ToolError("the request exceeded its time limit; narrow the stores or "
                        "date range") from None
    except ToolError:
        outcome = "error"
        raise
    except Exception as e:                                       # noqa: BLE001
        outcome = "error"
        log.error("tool_error tool=%s key=%s type=%s", tool, p.key_id, type(e).__name__)
        raise ToolError("internal error; the request was not completed") from None
    finally:
        keystore.audit(p.key_id, tool, outcome,
                       duration_ms=int((time.monotonic() - t0) * 1000),
                       store_count=store_count, range_days=range_days)


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        "laynes-intelligence", instructions=INSTRUCTIONS,
        stateless_http=True, json_response=True, streamable_http_path="/mcp",
        max_request_body_size=settings.MAX_BODY_BYTES,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts(),
            allowed_origins=settings.allowed_origins()))

    @mcp.tool(description=(
        "Revel POS sales and R365 net/gross sales with guest and transaction counts, per "
        "store, grouped by day or store. Revel and R365 figures are separate sources with "
        "exact definitions in the result; missing data is null, never zero."))
    async def get_sales_guest_counts(
            ctx: Context, start_date: IsoDate, end_date: IsoDate,
            group_by: Literal["day", "store"] = "day",
            store_ids: StoreIds = None) -> dict[str, Any]:
        def work(p):
            ids = resolve_stores(p, store_ids)
            s, e = parse_range(start_date, end_date)
            return analytics.sales_guest_counts(ids, s, e, group_by)
        return await _run(ctx, "get_sales_guest_counts", work)

    @mcp.tool(description=(
        "Labor cost, labor hours and labor % of sales per store. One source per result "
        "(R365 or Revel timesheet estimate) with the sales denominator named; sources are "
        "never mixed."))
    async def get_labor_metrics(
            ctx: Context, start_date: IsoDate, end_date: IsoDate,
            source: Annotated[Literal["r365", "revel"] | None, Field(
                description="Omit to let the server pick one source and explain why.")] = None,
            store_ids: StoreIds = None) -> dict[str, Any]:
        def work(p):
            ids = resolve_stores(p, store_ids)
            s, e = parse_range(start_date, end_date)
            return analytics.labor_metrics(ids, s, e, source)
        return await _run(ctx, "get_labor_metrics", work)

    @mcp.tool(description=(
        "R365 figures for one AYG fiscal period (13 periods x 28 days): net sales, labor, "
        "and VERIFIED expense categories (electricity, gas, water, rent, repairs). Food "
        "cost/COGS and operating profit are returned as explicitly unavailable -- no "
        "verified source exists."))
    async def get_pnl_food_cost(
            ctx: Context,
            fiscal_year: Annotated[int, Field(description="e.g. 2026 (FY2026 and FY2027 exist)")],
            period: Annotated[int | str, Field(description="Fiscal period 1-13, or 'P10'")],
            report_type: Literal["summary", "pnl", "food_cost"] = "summary",
            store_ids: StoreIds = None) -> dict[str, Any]:
        def work(p):
            ids = resolve_stores(p, store_ids)
            return analytics.pnl_food_cost(ids, fiscal_year, period, report_type)
        return await _run(ctx, "get_pnl_food_cost", work)

    @mcp.tool(description=(
        "HME drive-thru speed of service (average lane time, queue time) and car counts per "
        "store. Daily grain only: a daypart request returns 'unavailable'. Stores without "
        "HME mapping are reported as not covered, never as zero seconds."))
    async def get_drive_thru_speed(
            ctx: Context, start_date: IsoDate, end_date: IsoDate,
            daypart: Annotated[str | None, Field(
                description="Not supported (no hourly HME data); returns unavailable.")] = None,
            store_ids: StoreIds = None) -> dict[str, Any]:
        def work(p):
            ids = resolve_stores(p, store_ids)
            s, e = parse_range(start_date, end_date)
            return analytics.drive_thru_speed(ids, s, e, daypart)
        return await _run(ctx, "get_drive_thru_speed", work)

    @mcp.tool(description=(
        "Ask the Laynes Intelligence assistant a read-only business question in plain "
        "English. Runs through the same validated, store-restricted data layer as the "
        "website assistant. If it takes longer than the wait limit the result is "
        "status 'pending' with a job_id; call again with only job_id to retrieve it "
        "(the job is not restarted). Pass idempotency_key to make retries safe."))
    async def ask_laynes_question(
            ctx: Context,
            question: Annotated[str | None, Field(
                description="Plain-English question. Omit when retrieving by job_id.")] = None,
            store_ids: StoreIds = None,
            start_date: Annotated[str | None, Field(description="Optional YYYY-MM-DD focus")] = None,
            end_date: Annotated[str | None, Field(description="Optional YYYY-MM-DD focus")] = None,
            idempotency_key: Annotated[str | None, Field(
                description="Client-chosen unique key (8-100 chars) for safe retries")] = None,
            job_id: Annotated[str | None, Field(
                description="Retrieve a previously returned pending job")] = None
            ) -> dict[str, Any]:
        def work(p):
            return asker.ask(p, question, store_ids, start_date, end_date,
                             idempotency_key, job_id)
        return await _run(ctx, "ask_laynes_question", work)

    # Discovery shows only the tools this credential may call.
    @mcp._mcp_server.list_tools()
    async def _list_permitted():
        p = _principal(mcp.get_context())
        return [t for t in await mcp.list_tools() if t.name in p.tools]

    return mcp


# ── outer ASGI gate ─────────────────────────────────────────────────────────
class McpGate:
    """Path allowlist, Origin check, body cap and bearer authentication.

    Runs before the SDK sees a request, so an unauthenticated caller reaches no
    MCP code path (not even initialize / tools/list).
    """

    def __init__(self, inner, path: str = "/mcp"):
        self.inner, self.path = inner, path

    @staticmethod
    async def _send(send, status: int, body: dict, extra=None):
        data = json.dumps(body).encode()
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(data)).encode()),
                   (b"cache-control", b"no-store")] + (extra or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": data})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":                  # lifespan passes straight through
            return await self.inner(scope, receive, send)
        path = scope["path"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope["headers"]}

        if path == "/healthz":
            return await self._send(send, 200, {"status": "ok"})
        if path.rstrip("/") != self.path:
            return await self._send(send, 404, {"error": "not_found"})

        origin = headers.get("origin")
        if origin is not None and origin.rstrip("/") not in settings.allowed_origins():
            keystore.audit(None, None, "bad_origin")
            return await self._send(send, 403, {"error": "forbidden_origin"})

        try:
            if int(headers.get("content-length", "0")) > settings.MAX_BODY_BYTES:
                return await self._send(send, 413, {"error": "request_too_large"})
        except ValueError:
            return await self._send(send, 400, {"error": "bad_content_length"})

        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
        principal = await asyncio.to_thread(keystore.verify_token, token)
        if principal is None:
            keystore.audit(None, None, "auth_failed")
            return await self._send(
                send, 401, {"error": "unauthorized"},
                [(b"www-authenticate", b'Bearer realm="laynes-mcp"')])
        if await asyncio.to_thread(keystore.rate_limited, principal.key_id,
                                   principal.rate_per_min):
            keystore.audit(principal.key_id, None, "rate_limited")
            return await self._send(send, 429, {"error": "rate_limited"},
                                    [(b"retry-after", b"30")])

        # Stateless JSON transport: only POST is served. GET (server-initiated SSE
        # stream), DELETE (session teardown) and everything else are answered 405,
        # which the Streamable HTTP spec permits -- and it means no idle
        # long-lived connection is ever held open behind the proxy.
        if scope["method"] != "POST":
            return await self._send(send, 405, {"error": "method_not_allowed"},
                                    [(b"allow", b"POST")])

        scope = dict(scope)
        scope["mcp_principal"] = principal
        seen = 0

        async def capped_receive():                  # chunked bodies too
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > settings.MAX_BODY_BYTES:
                    raise RuntimeError("request body too large")
            return msg

        try:
            await self.inner(scope, capped_receive, send)
        except RuntimeError as e:
            if "too large" in str(e):
                log.warning("body_too_large key=%s", principal.key_id)
                return
            raise


def build_app():
    mcp = build_mcp()
    asker.sweep_interrupted()
    return McpGate(mcp.streamable_http_app())


_app = None


def __getattr__(name):              # lazy: importing the module must not open the DB
    global _app
    if name == "app":
        if _app is None:
            _app = build_app()
        return _app
    raise AttributeError(name)
