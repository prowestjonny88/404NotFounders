from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.exceptions import ValidationFailed
from app.services.reference_data_service import load_all_reference_data

router = APIRouter(prefix="/ingest/reference", tags=["ingest-reference"])


@router.post("/load")
async def ingest_reference_data() -> dict[str, int]:
    try:
        payload = load_all_reference_data()
    except ValidationFailed as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "freight_rates": len(payload["freight_rates"]),
        "tariffs": len(payload["tariffs"]),
        "ports": len(payload["ports"]),
        "supplier_seeds": len(payload["supplier_seeds"]),
    }
