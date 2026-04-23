from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.exceptions import ExternalFetchFailed, SnapshotWriteFailed
from app.schemas.common import SnapshotEnvelope
from app.services.market_data_service import refresh_energy_snapshot, refresh_fx_snapshot

router = APIRouter(prefix="/ingest/market", tags=["ingest-market"])


class FXRefreshRequest(BaseModel):
    pair: str


class EnergyRefreshRequest(BaseModel):
    symbol: str = "BZ=F"


@router.post("/fx", response_model=SnapshotEnvelope)
async def ingest_fx_snapshot(request: FXRefreshRequest) -> SnapshotEnvelope:
    try:
        return await refresh_fx_snapshot(pair=request.pair)
    except ExternalFetchFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SnapshotWriteFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/energy", response_model=SnapshotEnvelope)
async def ingest_energy_snapshot(request: EnergyRefreshRequest) -> SnapshotEnvelope:
    try:
        return await refresh_energy_snapshot(symbol=request.symbol)
    except ExternalFetchFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SnapshotWriteFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
