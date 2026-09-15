"""Push unsynced local sightings to HQ's ingest API.

Mirrors tools/mock_punch_source.py's client shape: one httpx.Client, POST a
PunchBatch-shaped body to /api/v1/punches, X-API-Key auth. Only sighting
events (uid, timestamp) go out — never the face gallery. The local
SightingStore is the durable queue: a failed sync leaves rows unsynced for
the next attempt instead of raising.
"""

from __future__ import annotations

import httpx

from .config import DEVICE_ID, HQ_API_KEY, HQ_BASE_URL, SOURCE_TAG
from .store import SightingStore

BATCH_SIZE = 500


def run_sync(store: SightingStore) -> dict:
    if not HQ_API_KEY:
        return {"ok": False, "error": "FACEKIOSK_HQ_API_KEY is not set"}

    rows = store.unsynced(limit=BATCH_SIZE * 20)
    if not rows:
        return {"ok": True, "submitted": 0, "accepted": 0, "duplicate": 0}

    submitted = accepted = duplicate = 0
    try:
        with httpx.Client(
            base_url=HQ_BASE_URL, headers={"X-API-Key": HQ_API_KEY}, timeout=30
        ) as client:
            for i in range(0, len(rows), BATCH_SIZE):
                chunk = rows[i : i + BATCH_SIZE]
                payload = {
                    "agent_id": SOURCE_TAG,
                    "device_id": DEVICE_ID,
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
