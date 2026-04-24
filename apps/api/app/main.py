import asyncio
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app.api.routes import analysis, health, ingest_holidays, ingest_market, ingest_reference, ingest_macro, ingest_news, ingest_resin, ingest_weather, quote_upload, snapshots
from app.core.logging import setup_logging
from app.services.holiday_service import ensure_holiday_snapshot_fresh
from app.services.news_event_service import build_default_news_service
from app.services.resin_benchmark_service import ensure_resin_snapshot_fresh

setup_logging()
logger = logging.getLogger(__name__)


async def _hourly_news_refresh_loop() -> None:
    service = build_default_news_service()
    while True:
        try:
            envelope = await service.refresh_news_snapshot(keep_history=False)
            logger.info(
                "Hourly GNews refresh succeeded: status=%s rows=%s",
                envelope.status,
                envelope.record_count,
            )
        except Exception as exc:
            logger.error("Hourly GNews refresh failed: %s", exc)
        await asyncio.sleep(60 * 60)


async def _daily_holiday_refresh_loop() -> None:
    while True:
        try:
            envelope = await asyncio.to_thread(ensure_holiday_snapshot_fresh, max_age_hours=24)
            logger.info(
                "Daily holiday refresh ready: status=%s rows=%s",
                envelope.status,
                envelope.record_count,
            )
        except Exception as exc:
            logger.error("Daily holiday refresh failed: %s", exc)
        await asyncio.sleep(24 * 60 * 60)


async def _daily_resin_refresh_loop() -> None:
    while True:
        try:
            envelope = await asyncio.to_thread(ensure_resin_snapshot_fresh, max_age_hours=24)
            logger.info(
                "Daily SunSirs resin refresh ready: status=%s rows=%s",
                envelope.status,
                envelope.record_count,
            )
        except Exception as exc:
            logger.error("Daily SunSirs resin refresh failed: %s", exc)
        await asyncio.sleep(24 * 60 * 60)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("LintasNiaga API ready")
    news_task = asyncio.create_task(_hourly_news_refresh_loop())
    holiday_task = asyncio.create_task(_daily_holiday_refresh_loop())
    resin_task = asyncio.create_task(_daily_resin_refresh_loop())
    try:
        yield
    finally:
        news_task.cancel()
        holiday_task.cancel()
        resin_task.cancel()
        with suppress(asyncio.CancelledError):
            await news_task
        with suppress(asyncio.CancelledError):
            await holiday_task
        with suppress(asyncio.CancelledError):
            await resin_task

app = FastAPI(title="LintasNiaga API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(ingest_market.router)
app.include_router(ingest_reference.router)
app.include_router(ingest_holidays.router)
app.include_router(ingest_macro.router)
app.include_router(ingest_news.router)
app.include_router(ingest_resin.router)
app.include_router(ingest_weather.router)
app.include_router(quote_upload.router)
app.include_router(analysis.router)
app.include_router(snapshots.router)
