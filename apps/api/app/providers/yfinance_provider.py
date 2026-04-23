import asyncio
from typing import Final

import pandas as pd
import yfinance as yf
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.exceptions import ExternalFetchFailed, NormalizationFailed

PAIR_TICKER_MAP: Final[dict[str, str]] = {
    "USDMYR": "MYR=X",
    "CNYMYR": "CNYMYR=X",
    "THBMYR": "THBMYR=X",
}


def _normalize_history_frame(history: pd.DataFrame) -> pd.DataFrame:
    if history.empty:
        raise NormalizationFailed("yfinance returned an empty dataset")

    normalized = history.copy()

    if isinstance(normalized.columns, pd.MultiIndex):
        normalized.columns = [
            str(level_0).lower() if str(level_0).lower() != "price" else str(level_1).lower()
            for level_0, level_1 in normalized.columns.to_flat_index()
        ]
    else:
        normalized.columns = [str(column).lower() for column in normalized.columns]

    normalized = normalized.reset_index()
    normalized.columns = [str(column).lower() for column in normalized.columns]

    if "date" not in normalized.columns:
        datetime_columns = [column for column in normalized.columns if "date" in column]
        if datetime_columns:
            normalized = normalized.rename(columns={datetime_columns[0]: "date"})

    required_columns = ["date", "open", "high", "low", "close"]
    missing_columns = [column for column in required_columns if column not in normalized.columns]
    if missing_columns:
        raise NormalizationFailed(
            f"yfinance payload missing required columns: {', '.join(missing_columns)}"
        )

    normalized = normalized[required_columns].copy()
    normalized["date"] = pd.to_datetime(normalized["date"]).dt.strftime("%Y-%m-%d")
    normalized[["open", "high", "low", "close"]] = normalized[
        ["open", "high", "low", "close"]
    ].astype(float)

    return normalized.sort_values("date").reset_index(drop=True)


async def _download_history(ticker: str, period: str) -> pd.DataFrame:
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(ExternalFetchFailed),
        reraise=True,
    ):
        with attempt:
            try:
                history = await asyncio.to_thread(
                    yf.download,
                    tickers=ticker,
                    period=period,
                    progress=False,
                    auto_adjust=False,
                    threads=False,
                )
            except Exception as exc:
                raise ExternalFetchFailed(f"Failed to fetch {ticker} from yfinance: {exc}") from exc

            try:
                return _normalize_history_frame(history)
            except NormalizationFailed as exc:
                raise ExternalFetchFailed(
                    f"Failed to normalize {ticker} history from yfinance: {exc}"
                ) from exc

    raise ExternalFetchFailed(f"Exhausted retries fetching {ticker} from yfinance")


async def fetch_fx_history(pair: str, period: str = "1y") -> pd.DataFrame:
    ticker = PAIR_TICKER_MAP.get(pair.upper())
    if not ticker:
        raise ExternalFetchFailed(f"Unsupported FX pair: {pair}")

    return await _download_history(ticker=ticker, period=period)


async def fetch_energy_history(symbol: str = "BZ=F", period: str = "1y") -> pd.DataFrame:
    return await _download_history(ticker=symbol, period=period)
