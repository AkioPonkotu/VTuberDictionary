"""Deterministic pre-agent filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .domain import AudienceMetrics, Candidate, DictionaryEntry


class ThresholdFilter:
    def __init__(self, youtube_minimum: int, twitch_minimum: int, mode: str = "any") -> None:
        self.youtube_minimum, self.twitch_minimum, self.mode = youtube_minimum, twitch_minimum, mode

    def accepts(self, metrics: AudienceMetrics) -> bool:
        values = [
            metrics.youtube_subscribers is not None
            and metrics.youtube_subscribers >= self.youtube_minimum,
            metrics.twitch_followers is not None
            and metrics.twitch_followers >= self.twitch_minimum,
        ]
        available = [
            value
            for value, known in zip(
                values,
                (metrics.youtube_subscribers is not None, metrics.twitch_followers is not None),
                strict=True,
            )
            if known
        ]
        return bool(available) and (all(available) if self.mode == "all" else any(available))


class ExistingEntryFilter:
    def __init__(self, entries: list[DictionaryEntry], reverify_after_days: int) -> None:
        self.entries = {entry.canonical_id: entry for entry in entries}
        self.reverify_after = timedelta(days=reverify_after_days)

    def needs_research(self, candidate: Candidate, today: datetime | None = None) -> bool:
        existing = self.entries.get(candidate.canonical_id)
        if existing is None:
            return True
        today = today or datetime.now(UTC)
        return today - existing.verified_at > self.reverify_after
