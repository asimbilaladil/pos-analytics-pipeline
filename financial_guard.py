"""Deterministic checks for generated financial reports (2026-11 audit).

Conversation 688 (P11 FY2026) printed, for Airtex,
    sales 236,892.94 | forecast 163,258.04 | difference +11,876.89
-- three figures that were each correct and do not add up: the sales were all
28 days, the forecast and R365's variance only the 21 days that had a
forecast. The model then called it a full-period comparison. Every number came
straight from SQL; the error was in which numbers were put side by side.

This module never computes a figure for the answer itself. It
  * parses the markdown tables of a draft answer and checks, row by row, that
    actual - forecast = difference (and difference / forecast = difference %)
    within display rounding;
  * reads the forecast COVERAGE of the declared scope from the allowlisted
    v_r365_store_daily_llm view (read-only role), and checks that every store
    whose forecast does not cover the whole scope has its coverage stated, and
    that no sentence claims a full-period forecast comparison for it;
  * renders the coverage facts and any remaining problems as a deterministic
    note, so a report that still fails is never sent without the correction.

Pure functions except forecast_coverage(), which takes a cursor.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

GUARD_VERSION = "financial_guard/1.0.0"

# ── text helpers ────────────────────────────────────────────────────────────


def _fold(s: str) -> str:
    """lower-case, accents removed, markdown emphasis removed."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("*", "").replace("_", " ").lower().strip()


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass
class Cell:
    value: Decimal | None
    decimals: int = 0
    is_pct: bool = False


def parse_cell(text: str) -> Cell:
    """'+$11,876.89' -> 11876.89 ; '−$12,720.81' -> -12720.81 ; '(1,234)' ->
    -1234 ; '+7.27%' -> 7.27 (pct). Anything else -> value None."""
    t = (text or "").replace("**", "").replace(" ", " ").strip()
    if not t or t.lower() in ("n/a", "na", "-", "—", "–"):
        return Cell(None)
    m = _NUM.search(t)
    if not m:
        return Cell(None)
    rest = (t[:m.start()] + t[m.end():]).replace("$", "").replace("%", "").strip(" ()+-−–")
    if re.search(r"[a-zA-Z]", rest):          # '3 of 4 weeks', '1.2k', dates ...
        return Cell(None)
    if re.search(r"\d", t[m.end():]):          # a second number: a range or a date
        return Cell(None)
    raw = m.group(0).replace(",", "")
    try:
        v = Decimal(raw)
    except InvalidOperation:
        return Cell(None)
    before = t[:m.start()]
    if re.search(r"[-−–]", before) or (before.strip().startswith("(") and ")" in t[m.end():]):
        v = -v
    dec = len(raw.split(".")[1]) if "." in raw else 0
    return Cell(v, dec, "%" in t)


# Header roles. Order matters: "sales on forecast days" mentions forecast but is
# an ACTUAL column; "sales vs forecast" is a DIFFERENCE column.
_COVERED_ACTUAL = ("forecast day", "forecast-covered", "forecast covered", "covered day",
                   "dias con forecast", "dias con pronostico", "con forecast",
                   "dias cubiertos", "cubiertos", "sales on forecast", "ventas en dias")
_DIFF = ("diferencia", "diff", "variance", "varianza", "variacion", "delta",
         "vs forecast", "vs. forecast", "vs pronostico", "over/under", "+/-", "desviacion")
_FORECAST = ("forecast", "pronostico", "presupuesto", "budget", "goal", "objetivo")
_ACTUAL = ("venta", "sales", "actual", "real")
_FULL_PERIOD_HDR = ("full-period", "full period", "periodo completo", "todo el periodo",
                    "28 dias", "all days", "todos los dias", "total del periodo")


def column_roles(headers: list[str]) -> dict:
    roles = {"actual": [], "full_actual": [], "forecast": None, "diff": None, "diff_pct": None}
    for i, h in enumerate(headers):
        f = _fold(h)
        if any(k in f for k in _COVERED_ACTUAL):
            roles["actual"].insert(0, i)
        elif any(k in f for k in _DIFF) or (f.startswith("vs") and "forecast" in f):
            if "%" in f or "pct" in f or "porcentaje" in f:
                roles["diff_pct"] = i
            elif roles["diff"] is None:
                roles["diff"] = i
            elif roles["diff_pct"] is None:      # "Variance | Variance" ($ then %)
                roles["diff_pct"] = i
        elif any(k in f for k in _FORECAST):
            if roles["forecast"] is None:
                roles["forecast"] = i
        elif any(k in f for k in _ACTUAL):
            if any(k in f for k in _FULL_PERIOD_HDR):
                roles["full_actual"].append(i)
            else:
                roles["actual"].append(i)
    return roles


def markdown_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """[(headers, rows)] for every pipe table in the text."""
    out, block = [], []
    for line in (text or "").splitlines() + [""]:
        if line.strip().startswith("|"):
            block.append(line.strip())
            continue
        if len(block) >= 3 and re.match(r"^\|?\s*:?-{2,}", block[1].strip("| ")):
            split = [[c.strip() for c in row.strip("|").split("|")] for row in block]
            out.append((split[0], split[2:]))
        block = []
    return out


def _tol_amount(*cells: Cell) -> Decimal:
    # Whole-dollar display: each figure may be off by 0.50.
    if any(c.decimals == 0 for c in cells):
        return Decimal("1.51")
    return Decimal("0.011")


@dataclass
class Issue:
    kind: str                 # ARITHMETIC | PERCENT | COVERAGE_UNDISCLOSED | FULL_PERIOD_CLAIM
    row: str
    detail: str
    data: dict = field(default_factory=dict)


def check_tables(text: str) -> list[Issue]:
    issues: list[Issue] = []
    for headers, rows in markdown_tables(text):
        r = column_roles(headers)
        if r["forecast"] is None or (r["diff"] is None and r["diff_pct"] is None):
            continue
        for row in rows:
            if len(row) != len(headers):
                continue
            label = re.sub(r"\*", "", row[0]).strip() or "(row)"
            fc = parse_cell(row[r["forecast"]])
            if fc.value is None or fc.is_pct:
                continue
            diff = parse_cell(row[r["diff"]]) if r["diff"] is not None else Cell(None)
            if diff.is_pct:
                diff = Cell(None)
            if diff.value is not None and r["actual"]:
                acts = [parse_cell(row[i]) for i in r["actual"]]
                acts = [a for a in acts if a.value is not None and not a.is_pct]
                if acts and not any(abs(a.value - fc.value - diff.value) <= _tol_amount(a, fc, diff)
                                    for a in acts):
                    a = acts[0]
                    issues.append(Issue(
                        "ARITHMETIC", label,
                        f"{a.value:,.2f} - {fc.value:,.2f} = {a.value - fc.value:,.2f}, "
                        f"but the table shows {diff.value:+,.2f}",
                        {"actual": str(a.value), "forecast": str(fc.value),
                         "difference": str(diff.value)}))
            if r["diff_pct"] is not None and fc.value:
                pct = parse_cell(row[r["diff_pct"]])
                base = diff.value
                if base is None and r["actual"]:
                    a0 = parse_cell(row[r["actual"][0]])
                    base = (a0.value - fc.value) if a0.value is not None else None
                if pct.value is not None and base is not None:
                    want = base / fc.value * 100
                    tol = Decimal(6) / (Decimal(10) ** (pct.decimals + 1)) + Decimal("0.0001")
                    if fc.decimals == 0 or (diff.value is not None and diff.decimals == 0):
                        # whole-dollar amounts: each may be off by 0.50
                        tol += 100 * (1 + abs(base / fc.value)) / abs(fc.value)
                    if abs(want - pct.value) > max(tol, Decimal("0.006")):
                        issues.append(Issue(
                            "PERCENT", label,
                            f"{base:+,.2f} / {fc.value:,.2f} = {want:+.2f}%, "
                            f"but the table shows {pct.value:+}%"))
    return issues


# ── forecast coverage (read-only, allowlisted view) ─────────────────────────

FORECAST_COVERAGE_SQL = """
SELECT establishment_id, store_name,
       count(*)                                   AS days_loaded,
       count(forecasted_sales)                    AS forecast_days,
       sum(net_sales)                             AS net_sales,
       sum(forecast_sales_base)                   AS sales_on_forecast_days,
       sum(forecasted_sales)                      AS forecasted_sales,
       sum(forecast_sales_base) - sum(forecasted_sales) AS sales_vs_forecast_amount,
       array_agg(business_date ORDER BY business_date)
           FILTER (WHERE forecasted_sales IS NULL) AS days_without_forecast
  FROM v_r365_store_daily_llm
 WHERE business_date >= %(start)s AND business_date < %(end)s
   AND (%(est)s::int IS NULL OR establishment_id = %(est)s::int)
 GROUP BY establishment_id, store_name
 ORDER BY store_name
"""


def ranges(days: list[date]) -> list[tuple[date, date]]:
    out: list[list[date]] = []
    for d in sorted(days or []):
        if out and d == out[-1][1] + timedelta(days=1):
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(a, b) for a, b in out]


def forecast_coverage(cur, establishment_id, start: str, end_exclusive: str) -> list[dict]:
    """Per store in scope: forecast days vs calendar days, the sales that the
    forecast is compared with, full-scope sales, and the uncovered dates."""
    cur.execute(FORECAST_COVERAGE_SQL, {"est": establishment_id, "start": start,
                                        "end": end_exclusive})
    n_days = (date.fromisoformat(end_exclusive) - date.fromisoformat(start)).days
    out = []
    for r in cur.fetchall():
        r = dict(r)
        fd = int(r["forecast_days"] or 0)
        status = "NONE" if fd == 0 else ("FULL" if fd >= n_days else "PARTIAL")
        out.append({**r, "forecast_days": fd, "days_in_scope": n_days,
                    "coverage_status": status,
                    "days_without_forecast": ranges(r.get("days_without_forecast") or [])})
    return out


# ── coverage disclosure / full-period claims ────────────────────────────────

_CLAIM_FULL = re.compile(
    r"(full[- ]period|entire period|whole period|complete period|"
    r"per[ií]odo completo|todo el per[ií]odo|total completo del per[ií]odo|"
    r"per[ií]odo totalmente cubierto|totalmente cubierto)", re.I)
_CLAIM_CTX = re.compile(r"(compar|forecast|pron[oó]stic|goal|meta|cumpl|met )", re.I)
_PARTIAL_WORDS = re.compile(r"(only|solo|s[oó]lamente|partial|parcial|not the full|no es el per[ií]odo|"
                            r"sin forecast|no forecast|without (a )?forecast)", re.I)


def _disclosed(text: str, fd: int, n: int) -> bool:
    t = text or ""
    pats = (rf"\b{fd}\s*/\s*{n}\b", rf"\b{fd}\s+of\s+(the\s+)?{n}\b", rf"\b{fd}\s+de\s+(los\s+)?{n}\b")
    return any(re.search(p, t, re.I) for p in pats)


def check_coverage(text: str, coverage: list[dict]) -> list[Issue]:
    issues: list[Issue] = []
    partial = [c for c in coverage if c["coverage_status"] == "PARTIAL"]
    if not partial:
        return issues
    for c in partial:
        if not _disclosed(text, c["forecast_days"], c["days_in_scope"]):
            issues.append(Issue(
                "COVERAGE_UNDISCLOSED", c["store_name"],
                f"forecast covers {c['forecast_days']} of {c['days_in_scope']} days; "
                "the answer does not say so", {"forecast_days": c["forecast_days"],
                                                "days": c["days_in_scope"]}))
    prose = "\n".join(l for l in (text or "").splitlines() if not l.strip().startswith("|"))
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", prose):
        # "..., no parcial" / "not partial" asserts completeness; it is not a
        # partial-coverage disclaimer.
        hedge = re.sub(r"\b(no|not|ni)\s+(es\s+|is\s+)?(parcial|partial)\b", "", sentence, flags=re.I)
        if (_CLAIM_FULL.search(sentence) and _CLAIM_CTX.search(sentence)
                and not _PARTIAL_WORDS.search(hedge)):
            issues.append(Issue(
                "FULL_PERIOD_CLAIM", ", ".join(c["store_name"] for c in partial),
                "a sentence presents the forecast comparison as full-period: "
                + sentence.strip()[:160]))
            break
    return issues


# ── rendering ───────────────────────────────────────────────────────────────

_ES = re.compile(r"\b(las|los|del|ventas|tienda|tiendas|período|periodo|semana|cumplieron)\b", re.I)


def is_spanish(text: str) -> bool:
    return len(_ES.findall(text or "")) >= 3


def _usd(v) -> str:
    if v is None:
        return "n/a"
    v = Decimal(str(v))
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def _signed(v) -> str:
    v = Decimal(str(v))
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def _span(rs, es: bool) -> str:
    j = " y " if es else " and "
    return j.join(a.isoformat() if a == b else f"{a.isoformat()}–{b.isoformat()}" for a, b in rs)


def coverage_lines(coverage: list[dict], es: bool) -> list[str]:
    lines = []
    for c in coverage:
        if c["coverage_status"] == "FULL":
            continue
        if c["coverage_status"] == "NONE":
            lines.append(f"- {c['store_name']}: " + (
                "sin forecast en R365 en este rango (ni cumplió ni falló); ventas "
                if es else "no R365 forecast in this range (neither met nor missed); sales ")
                + f"{_usd(c['net_sales'])}.")
            continue
        miss = _span(c["days_without_forecast"], es)
        if es:
            lines.append(
                f"- {c['store_name']}: forecast en {c['forecast_days']} de "
                f"{c['days_in_scope']} días (sin forecast: {miss}). Ventas en días con "
                f"forecast {_usd(c['sales_on_forecast_days'])} vs forecast "
                f"{_usd(c['forecasted_sales'])} = {_signed(c['sales_vs_forecast_amount'])}. "
                f"Ventas de todo el rango (aparte, no comparables con el forecast): "
                f"{_usd(c['net_sales'])}.")
        else:
            lines.append(
                f"- {c['store_name']}: forecast on {c['forecast_days']} of "
                f"{c['days_in_scope']} days (no forecast: {miss}). Sales on forecast days "
                f"{_usd(c['sales_on_forecast_days'])} vs forecast "
                f"{_usd(c['forecasted_sales'])} = {_signed(c['sales_vs_forecast_amount'])}. "
                f"Full-range sales (separate; not comparable with the forecast): "
                f"{_usd(c['net_sales'])}.")
    return lines


def correction_prompt(issues: list[Issue], coverage: list[dict]) -> str:
    """One-time instruction back to the model when its draft fails."""
    parts = ["FINANCIAL CONSISTENCY CHECK (server-side, deterministic): the draft "
             "answer cannot be sent as written."]
    for i in issues:
        parts.append(f"- [{i.kind}] {i.row}: {i.detail}")
    cov = coverage_lines(coverage, es=False)
    if cov:
        parts.append("FORECAST COVERAGE FACTS (server-computed from v_r365_store_daily_llm "
                     "for the declared scope; quote these exactly):")
        parts.extend(cov)
    parts.append(
        "Rewrite the answer. Beside forecast and variance, the sales column must be "
        "SALES ON FORECAST DAYS (forecast_sales_base / sales_on_forecast_days), never "
        "full-period net_sales. Show full-period sales only in a separate, clearly "
        "labelled column. For every store whose forecast does not cover every day, "
        "state the coverage as 'N of M days' and never call it a full-period "
        "comparison. Every table row must satisfy sales - forecast = difference. Quote "
        "figures from tool results only; do not compute new figures by hand. Answer in "
        "the user's language.")
    return "\n".join(parts)


def _issue_text(i: Issue, es: bool) -> str:
    d = i.data
    if es and i.kind == "ARITHMETIC" and d:
        a, f, x = (Decimal(d[k]) for k in ("actual", "forecast", "difference"))
        return (f"{a:,.2f} - {f:,.2f} = {a - f:,.2f}, pero la tabla muestra {x:+,.2f} "
                "(ventas y diferencia de bases distintas)")
    return i.detail


def final_note(issues: list[Issue], coverage: list[dict], text: str) -> str:
    """Deterministic note appended to an answer that still needs it."""
    es = is_spanish(text)
    out = []
    cov_needed = [c for c in coverage if c["coverage_status"] == "PARTIAL"
                  and not _disclosed(text, c["forecast_days"], c["days_in_scope"])]
    if cov_needed or any(i.kind == "FULL_PERIOD_CLAIM" for i in issues):
        out.append("**Cobertura del forecast (verificación automática):**" if es
                   else "**Forecast coverage (automatic check):**")
        out.extend(coverage_lines([c for c in coverage if c["coverage_status"] != "FULL"], es))
        out.append("La comparación con el forecast cubre solo los días con forecast; no es "
                   "una comparación del período completo." if es else
                   "The forecast comparison covers forecast days only; it is not a "
                   "full-period comparison.")
    arith = [i for i in issues if i.kind in ("ARITHMETIC", "PERCENT")]
    if arith:
        out.append("**⚠️ Verificación aritmética automática — estas filas no cuadran:**" if es
                   else "**⚠️ Automatic arithmetic check — these rows do not add up:**")
        out.extend(f"- {i.row}: {_issue_text(i, es)}" for i in arith)
        out.append("Use las cifras de cobertura de arriba." if es and cov_needed else
                   ("Use the coverage figures above." if cov_needed else
                    ("No use esas filas sin revisarlas." if es else
                     "Do not rely on those rows without review.")))
    return "\n".join(out)


def review(text: str, coverage: list[dict] | None) -> list[Issue]:
    return check_tables(text) + check_coverage(text, coverage or [])


# ── which SQL needs a coverage check ────────────────────────────────────────

_R365_STORE_VIEW = re.compile(r"\bv_r365_store_(daily|weekly|period)_llm\b", re.I)
_FORECAST_COL = re.compile(r"\b(forecasted_sales|forecast_sales_base|sales_vs_forecast_\w+|"
                           r"sales_on_forecast_days|forecast_days)\b", re.I)


def needs_forecast_check(sql: str) -> bool:
    return bool(sql and _R365_STORE_VIEW.search(sql) and _FORECAST_COL.search(sql))
