"""Admin-only monitoring endpoints: /api/admin/monitoring/*.

Reads the data monitor's own SQLite store (monitoring/store.py). It never
queries the production business database, so opening the dashboard costs the
database nothing.

Storage access is split:
  * every GET opens the store with SQLite mode=ro (Store.open_readonly): no
    schema creation, no migration, no PRAGMA writes -- a read cannot change it;
  * the acknowledgement opens it read-write WITHOUT schema rights
    (Store.open_for_ack) and runs exactly one statement family:
    Store.acknowledge (UPDATE incidents ... WHERE status='OPEN' + one
    incident_events row). Everything else in the store is written only by the
    monitor process.
  * a missing, locked, corrupt or wrong-version store is a 503 with a fixed
    message, never a partial or empty "all clear".

Every route depends on current_user (401 without a session) and then requires
role super_admin (403 otherwise). Responses carry no credentials, customer data
or chat content: incidents hold only monitor-generated fields.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import current_user


def monitoring_admin(user=Depends(current_user)):
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


router = APIRouter(prefix="/api/admin/monitoring", dependencies=[Depends(monitoring_admin)])


def _cfg():
    from monitoring import config as mcfg
    try:
        return mcfg.load()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Monitor config invalid: {type(e).__name__}")


def _open(write: bool = False):
    from monitoring.store import Store
    path = _cfg()["monitor"]["state_db"]
    if not os.path.exists(path):
        raise HTTPException(status_code=503,
                            detail="The data monitor has not run yet (no incident store).")
    try:
        return Store.open_for_ack(path) if write else Store.open_readonly(path)
    except Exception as e:  # noqa: BLE001 -- permissions, schema version, corruption
        raise HTTPException(status_code=503,
                            detail=f"Monitor store unavailable ({type(e).__name__}).")


def _read(fn):
    """Run a read against the store; storage faults are 503, never a blank page."""
    import sqlite3
    s = _open()
    try:
        return fn(s)
    except sqlite3.Error as e:
        raise HTTPException(status_code=503,
                            detail=f"Monitor store unavailable ({type(e).__name__}).")
    finally:
        s.close()


class AckIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.get("/overview")
def overview():
    from monitoring.clock import utcnow
    from monitoring.summary import overview as build
    cfg = _cfg()
    return _read(lambda s: build(s, utcnow(), cfg))


@router.get("/incidents")
def incidents(status: str | None = None, source: str | None = None, limit: int = 200):
    from monitoring.store import row_dict
    if status and status not in ("OPEN", "ACKNOWLEDGED", "RESOLVED"):
        raise HTTPException(status_code=422, detail="bad status")
    return _read(lambda s: [row_dict(r) for r in s.history(min(max(limit, 1), 1000), status,
                                                          source)])


@router.get("/incidents/{incident_id}")
def incident(incident_id: str):
    from monitoring.store import row_dict
    def get(s):
        row = s.get(incident_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        return {"incident": row_dict(row), "events": [row_dict(e) for e in s.events(incident_id)]}
    return _read(get)


@router.post("/incidents/{incident_id}/ack")
def acknowledge(incident_id: str, body: AckIn, user=Depends(monitoring_admin)):
    import sqlite3
    from monitoring.clock import utcnow
    s = _open(write=True)
    try:
        s.begin()
        ok = s.acknowledge(incident_id, utcnow(), user["email"], body.note)
        s.commit()
    except sqlite3.Error as e:
        s.rollback()
        raise HTTPException(status_code=503,
                            detail=f"Monitor store unavailable ({type(e).__name__}).")
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
    if not ok:
        raise HTTPException(status_code=409, detail="Incident is not OPEN (already acknowledged "
                                                    "or resolved), or does not exist")
    return {"acknowledged": incident_id}


@router.get("/notifications")
def notifications(limit: int = 50):
    from monitoring.store import row_dict
    return _read(lambda s: [row_dict(r) for r in s.notifications(min(max(limit, 1), 500))])
