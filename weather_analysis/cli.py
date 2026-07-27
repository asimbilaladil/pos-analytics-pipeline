"""Command-line entry point and composition root.

This is the only module that constructs concrete implementations and wires them
together. Every other module depends on abstractions; the choice of Postgres and
Open-Meteo lives here and nowhere else.

Usage:
    python -m weather_analysis.cli backfill        # fetch + store weather
    python -m weather_analysis.cli analyze         # correlate stored weather
    python -m weather_analysis.cli run             # backfill then analyze
    python -m weather_analysis.cli run --start 2026-02-10 --end 2026-07-15
    python -m weather_analysis.cli backtest        # median-only vs median x weather
    python -m weather_analysis.cli backtest --split 2026-06-01 --features rain
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date

from dotenv import load_dotenv

from .analysis import CorrelationAnalyzer, RatioBuilder
from .config import AnalysisWindow, CoordinateCatalogue, default_window, load_database_settings
from .database import ConnectionFactory
from .forecasting import Backtester
from .models import BacktestReport, CorrelationReport
from .repositories import (
    PostgresDemandRepository,
    PostgresLocationRepository,
    PostgresTenderDemandRepository,
    PostgresWeatherRepository,
)
from .services import TenderBacktestService, WeatherBackfillService, WeatherCorrelationService
from .tender_counting import TenderCounter
from .weather_provider import CachingWeatherProvider, OpenMeteoArchiveProvider


@dataclass
class Application:
    """The assembled object graph, exposing the use cases."""

    backfill: WeatherBackfillService
    correlation: WeatherCorrelationService
    backtest: TenderBacktestService


def build_application() -> Application:
    """Composition root: construct and wire every concrete dependency."""
    connections = ConnectionFactory(load_database_settings())
    locations = PostgresLocationRepository(connections, CoordinateCatalogue())
    weather = PostgresWeatherRepository(connections)
    demand = PostgresDemandRepository(connections)
    tenders = PostgresTenderDemandRepository(connections, TenderCounter())
    provider = CachingWeatherProvider(OpenMeteoArchiveProvider())
    return Application(
        backfill=WeatherBackfillService(locations, weather, provider),
        correlation=WeatherCorrelationService(demand, weather, RatioBuilder(), CorrelationAnalyzer()),
        backtest=TenderBacktestService(tenders, weather, Backtester()),
    )


def render_report(report: CorrelationReport) -> str:
    """Format a correlation report as an aligned console table."""
    header = (
        f"Weather vs demand-ratio correlation\n"
        f"  window : {report.first_date} → {report.last_date}\n"
        f"  samples: {report.sample_size} location-days\n\n"
        f"  {'variable':<20}{'pearson r':>11}{'p':>9}{'spearman r':>13}{'p':>9}{'n':>7}\n"
        f"  {'-' * 68}"
    )
    rows = "\n".join(_render_variable(v) for v in report.variables)
    footer = f"\n  {'-' * 68}\n\n  Verdict: {report.verdict}"
    return f"{header}\n{rows}{footer}"


def _render_variable(variable) -> str:
    def fmt(value: float, width: int) -> str:
        return f"{'n/a':>{width}}" if value != value else f"{value:>{width}.3f}"  # NaN check

    return (
        f"  {variable.variable:<20}"
        f"{fmt(variable.pearson_r, 11)}{fmt(variable.pearson_p, 9)}"
        f"{fmt(variable.spearman_r, 13)}{fmt(variable.spearman_p, 9)}"
        f"{variable.sample_size:>7}"
    )


def render_backtest(report: BacktestReport) -> str:
    """Format a backtest report as a compact console summary."""

    def line(accuracy) -> str:
        return (
            f"  {accuracy.label:<18}"
            f"MAE {accuracy.mae:8.1f}   "
            f"MAPE {accuracy.mape_pct:6.1f}%   "
            f"bias {accuracy.bias:+8.1f}   "
            f"n {accuracy.sample_size}"
        )

    return (
        f"Tender forecast backtest (daily totals per location)\n"
        f"  features : {report.feature_set}\n"
        f"  train    : {report.train_first} → {report.train_last}\n"
        f"  test     : {report.test_first} → {report.test_last}\n\n"
        f"{line(report.baseline)}\n"
        f"{line(report.weather)}\n\n"
        f"  Verdict: {report.verdict}"
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="weather_analysis", description=__doc__)
    parser.add_argument("command", choices=("backfill", "analyze", "run", "backtest"))
    parser.add_argument("--start", type=date.fromisoformat, help="inclusive start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=date.fromisoformat, help="inclusive end date (YYYY-MM-DD)")
    parser.add_argument("--split", type=date.fromisoformat, default=date(2026, 6, 1),
                        help="backtest train/test boundary (test starts on this date)")
    parser.add_argument("--features", choices=("all", "rain"), default="all",
                        help="backtest weather feature set")
    return parser.parse_args(argv)


def _resolve_window(args: argparse.Namespace) -> AnalysisWindow:
    window = default_window()
    return AnalysisWindow(
        start=args.start or window.start,
        end=args.end or window.end,
    )


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    window = _resolve_window(args)
    app = build_application()

    if args.command in ("backfill", "run"):
        app.backfill.run(window)
    if args.command in ("analyze", "run"):
        report = app.correlation.run(window)
        print("\n" + render_report(report) + "\n")
    if args.command == "backtest":
        backtest = app.backtest.run(window, args.split, args.features)
        print("\n" + render_backtest(backtest) + "\n")


if __name__ == "__main__":
    main()
