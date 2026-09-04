"""Immutable domain objects shared across the package.

These carry data only. They hold no database handles, no HTTP clients, and no
computation beyond trivial derived properties, so every other layer can depend
on them without picking up a dependency on infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class GeoLocation:
    """A point on the earth, used to query a weather source."""

    latitude: float
    longitude: float


@dataclass(frozen=True)
class Location:
    """A Laynes establishment enriched with the coordinates to weather it."""

    establishment_id: int
    name: str
    city: str
    geo: GeoLocation


@dataclass(frozen=True)
class WeatherObservation:
    """One day of weather at a coordinate, independent of any establishment.

    Every metric is optional: a weather source may not report every field for
    every day, and the analysis tolerates gaps by dropping them pairwise.
    """

    observed_on: date
    temp_max_c: float | None
    temp_min_c: float | None
    temp_mean_c: float | None
    precipitation_mm: float | None
    rain_mm: float | None
    precipitation_hours: float | None
    wind_max_kmh: float | None
    weather_code: int | None


@dataclass(frozen=True)
class DailyWeather:
    """A weather observation attributed to a specific establishment."""

    establishment_id: int
    observation: WeatherObservation


@dataclass(frozen=True)
class DailyDemand:
    """How busy one establishment was on one local business date."""

    establishment_id: int
    business_date: date
    order_count: int


@dataclass(frozen=True)
class DemandRatio:
    """Demand expressed relative to a typical same-weekday for that location.

    ``ratio`` above 1.0 means the day ran busier than a normal weekday; below
    1.0 means quieter. Removing the weekday baseline is what lets weather show
    up as signal instead of being swamped by the weekly rhythm.
    """

    establishment_id: int
    business_date: date
    order_count: int
    weekday_median: float
    ratio: float


@dataclass(frozen=True)
class VariableCorrelation:
    """Correlation of a single weather variable against the demand ratio."""

    variable: str
    pearson_r: float
    pearson_p: float
    spearman_r: float
    spearman_p: float
    sample_size: int

    @property
    def is_significant(self) -> bool:
        """True when the linear correlation clears the usual 5% threshold."""
        return self.pearson_p < 0.05


@dataclass(frozen=True)
class CorrelationReport:
    """The full result of a correlation run, ready to render."""

    sample_size: int
    first_date: date
    last_date: date
    variables: tuple[VariableCorrelation, ...]
    combined_r_squared: float
    verdict: str


@dataclass(frozen=True)
class ForecastAccuracy:
    """How close one forecasting method got, over a set of location-days."""

    label: str
    mae: float
    mape_pct: float
    bias: float
    sample_size: int


@dataclass(frozen=True)
class BacktestReport:
    """Head-to-head result of median-only vs weather-adjusted forecasting."""

    feature_set: str
    train_first: date
    train_last: date
    test_first: date
    test_last: date
    baseline: ForecastAccuracy
    weather: ForecastAccuracy

    @property
    def mae_improvement_pct(self) -> float:
        """Positive means the weather-adjusted forecast beat median-only."""
        if self.baseline.mae == 0:
            return 0.0
        return (self.baseline.mae - self.weather.mae) / self.baseline.mae * 100

    @property
    def verdict(self) -> str:
        delta = self.mae_improvement_pct
        if delta > 2:
            return f"weather-adjusted forecast helps: {delta:+.1f}% lower error than median-only"
        if delta < -2:
            return f"weather-adjusted forecast hurts: {delta:+.1f}% (overfit / confound) — keep median-only"
        return f"no meaningful difference ({delta:+.1f}%) — median-only is simpler and just as good"
