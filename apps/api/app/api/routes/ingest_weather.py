from fastapi import APIRouter, HTTPException

from app.core.exceptions import ProviderError
from app.services.weather_risk_service import build_default_weather_service
from app.schemas.common import SnapshotEnvelope

router = APIRouter(prefix="/ingest/weather", tags=["ingestion"])


@router.post("", response_model=SnapshotEnvelope)
async def ingest_weather() -> SnapshotEnvelope:
    """
    Fetch the longest OpenWeather forecast available for the configured key
    for all origin/destination ports, clean slot/day evidence, and write to
    the weather snapshot. The snapshot records the endpoint actually used.
    """
    service = build_default_weather_service()
    try:
        return await service.refresh_weather_snapshot()
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Internal error during weather ingestion: {exc}")
