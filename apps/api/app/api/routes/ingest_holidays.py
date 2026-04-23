from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.exceptions import ProviderError, SnapshotWriteFailed
from app.services.holiday_service import refresh_holiday_snapshot

router = APIRouter(prefix="/ingest", tags=["ingest-holidays"])


@router.post("/holidays")
async def ingest_holiday_snapshot() -> dict[str, str | int]:
    try:
        envelope = refresh_holiday_snapshot()
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SnapshotWriteFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "dataset": envelope.dataset,
        "source": envelope.source,
        "status": envelope.status,
        "record_count": envelope.record_count,
        "fetched_at": str(envelope.fetched_at),
        "as_of": "" if envelope.as_of is None else str(envelope.as_of),
    }
