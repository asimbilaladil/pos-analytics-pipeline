"""Persistence interfaces and their Postgres implementations.

Services depend on the abstract base classes here, never on the concrete
Postgres versions, so the storage engine can be swapped or faked in a test
without changing a line of orchestration code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from psycopg2.extras import execute_values

from .config import CoordinateCatalogue
from .database import ConnectionFactory
from .models import DailyDemand, DailyWeather, Location, WeatherObservation
from .tender_counting import TenderCounter


class LocationRepository(ABC):
    """Reads the establishments to be analysed."""

    @abstractmethod
    def list_active(self) -> list[Location]:
        """Return every active establishment, enriched with coordinates."""


class WeatherRepository(ABC):
    """Stores and reads per-establishment daily weather."""

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create the backing table if it does not yet exist."""

    @abstractmethod
    def upsert(self, rows: list[DailyWeather]) -> int:
        """Insert or update the given rows; return how many were written."""

    @abstractmethod
    def list_all(self) -> list[DailyWeather]:
        """Return every stored observation."""


class DemandRepository(ABC):
    """Reads how busy each establishment was per business day."""

    @abstractmethod
    def daily_order_counts(self, start: date, end: date) -> list[DailyDemand]:
        """Return closed-order counts per establishment per local date."""


class TenderDemandRepository(ABC):
    """Reads total tenders (fingers) sold per establishment per business day."""

    @abstractmethod
    def daily_tender_counts(self, start: date, end: date) -> list[DailyDemand]:
        """Return summed finger counts per establishment per local date."""


class PostgresLocationRepository(LocationRepository):
    """Reads establishments from Postgres and attaches coordinates."""

    def __init__(self, connections: ConnectionFactory, catalogue: CoordinateCatalogue) -> None:
        self._connections = connections
        self._catalogue = catalogue

    def list_active(self) -> list[Location]:
        with self._connections.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, city FROM establishments "
                "WHERE active = TRUE AND city IS NOT NULL ORDER BY id"
            )
            rows = cur.fetchall()
        return [
            Location(
                establishment_id=est_id,
                name=name,
                city=city,
                geo=self._catalogue.for_city(city),
            )
            for est_id, name, city in rows
        ]


class PostgresWeatherRepository(WeatherRepository):
    """Persists daily weather in the ``weather_daily`` table."""

    _COLUMNS = (
        "establishment_id",
        "observed_on",
        "temp_max_c",
        "temp_min_c",
        "temp_mean_c",
        "precipitation_mm",
        "rain_mm",
        "precipitation_hours",
        "wind_max_kmh",
        "weather_code",
    )

    def __init__(self, connections: ConnectionFactory) -> None:
        self._connections = connections

    def ensure_schema(self) -> None:
        with self._connections.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS weather_daily (
                    establishment_id    INTEGER NOT NULL REFERENCES establishments(id),
                    observed_on         DATE    NOT NULL,
                    temp_max_c          NUMERIC(5,2),
                    temp_min_c          NUMERIC(5,2),
                    temp_mean_c         NUMERIC(5,2),
                    precipitation_mm    NUMERIC(6,2),
                    rain_mm             NUMERIC(6,2),
                    precipitation_hours NUMERIC(5,2),
                    wind_max_kmh        NUMERIC(6,2),
                    weather_code        SMALLINT,
                    PRIMARY KEY (establishment_id, observed_on)
                )
                """
            )

    def upsert(self, rows: list[DailyWeather]) -> int:
        if not rows:
            return 0
        values = [self._to_tuple(row) for row in rows]
        with self._connections.connect() as conn, conn.cursor() as cur:
            execute_values(cur, self._upsert_sql(), values)
        return len(values)

    def list_all(self) -> list[DailyWeather]:
        with self._connections.connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(self._COLUMNS)} FROM weather_daily")
            rows = cur.fetchall()
        return [self._from_row(row) for row in rows]

    def _upsert_sql(self) -> str:
        columns = ", ".join(self._COLUMNS)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in self._COLUMNS[2:])
        return (
            f"INSERT INTO weather_daily ({columns}) VALUES %s "
            f"ON CONFLICT (establishment_id, observed_on) DO UPDATE SET {updates}"
        )

    @staticmethod
    def _to_tuple(row: DailyWeather) -> tuple:
        obs = row.observation
        return (
            row.establishment_id,
            obs.observed_on,
            obs.temp_max_c,
            obs.temp_min_c,
            obs.temp_mean_c,
            obs.precipitation_mm,
            obs.rain_mm,
            obs.precipitation_hours,
            obs.wind_max_kmh,
            obs.weather_code,
        )

    @staticmethod
    def _from_row(row: tuple) -> DailyWeather:
        est_id, observed_on, *metrics = row
        floats = [float(m) if m is not None else None for m in metrics[:-1]]
        code = int(metrics[-1]) if metrics[-1] is not None else None
        observation = WeatherObservation(observed_on, *floats, weather_code=code)
        return DailyWeather(establishment_id=est_id, observation=observation)


class PostgresDemandRepository(DemandRepository):
    """Reads closed-order counts per establishment per local date."""

    def __init__(self, connections: ConnectionFactory) -> None:
        self._connections = connections

    def daily_order_counts(self, start: date, end: date) -> list[DailyDemand]:
        with self._connections.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT establishment_id,
                       DATE(created_date AT TIME ZONE 'America/Chicago') AS business_date,
                       COUNT(*) AS order_count
                FROM orders_v2
                WHERE closed = TRUE AND deleted = FALSE
                  AND DATE(created_date AT TIME ZONE 'America/Chicago') BETWEEN %s AND %s
                GROUP BY establishment_id, business_date
                ORDER BY establishment_id, business_date
                """,
                (start, end),
            )
            rows = cur.fetchall()
        return [DailyDemand(est_id, business_date, count) for est_id, business_date, count in rows]


class PostgresTenderDemandRepository(TenderDemandRepository):
    """Sums finger counts from closed order lines, per establishment per date."""

    def __init__(self, connections: ConnectionFactory, counter: TenderCounter) -> None:
        self._connections = connections
        self._counter = counter

    def daily_tender_counts(self, start: date, end: date) -> list[DailyDemand]:
        rows = self._fetch_line_quantities(start, end)
        totals: dict[tuple[int, date], int] = {}
        for est_id, business_date, product_name, qty in rows:
            fingers = self._counter.fingers_for(product_name)
            if fingers:
                totals[(est_id, business_date)] = totals.get((est_id, business_date), 0) + fingers * qty
        return [DailyDemand(est_id, day, count) for (est_id, day), count in sorted(totals.items())]

    def _fetch_line_quantities(self, start: date, end: date) -> list[tuple]:
        with self._connections.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT oi.establishment_id,
                       DATE(oi.created_date AT TIME ZONE 'America/Chicago') AS business_date,
                       oi.product_name,
                       SUM(oi.quantity)::int AS qty
                FROM order_items_v2 oi
                JOIN orders_v2 o ON o.id = oi.order_id
                    AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day'
                                           AND oi.created_date + INTERVAL '1 day'
                    AND o.closed = TRUE AND o.deleted = FALSE
                WHERE oi.deleted = FALSE AND oi.is_voided = FALSE
                  AND oi.product_name IS NOT NULL
                  AND DATE(oi.created_date AT TIME ZONE 'America/Chicago') BETWEEN %s AND %s
                GROUP BY oi.establishment_id, business_date, oi.product_name
                """,
                (start, end),
            )
            return cur.fetchall()
