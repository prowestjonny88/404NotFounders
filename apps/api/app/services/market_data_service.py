from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.constants import DEFAULT_ENERGY_SERIES, DEFAULT_FX_PAIR_TICKERS
from app.core.exceptions import ProviderError
from app.providers.yfinance_provider import (
    YFinanceMarketDataProvider,
    fetch_energy_history,
    fetch_fx_history,
)
from app.repositories.snapshot_repository import SnapshotRepository
from app.schemas.common import (
    SnapshotEnvelope,
    make_snapshot_envelope,
    validate_energy_record,
    validate_fx_record,
)
from app.schemas.market import EnergySnapshotRecord, FXSnapshotRecord


class MarketDataService:
    def __init__(self, provider: Any, snapshot_repository: SnapshotRepository) -> None:
        self.provider = provider
        self.snapshot_repository = snapshot_repository

    def ingest_fx(self, pair_tickers: dict[str, str] | None = None) -> dict[str, Any]:
        pair_tickers = pair_tickers or DEFAULT_FX_PAIR_TICKERS
        return self._ingest_market_dataset(
            dataset="fx",
            source="yfinance",
            item_map=pair_tickers,
            record_builder=self._build_fx_record,
            record_validator=validate_fx_record,
        )

    def ingest_energy(self, series_tickers: dict[str, str] | None = None) -> dict[str, Any]:
        series_tickers = series_tickers or DEFAULT_ENERGY_SERIES
        return self._ingest_market_dataset(
            dataset="energy",
            source="yfinance",
            item_map=series_tickers,
            record_builder=self._build_energy_record,
            record_validator=validate_energy_record,
        )

    def _ingest_market_dataset(
        self,
        *,
        dataset: str,
        source: str,
        item_map: dict[str, str],
        record_builder: Any,
        record_validator: Any,
    ) -> dict[str, Any]:
        fetched_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        records: list[dict[str, Any]] = []
        try:
            for external_name, ticker in item_map.items():
                history_rows = self.provider.fetch_history(ticker)
                for row in history_rows:
                    normalized = record_builder(external_name, row)
                    record_validator(normalized)
                    records.append(normalized)
            as_of = max(record["date"] for record in records) if records else None
            envelope = make_snapshot_envelope(
                dataset=dataset,
                source=source,
                fetched_at=fetched_at,
                as_of=as_of,
                status="success",
                data=records,
            )
            path = self.snapshot_repository.write_snapshot(dataset, envelope)
            return {
                "status": "success",
                "snapshot_path": str(path),
                "record_count": len(records),
                "fetched_at": fetched_at,
            }
        except Exception as exc:
            latest = self.snapshot_repository.load_latest(dataset)
            if latest is None:
                raise ProviderError(
                    f"{dataset} ingestion failed and no last-known-good snapshot exists: {exc}"
                ) from exc
            return {
                "status": "failed",
                "warning": str(exc),
                "fallback_snapshot_path": f"{dataset}/latest.json",
                "record_count": latest["record_count"],
                "fetched_at": fetched_at,
            }

    @staticmethod
    def _build_fx_record(pair: str, row: dict[str, Any]) -> dict[str, Any]:
        record = {
            "pair": pair,
            "date": row["date"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        if row.get("volume") is not None:
            record["volume_optional"] = float(row["volume"])
        return record

    @staticmethod
    def _build_energy_record(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "series_name": DEFAULT_ENERGY_SERIES.get(symbol, symbol),
            "date": row["date"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
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
        fetched_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        as_of=records[-1].date.isoformat(),
        status="success",
        record_count=len(records),
        data=[record.model_dump(mode="json") for record in records],
    )
    SnapshotRepository().write_snapshot(dataset=f"fx/{normalized_pair}", envelope=envelope)
    return envelope


async def refresh_energy_snapshot(symbol: str) -> SnapshotEnvelope:
    normalized_symbol = symbol.upper()
    history = await fetch_energy_history(symbol=normalized_symbol)
    records = [
        EnergySnapshotRecord(
            symbol=normalized_symbol,
            series_name=DEFAULT_ENERGY_SERIES.get(normalized_symbol, normalized_symbol),
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
        fetched_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        as_of=records[-1].date.isoformat(),
        status="success",
        record_count=len(records),
        data=[record.model_dump(mode="json") for record in records],
    )
    SnapshotRepository().write_snapshot(dataset=f"energy/{normalized_symbol}", envelope=envelope)
    return envelope


def build_default_market_service() -> MarketDataService:
    return MarketDataService(YFinanceMarketDataProvider(), SnapshotRepository())
