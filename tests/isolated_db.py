"""Guard for tests that WRITE (inside a rolled-back transaction) or need a
schema change such as an unapplied migration.

They must run against an isolated, disposable copy of the database -- never
production. The guard refuses unless DB_NAME ends in "_test" and DB_PORT is not
the production port, and it never reads .env (so production credentials cannot
be picked up by accident).

Typical set-up (a throw-away cluster, schema + HME data copied read-only):
    DB_HOST=127.0.0.1 DB_PORT=55432 DB_NAME=laynes_test
    DB_USER=laynes_user DB_PASS=<test-only> DB_RO_USER=laynes_ro DB_RO_PASS=<test-only>
"""
import os
import sys

import psycopg2

PRODUCTION_PORT = "5432"


def require_isolated():
    name, port = os.environ.get("DB_NAME", ""), str(os.environ.get("DB_PORT", ""))
    if not name.endswith("_test") or port in ("", PRODUCTION_PORT):
        sys.exit(f"REFUSING: DB_NAME={name!r} DB_PORT={port!r} is not an isolated test "
                 f"database (needs DB_NAME ending in _test on a non-production port)")


def rw():
    require_isolated()
    return psycopg2.connect(host=os.environ["DB_HOST"], port=os.environ["DB_PORT"],
                            dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                            password=os.environ["DB_PASS"])
