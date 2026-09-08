"""Deterministic pre-agent filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unicodedata import name, normalize

from .domain import AudienceMetrics, Candidate, DictionaryEntry


class KatakanaOrLatinNameFilter:
    """Reject names whose meaningful characters are only Katakana or Latin."""

    def excludes(self, candidate: Candidate) -> bool:
        characters = [
            character
            for character in normalize("NFKC", candidate.display_name)
            if character.isalpha()
        ]
        return bool(characters) and all(
            self._is_katakana_or_latin(character) for character in characters
        )

    @staticmethod
    def _is_katakana_or_latin(character: str) -> bool:
        unicode_name = name(character, "")
        return "KATAKANA" in unicode_name or "LATIN" in unicode_name


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
        self.entries_by_platform_identity = {
            key: entry
            for entry in entries
            for key in (
                f"youtube:{entry.youtube_channel_id}" if entry.youtube_channel_id else None,
                f"twitch:{entry.twitch_user_id}" if entry.twitch_user_id else None,
            )
            if key is not None
        }
        self.reverify_after = timedelta(days=reverify_after_days)

    def needs_research(self, candidate: Candidate, today: datetime | None = None) -> bool:
        existing = self.entries.get(candidate.canonical_id) or next(
            (
                self.entries_by_platform_identity[key]
                for key in candidate.identity_keys()
                if key in self.entries_by_platform_identity
            ),
            None,
        )
        if existing is None:
            return True
        today = today or datetime.now(UTC)
        return today - existing.verified_at > self.reverify_after
