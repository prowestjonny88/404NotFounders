from __future__ import annotations

from app.core.constants import SUPPORTED_INCOTERM
from app.schemas.quote import ExtractedQuote, QuoteValidationResult

SUPPORTED_CURRENCIES = {"USD", "CNY", "THB", "IDR"}

ORIGIN_KEYWORDS = {
    "CN": {"china", "ningbo", "shenzhen", "yantian", "zhejiang"},
    "TH": {"thailand", "bangkok", "laem chabang", "rayong"},
    "ID": {"indonesia", "jakarta", "tanjung priok"},
}


def _infer_origin_country(origin_port_or_country: str | None) -> str | None:
    if not origin_port_or_country:
        return None
    normalized = origin_port_or_country.strip().lower()
    for country_code, keywords in ORIGIN_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return country_code
    return None


def validate_quote(quote: ExtractedQuote) -> QuoteValidationResult:
    reason_codes: list[str] = []
    missing_fields: list[str] = []
    out_of_scope = False

    required_fields = {
        "supplier_name": quote.supplier_name,
        "unit_price": quote.unit_price,
        "moq": quote.moq,
        "lead_time_days": quote.lead_time_days,
    }
    for field_name, value in required_fields.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            missing_fields.append(field_name)

    if not quote.incoterm:
        missing_fields.append("incoterm")
    elif quote.incoterm.strip().upper() != SUPPORTED_INCOTERM:
        out_of_scope = True
        reason_codes.append("unsupported_incoterm")

    if not quote.currency:
        missing_fields.append("currency")
    elif quote.currency.strip().upper() not in SUPPORTED_CURRENCIES:
        out_of_scope = True
        reason_codes.append("unsupported_currency")

    origin_country = _infer_origin_country(quote.origin_port_or_country)
    if not quote.origin_port_or_country:
        missing_fields.append("origin_port_or_country")
    elif origin_country is None:
        out_of_scope = True
        reason_codes.append("unsupported_origin")

    if missing_fields and "missing_required_fields" not in reason_codes:
        reason_codes.append("missing_required_fields")

    status = "valid"
    if out_of_scope:
        status = "invalid_out_of_scope"
    elif missing_fields:
        status = "invalid_fixable"

    return QuoteValidationResult(
        quote_id=quote.quote_id,
        status=status,
        reason_codes=reason_codes,
        missing_fields=missing_fields,
    )
