from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from app.core.exceptions import ProviderError
from app.core.settings import AppSettings
from app.providers.gnews_provider import GNewsProvider
from app.repositories.snapshot_repository import SnapshotRepository
from app.schemas.common import SnapshotEnvelope, make_snapshot_envelope

logger = logging.getLogger(__name__)


class NewsEventService:
    def __init__(self, provider: GNewsProvider, snapshot_repository: SnapshotRepository) -> None:
        self.provider = provider
        self.snapshot_repository = snapshot_repository

    async def refresh_news_snapshot(self, *, keep_history: bool = False) -> SnapshotEnvelope:
        fetched_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            bucket_a, bucket_b, bucket_c = await asyncio.gather(
                asyncio.to_thread(self.provider.fetch_bucket_a),
                asyncio.to_thread(self.provider.fetch_bucket_b),
                asyncio.to_thread(self.provider.fetch_bucket_c),
            )
        except ProviderError as exc:
            raise ProviderError(f"GNews refresh failed: {exc}") from exc

        records = self.normalize_articles(bucket_a, category="logistics")
        records.extend(self.normalize_articles(bucket_b, category="finance"))
        records.extend(self.normalize_articles(bucket_c, category="geopolitical"))
        records = self._dedupe_records(records)
        records.sort(key=lambda row: (row["decision_relevance"], row["published_at"] or ""), reverse=True)

        envelope = SnapshotEnvelope(
            **make_snapshot_envelope(
                dataset="news",
                source="gnews",
                fetched_at=fetched_at,
                as_of=fetched_at[:10],
                status="success",
                data=records,
            )
        )
        self.snapshot_repository.write_snapshot("news", envelope, keep_history=keep_history)
        logger.info("News snapshot written: %d records", len(records))
        return envelope

    def normalize_articles(self, articles: list[dict[str, Any]], *, category: str) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for article in articles:
            title = (article.get("title") or "").strip()
            source_obj = article.get("source") or {}
            source = source_obj.get("name") if isinstance(source_obj, dict) else str(source_obj)
            source = source.strip()
            url = (article.get("url") or "").strip()
            dedupe_key = (title.lower(), source.lower())
            if not title or not url or dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            combined_text = f"{title} {article.get('description') or ''}".lower()
            signals = self._article_signals(combined_text)
            if not signals["matched_keywords"]:
                continue
            published_at = article.get("published_at") or article.get("publishedAt", "")
            published_dt = self._parse_published_at(str(published_at))
            recency_hours = self._age_hours(published_dt)
            relevance_score = self._relevance_score(signals, recency_hours)
            normalized.append(
                {
                    "event_id": f"{category}:{len(normalized) + 1}",
                    "title": title,
                    "published_at": published_at,
                    "published_at_iso": published_dt.isoformat().replace("+00:00", "Z") if published_dt else "",
                    "age_hours": recency_hours,
                    "source": source,
                    "url": url,
                    "category": category,
                    "relevance_score": relevance_score,
                    "decision_relevance": relevance_score,
                    "affected_dimension": category,
                    "notes": (article.get("description") or "")[:300],
                    "matched_keywords": signals["matched_keywords"],
                    "impact_channels": signals["impact_channels"],
                    "clean_summary": self._clean_summary(title, article.get("description") or "", signals),
                    "query": article.get("_query", ""),
                    "provider_mode": article.get("_provider_mode", "gnews_rss"),
                    "risk_hint": self._risk_hint(category, combined_text),
                    "glm_context": self._glm_context(title, source, published_at, signals),
                }
            )
        return sorted(normalized, key=lambda row: row["relevance_score"], reverse=True)

    def get_top_events_for_context(self, top_n: int = 5) -> list[dict[str, Any]]:
        snapshot = self.snapshot_repository.read_latest("news")
        if snapshot is None:
            return []
        records = snapshot.data or []
        return sorted(records, key=lambda row: row.get("decision_relevance", row.get("relevance_score", 0)), reverse=True)[:top_n]

    async def ensure_news_snapshot_fresh(self, *, max_age_minutes: int = 60) -> SnapshotEnvelope:
        latest = self.snapshot_repository.read_latest("news")
        if latest and latest.status == "success" and self._is_fresh(latest.fetched_at, max_age_minutes=max_age_minutes):
            return latest
        return await self.refresh_news_snapshot(keep_history=False)

    @staticmethod
    def _risk_hint(category: str, text: str) -> str:
        if category == "logistics" and any(term in text for term in ("congestion", "disruption", "freight")):
            return "lead_time_or_freight_risk"
        if category == "finance" and any(term in text for term in ("ringgit", "usd", "myr", "oil")):
            return "fx_or_energy_risk"
        if category == "geopolitical":
            if any(term in text for term in ("tariff", "trade war", "sanction")):
                return "tariff_or_policy_risk"
            return "geopolitical_supply_chain_risk"
        return category

    @staticmethod
    def _article_signals(text: str) -> dict[str, list[str]]:
        channel_keywords = {
            "resin_price": ["polypropylene", "pp resin", "petrochemical", "polymer", "plastics", "resin"],
            "freight_delay": ["freight", "shipping", "port", "congestion", "container", "red sea", "supply chain"],
            "fx": ["ringgit", "myr", "usd", "currency", "forex", "dollar"],
            "oil": ["oil", "brent", "crude", "energy"],
            "tariff_policy": ["tariff", "anti-dumping", "import duty", "sanction", "trade war"],
            "geopolitical": ["war", "conflict", "geopolitical", "middle east", "iran", "us-china", "red sea"],
            "manufacturing": ["manufacturing", "factory", "industrial", "exports", "imports"],
        }
        matched: list[str] = []
        channels: list[str] = []
        for channel, keywords in channel_keywords.items():
            hits = [keyword for keyword in keywords if keyword in text]
            if hits:
                channels.append(channel)
                matched.extend(hits)
        return {"matched_keywords": sorted(set(matched)), "impact_channels": sorted(set(channels))}

    @staticmethod
    def _relevance_score(signals: dict[str, list[str]], recency_hours: float | None) -> float:
        channel_count = len(signals["impact_channels"])
        keyword_count = len(signals["matched_keywords"])
        score = min(0.85, 0.18 * channel_count + 0.08 * keyword_count)
        if recency_hours is not None:
            if recency_hours <= 24:
                score += 0.15
            elif recency_hours <= 168:
                score += 0.08
            elif recency_hours > 24 * 30:
                score = min(score, 0.45)
            elif recency_hours > 24 * 14:
                score = min(score, 0.6)
            else:
                score = min(score, 0.75)
        else:
            score = min(score, 0.65)
        return round(min(1.0, score), 2)

    @staticmethod
    def _clean_summary(title: str, description: str, signals: dict[str, list[str]]) -> str:
        channels = ", ".join(signals["impact_channels"]) or "market"
        raw = description.strip() or title
        return f"{raw[:220]} Impact channels: {channels}."

    @staticmethod
    def _glm_context(title: str, source: str, published_at: str, signals: dict[str, list[str]]) -> str:
        channels = ", ".join(signals["impact_channels"]) or "market"
        keywords = ", ".join(signals["matched_keywords"][:8])
        return (
            f"News: '{title}' from {source or 'unknown source'} published {published_at or 'unknown date'}. "
            f"Relevant because it mentions {keywords}; potential impact channels: {channels}."
        )

    @staticmethod
    def _parse_published_at(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC)
            except ValueError:
                return None

    @staticmethod
    def _age_hours(value: datetime | None) -> float | None:
        if value is None:
            return None
        return round(max(0.0, (datetime.now(UTC) - value).total_seconds() / 3600), 2)

    @staticmethod
    def _is_fresh(fetched_at: str, *, max_age_minutes: int) -> bool:
        try:
            fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return datetime.now(UTC) - fetched.astimezone(UTC) <= timedelta(minutes=max_age_minutes)

    @staticmethod
    def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for record in records:
            key = (str(record.get("title", "")).lower(), str(record.get("source", "")).lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped


def build_default_news_service() -> NewsEventService:
    app_settings = AppSettings.from_env()
    return NewsEventService(GNewsProvider(max_results=8, api_key=app_settings.gnews_api_key), SnapshotRepository())
