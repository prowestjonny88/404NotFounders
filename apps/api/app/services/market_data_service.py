from datetime import UTC, datetime
from typing import Final

from app.providers.yfinance_provider import fetch_energy_history, fetch_fx_history
from app.repositories.snapshot_repository import SnapshotRepository
from app.schemas.common import SnapshotEnvelope
from app.schemas.market import EnergySnapshotRecord, FXSnapshotRecord

ENERGY_SERIES_NAMES: Final[dict[str, str]] = {
    "BZ=F": "Brent Crude",
    "NG=F": "Natural Gas",
}


async def refresh_fx_snapshot(pair: str) -> SnapshotEnvelope:
    normalized_pair = pair.upper()
    history = await fetch_fx_history(pair=normalized_pair)
    records = [
        FXSnapshotRecord(
            pair=normalized_pair,
            date=row.date,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
        )
        for row in history.itertuples(index=False)
    ]

    envelope = SnapshotEnvelope(
        dataset=f"fx/{normalized_pair}",
        source="yfinance",
        fetched_at=datetime.now(UTC),
        as_of=datetime.strptime(records[-1].date.isoformat(), "%Y-%m-%d"),
        status="success",
        record_count=len(records),
        data=[record.model_dump(mode="json") for record in records],
    )
    SnapshotRepository().write_snapshot(dataset=normalized_pair, envelope=envelope)
    return envelope


async def refresh_energy_snapshot(symbol: str) -> SnapshotEnvelope:
    normalized_symbol = symbol.upper()
    history = await fetch_energy_history(symbol=normalized_symbol)
    records = [
        EnergySnapshotRecord(
            symbol=normalized_symbol,
            series_name=ENERGY_SERIES_NAMES.get(normalized_symbol, normalized_symbol),
            date=row.date,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
        )
        for row in history.itertuples(index=False)
    ]

    envelope = SnapshotEnvelope(
        dataset=f"energy/{normalized_symbol}",
        source="yfinance",
        fetched_at=datetime.now(UTC),
        as_of=datetime.strptime(records[-1].date.isoformat(), "%Y-%m-%d"),
        status="success",
        record_count=len(records),
        data=[record.model_dump(mode="json") for record in records],
    )
    SnapshotRepository().write_snapshot(dataset=normalized_symbol, envelope=envelope)
    return envelope
