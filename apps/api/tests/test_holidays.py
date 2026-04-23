from app.services.holiday_service import refresh_holiday_snapshot


def test_refresh_holiday_snapshot_generates_envelope_for_my_and_cn() -> None:
    envelope = refresh_holiday_snapshot(country_codes=["MY", "CN"], year=2026)

    assert envelope.dataset == "holidays"
    assert envelope.source == "python-holidays"
    assert envelope.status == "success"
    assert envelope.record_count > 0


def test_refresh_holiday_snapshot_contains_known_2026_holidays() -> None:
    envelope = refresh_holiday_snapshot(country_codes=["MY", "CN"], year=2026)

    my_dates = {row["date"] for row in envelope.data if row["country_code"] == "MY"}
    cn_dates = {row["date"] for row in envelope.data if row["country_code"] == "CN"}

    assert "2026-05-01" in my_dates
    assert "2026-01-01" in cn_dates
