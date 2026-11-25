"""Admin-only monitoring endpoints: /api/admin/monitoring/*.

Reads the data monitor's own SQLite store (monitoring/store.py). It never
queries the production business database, so opening the dashboard costs the
database nothing. The only write is an acknowledgement, which changes the
monitor's incident record -- never business data.

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


def _store():
    from monitoring import config as mcfg
    from monitoring.store import Store
    try:
        cfg = mcfg.load()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Monitor config invalid: {type(e).__name__}")
    path = cfg["monitor"]["state_db"]
    if not os.path.exists(path):
        raise HTTPException(status_code=503,
                            detail="The data monitor has not run yet (no incident store).")
    return Store(path)


class AckIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.get("/overview")
def overview():
    from monitoring.clock import utcnow
    from monitoring.summary import overview as build
    s = _store()
    try:
        return build(s, utcnow())
    finally:
        s.close()


@router.get("/incidents")
def incidents(status: str | None = None, source: str | None = None, limit: int = 200):
    from monitoring.store import row_dict
    if status and status not in ("OPEN", "ACKNOWLEDGED", "RESOLVED"):
        raise HTTPException(status_code=422, detail="bad status")
    s = _store()
    try:
        return [row_dict(r) for r in s.history(min(max(limit, 1), 1000), status, source)]
    finally:
        s.close()


@router.get("/incidents/{incident_id}")
def incident(incident_id: str):
    from monitoring.store import row_dict
    s = _store()
    try:
        row = s.get(incident_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        return {"incident": row_dict(row), "events": [row_dict(e) for e in s.events(incident_id)]}
    finally:
        s.close()


@router.post("/incidents/{incident_id}/ack")
def acknowledge(incident_id: str, body: AckIn, user=Depends(monitoring_admin)):
    from monitoring.clock import utcnow
    s = _store()
    try:
        s.begin()
        ok = s.acknowledge(incident_id, utcnow(), user["email"], body.note)
        s.commit()
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
    s = _store()
    try:
        return [row_dict(r) for r in s.notifications(min(max(limit, 1), 500))]
    finally:
        s.close()
