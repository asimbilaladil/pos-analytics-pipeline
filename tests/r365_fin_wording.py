"""Speculation guard for R365 FINANCIAL chat answers (test helper, no I/O).

An answer may explain a gap, swing or low amount only from adjacent posting
dates it actually queried. Without such a lookup in the tool steps, these
phrases are unsupported causal claims.
"""
import re

UNSUPPORTED = [
    r"very plausible",
    r"\bplausible explanation",
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
