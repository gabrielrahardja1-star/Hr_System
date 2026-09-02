"""Pydantic models for the ingest API contract.

This contract is the seam between the site agent and HQ. It is deliberately
minimal: a punch is (device, user, timestamp) plus whatever raw type/status
codes the device gave. The agent does no interpretation — coding happens at HQ.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, field_validator


class PunchIn(BaseModel):
    device_user_id: str = Field(min_length=1, max_length=32)
    punched_at: dt.datetime
    raw_punch_type: int | None = None
    raw_status: int | None = None

    @field_validator("punched_at")
    @classmethod
    def _tz_aware_utc(cls, v: dt.datetime) -> dt.datetime:
        # Agent may send local time with offset, or naive UTC. Normalise to UTC.
        if v.tzinfo is None:
            return v.replace(tzinfo=dt.timezone.utc)
        return v.astimezone(dt.timezone.utc)


class PunchBatch(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=32)
    client_batch_ref: str | None = Field(default=None, max_length=64)
    punches: list[PunchIn] = Field(default_factory=list, max_length=5000)


class IngestResult(BaseModel):
    batch_id: int
    submitted: int
    accepted: int
    duplicate: int
    earliest_punch: dt.datetime | None = None
    latest_punch: dt.datetime | None = None
    recompute_queued_for: list[str] = Field(
        default_factory=list,
        description="work_date (YYYY-MM-DD) values touched by this batch",
    )


class SyncState(BaseModel):
    device_id: str
    last_punch_at: dt.datetime | None
    last_batch_at: dt.datetime | None
    total_punches: int


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    db_ok: bool
    time_utc: dt.datetime
