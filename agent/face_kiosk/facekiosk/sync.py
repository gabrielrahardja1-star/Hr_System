"""Push unsynced local sightings to HQ's ingest API.

Mirrors tools/mock_punch_source.py's client shape: one httpx.Client, POST a
PunchBatch-shaped body to /api/v1/punches, X-API-Key auth. Only sighting
events (uid, timestamp) go out — never the face gallery. The local
SightingStore is the durable queue: a failed sync leaves rows unsynced for
the next attempt instead of raising.
"""

from __future__ import annotations

import httpx

# Read through the module, not by importing the names: the admin Settings
# page rewrites them at runtime, and a from-import would freeze the values
# this module saw at startup.
from . import config
from .config import SOURCE_TAG
from .store import SightingStore

BATCH_SIZE = 500


def run_sync(store: SightingStore) -> dict:
    if not config.HQ_API_KEY:
        return {"ok": False, "error": "no API key set — open Settings in the admin page"}

    rows = store.unsynced(limit=BATCH_SIZE * 20)
    if not rows:
        return {"ok": True, "submitted": 0, "accepted": 0, "duplicate": 0}

    submitted = accepted = duplicate = 0
    try:
        with httpx.Client(
            base_url=config.HQ_BASE_URL, headers={"X-API-Key": config.HQ_API_KEY}, timeout=30
        ) as client:
            for i in range(0, len(rows), BATCH_SIZE):
                chunk = rows[i : i + BATCH_SIZE]
                payload = {
                    "agent_id": SOURCE_TAG,
                    "device_id": config.DEVICE_ID,
                    "client_batch_ref": f"kiosk-{chunk[0]['id']}-{chunk[-1]['id']}",
                    "punches": [
                        {"device_user_id": r["uid"], "punched_at": r["seen_at"]}
                        for r in chunk
                    ],
                }
                resp = client.post("/api/v1/punches", json=payload)
                resp.raise_for_status()
                result = resp.json()
                submitted += result["submitted"]
                accepted += result["accepted"]
                duplicate += result["duplicate"]
                store.mark_synced([r["id"] for r in chunk])
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "submitted": submitted,
            "accepted": accepted,
            "duplicate": duplicate,
        }

    return {"ok": True, "submitted": submitted, "accepted": accepted, "duplicate": duplicate}


def check_hq() -> dict:
    """Can this kiosk actually reach HQ with the key it has?

    Deliberately separate from run_sync: an operator setting a kiosk up needs to
    know the address and key are right *before* anyone clocks in, not discover
    it at the end of the first shift.
    """
    if not config.HQ_BASE_URL:
        return {"ok": False, "error": "no server address set"}
    if not config.HQ_API_KEY:
        return {"ok": False, "error": "no API key set"}
    try:
        with httpx.Client(
            base_url=config.HQ_BASE_URL,
            headers={"X-API-Key": config.HQ_API_KEY},
            timeout=10,
        ) as client:
            # sync-state wants the device_id, and passing the real one makes
            # this check the exact identity punches will arrive under.
            resp = client.get("/api/v1/sync-state",
                              params={"device_id": config.DEVICE_ID})
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"cannot reach {config.HQ_BASE_URL} ({exc})"}
    if resp.status_code == 401:
        return {"ok": False, "error": "server reached, but the API key was rejected"}
    if resp.status_code >= 400:
        return {"ok": False, "error": f"server returned HTTP {resp.status_code}"}
    return {"ok": True, "url": config.HQ_BASE_URL}
