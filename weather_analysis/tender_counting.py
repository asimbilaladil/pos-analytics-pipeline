"""Maps a product name to how many chicken tenders (fingers) it contains.

The rules are ported verbatim from ``dashboard.py``'s ``get_finger_count`` so
the backtest scores against exactly the tender definition the kitchen sees.

NOTE: this is intentionally a standalone, dependency-free module. The dashboard
still carries its own copy; the intended follow-up is to have ``dashboard.py``
import ``TenderCounter`` from here so the rule lives in one place.
"""

from __future__ import annotations

import re

_STRIP_DECORATION = re.compile(r"[\*\$]+")
_COLLAPSE_SPACES = re.compile(r"\s+")
_NUMBERED_PARTY_PACK = re.compile(r"^(\d+)\s+finger\s+party\s+pack")
_PARTY_PACK_NUMBERED = re.compile(r"^party\s+pack\s+(\d+)")
_MIXED_COMBO = re.compile(r"(\d+)\s+(?:regular|spicy)")

_FAMILY_FINGERS = 20
_KIDS_FINGERS = 2
_WRAP_FINGERS = 2
_SANDWICH_FINGERS = 3
_A_LA_CARTE_FINGERS = 1


class TenderCounter:
    """Resolves the finger count for a single product name."""

    def fingers_for(self, product_name: str) -> int:
        name = self._normalise(product_name)
        for rule in (
            self._numbered_pack,
            self._family,
            self._kids,
            self._grilled_cheese,
            self._wrap,
            self._sandwich,
            self._standard_meal,
            self._mixed_combo,
            self._a_la_carte,
        ):
            fingers = rule(name)
            if fingers is not None:
                return fingers
        return 0

    @staticmethod
    def _normalise(product_name: str) -> str:
        stripped = _STRIP_DECORATION.sub("", product_name).strip().lower()
        return _COLLAPSE_SPACES.sub(" ", stripped).strip()

    @staticmethod
    def _numbered_pack(name: str) -> int | None:
        match = _NUMBERED_PARTY_PACK.match(name) or _PARTY_PACK_NUMBERED.match(name)
        return int(match.group(1)) if match else None

    @staticmethod
    def _family(name: str) -> int | None:
        return _FAMILY_FINGERS if "family" in name else None

    @staticmethod
    def _kids(name: str) -> int | None:
        if "kid" in name and ("finger" in name or "meal" in name):
            return _KIDS_FINGERS
        return None

    @staticmethod
    def _grilled_cheese(name: str) -> int | None:
        return 0 if "grilled cheese" in name else None

    @staticmethod
    def _wrap(name: str) -> int | None:
        return _WRAP_FINGERS if "wrap" in name else None

    @staticmethod
    def _sandwich(name: str) -> int | None:
        if "club" in name or "sandwich" in name:
            return _SANDWICH_FINGERS
        return None

    @staticmethod
    def _standard_meal(name: str) -> int | None:
        for count in (3, 4, 5):
            if f"{count} finger" in name:
                return count
        return None

    @staticmethod
    def _mixed_combo(name: str) -> int | None:
        numbers = _MIXED_COMBO.findall(name)
        return sum(int(x) for x in numbers) if numbers else None

    @staticmethod
    def _a_la_carte(name: str) -> int | None:
        return _A_LA_CARTE_FINGERS if "chicken finger" in name else None
