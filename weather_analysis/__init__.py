"""Weather correlation analysis for Laynes tender demand.

A small, layered tool that answers one question: does weather explain any of
the day-to-day swing in order volume, once the normal weekday pattern is
removed?

Layers (each in its own module, dependencies point inward at abstractions):

    models          - immutable domain objects (no behaviour, no I/O)
    config          - settings + the city -> coordinate catalogue
    database        - a single context-managed connection factory
    repositories    - persistence interfaces + their Postgres implementations
    weather_provider- weather-source interface + the Open-Meteo implementation
    analysis        - pure computation (demand ratios, correlation stats)
    services        - use-case orchestration (backfill, correlate)
    cli             - the composition root that wires concretes together
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
