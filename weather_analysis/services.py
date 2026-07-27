"""Use-case orchestration.

Each service coordinates the layers to satisfy one use case. They own the
"what happens in what order" and delegate every "how" to an injected
repository, provider, or analyzer — so they read like a summary of the workflow.
"""

from __future__ import annotations

import logging

from datetime import date

from .analysis import CorrelationAnalyzer, RatioBuilder
from .config import AnalysisWindow
from .forecasting import Backtester
from .models import BacktestReport, CorrelationReport, DailyWeather, GeoLocation, Location, WeatherObservation
from .repositories import (
    DemandRepository,
    LocationRepository,
    TenderDemandRepository,
    WeatherRepository,
)
from .weather_provider import WeatherProvider

log = logging.getLogger(__name__)


class WeatherBackfillService:
    """Fills ``weather_daily`` for every active location over a date window."""

    def __init__(
        self,
        locations: LocationRepository,
        weather: WeatherRepository,
        provider: WeatherProvider,
    ) -> None:
        self._locations = locations
        self._weather = weather
        self._provider = provider

    def run(self, window: AnalysisWindow) -> int:
        self._weather.ensure_schema()
        locations = self._locations.list_active()
        rows = self._collect(locations, window)
        written = self._weather.upsert(rows)
        log.info("Backfilled %d weather rows for %d locations", written, len(locations))
        return written

    def _collect(self, locations: list[Location], window: AnalysisWindow) -> list[DailyWeather]:
        rows: list[DailyWeather] = []
        for location in locations:
            observations = self._fetch(location.geo, window)
            rows.extend(DailyWeather(location.establishment_id, obs) for obs in observations)
            log.info("Fetched %d days for %s", len(observations), location.name)
        return rows

    def _fetch(self, geo: GeoLocation, window: AnalysisWindow) -> list[WeatherObservation]:
        return self._provider.fetch_daily(geo, window.start, window.end)


class WeatherCorrelationService:
    """Answers whether weather correlates with demand over a date window."""

    def __init__(
        self,
        demand: DemandRepository,
        weather: WeatherRepository,
        ratio_builder: RatioBuilder,
        analyzer: CorrelationAnalyzer,
    ) -> None:
        self._demand = demand
        self._weather = weather
        self._ratio_builder = ratio_builder
        self._analyzer = analyzer

    def run(self, window: AnalysisWindow) -> CorrelationReport:
        demand = self._demand.daily_order_counts(window.start, window.end)
        ratios = self._ratio_builder.build(demand)
        weather = self._weather.list_all()
        return self._analyzer.analyze(ratios, weather)


class TenderBacktestService:
    """Backtests median-only vs weather-adjusted tender forecasting."""

    def __init__(
        self,
        tender_demand: TenderDemandRepository,
        weather: WeatherRepository,
        backtester: Backtester,
    ) -> None:
        self._tender_demand = tender_demand
        self._weather = weather
        self._backtester = backtester

    def run(self, window: AnalysisWindow, split: date, feature_set: str) -> BacktestReport:
        demand = self._tender_demand.daily_tender_counts(window.start, window.end)
        weather = self._weather.list_all()
        return self._backtester.run(demand, weather, feature_set, split)
