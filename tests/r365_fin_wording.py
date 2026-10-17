"""Speculation guard for R365 FINANCIAL chat answers (test helper, no I/O).

An answer may explain a gap, swing or low amount only from adjacent posting
dates it actually queried. Without such a lookup in the tool steps, these
phrases are unsupported causal claims.
"""
import re

UNSUPPORTED = [
    r"\bplausib",                      # plausible / plausibly, in any phrasing
    r"\blikely (?:posting |billing |bill )?timing",
    r"\btiming is likely",
    r"\bprobably (?:because|timing|due)",
    r"\bmost(?:ly)? (?:of (?:that|the) )?(?:network )?(?:drop|decline|difference|swing)\b"
    r"[^.]{0,40}\b(?:is|was) (?:driven by|due to|just|simply)",
    r"partial[- ]period (?:posting|bill|invoice)",
    r"\bpartial (?:bill|invoice)",
    r"true (?:low )?usage",
    r"\bcatch[- ]up (?:bill|posting)",
    r"\bestimated bill",
    r"\bbilling error",
]
# Never acceptable, with or without a posting-date lookup.
ALWAYS_BANNED = [
    r"\boverdue\b",
    r"\bmissed bill",
    r"\busage (?:anomaly|change)",
    r"\bnot (?:a )?usage\b",
    r"\b(?:main|companion|secondary) (?:bill|charge|invoice)",
]
_RX = re.compile("|".join(UNSUPPORTED), re.IGNORECASE)
_ALWAYS = re.compile("|".join(ALWAYS_BANNED), re.IGNORECASE)


def queried_adjacent_postings(steps) -> bool:
    """True when a tool step listed one store's dated lines from the transaction
    view, i.e. the adjacent posting dates were actually looked up."""
    for s in steps or []:
        sql = (s.get("sql") or "").lower()
        if ("v_r365_financial_transaction_llm" in sql and "accounting_date" in sql
                and re.search(r"establishment_id\s*(=|in)\s*\(?\s*\d", sql)
                and not s.get("error")):
            return True
    return False


def unsupported_claims(answer: str, steps=None) -> list[str]:
    """Unsupported causal phrases in an answer. Empty when the answer is clean,
    or when adjacent posting dates were queried (the claim can then rest on
    them). ALWAYS_BANNED phrases count either way."""
    found = [m.group(0) for m in _ALWAYS.finditer(answer or "")]
    if not queried_adjacent_postings(steps):
        found += [m.group(0) for m in _RX.finditer(answer or "")]
    return found


# ---------------------------------------------------------------------------
# No posting must never become $0; "no identifiable" is all-history only;
# relative day counts must be arithmetically right.

_ZERO = re.compile(r"contributed (?:\$0|nothing)|\$0(?:\.00)?(?![\d,.]\d)|\bzero (?:\w+ )?(?:expense|spend|cost)"
                   r"|\bspent nothing", re.IGNORECASE)
_NEGATION = re.compile(r"(?:\bnot|\bnever|n't|\bno|\brather than|\binstead of)\b[^.;:\n]{0,32}$",
                       re.IGNORECASE)


def zero_for_missing(answer: str) -> list[str]:
    """$0 / zero wording that is not explicitly negated ("not $0" is fine)."""
    out = []
    for m in _ZERO.finditer(answer or ""):
        before = (answer or "")[max(0, m.start() - 30):m.start()]
        if m.group(0).lower().startswith("contributed") or not _NEGATION.search(before):
            out.append(m.group(0))
    return out


_NO_IDENT = re.compile(r"no identifiable (\w+(?: & \w+)?) posting", re.IGNORECASE)


def misused_no_identifiable(answer: str, allowed: dict[str, set[str]],
                            stores: list[str]) -> list[str]:
    """Sentences using "no identifiable <category> posting" that do not name
    only stores with no line of that category in the whole loaded history.
    allowed: {"electricity": {"Downtown Houston"}}; stores: all short names."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\n", answer or ""):
        for m in _NO_IDENT.finditer(sent):
            ok = allowed.get(m.group(1).lower(), set())
            named = {st for st in stores if st.lower() in sent.lower()}
            if not named or not named <= ok:
                out.append(sent.strip())
    return out


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10, "a": 1, "a single": 1}
_DATE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2})\b")
_REL = re.compile(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|a) days? "
                  r"(after|before|into) (?:the (?:end|start) of )?(P\d+)", re.IGNORECASE)


def wrong_day_counts(answer: str, periods: dict[str, tuple], year: int = 2026) -> list[str]:
    """Relative day counts ("one day after P10 ended", "two days into P11")
    checked against the posting date named just before them on the same line.
    periods: {"P10": (date(2026, 9, 10), date(2026, 10, 7)), ...}."""
    from datetime import date
    out = []
    for line in (answer or "").split("\n"):
        for m in _REL.finditer(line):
            if m.group(3).upper() not in periods:
                continue
            # the posting date the count refers to: the last date in the same
            # clause; a count with no date of its own cannot be checked
            start, end = periods[m.group(3).upper()]
            n = int(m.group(1)) if m.group(1).isdigit() else _NUM[m.group(1).lower()]
            kind = m.group(2).lower()

            def count(d):
                return {"after": (d - end).days, "before": (start - d).days,
                        "into": (d - start).days + 1}[kind]

            clause = re.split(r"[;\u2014]| and | but | - ", line[:m.start()])[-1]
            own = [_as_date(x, year) for x in _DATE.finditer(clause)]
            if own:            # the date in the count's own clause decides
                if count(own[-1]) != n:
                    out.append(f"{m.group(0)} (from {own[-1]:%b %-d}: should be {count(own[-1])})")
                continue
            # no date of its own ("the next bill landed two days into P11"):
            # it must fit a posting date stated in the answer on that side of
            # the boundary
            side = [d for d in (_as_date(x, year) for x in _DATE.finditer(answer or ""))
                    if (kind == "into" and start <= d <= end) or (kind == "after" and d > end)
                    or (kind == "before" and d < start)]
            if not any(count(d) == n for d in side):
                out.append(f"{m.group(0)} (fits no stated posting date)")
    return out


def _as_date(m, year):
    from datetime import date
    return date(year, _MONTHS[m.group(1).lower()[:3]], int(m.group(2)))


_REASONS = re.compile(r"bundled|landlord|\bCAM\b|common[- ]area|another account|"
                      r"different account|\brent\b", re.IGNORECASE)


def reasons_for_unidentified(answer: str) -> list[str]:
    """For an answer about a store with NO identifiable posting: any candidate
    reason mentioned at all, even ruled out ("not bundled into rent")."""
    return [m.group(0) for m in _REASONS.finditer(answer or "")]
