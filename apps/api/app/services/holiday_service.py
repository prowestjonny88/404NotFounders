from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import holidays

from app.core.constants import DEFAULT_HOLIDAY_COUNTRIES
from app.core.exceptions import ProviderError
from app.repositories.snapshot_repository import SnapshotRepository
from app.schemas.common import SnapshotEnvelope, validate_holiday_record

COUNTRY_NAMES = {
    "MY": "Malaysia",
    "CN": "China",
    "TH": "Thailand",
    "ID": "Indonesia",
}


def refresh_holiday_snapshot(
    country_codes: list[str] | tuple[str, ...] = DEFAULT_HOLIDAY_COUNTRIES,
    year: int | None = None,
    *,
    start_date: date | None = None,
    window_days: int = 365,
    procurement_window_days: int = 30,
    keep_history: bool = False,
) -> SnapshotEnvelope:
    """
    Build a forward procurement holiday calendar from python-holidays.

    Default behavior is rolling: today through the next 365 days for Malaysia,
    China, Thailand, and Indonesia. Tests can still pass year=2026 to build a
    calendar-year snapshot.
    """
    fetched_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if year is not None and start_date is None:
        start_date = date(year, 1, 1)
    start = start_date or date.today()
    end = start + timedelta(days=window_days)
    years = set(range(start.year, end.year + 1))

    records: list[dict[str, Any]] = []
    country_summaries: list[dict[str, Any]] = []

    for country_code in country_codes:
        try:
            calendar = holidays.country_holidays(country_code, years=years)
        except Exception as exc:
            raise ProviderError(f"Unable to build holiday calendar for {country_code}: {exc}") from exc

        country_records: list[dict[str, Any]] = []
        holiday_items = [
            (holiday_date, str(holiday_name))
            for holiday_date, holiday_name in sorted(calendar.items())
            if start <= holiday_date < end
        ]
        holiday_dates = [item[0] for item in holiday_items]
        for holiday_date, holiday_name in holiday_items:
            days_from_start = (holiday_date - start).days
            is_long_weekend = holiday_date.weekday() in {0, 4}
            within_procurement_window = days_from_start <= procurement_window_days
            record = {
                "country_code": country_code,
                "country_name": COUNTRY_NAMES.get(country_code, country_code),
                "date": holiday_date.isoformat(),
                "holiday_name": holiday_name,
                "is_holiday": True,
                "is_long_weekend": is_long_weekend,
                "days_until_next_holiday": days_from_start,
                "days_from_snapshot_start": days_from_start,
                "within_procurement_window": within_procurement_window,
                "procurement_window_days": procurement_window_days,
                "lead_time_risk": "holiday_delay" if within_procurement_window else "calendar_watch",
                "glm_context": (
                    f"{COUNTRY_NAMES.get(country_code, country_code)} has '{holiday_name}' on "
                    f"{holiday_date.isoformat()}, {days_from_start} day(s) from the analysis date. "
                    f"{'This falls inside' if within_procurement_window else 'This is outside'} the "
                    f"{procurement_window_days}-day procurement window."
                ),
            }
            validate_holiday_record(record)
            records.append(record)
            country_records.append(record)

        next_holiday = country_records[0] if country_records else None
        window_holidays = [record for record in country_records if record["within_procurement_window"]]
        country_summaries.append(
            {
                "country_code": country_code,
                "country_name": COUNTRY_NAMES.get(country_code, country_code),
                "holiday_count": len(country_records),
                "procurement_window_holiday_count": len(window_holidays),
                "next_holiday_date": next_holiday["date"] if next_holiday else "",
                "next_holiday_name": next_holiday["holiday_name"] if next_holiday else "",
                "next_holiday_days": next_holiday["days_from_snapshot_start"] if next_holiday else None,
            }
        )

    records.sort(key=lambda row: (row["date"], row["country_code"]))
    envelope = SnapshotEnvelope(
        dataset="holidays",
        source="python-holidays",
        fetched_at=fetched_at,
        as_of=start.isoformat(),
        status="success",
        record_count=len(records),
        data=records,
    )
    # Extra metadata lives inside data records for schema compatibility; this
    # summary is duplicated as a synthetic record only for API callers that need
    # country-level context without recomputing counts.
    envelope.data.insert(
        0,
        {
            "country_code": "ALL",
            "country_name": "Procurement countries",
            "date": start.isoformat(),
            "holiday_name": "Procurement holiday summary",
            "is_holiday": False,
            "is_long_weekend": False,
            "days_until_next_holiday": 0,
            "days_from_snapshot_start": 0,
            "within_procurement_window": False,
            "procurement_window_days": procurement_window_days,
            "lead_time_risk": "summary",
            "country_summaries": country_summaries,
            "glm_context": _summary_glm_context(country_summaries, procurement_window_days),
        },
    )
    envelope.record_count = len(envelope.data)
    SnapshotRepository().write_snapshot("holidays", envelope, keep_history=keep_history)
    return envelope


def ensure_holiday_snapshot_fresh(
    *,
    max_age_hours: int = 24,
    country_codes: list[str] | tuple[str, ...] = DEFAULT_HOLIDAY_COUNTRIES,
    window_days: int = 365,
    procurement_window_days: int = 30,
) -> SnapshotEnvelope:
    latest = SnapshotRepository().read_latest("holidays")
    if latest and latest.status == "success" and _is_fresh(latest.fetched_at, max_age_hours=max_age_hours):
        return latest
    return refresh_holiday_snapshot(
        country_codes=country_codes,
        window_days=window_days,
        procurement_window_days=procurement_window_days,
        keep_history=False,
    )


def _summary_glm_context(country_summaries: list[dict[str, Any]], procurement_window_days: int) -> str:
    affected = [
        f"{item['country_name']}: {item['procurement_window_holiday_count']} holiday(s), next "
        f"{item['next_holiday_name'] or 'none'} on {item['next_holiday_date'] or 'n/a'}"
        for item in country_summaries
        if item["procurement_window_holiday_count"]
    ]
    if not affected:
        return f"No Malaysia/China/Thailand/Indonesia public holidays fall inside the next {procurement_window_days} days."
    return f"Public holidays inside the next {procurement_window_days} days: " + "; ".join(affected) + "."


def _is_fresh(fetched_at: str, *, max_age_hours: int) -> bool:
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(UTC) - fetched.astimezone(UTC) <= timedelta(hours=max_age_hours)
