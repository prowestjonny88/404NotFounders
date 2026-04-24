from fastapi import APIRouter, HTTPException

from app.core.exceptions import ProviderError
from app.services.news_event_service import build_default_news_service
from app.schemas.common import SnapshotEnvelope

router = APIRouter(prefix="/ingest/news", tags=["ingestion"])


@router.post("/gnews", response_model=SnapshotEnvelope)
async def ingest_gnews() -> SnapshotEnvelope:
    """
    Fetch GNews articles across logistics, finance, and geopolitical buckets,
    normalize into cleaned decision evidence, and write to news snapshot.
    """
    service = build_default_news_service()
    try:
        return await service.refresh_news_snapshot()
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Internal error during news ingestion: {exc}")


@router.post("/gnews/ensure-fresh", response_model=SnapshotEnvelope)
async def ensure_fresh_gnews() -> SnapshotEnvelope:
    """
    Return the current news snapshot if it is under 60 minutes old;
    otherwise fetch fresh GNews data.
    """
    service = build_default_news_service()
    try:
        return await service.ensure_news_snapshot_fresh(max_age_minutes=60)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Internal error during news freshness check: {exc}")
