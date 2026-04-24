from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class QuoteUpload(BaseModel):
    upload_id: UUID
    filename: str
    storage_path: str
    uploaded_at: datetime
    status: Literal["pending", "extracted", "validated", "invalid"]


class ExtractedQuote(BaseModel):
    quote_id: UUID
    upload_id: UUID
    supplier_name: str | None = None
    origin_port_or_country: str | None = None
    incoterm: str | None = None
    unit_price: float | None = None
    currency: str | None = None
    moq: int | None = None
    lead_time_days: int | None = None
    payment_terms: str | None = None
    extraction_confidence: float | None = None


class QuoteValidationResult(BaseModel):
    quote_id: UUID
    status: Literal["valid", "invalid_fixable", "invalid_out_of_scope"]
    reason_codes: list[str]
    missing_fields: list[str]


class QuoteState(BaseModel):
    upload: QuoteUpload
    extracted_quote: ExtractedQuote | None = None
    validation: QuoteValidationResult | None = None
    extraction_method: str | None = None
    extraction_trace_urls: list[str] = []
    extraction_trace_ids: list[str] = []


class QuoteRepairRequest(BaseModel):
    supplier_name: str | None = None
    origin_port_or_country: str | None = None
    incoterm: str | None = None
    unit_price: float | None = None
    currency: str | None = None
    moq: int | None = None
    lead_time_days: int | None = None
    payment_terms: str | None = None
    extraction_confidence: float | None = None
