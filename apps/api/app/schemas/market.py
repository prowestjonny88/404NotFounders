from datetime import date

from pydantic import BaseModel


class FXSnapshotRecord(BaseModel):
    pair: str
    date: date
    open: float
    high: float
    low: float
    close: float


class EnergySnapshotRecord(BaseModel):
    symbol: str
    series_name: str
    date: date
    open: float
    high: float
    low: float
    close: float
