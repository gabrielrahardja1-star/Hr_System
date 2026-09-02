"""Ingest API — the seam between a site agent and HQ.

    POST /api/v1/punches      accept a batch of raw punches (idempotent)
    GET  /api/v1/sync-state   what the agent needs to know to resume
    GET  /api/v1/health       liveness + db check

Idempotency: a punch is identified by (device_id, device_user_id, punched_at).
Re-sending a batch (agent crashed mid-ack, retried from spool) inserts nothing
new and reports the rows as `duplicate`. The agent can safely replay its spool.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server import __version__
from server.api.auth import require_api_key
from server.config import get_settings
from server.core.recompute import recompute_for_dates
from server.db import get_session
from server.models import IngestBatch, Punch, PunchSource
from server.schemas import (
    HealthResponse,
    IngestResult,
    PunchBatch,
    SyncState,
)

router = APIRouter(prefix="/api/v1", tags=["ingest"])


@router.get("/health", response_model=HealthResponse)
def health(session: Session = Depends(get_session)) -> HealthResponse:
    db_ok = True
    try:
        session.execute(select(1))
    except Exception:  # pragma: no cover
        db_ok = False
    return HealthResponse(
        version=__version__,
        db_ok=db_ok,
        time_utc=dt.datetime.now(dt.timezone.utc),
    )


@router.get("/sync-state", response_model=SyncState)
def sync_state(
    device_id: str,
    session: Session = Depends(get_session),
    _key: str = Depends(require_api_key),
) -> SyncState:
    last_punch = session.execute(
        select(func.max(Punch.punched_at)).where(Punch.device_id == device_id)
    ).scalar_one_or_none()
    last_batch = session.execute(
        select(func.max(IngestBatch.received_at)).where(IngestBatch.device_id == device_id)
    ).scalar_one_or_none()
    total = session.execute(
        select(func.count(Punch.id)).where(Punch.device_id == device_id)
    ).scalar_one()
    return SyncState(
        device_id=device_id,
        last_punch_at=last_punch,
        last_batch_at=last_batch,
        total_punches=total,
    )


@router.post("/punches", response_model=IngestResult)
def ingest_punches(
    batch: PunchBatch,
    session: Session = Depends(get_session),
    _key: str = Depends(require_api_key),
) -> IngestResult:
    if not batch.punches:
        raise HTTPException(422, "Empty batch")

    ib = IngestBatch(
        agent_id=batch.agent_id,
        device_id=batch.device_id,
        client_batch_ref=batch.client_batch_ref,
        punches_submitted=len(batch.punches),
    )
    session.add(ib)
    session.flush()

    # Pre-load existing identities for this device to classify duplicates cheaply.
    incoming_ts = {p.punched_at for p in batch.punches}
    existing = set(
        session.execute(
            select(Punch.device_user_id, Punch.punched_at).where(
                Punch.device_id == batch.device_id,
                Punch.punched_at.in_(incoming_ts),
            )
        ).all()
    )

    accepted = 0
    duplicate = 0
    seen_in_batch: set[tuple[str, dt.datetime]] = set()
    touched_dates: set[dt.date] = set()
    tz = get_settings().timezone

    for p in batch.punches:
        identity = (p.device_user_id, p.punched_at)
        if identity in existing or identity in seen_in_batch:
            duplicate += 1
            continue
        seen_in_batch.add(identity)
        session.add(
            Punch(
                device_id=batch.device_id,
                device_user_id=p.device_user_id,
                punched_at=p.punched_at,
                raw_punch_type=p.raw_punch_type,
                raw_status=p.raw_status,
                source=PunchSource.device,
                ingest_batch_id=ib.id,
            )
        )
        accepted += 1
        # Local calendar date — recompute pads +/-1 day, so a punch near
        # midnight still reaches the right work_date.
        touched_dates.add(p.punched_at.astimezone(tz).date())

    try:
        session.flush()
    except IntegrityError:  # pragma: no cover - race with a concurrent identical batch
        session.rollback()
        raise HTTPException(409, "Concurrent duplicate batch; retry")

    ib.punches_accepted = accepted
    ib.punches_duplicate = duplicate
    ib.earliest_punch = min(incoming_ts)
    ib.latest_punch = max(incoming_ts)

    if accepted:
        recompute_for_dates(session, touched_dates)

    session.commit()

    return IngestResult(
        batch_id=ib.id,
        submitted=len(batch.punches),
        accepted=accepted,
        duplicate=duplicate,
        earliest_punch=min(incoming_ts),
        latest_punch=max(incoming_ts),
        recompute_queued_for=sorted(d.isoformat() for d in touched_dates),
    )
