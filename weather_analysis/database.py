"""A single place that knows how to open a Postgres connection.

Repositories depend on this factory rather than importing psycopg2 and reading
settings themselves, so the connection policy (and any future pooling) lives in
exactly one spot.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg2
from psycopg2.extensions import connection as Connection

from .config import DatabaseSettings


class ConnectionFactory:
    """Hands out short-lived, auto-closing connections."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        """Yield a connection, committing on success and closing always."""
        conn = psycopg2.connect(
            host=self._settings.host,
            port=self._settings.port,
            dbname=self._settings.name,
            user=self._settings.user,
            password=self._settings.password,
        )
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
