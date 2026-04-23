from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile

try:
    import fitz
except ImportError:  # pragma: no cover - runtime dependency
    fitz = None

from app.core.exceptions import DependencyNotAvailableError, ExtractionFailed
from app.core.settings import AppSettings
from app.providers.llm_provider import build_llm_provider
from app.schemas.quote import ExtractedQuote, QuoteState, QuoteUpload
from app.services.quote_validation_service import validate_quote

QUOTE_STATES: dict[UUID, QuoteState] = {}


def _upload_dir() -> Path:
    settings = AppSettings.from_env()
    configured = os.getenv("UPLOAD_DIR")
    if configured:
        return Path(configured)
    return settings.root_dir / "apps" / "api" / "uploads"


def _merge_quotes(base_quote_id: UUID, upload_id: UUID, page_quotes: list[ExtractedQuote]) -> ExtractedQuote:
    merged = ExtractedQuote(quote_id=base_quote_id, upload_id=upload_id)
    fields = [
        "supplier_name",
        "origin_port_or_country",
        "incoterm",
        "unit_price",
        "currency",
        "moq",
        "lead_time_days",
        "payment_terms",
    ]
    confidences = [quote.extraction_confidence for quote in page_quotes if quote.extraction_confidence is not None]

    for field_name in fields:
        for quote in page_quotes:
            value = getattr(quote, field_name)
            if value is not None and (not isinstance(value, str) or value.strip()):
                setattr(merged, field_name, value)
                break

    merged.extraction_confidence = sum(confidences) / len(confidences) if confidences else None
    return merged


def _render_first_two_pages(pdf_bytes: bytes) -> list[bytes]:
    if fitz is None:
        raise DependencyNotAvailableError("PyMuPDF (fitz) is required for quote PDF rendering.")
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ExtractionFailed(f"Failed to open uploaded PDF: {exc}") from exc

    images: list[bytes] = []
    for page_index in range(min(2, document.page_count)):
        page = document.load_page(page_index)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        images.append(pixmap.tobytes("png"))
    document.close()
    if not images:
        raise ExtractionFailed("Uploaded PDF has no renderable pages.")
    return images


async def process_upload(file: UploadFile) -> QuoteUpload:
    upload_id = uuid4()
    quote_id = uuid4()
    upload_dir = _upload_dir()
    upload_dir.mkdir(parents=True, exist_ok=True)

    pdf_bytes = await file.read()
    storage_path = upload_dir / f"{upload_id}_{file.filename}"
    storage_path.write_bytes(pdf_bytes)

    upload = QuoteUpload(
        upload_id=upload_id,
        filename=file.filename or f"{upload_id}.pdf",
        storage_path=str(storage_path),
        uploaded_at=datetime.now(UTC),
        status="pending",
    )
    QUOTE_STATES[quote_id] = QuoteState(upload=upload)

    provider = build_llm_provider()
    page_images = _render_first_two_pages(pdf_bytes)
    extracted_pages = [provider.extract_quote_fields(image_bytes) for image_bytes in page_images]
    merged_quote = _merge_quotes(quote_id=quote_id, upload_id=upload_id, page_quotes=extracted_pages)
    validation = validate_quote(merged_quote)

    upload.status = "validated" if validation.status == "valid" else "invalid"
    QUOTE_STATES[quote_id] = QuoteState(
        upload=upload,
        extracted_quote=merged_quote,
        validation=validation,
    )
    return upload


def get_quote_state(quote_id: UUID) -> QuoteState | None:
    return QUOTE_STATES.get(quote_id)


def get_quote_state_by_upload_id(upload_id: UUID) -> QuoteState | None:
    for state in QUOTE_STATES.values():
        if state.upload.upload_id == upload_id:
            return state
    return None


def repair_quote(quote_id: UUID, updates: dict[str, object]) -> QuoteState | None:
    state = QUOTE_STATES.get(quote_id)
    if state is None or state.extracted_quote is None:
        return None

    updated_payload = state.extracted_quote.model_dump()
    for key, value in updates.items():
        if value is not None and key in updated_payload:
            updated_payload[key] = value
    repaired_quote = ExtractedQuote(**updated_payload)
    validation = validate_quote(repaired_quote)
    state.upload.status = "validated" if validation.status == "valid" else "invalid"
    updated_state = QuoteState(upload=state.upload, extracted_quote=repaired_quote, validation=validation)
    QUOTE_STATES[quote_id] = updated_state
    return updated_state
