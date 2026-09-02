"""API-key auth for the ingest endpoints.

Each site agent is issued one key (HR_INGEST_API_KEYS, comma-separated). This is
not a user auth system — it only gates machine-to-machine sync. The web UI has
its own session handling (Phase 2).
"""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from server.config import get_settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    keys = get_settings().ingest_api_keys
    if not keys:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Ingest disabled: no API keys configured (set HR_INGEST_API_KEYS)",
        )
    if x_api_key is None or x_api_key not in keys:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing X-API-Key")
    return x_api_key
