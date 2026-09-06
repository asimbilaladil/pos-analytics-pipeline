"""Safe extraction of loyalty facts from Revel's Order.gift_reward_data.

The raw payload carries plaintext PII -- customerName, firstName, lastName,
phoneNumber, birthday -- inside the same JSON value as the loyalty fields, so
Revel's `fields=` parameter cannot narrow it. The payload is therefore handled
in memory only: parsed here, reduced to the safe facts below, and dropped. No
function in this module returns, logs or stores a name, phone number, birthday
or printed card number, and every failure path is silent about payload content.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

# Keys known to carry PII. Never read, never emitted -- listed so the intent is
# explicit and so tests can assert none of them escape.
PII_KEYS = frozenset({
    "customerName", "firstName", "lastName", "phoneNumber", "birthday",
    "printedCardNumber", "email", "address", "notes", "remarks",
})

SAFE_FIELDS = (
    "has_loyalty_payload", "loyalty_registered", "has_applied_reward",
    "applied_rewards_count", "total_points_snapshot", "has_reward_card",
    "loyalty_key_hash",
)

_EMPTY = {
    "has_loyalty_payload": False,
    "loyalty_registered": None,
    "has_applied_reward": None,
    "applied_rewards_count": None,
    "total_points_snapshot": None,
    "has_reward_card": None,
    "loyalty_key_hash": None,
}


def _secret() -> bytes:
    key = os.getenv("LOYALTY_HASH_SECRET", "")
    if not key:
        raise RuntimeError(
            "LOYALTY_HASH_SECRET is not set; refusing to derive loyalty keys "
            "with an empty secret (an unkeyed hash of a stable id is "
            "reversible by enumeration)."
        )
    return key.encode("utf-8")


def hash_external_id(external_id) -> str | None:
    """HMAC-SHA256 an externalId. Returns None for absent/blank input."""
    if external_id is None:
        return None
    text = str(external_id).strip()
    if not text:
        return None
    return hmac.new(_secret(), text.encode("utf-8"), hashlib.sha256).hexdigest()


def _as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _count(value):
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return None


def extract(raw) -> dict:
    """Reduce a gift_reward_data value to safe loyalty facts.

    Accepts the raw JSON string, an already-parsed dict, or None/blank. Any
    payload that cannot be parsed is treated as absent rather than raising,
    because raising would risk the payload reaching a traceback.
    """
    if raw is None:
        return dict(_EMPTY)

    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return dict(_EMPTY)

    if isinstance(raw, str):
        if not raw.strip() or raw.strip() in ("{}", "null", "[]"):
            return dict(_EMPTY)
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            # Deliberately no payload content in the message.
            return dict(_EMPTY)

    if not isinstance(raw, dict) or not raw:
        return dict(_EMPTY)

    applied = raw.get("appliedRewards")
    applied_count = _count(applied)
    payment_reward = raw.get("appliedPaymentReward")
    has_applied = bool(applied_count) or bool(payment_reward)

    registered = raw.get("isRegistered")
    if not isinstance(registered, bool):
        registered = None

    # Presence only -- the card number itself is never read out.
    card = raw.get("printedCardNumber")
    has_card = bool(card) if card is not None else None

    return {
        "has_loyalty_payload": True,
        "loyalty_registered": registered,
        "has_applied_reward": has_applied,
        "applied_rewards_count": applied_count if applied_count is not None else 0,
        "total_points_snapshot": _as_int(raw.get("totalPoints")),
        "has_reward_card": has_card,
        "loyalty_key_hash": hash_external_id(raw.get("externalId")),
    }
