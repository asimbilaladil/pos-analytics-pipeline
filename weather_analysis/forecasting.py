"""Forecasting components and the median-vs-weather backtest.

Two forecasters are compared out-of-time:

    baseline  : prediction = same-weekday median (fit on the training period)
    weather   : prediction = same-weekday median x day_factor(weather)

``day_factor`` is a linear model of the demand ratio on weather, fit on the
training period only and applied to unseen test days, so the comparison is
honest (no label leakage).
"""

from __future__ import annotations

from datetime import date

import numpy as np

from .models import BacktestReport, DailyDemand, DailyWeather, ForecastAccuracy

# Named weather feature sets. "rain" is the confound-free subset; "all" adds the
# temperature terms, which are partly a seasonal artifact over Feb->Jul.
FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "all": (
        "temp_max_c",
        "temp_min_c",
        "temp_mean_c",
        "precipitation_mm",
        "rain_mm",
        "precipitation_hours",
        "wind_max_kmh",
    ),
    "rain": ("rain_mm", "precipitation_hours"),
}

# A day factor outside this band is almost certainly extrapolation noise.
_FACTOR_FLOOR = 0.6
_FACTOR_CEILING = 1.4


class WeekdayMedianBaseline:
    """Per-(establishment, weekday) median demand, learned from training days."""

    def __init__(self) -> None:
        self._medians: dict[tuple[int, int], float] = {}

    def fit(self, demand: list[DailyDemand]) -> "WeekdayMedianBaseline":
        grouped: dict[tuple[int, int], list[int]] = {}
        for day in demand:
            grouped.setdefault((day.establishment_id, day.business_date.weekday()), []).append(day.order_count)
        self._medians = {key: float(np.median(counts)) for key, counts in grouped.items()}
        return self

    def predict(self, establishment_id: int, business_date: date) -> float | None:
        return self._medians.get((establishment_id, business_date.weekday()))


class DayFactorModel:
    """Linear model of the demand ratio (actual / median) on weather."""

    def __init__(self, features: tuple[str, ...]) -> None:
        self._features = features
        self._coefficients: np.ndarray | None = None

    def fit(self, weather_rows: list[dict], ratios: list[float]) -> "DayFactorModel":
        if len(ratios) <= len(self._features) + 1:
            return self
        design = self._design_matrix(weather_rows)
        target = np.asarray(ratios, dtype=float)
        self._coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)
        return self

    def predict(self, weather_row: dict) -> float:
        if self._coefficients is None:
            return 1.0
        design = self._design_matrix([weather_row])
        return float(np.clip(design @ self._coefficients, _FACTOR_FLOOR, _FACTOR_CEILING)[0])

    def _design_matrix(self, weather_rows: list[dict]) -> np.ndarray:
        rows = [[row.get(feature, 0.0) or 0.0 for feature in self._features] for row in weather_rows]
        features = np.asarray(rows, dtype=float)
        return np.column_stack([np.ones(len(features)), features])


class Backtester:
    """Runs the out-of-time median-vs-weather comparison."""

    def run(
        self,
        demand: list[DailyDemand],
        weather: list[DailyWeather],
        feature_set: str,
        split: date,
    ) -> BacktestReport:
        features = FEATURE_SETS[feature_set]
        weather_index = self._index_weather(weather)
        train = [d for d in demand if d.business_date < split]
        test = [d for d in demand if d.business_date >= split]

        baseline = WeekdayMedianBaseline().fit(train)
        model = self._fit_factor_model(train, baseline, weather_index, features)

        baseline_preds, weather_preds, actuals = self._score(test, baseline, model, weather_index)
        return BacktestReport(
            feature_set=feature_set,
            train_first=min(d.business_date for d in train),
            train_last=max(d.business_date for d in train),
            test_first=min(d.business_date for d in test),
            test_last=max(d.business_date for d in test),
            baseline=self._accuracy("median-only", baseline_preds, actuals),
            weather=self._accuracy("median x weather", weather_preds, actuals),
        )

    @staticmethod
    def _index_weather(weather: list[DailyWeather]) -> dict[tuple[int, date], dict]:
        index: dict[tuple[int, date], dict] = {}
        for row in weather:
            obs = row.observation
            index[(row.establishment_id, obs.observed_on)] = {
                feature: getattr(obs, feature) for feature in FEATURE_SETS["all"]
            }
        return index

    def _fit_factor_model(self, train, baseline, weather_index, features) -> DayFactorModel:
        weather_rows: list[dict] = []
        ratios: list[float] = []
        for day in train:
            median = baseline.predict(day.establishment_id, day.business_date)
            weather = weather_index.get((day.establishment_id, day.business_date))
            if median and weather is not None:
                weather_rows.append(weather)
                ratios.append(day.order_count / median)
        return DayFactorModel(features).fit(weather_rows, ratios)

    def _score(self, test, baseline, model, weather_index):
        baseline_preds: list[float] = []
        weather_preds: list[float] = []
        actuals: list[int] = []
        for day in test:
            median = baseline.predict(day.establishment_id, day.business_date)
            weather = weather_index.get((day.establishment_id, day.business_date))
            if not median or weather is None:
                continue
            baseline_preds.append(median)
            weather_preds.append(median * model.predict(weather))
            actuals.append(day.order_count)
        return baseline_preds, weather_preds, actuals

    @staticmethod
    def _accuracy(label: str, predictions: list[float], actuals: list[int]) -> ForecastAccuracy:
        if not predictions:
            return ForecastAccuracy(label, float("nan"), float("nan"), float("nan"), 0)
        preds = np.asarray(predictions, dtype=float)
        truth = np.asarray(actuals, dtype=float)
        residuals = preds - truth
        nonzero = truth > 0
        mape = float(np.mean(np.abs(residuals[nonzero]) / truth[nonzero]) * 100)
        return ForecastAccuracy(
            label=label,
            mae=float(np.abs(residuals).mean()),
            mape_pct=mape,
            bias=float(residuals.mean()),
            sample_size=len(predictions),
        )
