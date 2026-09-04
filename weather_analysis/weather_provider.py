"""Weather-source interface and the Open-Meteo implementation.

The service layer depends on ``WeatherProvider``; swapping Open-Meteo for
another source (or a recorded fixture in a test) means writing one new class,
not editing the backfill.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import requests

from .models import GeoLocation, WeatherObservation

# Open-Meteo daily field name -> our observation attribute. Kept in one place so
# a field rename upstream is a single-line change.
_FIELD_MAP: dict[str, str] = {
    "temperature_2m_max": "temp_max_c",
    "temperature_2m_min": "temp_min_c",
    "temperature_2m_mean": "temp_mean_c",
    "precipitation_sum": "precipitation_mm",
    "rain_sum": "rain_mm",
    "precipitation_hours": "precipitation_hours",
    "wind_speed_10m_max": "wind_max_kmh",
    "weather_code": "weather_code",
}


class WeatherProvider(ABC):
    """Fetches historical daily weather for a coordinate."""

    @abstractmethod
    def fetch_daily(self, geo: GeoLocation, start: date, end: date) -> list[WeatherObservation]:
        """Return one observation per day in the inclusive range."""


class OpenMeteoArchiveProvider(WeatherProvider):
    """Reads the free Open-Meteo ERA5 archive (no API key required)."""

    _URL = "https://archive-api.open-meteo.com/v1/archive"
    _TIMEZONE = "America/Chicago"

    def __init__(self, session: requests.Session | None = None, timeout_seconds: int = 30) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout_seconds

    def fetch_daily(self, geo: GeoLocation, start: date, end: date) -> list[WeatherObservation]:
        payload = self._request(geo, start, end)
        return self._parse(payload)

    def _request(self, geo: GeoLocation, start: date, end: date) -> dict:
        response = self._session.get(
            self._URL,
            params={
                "latitude": geo.latitude,
                "longitude": geo.longitude,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "daily": ",".join(_FIELD_MAP),
                "timezone": self._TIMEZONE,
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    def _parse(self, payload: dict) -> list[WeatherObservation]:
        daily = payload.get("daily") or {}
        days = daily.get("time") or []
        return [self._build_observation(daily, index, day) for index, day in enumerate(days)]

    @staticmethod
    def _build_observation(daily: dict, index: int, day: str) -> WeatherObservation:
        def value(field: str):
            series = daily.get(field) or []
            return series[index] if index < len(series) else None

        metrics = {attr: value(field) for field, attr in _FIELD_MAP.items()}
        code = metrics.pop("weather_code")
        return WeatherObservation(
            observed_on=date.fromisoformat(day),
            weather_code=int(code) if code is not None else None,
            **metrics,
        )


class CachingWeatherProvider(WeatherProvider):
    """Memoises a wrapped provider so shared coordinates are fetched once.

    Several establishments sit in the same city and resolve to identical
    coordinates; without this, each would trigger its own network round-trip.
    """

    def __init__(self, inner: WeatherProvider) -> None:
        self._inner = inner
        self._cache: dict[tuple[float, float, date, date], list[WeatherObservation]] = {}

    def fetch_daily(self, geo: GeoLocation, start: date, end: date) -> list[WeatherObservation]:
        key = (geo.latitude, geo.longitude, start, end)
        if key not in self._cache:
            self._cache[key] = self._inner.fetch_daily(geo, start, end)
        return self._cache[key]
