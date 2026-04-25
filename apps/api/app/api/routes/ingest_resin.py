from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.exceptions import ProviderError, SnapshotWriteFailed
from app.schemas.common import SnapshotEnvelope
from app.services.resin_benchmark_service import build_default_resin_service, ensure_resin_snapshot_fresh

router = APIRouter(prefix="/ingest/resin", tags=["ingestion"])


@router.post("/sunsirs", response_model=SnapshotEnvelope)
async def ingest_sunsirs_resin() -> SnapshotEnvelope:
    service = build_default_resin_service()
    try:
        return service.refresh_sunsirs_snapshot()
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SnapshotWriteFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Internal error during resin ingestion: {exc}") from exc


@router.post("/sunsirs/ensure-fresh", response_model=SnapshotEnvelope)
async def ensure_fresh_sunsirs_resin() -> SnapshotEnvelope:
    try:
        return ensure_resin_snapshot_fresh(max_age_hours=24)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SnapshotWriteFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Internal error during resin freshness check: {exc}") from exc
