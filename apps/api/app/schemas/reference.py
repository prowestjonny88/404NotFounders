from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class FreightRate(BaseModel):
    origin_country: str
    origin_port: str
    destination_port: str
    incoterm: str
    currency: str
    rate_value: float
    rate_unit: str
    valid_from: date
    valid_to: date
    source_note: str


class TariffRule(BaseModel):
    hs_code: str
    product_name: str
    import_country: str
    tariff_rate_pct: float
    tariff_type: str
    source_note: str


class PortMetadata(BaseModel):
    port_code: str
    port_name: str
    country_code: str
    latitude: float
    longitude: float
    is_destination_hub: bool


class SupplierSeed(BaseModel):
    supplier_name: str
    country_code: str
    port: str
    reliability_score: float
    typical_lead_days: int
    notes: str


class HolidaySnapshotRecord(BaseModel):
    country_code: str
    date: date
    holiday_name: str
    is_holiday: bool = Field(default=True)
