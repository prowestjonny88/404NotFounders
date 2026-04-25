from __future__ import annotations

import math
import os
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterator

import numpy as np
from pydantic import BaseModel

from app.core.config import settings
from app.core.constants import SUPPORTED_HS_CODE, SUPPORTED_IMPORT_COUNTRY
from app.core.exceptions import ComputationFailed, SnapshotStaleUsingLastValid
from app.core.settings import AppSettings
from app.repositories.snapshot_repository import SnapshotRepository
from app.schemas.common import SnapshotEnvelope
from app.schemas.quote import ExtractedQuote
from app.schemas.reference import FreightRate, SupplierSeed, TariffRule

try:  # pragma: no cover - optional observability dependency
    from langfuse import get_client
except ImportError:  # pragma: no cover - optional observability dependency
    get_client = None


PORT_KEYWORDS = {
    "CNNGB": {"ningbo", "zhejiang", "beilun"},
    "CNSZX": {"shenzhen", "yantian"},
    "THBKK": {"bangkok", "laem chabang"},
    "IDJKT": {"jakarta", "tanjung priok"},
}
COUNTRY_TO_DEFAULT_PORT = {
    "CN": "CNNGB",
    "TH": "THBKK",
    "ID": "IDJKT",
}
COUNTRY_KEYWORDS = {
    "CN": {"china", "ningbo", "shenzhen", "zhejiang", "yantian", "cn"},
    "TH": {"thailand", "bangkok", "laem chabang", "thai", "th"},
    "ID": {"indonesia", "jakarta", "tanjung priok", "id"},
}


class DailyBand(BaseModel):
    day: int
    p10: float
    p50: float
    p90: float


class LandedCostSimulationResult(BaseModel):
    quote_id: str
    supplier_name: str
    currency: str
    current_spot: float
    implied_vol_annualised: float
    T: int
    horizon_days: int
    daily_bands: list[DailyBand]
    p10_at_delivery: float
    p50_at_delivery: float
    p90_at_delivery: float
    lc_distribution: list[float]
    material_p10: float
    material_p50: float
    material_p90: float
    freight_p50: float
    tariff_p50: float
    moq_penalty: float
    trust_penalty: float
    n_paths: int
    rho_fx_oil: float
    rho_usdmyr_oil: float
    oil_elasticity: float
    snapshot_datasets: list[str]
    trace_url: str | None = None


@dataclass(frozen=True)
class _SeriesStats:
    dates: list[str]
    closes: np.ndarray
    return_dates: list[str]
    log_returns: np.ndarray
    mu_annual: float
    sigma_annual: float


async def simulate_landed_cost(
    quote: ExtractedQuote,
    quantity_mt: float,
    weather_delay_days: int,
    holiday_buffer_days: int,
    reference_data: dict[str, Any],
    n_paths: int | None = None,
    *,
    run_id: str | None = None,
    hedge_ratio_pct: float = 0.0,
    snapshot_repository: SnapshotRepository | None = None,
    enable_trace: bool = True,
) -> LandedCostSimulationResult:
    """
    Snapshot-only Monte Carlo landed-cost simulation.

    The model uses historical close prices from saved FX and Brent snapshots.
    It does not perform live provider calls.
    """
    if quote.unit_price is None:
        raise ComputationFailed("Quote unit_price is required for landed-cost simulation.")
    if quantity_mt <= 0:
        raise ComputationFailed("quantity_mt must be positive for landed-cost simulation.")

    repository = snapshot_repository or SnapshotRepository()
    path_count = int(n_paths or settings.MONTE_CARLO_N or 500)
    if path_count <= 0:
        raise ComputationFailed("n_paths must be positive for landed-cost simulation.")

    currency = (quote.currency or "").upper()
    if not currency:
        raise ComputationFailed("Quote currency is required for landed-cost simulation.")

    quote_pair = f"{currency}MYR"
    quote_dataset = f"fx/{quote_pair}"
    usd_dataset = "fx/USDMYR"
    oil_dataset = "energy/BZ=F"
    datasets = [oil_dataset, usd_dataset]
    if currency != "MYR":
        datasets.insert(0, quote_dataset)

    quote_stats = (
        _constant_myr_stats()
        if currency == "MYR"
        else _series_stats(_require_snapshot(repository, quote_dataset), label=quote_dataset)
    )
    usd_stats = _series_stats(_require_snapshot(repository, usd_dataset), label=usd_dataset)
    oil_stats = _series_stats(_require_snapshot(repository, oil_dataset), label=oil_dataset)

    rho_quote_oil = 0.0 if currency == "MYR" else _aligned_correlation(quote_stats, oil_stats)
    rho_usd_oil = _aligned_correlation(usd_stats, oil_stats)

    freight = _match_freight_rate(quote, reference_data)
    tariff = _match_tariff_rule(reference_data)
    supplier = _match_supplier_seed(quote, reference_data)

    calendar_exposure_days = int(quote.lead_time_days or supplier.typical_lead_days or 0)
    calendar_exposure_days += max(0, int(weather_delay_days)) + max(0, int(holiday_buffer_days))
    T = min(180, max(10, int(round(calendar_exposure_days * 5 / 7))))

    seed = _stable_seed(run_id or "adhoc", str(quote.quote_id), hedge_ratio_pct)
    rng = np.random.default_rng(seed)
    hedge = min(1.0, max(0.0, hedge_ratio_pct / 100.0))
    oil_elasticity = 0.30

    trace_input = {
        "run_id": run_id,
        "quote_id": str(quote.quote_id),
        "supplier_name": quote.supplier_name,
        "currency": currency,
        "quantity_mt": quantity_mt,
        "T": T,
        "hedge_ratio_pct": hedge_ratio_pct,
        "n_paths": path_count,
        "snapshot_datasets": datasets,
    }
    trace_url: str | None = None
    span_context = _langfuse_span("lintasniaga-fx-oil-monte-carlo", trace_input) if enable_trace else nullcontext(None)
    with span_context as span:
        oil_z = rng.normal(0.0, 1.0, size=(path_count, T))
        quote_z = _correlate_with_oil(
            rng=rng,
            oil_z=oil_z,
            rho=rho_quote_oil,
            n_paths=path_count,
            T=T,
        )
        usd_z = (
            quote_z
            if currency == "USD"
            else _correlate_with_oil(rng=rng, oil_z=oil_z, rho=rho_usd_oil, n_paths=path_count, T=T)
        )

        quote_paths = (
            np.ones((path_count, T + 1), dtype=float)
            if currency == "MYR"
            else _gbm_paths(
                spot=float(quote_stats.closes[-1]),
                mu_annual=quote_stats.mu_annual,
                sigma_annual=quote_stats.sigma_annual,
                z=quote_z,
            )
        )
        usd_paths = (
            quote_paths
            if currency == "USD"
            else _gbm_paths(
                spot=float(usd_stats.closes[-1]),
                mu_annual=usd_stats.mu_annual,
                sigma_annual=usd_stats.sigma_annual,
                z=usd_z,
            )
        )
        oil_paths = _gbm_paths(
            spot=float(oil_stats.closes[-1]),
            mu_annual=oil_stats.mu_annual,
            sigma_annual=oil_stats.sigma_annual,
            z=oil_z,
        )

        effective_quote_fx = hedge * float(quote_stats.closes[-1]) + (1.0 - hedge) * quote_paths
        effective_usd_fx = effective_quote_fx if currency == "USD" else usd_paths

        material_all = float(quote.unit_price) * float(quantity_mt) * effective_quote_fx
        surcharge_all = np.maximum(0.0, (oil_paths / oil_paths[:, [0]] - 1.0) * oil_elasticity)
        freight_base_usd = _freight_base_usd(freight, quantity_mt)
        freight_all = freight_base_usd * (1.0 + surcharge_all) * effective_usd_fx
        tariff_rate = float(tariff.tariff_rate_pct) / 100.0
        tariff_all = material_all * tariff_rate

        moq_excess = max(0.0, float(quote.moq or 0) - float(quantity_mt))
        moq_penalty = moq_excess * float(quote.unit_price) * float(quote_stats.closes[-1]) * 0.15
        reliability = float(supplier.reliability_score or 0.80)
        trust_penalty = (1.0 - reliability) * float(np.mean(material_all[:, -1])) * 0.02

        landed_all = material_all + freight_all + tariff_all + moq_penalty + trust_penalty
        percentiles = np.percentile(landed_all, [10, 50, 90], axis=0)
        material_percentiles = np.percentile(material_all[:, -1], [10, 50, 90])
        freight_p50 = float(np.percentile(freight_all[:, -1], 50))
        tariff_p50 = float(np.percentile(tariff_all[:, -1], 50))
        delivery_distribution = landed_all[:, -1]

        result = LandedCostSimulationResult(
            quote_id=str(quote.quote_id),
            supplier_name=quote.supplier_name or "Unknown supplier",
            currency=currency,
            current_spot=float(quote_stats.closes[-1]),
            implied_vol_annualised=float(quote_stats.sigma_annual),
            T=T,
            horizon_days=T,
            daily_bands=[
                DailyBand(
                    day=day,
                    p10=round(float(percentiles[0, day]), 2),
                    p50=round(float(percentiles[1, day]), 2),
                    p90=round(float(percentiles[2, day]), 2),
                )
                for day in range(T + 1)
            ],
            p10_at_delivery=round(float(np.percentile(delivery_distribution, 10)), 2),
            p50_at_delivery=round(float(np.percentile(delivery_distribution, 50)), 2),
            p90_at_delivery=round(float(np.percentile(delivery_distribution, 90)), 2),
            lc_distribution=[round(float(value), 2) for value in delivery_distribution.tolist()],
            material_p10=round(float(material_percentiles[0]), 2),
            material_p50=round(float(material_percentiles[1]), 2),
            material_p90=round(float(material_percentiles[2]), 2),
            freight_p50=round(freight_p50, 2),
            tariff_p50=round(tariff_p50, 2),
            moq_penalty=round(float(moq_penalty), 2),
            trust_penalty=round(float(trust_penalty), 2),
            n_paths=path_count,
            rho_fx_oil=round(float(rho_quote_oil), 4),
            rho_usdmyr_oil=round(float(rho_usd_oil), 4),
            oil_elasticity=oil_elasticity,
            snapshot_datasets=datasets,
        )
        trace_output = {
            "p10_at_delivery": result.p10_at_delivery,
            "p50_at_delivery": result.p50_at_delivery,
            "p90_at_delivery": result.p90_at_delivery,
            "implied_vol_annualised": result.implied_vol_annualised,
            "rho_fx_oil": result.rho_fx_oil,
            "rho_usdmyr_oil": result.rho_usdmyr_oil,
        }
        trace_url = _update_span(span, output=trace_output)
        if trace_url:
            result.trace_url = trace_url
        return result


def _require_snapshot(repository: SnapshotRepository, dataset: str) -> SnapshotEnvelope:
    envelope = repository.read_latest(dataset)
    if envelope is None:
        raise SnapshotStaleUsingLastValid(f"Missing required snapshot: {dataset}")
    if envelope.status != "success":
        raise SnapshotStaleUsingLastValid(
            f"Snapshot {dataset} status is {envelope.status}; expected success."
        )
    if len(envelope.data or []) < 30:
        raise SnapshotStaleUsingLastValid(
            f"Snapshot {dataset} has {len(envelope.data or [])} rows; expected at least 30."
        )
    return envelope


def _series_stats(envelope: SnapshotEnvelope, *, label: str) -> _SeriesStats:
    rows = sorted(envelope.data, key=lambda item: str(item.get("date", "")))
    dates: list[str] = []
    closes: list[float] = []
    for row in rows:
        try:
            close = float(row["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotStaleUsingLastValid(f"Snapshot {label} contains an invalid close value.") from exc
        if close <= 0:
            raise SnapshotStaleUsingLastValid(f"Snapshot {label} contains non-positive close value.")
        dates.append(str(row["date"]))
        closes.append(close)
    if len(closes) < 30:
        raise SnapshotStaleUsingLastValid(f"Snapshot {label} needs at least 30 close prices.")

    close_arr = np.asarray(closes, dtype=float)
    returns = np.diff(np.log(close_arr))
    if len(returns) < 29:
        raise SnapshotStaleUsingLastValid(f"Snapshot {label} needs at least 29 log returns.")
    sigma_daily = float(np.std(returns))
    return _SeriesStats(
        dates=dates,
        closes=close_arr,
        return_dates=dates[1:],
        log_returns=returns,
        mu_annual=float(np.mean(returns) * 252.0),
        sigma_annual=float(sigma_daily * math.sqrt(252.0)),
    )


def _constant_myr_stats() -> _SeriesStats:
    closes = np.ones(30, dtype=float)
    returns = np.zeros(29, dtype=float)
    return _SeriesStats(
        dates=[date.today().isoformat()] * 30,
        closes=closes,
        return_dates=[date.today().isoformat()] * 29,
        log_returns=returns,
        mu_annual=0.0,
        sigma_annual=0.0,
    )


def _aligned_correlation(left: _SeriesStats, right: _SeriesStats) -> float:
    left_by_date = dict(zip(left.return_dates, left.log_returns, strict=False))
    right_by_date = dict(zip(right.return_dates, right.log_returns, strict=False))
    common_dates = sorted(set(left_by_date) & set(right_by_date))
    if len(common_dates) < 30:
        raise SnapshotStaleUsingLastValid(
            f"Not enough overlapping dates to compute FX/oil correlation: {len(common_dates)}."
        )
    left_returns = np.asarray([left_by_date[item] for item in common_dates], dtype=float)
    right_returns = np.asarray([right_by_date[item] for item in common_dates], dtype=float)
    if float(np.std(left_returns)) == 0.0 or float(np.std(right_returns)) == 0.0:
        return 0.0
    rho = float(np.corrcoef(left_returns, right_returns)[0, 1])
    if math.isnan(rho):
        return 0.0
    return max(-0.95, min(0.95, rho))


def _correlate_with_oil(
    *,
    rng: np.random.Generator,
    oil_z: np.ndarray,
    rho: float,
    n_paths: int,
    T: int,
) -> np.ndarray:
    eps = rng.normal(0.0, 1.0, size=(n_paths, T))
    rho = max(-0.95, min(0.95, float(rho)))
    return rho * oil_z + math.sqrt(1.0 - rho**2) * eps


def _gbm_paths(*, spot: float, mu_annual: float, sigma_annual: float, z: np.ndarray) -> np.ndarray:
    n_paths, T = z.shape
    dt = 1.0 / 252.0
    increments = (mu_annual - 0.5 * sigma_annual**2) * dt + sigma_annual * math.sqrt(dt) * z
    log_paths = np.cumsum(increments, axis=1)
    paths = np.empty((n_paths, T + 1), dtype=float)
    paths[:, 0] = spot
    paths[:, 1:] = spot * np.exp(log_paths)
    return paths


def _freight_base_usd(freight: FreightRate, quantity_mt: float) -> float:
    if freight.currency.upper() != "USD":
        raise ComputationFailed(f"Freight currency must be USD; got {freight.currency}.")
    if freight.rate_unit.lower() == "mt":
        return float(freight.rate_value) * float(quantity_mt)
    return float(freight.rate_value) * (float(quantity_mt) / 20.0)


def _match_freight_rate(quote: ExtractedQuote, reference_data: dict[str, Any]) -> FreightRate:
    rates = [_as_model(FreightRate, item) for item in reference_data.get("freight_rates", [])]
    supplier = _match_supplier_seed(quote, reference_data)
    origin_country = _infer_origin_country(quote.origin_port_or_country) or supplier.country_code
    origin_port = _infer_origin_port_code(quote.origin_port_or_country, origin_country)
    for rate in rates:
        if rate.destination_port != "MYPKG":
            continue
        if origin_country and rate.origin_country != origin_country:
            continue
        if origin_port and rate.origin_port != origin_port:
            continue
        return rate
    if origin_country:
        match = next((rate for rate in rates if rate.origin_country == origin_country), None)
        if match:
            return match
    raise ComputationFailed("No freight rate found for corridor")


def _match_tariff_rule(reference_data: dict[str, Any]) -> TariffRule:
    tariffs = [_as_model(TariffRule, item) for item in reference_data.get("tariffs", [])]
    match = next(
        (
            tariff
            for tariff in tariffs
            if tariff.hs_code == SUPPORTED_HS_CODE and tariff.import_country == SUPPORTED_IMPORT_COUNTRY
        ),
        None,
    )
    if match is None:
        raise ComputationFailed(f"No tariff rule found for HS {SUPPORTED_HS_CODE}.")
    return match


def _match_supplier_seed(quote: ExtractedQuote, reference_data: dict[str, Any]) -> SupplierSeed:
    seeds = [_as_model(SupplierSeed, item) for item in reference_data.get("supplier_seeds", [])]
    if quote.supplier_name:
        normalized = quote.supplier_name.strip().lower()
        exact = next((seed for seed in seeds if seed.supplier_name.strip().lower() == normalized), None)
        if exact:
            return exact
        fuzzy = next(
            (
                seed
                for seed in seeds
                if seed.supplier_name.strip().lower() in normalized
                or normalized in seed.supplier_name.strip().lower()
            ),
            None,
        )
        if fuzzy:
            return fuzzy
    origin_country = _infer_origin_country(quote.origin_port_or_country)
    if origin_country:
        candidates = [seed for seed in seeds if seed.country_code == origin_country]
        if candidates:
            return min(
                candidates,
                key=lambda seed: abs(seed.typical_lead_days - (quote.lead_time_days or seed.typical_lead_days)),
            )
    return SupplierSeed(
        supplier_name=quote.supplier_name or "Unknown supplier",
        country_code=origin_country or "CN",
        port=COUNTRY_TO_DEFAULT_PORT.get(origin_country or "CN", "CNNGB"),
        reliability_score=0.80,
        typical_lead_days=quote.lead_time_days or 30,
        notes="Default reliability used because supplier seed was not found.",
    )


def _infer_origin_country(origin_port_or_country: str | None) -> str | None:
    if not origin_port_or_country:
        return None
    normalized = origin_port_or_country.strip().lower()
    for country_code, keywords in COUNTRY_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return country_code
    return None


def _infer_origin_port_code(origin_port_or_country: str | None, origin_country: str | None) -> str | None:
    if origin_port_or_country:
        normalized = origin_port_or_country.strip().lower()
        for port_code, keywords in PORT_KEYWORDS.items():
            if any(keyword in normalized for keyword in keywords):
                return port_code
    if origin_country:
        return COUNTRY_TO_DEFAULT_PORT.get(origin_country)
    return None


def _as_model(model_type: type[Any], item: Any) -> Any:
    if isinstance(item, model_type):
        return item
    return model_type(**item)


def _stable_seed(run_id: str, quote_id: str, hedge_ratio_pct: float) -> int:
    # Hedge intentionally does not enter the seed; slider replay narrows the
    # same shocks instead of rerolling a new future.
    import hashlib

    digest = hashlib.sha256(f"{run_id}:{quote_id}:fx-oil-v1".encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % (2**32)


@contextmanager
def _langfuse_span(name: str, input_payload: dict[str, Any]) -> Iterator[Any]:
    if get_client is None:
        yield None
        return
    _ensure_langfuse_env()
    try:
        client = get_client()
        starter = getattr(client, "start_as_current_observation", None)
    except Exception:
        # Observability must never corrupt procurement math.
        yield None
        return
    if starter is None:
        yield None
        return
    with starter(as_type="span", name=name, input=input_payload) as span:
        try:
            yield span
        finally:
            flush = getattr(client, "flush", None)
            if callable(flush):
                flush()


def _update_span(span: Any, *, output: dict[str, Any]) -> str | None:
    if span is None:
        return None
    try:
        span.update(output=output)
    except Exception:
        return None
    try:
        trace_id = getattr(span, "trace_id", None) or getattr(span, "trace_id_", None)
        host = os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST")
        project_id = os.getenv("LANGFUSE_PROJECT_ID")
        if trace_id and host and project_id:
            return f"{host.rstrip('/')}/project/{project_id}/traces/{trace_id}"
    except Exception:
        return None
    return None


def _ensure_langfuse_env() -> None:
    AppSettings.from_env()
    host = os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST")
    if host and not os.getenv("LANGFUSE_BASE_URL"):
        os.environ["LANGFUSE_BASE_URL"] = host
