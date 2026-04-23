from uuid import uuid4

from app.schemas.quote import ExtractedQuote
from app.services.quote_validation_service import validate_quote


def make_quote(**overrides) -> ExtractedQuote:
    payload = {
        "quote_id": uuid4(),
        "upload_id": uuid4(),
        "supplier_name": "Ningbo Precision Plastics Co. Ltd.",
        "origin_port_or_country": "Ningbo, China",
        "incoterm": "FOB",
        "unit_price": 1180.0,
        "currency": "USD",
        "moq": 80,
        "lead_time_days": 35,
        "payment_terms": "T/T 30 days",
        "extraction_confidence": 0.92,
    }
    payload.update(overrides)
    return ExtractedQuote(**payload)


def test_valid_quote_passes_validation() -> None:
    result = validate_quote(make_quote())
    assert result.status == "valid"
    assert result.reason_codes == []
    assert result.missing_fields == []


def test_non_fob_quote_is_out_of_scope() -> None:
    result = validate_quote(make_quote(incoterm="CIF"))
    assert result.status == "invalid_out_of_scope"
    assert "unsupported_incoterm" in result.reason_codes


def test_missing_required_fields_is_fixable() -> None:
    result = validate_quote(make_quote(unit_price=None, moq=None))
    assert result.status == "invalid_fixable"
    assert "missing_required_fields" in result.reason_codes
    assert "unit_price" in result.missing_fields
    assert "moq" in result.missing_fields


def test_unsupported_currency_is_out_of_scope() -> None:
    result = validate_quote(make_quote(currency="EUR"))
    assert result.status == "invalid_out_of_scope"
    assert "unsupported_currency" in result.reason_codes


def test_unsupported_origin_is_out_of_scope() -> None:
    result = validate_quote(make_quote(origin_port_or_country="Ho Chi Minh, Vietnam"))
    assert result.status == "invalid_out_of_scope"
    assert "unsupported_origin" in result.reason_codes
