from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.exceptions import ProviderError
from app.providers.sunsirs_provider import SUNSIRS_PP_URL, SunSirsProvider
from app.repositories.raw_repository import RawRepository
from app.repositories.snapshot_repository import SnapshotRepository
from app.schemas.analysis import FxSimulationResult
from app.schemas.common import SnapshotEnvelope, make_snapshot_envelope, validate_resin_record
from app.schemas.quote import ExtractedQuote
from app.scrapers.sunsirs_pp_parser import extract_sunsirs_pp_text, parse_sunsirs_pp_rows

logger = logging.getLogger(__name__)

FX_FALLBACKS_TO_MYR = {
    "MYR": 1.0,
    "USD": 4.7,
    "CNY": 0.65,
    "THB": 0.13,
    "IDR": 0.00014,
}


class ResinBenchmarkService:
    def __init__(
        self,
        *,
        provider: SunSirsProvider | None = None,
        raw_repository: RawRepository | None = None,
        snapshot_repository: SnapshotRepository | None = None,
    ) -> None:
        self.provider = provider or SunSirsProvider()
        self.raw_repository = raw_repository or RawRepository()
        self.snapshot_repository = snapshot_repository or SnapshotRepository()

    def refresh_sunsirs_snapshot(
        self,
        source_url: str = SUNSIRS_PP_URL,
        *,
        allow_partial: bool = False,
        keep_history: bool = False,
    ) -> SnapshotEnvelope:
        fetched_at = _utc_now()
        try:
            html = self.provider.fetch_pp_html(source_url)
            html_path = self._write_text_artifact("resin_html", "sunsirs_pp", ".html", html, fetched_at)
            text, extraction_method = extract_sunsirs_pp_text(html)
            text_path = self._write_text_artifact("resin_text", "sunsirs_pp", ".txt", text, fetched_at)
            scraped_records = parse_sunsirs_pp_rows(text, source_url=source_url)
        except Exception as exc:
            if allow_partial:
                return self._fallback_or_raise(f"SunSirs resin scrape failed: {exc}")
            raise ProviderError(f"SunSirs resin scrape failed: {exc}") from exc

        for record in scraped_records:
            record["raw_html_path"] = str(html_path)
            record["cleaned_text_path"] = str(text_path)
            record["extraction_method"] = extraction_method
            record["glm_context"] = self._record_glm_context(record)
            validate_resin_record(record)

        records = self._merge_with_latest_history(scraped_records)
        records = self._attach_history_context(records)

        envelope = SnapshotEnvelope(
            **make_snapshot_envelope(
                dataset="resin",
                source="SunSirs",
                fetched_at=fetched_at,
                as_of=records[0]["date_reference"],
                status="success",
                data=records,
            )
        )
        self.snapshot_repository.write_snapshot("resin", envelope, keep_history=keep_history)
        logger.info(
            "Resin snapshot written: %d records (%d scraped this run)",
            len(records),
            len(scraped_records),
        )
        return envelope

    def get_latest_benchmark_for_context(self) -> dict[str, Any] | None:
        snapshot = self.snapshot_repository.read_latest("resin")
        if snapshot is None or not snapshot.data:
            return None
        return snapshot.data[0]

    def build_market_price_risks(
        self,
        quotes: list[ExtractedQuote],
        fx_sims: dict[str, FxSimulationResult],
    ) -> list[dict[str, Any]]:
        benchmark = self.get_latest_benchmark_for_context()
        if not benchmark:
            return []

        benchmark_myr = self._to_myr_per_mt(
            float(benchmark["price_value"]),
            str(benchmark["currency"]),
            fx_sims,
        )
        risks: list[dict[str, Any]] = []
        for quote in quotes:
            if quote.unit_price is None or not quote.currency:
                continue
            quote_myr = self._to_myr_per_mt(float(quote.unit_price), quote.currency, fx_sims)
            premium_pct = ((quote_myr - benchmark_myr) / benchmark_myr) * 100.0 if benchmark_myr else 0.0
            risks.append(
                {
                    "quote_id": str(quote.quote_id),
                    "quote_price_value": quote.unit_price,
                    "quote_currency": quote.currency,
                    "quote_price_myr_per_mt": round(quote_myr, 2),
                    "benchmark_price_value": benchmark["price_value"],
                    "benchmark_currency": benchmark["currency"],
                    "benchmark_price_myr_per_mt": round(benchmark_myr, 2),
                    "premium_pct": round(premium_pct, 2),
                    "risk_label": price_risk_label(premium_pct),
                    "source_url": benchmark["source_url"],
                    "as_of": benchmark["date_reference"],
                }
            )
        return risks

    def build_price_scenario(self, horizon_days: int = 90) -> dict[str, Any] | None:
        snapshot = self.snapshot_repository.read_latest("resin")
        if snapshot is None or not snapshot.data:
            return None

        records = sorted(snapshot.data, key=lambda item: item["date_reference"])
        prices = [float(record["price_value"]) for record in records if float(record["price_value"]) > 0]
        if not prices:
            return None

        returns = [math.log(prices[index] / prices[index - 1]) for index in range(1, len(prices))]
        mean_return = statistics.fmean(returns) if returns else 0.0
        observed_vol = statistics.pstdev(returns) if len(returns) > 1 else 0.0
        vol_floor = 0.012
        daily_vol = max(observed_vol, vol_floor)
        method = "historical_log_return"
        if len(returns) < 5 or observed_vol < vol_floor:
            method = "sparse_history_with_vol_floor"

        current = prices[-1]
        p10: list[float] = []
        p50: list[float] = []
        p90: list[float] = []
        z90 = 1.28155
        for day in range(horizon_days):
            step = day + 1
            median = current * math.exp(mean_return * step)
            spread = z90 * daily_vol * math.sqrt(step)
            p10.append(round(median * math.exp(-spread), 2))
            p50.append(round(median, 2))
            p90.append(round(median * math.exp(spread), 2))

        latest = snapshot.data[0]
        recent_history = [
            {
                "date": record["date_reference"],
                "price_value": record["price_value"],
                "currency": record["currency"],
                "unit": record["unit"],
            }
            for record in sorted(snapshot.data, key=lambda item: item["date_reference"], reverse=True)[:10]
        ]
        first_price = float(prices[0])
        latest_price = float(latest["price_value"])
        history_move_pct = ((latest_price - first_price) / first_price) * 100.0 if first_price else 0.0
        return {
            "series_key": latest.get("series_key", "sunsirs.pp.wire_drawing.cn"),
            "source": "SunSirs",
            "currency": latest["currency"],
            "unit": latest["unit"],
            "current_price": latest["price_value"],
            "as_of": latest["date_reference"],
            "method": method,
            "horizon_days": horizon_days,
            "p10_envelope": p10,
            "p50_envelope": p50,
            "p90_envelope": p90,
            "recent_history": recent_history,
            "history_move_pct": round(history_move_pct, 2),
            "history_observation_count": len(prices),
            "glm_context": (
                f"SunSirs China PP benchmark is {latest_price:,.2f} {latest['unit']} as of {latest['date_reference']}. "
                f"Stored history has {len(prices)} observation(s); oldest-to-latest move is {history_move_pct:.2f}%. "
                f"Scenario method: {method}."
            ),
        }

    def _fallback_or_raise(self, message: str) -> SnapshotEnvelope:
        latest = self.snapshot_repository.read_latest("resin")
        if not settings.USE_LAST_VALID_SNAPSHOT_ON_FAILURE or latest is None:
            raise ProviderError(f"{message} and no fallback snapshot exists.")
        logger.warning("%s. Using latest resin snapshot.", message)
        return SnapshotEnvelope(
            dataset=latest.dataset,
            source=latest.source,
            fetched_at=_utc_now(),
            as_of=latest.as_of,
            status="partial",
            record_count=latest.record_count,
            data=latest.data,
        )

    def _merge_with_latest_history(self, scraped_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest = self.snapshot_repository.read_latest("resin")
        existing_records = latest.data if latest is not None else []
        merged: dict[tuple[str, str], dict[str, Any]] = {}

        for record in existing_records:
            key = (str(record.get("series_key", "")), str(record.get("date_reference", "")))
            if all(key):
                merged[key] = record

        # New scrape wins for the same series/date, which handles source corrections
        # while keeping re-scrapes idempotent when values are unchanged.
        for record in scraped_records:
            key = (str(record.get("series_key", "")), str(record.get("date_reference", "")))
            merged[key] = record

        records = sorted(
            merged.values(),
            key=lambda item: str(item["date_reference"]),
            reverse=True,
        )
        return records[:180]

    def _attach_history_context(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not records:
            return records
        ordered = sorted(records, key=lambda item: str(item["date_reference"]))
        oldest = ordered[0]
        latest = records[0]
        previous = records[1] if len(records) > 1 else None
        latest_price = float(latest["price_value"])
        oldest_price = float(oldest["price_value"])
        move_pct = ((latest_price - oldest_price) / oldest_price) * 100.0 if oldest_price else 0.0
        day_change_text = ""
        if previous:
            previous_price = float(previous["price_value"])
            day_change_pct = ((latest_price - previous_price) / previous_price) * 100.0 if previous_price else 0.0
            day_change_text = f" Previous observed price was {previous_price:,.2f} on {previous['date_reference']} ({day_change_pct:.2f}% change)."
        for record in records:
            record["history_observation_count"] = len(records)
            record["history_start_date"] = oldest["date_reference"]
            record["history_latest_date"] = latest["date_reference"]
            record["history_move_pct"] = round(move_pct, 2)
        latest["glm_context"] = (
            f"SunSirs scraped PP wire-drawing benchmark from China at {latest_price:,.2f} {latest['unit']} "
            f"on {latest['date_reference']}. History covers {len(records)} scraped observation(s) from "
            f"{oldest['date_reference']} to {latest['date_reference']}, moving {move_pct:.2f}%."
            f"{day_change_text} Source evidence: {latest.get('evidence_snippet', '')}"
        )
        return records

    @staticmethod
    def _record_glm_context(record: dict[str, Any]) -> str:
        return (
            f"SunSirs PP benchmark row: {record['price_value']:,.2f} {record['unit']} "
            f"on {record['date_reference']} for {record['region']} {record['sector']}. "
            f"Evidence: {record.get('evidence_snippet', '')}"
        )

    def _to_myr_per_mt(
        self,
        value: float,
        currency: str,
        fx_sims: dict[str, FxSimulationResult],
    ) -> float:
        normalized = currency.upper()
        if normalized == "MYR":
            return value
        pair = f"{normalized}MYR"
        rate = fx_sims[pair].current_spot if pair in fx_sims else FX_FALLBACKS_TO_MYR.get(normalized)
        if rate is None:
            raise ProviderError(f"No MYR conversion rate available for {currency}.")
        return value * rate

    def _write_text_artifact(self, dataset: str, prefix: str, suffix: str, content: str, fetched_at: str) -> Path:
        timestamp = fetched_at.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "Z")
        return self.raw_repository.write_text(dataset, f"{prefix}_{timestamp}{suffix}", content)


def price_risk_label(premium_pct: float) -> str:
    if premium_pct <= -10.0:
        return "below_market"
    if premium_pct <= 10.0:
        return "fair"
    if premium_pct <= 20.0:
        return "premium"
    return "high_premium"


def build_default_resin_service() -> ResinBenchmarkService:
    return ResinBenchmarkService()


def ensure_resin_snapshot_fresh(*, max_age_hours: int = 24) -> SnapshotEnvelope:
    service = build_default_resin_service()
    latest = service.snapshot_repository.read_latest("resin")
    if latest and latest.status == "success" and _is_fresh(str(latest.fetched_at), max_age_hours=max_age_hours):
        return latest
    return service.refresh_sunsirs_snapshot(keep_history=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_fresh(fetched_at: str, *, max_age_hours: int) -> bool:
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - fetched.astimezone(timezone.utc) <= timedelta(hours=max_age_hours)
