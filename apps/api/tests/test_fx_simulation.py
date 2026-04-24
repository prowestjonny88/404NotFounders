import asyncio
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import numpy as np
import pytest

from app.core.exceptions import SnapshotStaleUsingLastValid
from app.schemas.common import SnapshotEnvelope
from app.schemas.quote import ExtractedQuote
from app.schemas.reference import FreightRate, SupplierSeed, TariffRule
from app.services.fx_simulation_service import simulate_landed_cost


class _FakeSnapshotRepository:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.requested = []

    def read_latest(self, dataset: str):
        self.requested.append(dataset)
        return self.snapshots.get(dataset)


def _snapshot(dataset: str, source: str, rows: list[dict]) -> SnapshotEnvelope:
    return SnapshotEnvelope(
        dataset=dataset,
        source=source,
        fetched_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        as_of=rows[-1]["date"],
        status="success",
        record_count=len(rows),
        data=rows,
    )


def _market_rows(*, key: str, start_close: float, sigma: float, drift: float = 0.0002, n: int = 220):
    rng = np.random.default_rng(123)
    returns = rng.normal(drift, sigma, size=n)
    closes = start_close * np.exp(np.cumsum(returns))
    start = date(2025, 1, 1)
    rows = []
    for idx, close in enumerate(closes):
        day = start + timedelta(days=idx)
        rows.append(
            {
                "pair" if key != "BZ=F" else "symbol": key,
                "series_name": "Brent Crude" if key == "BZ=F" else key,
                "date": day.isoformat(),
                "open": float(close * 0.998),
                "high": float(close * 1.003),
                "low": float(close * 0.995),
                "close": float(close),
            }
        )
    return rows


def _snapshots(*, cny_sigma: float = 0.004):
    return {
        "fx/USDMYR": _snapshot("fx/USDMYR", "yfinance", _market_rows(key="USDMYR", start_close=4.45, sigma=0.003)),
        "fx/CNYMYR": _snapshot("fx/CNYMYR", "yfinance", _market_rows(key="CNYMYR", start_close=0.62, sigma=cny_sigma)),
        "energy/BZ=F": _snapshot("energy/BZ=F", "yfinance", _market_rows(key="BZ=F", start_close=78.0, sigma=0.012)),
    }


def _reference_data():
    return {
        "freight_rates": [
            FreightRate(
                origin_country="CN",
                origin_port="CNNGB",
                destination_port="MYPKG",
                incoterm="FOB",
                currency="USD",
                rate_value=82.0,
                rate_unit="mt",
                valid_from=date(2026, 1, 1),
                valid_to=date(2026, 12, 31),
                source_note="test",
            )
        ],
        "tariffs": [
            TariffRule(
                hs_code="3902.10",
                product_name="PP Resin",
                import_country="MY",
                tariff_rate_pct=5.0,
                tariff_type="MFN",
                source_note="test",
            )
        ],
        "supplier_seeds": [
            SupplierSeed(
                supplier_name="Sinopec Trading (Shenzhen)",
                country_code="CN",
                port="CNNGB",
                reliability_score=0.9,
                typical_lead_days=30,
                notes="test",
            )
        ],
    }


def _quote(*, currency: str = "CNY", moq: int = 80, lead_time_days: int = 30) -> ExtractedQuote:
    return ExtractedQuote(
        quote_id=uuid4(),
        upload_id=uuid4(),
        supplier_name="Sinopec Trading (Shenzhen)",
        origin_port_or_country="China Ningbo",
        incoterm="FOB",
        unit_price=840.0 if currency == "USD" else 6000.0,
        currency=currency,
        moq=moq,
        lead_time_days=lead_time_days,
        payment_terms="30 days",
    )


def _run(coro):
    return asyncio.run(coro)


def _simulate(*args, **kwargs):
    kwargs.setdefault("enable_trace", False)
    return simulate_landed_cost(*args, **kwargs)


def test_p10_less_than_p50_less_than_p90():
    result = _run(
        _simulate(
            _quote(),
            quantity_mt=100,
            weather_delay_days=0,
            holiday_buffer_days=0,
            reference_data=_reference_data(),
            n_paths=800,
            run_id="ordering",
            snapshot_repository=_FakeSnapshotRepository(_snapshots()),
        )
    )

    assert result.p10_at_delivery < result.p50_at_delivery < result.p90_at_delivery


def test_longer_T_widens_bands():
    repo = _FakeSnapshotRepository(_snapshots(cny_sigma=0.012))
    quote = _quote(lead_time_days=14)
    short = _run(
        _simulate(
            quote,
            quantity_mt=100,
            weather_delay_days=0,
            holiday_buffer_days=0,
            reference_data=_reference_data(),
            n_paths=1000,
            run_id="same-run",
            snapshot_repository=repo,
        )
    )
    long = _run(
        _simulate(
            quote.model_copy(update={"lead_time_days": 90}),
            quantity_mt=100,
            weather_delay_days=0,
            holiday_buffer_days=0,
            reference_data=_reference_data(),
            n_paths=1000,
            run_id="same-run",
            snapshot_repository=repo,
        )
    )

    assert (long.p90_at_delivery - long.p10_at_delivery) > (short.p90_at_delivery - short.p10_at_delivery)


def test_higher_vol_widens_bands():
    quote = _quote()
    low = _run(
        _simulate(
            quote,
            quantity_mt=100,
            weather_delay_days=0,
            holiday_buffer_days=0,
            reference_data=_reference_data(),
            n_paths=1000,
            run_id="vol-test",
            snapshot_repository=_FakeSnapshotRepository(_snapshots(cny_sigma=0.002)),
        )
    )
    high = _run(
        _simulate(
            quote,
            quantity_mt=100,
            weather_delay_days=0,
            holiday_buffer_days=0,
            reference_data=_reference_data(),
            n_paths=1000,
            run_id="vol-test",
            snapshot_repository=_FakeSnapshotRepository(_snapshots(cny_sigma=0.03)),
        )
    )

    assert (high.p90_at_delivery - high.p10_at_delivery) > (low.p90_at_delivery - low.p10_at_delivery)


def test_moq_penalty_zero_when_qty_exceeds_moq():
    result = _run(
        _simulate(
            _quote(moq=80),
            quantity_mt=100,
            weather_delay_days=0,
            holiday_buffer_days=0,
            reference_data=_reference_data(),
            n_paths=500,
            run_id="moq",
            snapshot_repository=_FakeSnapshotRepository(_snapshots()),
        )
    )

    assert result.moq_penalty == 0


def test_cny_quote_uses_cnymyr_fx():
    repo = _FakeSnapshotRepository(_snapshots())
    result = _run(
        _simulate(
            _quote(currency="CNY"),
            quantity_mt=100,
            weather_delay_days=0,
            holiday_buffer_days=0,
            reference_data=_reference_data(),
            n_paths=500,
            run_id="cny",
            snapshot_repository=repo,
        )
    )

    assert "fx/CNYMYR" in repo.requested
    assert "fx/USDMYR" in repo.requested
    assert result.current_spot < 1.0
    assert result.material_p50 < 1_000_000


def test_hedge_reuses_shocks_and_narrows_fx_band():
    quote = _quote(currency="USD")
    repo = _FakeSnapshotRepository(_snapshots())
    unhedged = _run(
        _simulate(
            quote,
            quantity_mt=100,
            weather_delay_days=0,
            holiday_buffer_days=0,
            reference_data=_reference_data(),
            n_paths=800,
            run_id="hedge-stability",
            hedge_ratio_pct=0,
            snapshot_repository=repo,
        )
    )
    unhedged_repeat = _run(
        _simulate(
            quote,
            quantity_mt=100,
            weather_delay_days=0,
            holiday_buffer_days=0,
            reference_data=_reference_data(),
            n_paths=800,
            run_id="hedge-stability",
            hedge_ratio_pct=0,
            snapshot_repository=repo,
        )
    )
    hedged = _run(
        _simulate(
            quote,
            quantity_mt=100,
            weather_delay_days=0,
            holiday_buffer_days=0,
            reference_data=_reference_data(),
            n_paths=800,
            run_id="hedge-stability",
            hedge_ratio_pct=100,
            snapshot_repository=repo,
        )
    )

    assert unhedged.daily_bands == unhedged_repeat.daily_bands
    unhedged_width = unhedged.p90_at_delivery - unhedged.p10_at_delivery
    hedged_width = hedged.p90_at_delivery - hedged.p10_at_delivery
    assert hedged_width < unhedged_width


def test_missing_snapshot_raises_typed_error():
    with pytest.raises(SnapshotStaleUsingLastValid):
        _run(
            _simulate(
                _quote(),
                quantity_mt=100,
                weather_delay_days=0,
                holiday_buffer_days=0,
                reference_data=_reference_data(),
                n_paths=500,
                run_id="missing",
                snapshot_repository=_FakeSnapshotRepository({}),
            )
        )
