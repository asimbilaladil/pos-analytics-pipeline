"""Settings and the city -> coordinate catalogue.

Database credentials come from the environment (the same ``.env`` the rest of
the project uses). Coordinates are kept here rather than in the database
because the ``establishments`` table has no lat/long columns; the values are
approximate city centres, which is more than accurate enough to test whether
weather moves demand.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta

from .models import GeoLocation

# Open-Meteo's archive (ERA5) lags real time by a few days. Ending the default
# window short of today keeps us inside the range that always has data.
_ARCHIVE_LAG_DAYS = 5

# History begins when the backfill first populated the orders table.
_DEFAULT_START = date(2026, 2, 10)


@dataclass(frozen=True)
class DatabaseSettings:
    """Everything needed to open a Postgres connection."""

    host: str
    port: int
    name: str
    user: str
    password: str


@dataclass(frozen=True)
class AnalysisWindow:
    """The inclusive date range an analysis or backfill run covers."""

    start: date
    end: date


def load_database_settings() -> DatabaseSettings:
    """Read database settings from the environment.

    Assumes ``.env`` has already been loaded (the CLI does this at startup).
    """
    return DatabaseSettings(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        name=os.getenv("DB_NAME", "laynes"),
        user=os.getenv("DB_USER", "laynes_user"),
        password=os.getenv("DB_PASS", ""),
    )


def default_window(today: date | None = None) -> AnalysisWindow:
    """The window used when the caller does not specify one."""
    today = today or date.today()
    return AnalysisWindow(start=_DEFAULT_START, end=today - timedelta(days=_ARCHIVE_LAG_DAYS))


# Approximate city-centre coordinates for every city Laynes operates in.
_CITY_COORDINATES: dict[str, GeoLocation] = {
    "Katy": GeoLocation(29.7858, -95.8245),
    "Houston": GeoLocation(29.7604, -95.3698),
    "Beaumont": GeoLocation(30.0802, -94.1266),
    "Pasadena": GeoLocation(29.6911, -95.2091),
    "Nederland": GeoLocation(29.9741, -93.9927),
    "Missouri City": GeoLocation(29.6186, -95.5377),
    "Rosenberg": GeoLocation(29.5572, -95.8085),
    # LCF Cypress (est 54, opened Jun 2026) — store address 9850 Fry Rd, 77433.
    "Cypress": GeoLocation(29.9293, -95.7277),
}


class CoordinateCatalogue:
    """Resolves a city name to coordinates.

    A thin seam so the source of coordinates can change (a geocoder, new lat/long
    columns) without touching the repository that consumes it.
    """

    def __init__(self, coordinates: dict[str, GeoLocation] | None = None) -> None:
        self._coordinates = coordinates or _CITY_COORDINATES

    def for_city(self, city: str) -> GeoLocation:
        try:
            return self._coordinates[city]
        except KeyError as exc:
            known = ", ".join(sorted(self._coordinates))
            raise KeyError(f"No coordinates for city {city!r}. Known cities: {known}.") from exc
