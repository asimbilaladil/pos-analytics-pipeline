"""Pure computation: demand ratios and weather correlations.

Nothing here touches the network or the database. Everything takes domain
objects in and returns domain objects out, which makes the statistics trivial
to reason about and to unit-test in isolation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from .models import (
    CorrelationReport,
    DailyDemand,
    DailyWeather,
    DemandRatio,
    VariableCorrelation,
)

# The weather columns worth correlating (weather_code is categorical, so it is
# summarised separately rather than treated as a linear predictor).
WEATHER_VARIABLES: tuple[str, ...] = (
    "temp_max_c",
    "temp_min_c",
    "temp_mean_c",
    "precipitation_mm",
    "rain_mm",
    "precipitation_hours",
    "wind_max_kmh",
)

# |Pearson r| bands used only to phrase the human-readable verdict.
_NEGLIGIBLE = 0.10
_WEAK = 0.30
_MODERATE = 0.50


class RatioBuilder:
    """Turns raw daily demand into weekday-relative ratios per location."""

    def build(self, demand: list[DailyDemand]) -> list[DemandRatio]:
        if not demand:
            return []
        frame = self._to_frame(demand)
        frame["weekday_median"] = self._weekday_medians(frame)
        frame = frame[frame["weekday_median"] > 0].copy()
        frame["ratio"] = frame["order_count"] / frame["weekday_median"]
        return [self._to_ratio(row) for row in frame.itertuples(index=False)]

    @staticmethod
    def _to_frame(demand: list[DailyDemand]) -> pd.DataFrame:
        frame = pd.DataFrame(
            {
                "establishment_id": d.establishment_id,
                "business_date": d.business_date,
                "order_count": d.order_count,
            }
            for d in demand
        )
        frame["weekday"] = pd.to_datetime(frame["business_date"]).dt.weekday
        return frame

    @staticmethod
    def _weekday_medians(frame: pd.DataFrame) -> pd.Series:
        return frame.groupby(["establishment_id", "weekday"])["order_count"].transform("median")

    @staticmethod
    def _to_ratio(row) -> DemandRatio:
        return DemandRatio(
            establishment_id=int(row.establishment_id),
            business_date=row.business_date,
            order_count=int(row.order_count),
            weekday_median=float(row.weekday_median),
            ratio=float(row.ratio),
        )


class CorrelationAnalyzer:
    """Correlates the demand ratio against each weather variable."""

    def __init__(self, variables: tuple[str, ...] = WEATHER_VARIABLES) -> None:
        self._variables = variables

    def analyze(self, ratios: list[DemandRatio], weather: list[DailyWeather]) -> CorrelationReport:
        joined = self._join(ratios, weather)
        correlations = tuple(self._correlate(joined, variable) for variable in self._variables)
        r_squared = self._combined_r_squared(joined)
        return CorrelationReport(
            sample_size=len(joined),
            first_date=joined["business_date"].min(),
            last_date=joined["business_date"].max(),
            variables=correlations,
            combined_r_squared=r_squared,
            verdict=self._verdict(correlations, r_squared),
        )

    def _join(self, ratios: list[DemandRatio], weather: list[DailyWeather]) -> pd.DataFrame:
        ratio_frame = pd.DataFrame(
            {"establishment_id": r.establishment_id, "business_date": r.business_date, "ratio": r.ratio}
            for r in ratios
        )
        weather_frame = pd.DataFrame(self._weather_record(w) for w in weather)
        merged = ratio_frame.merge(
            weather_frame,
            left_on=["establishment_id", "business_date"],
            right_on=["establishment_id", "observed_on"],
            how="inner",
        )
        return merged.drop(columns=["observed_on"])

    @staticmethod
    def _weather_record(weather: DailyWeather) -> dict:
        obs = weather.observation
        record = {"establishment_id": weather.establishment_id, "observed_on": obs.observed_on}
        record.update({variable: getattr(obs, variable) for variable in WEATHER_VARIABLES})
        return record

    def _correlate(self, joined: pd.DataFrame, variable: str) -> VariableCorrelation:
        pair = joined[["ratio", variable]].dropna()
        if len(pair) < 3 or pair[variable].nunique() < 2:
            return VariableCorrelation(variable, np.nan, np.nan, np.nan, np.nan, len(pair))
        pearson_r, pearson_p = stats.pearsonr(pair[variable], pair["ratio"])
        spearman_r, spearman_p = stats.spearmanr(pair[variable], pair["ratio"])
        return VariableCorrelation(
            variable=variable,
            pearson_r=float(pearson_r),
            pearson_p=float(pearson_p),
            spearman_r=float(spearman_r),
            spearman_p=float(spearman_p),
            sample_size=len(pair),
        )

    def _combined_r_squared(self, joined: pd.DataFrame) -> float:
        columns = ["ratio", *self._variables]
        data = joined[columns].dropna()
        if len(data) <= len(self._variables) + 1:
            return float("nan")
        target = data["ratio"].to_numpy()
        design = np.column_stack([np.ones(len(data)), data[list(self._variables)].to_numpy()])
        coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)
        residual = target - design @ coefficients
        total = ((target - target.mean()) ** 2).sum()
        return float(1.0 - (residual @ residual) / total) if total else float("nan")

    @staticmethod
    def _verdict(correlations: tuple[VariableCorrelation, ...], r_squared: float) -> str:
        significant = [c for c in correlations if c.is_significant and not np.isnan(c.pearson_r)]
        strongest = max((abs(c.pearson_r) for c in significant), default=0.0)
        pct = 0.0 if np.isnan(r_squared) else max(r_squared, 0.0) * 100
        if strongest < _NEGLIGIBLE:
            strength = "negligible — weather does not meaningfully move demand"
        elif strongest < _WEAK:
            strength = "weak — a real but small effect"
        elif strongest < _MODERATE:
            strength = "moderate — worth incorporating"
        else:
            strength = "strong — clearly worth incorporating"
        return f"{strength}. Weather explains ~{pct:.1f}% of the day-to-day swing (combined R²)."
