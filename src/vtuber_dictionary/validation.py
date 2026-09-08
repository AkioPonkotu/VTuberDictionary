"""Rules that do not rely on an LLM's confidence."""

from __future__ import annotations

import re
import unicodedata

from .domain import Candidate, DictionaryEntry, ResearchResult, VerificationResult, WebSource

READING_PATTERN = re.compile(r"^[ぁ-ゖゝゞー]+$")
PRIMARY_SOURCE_TYPES = {
    "official_agency_profile",
    "official_profile",
    "official_website",
    "youtube_about",
    "twitch_about",
    "official_social",
}


class DeterministicValidator:
    @staticmethod
    def _same_url(left: str, right: str) -> bool:
        return left.rstrip("/") == right.rstrip("/")

    def can_skip_verification(
        self, candidate: Candidate, research: ResearchResult, sources: list[WebSource]
    ) -> bool:
        """Allow one-agent acceptance only for an app-fetched agency profile."""
        if candidate.agency is None or candidate.official_profile_url is None:
            return False
        if research.status != "resolved" or not research.canonical_name or not research.reading:
            return False
        profile_was_fetched = any(
            source.source_type == "official_agency_profile"
            and self._same_url(source.url, candidate.official_profile_url)
            for source in sources
        )
        return profile_was_fetched and any(
            evidence.source_type == "official_agency_profile"
            and self._same_url(evidence.url, candidate.official_profile_url)
            for evidence in research.evidence
        )

    def validate(
        self,
        candidate: Candidate,
        research: ResearchResult,
        verification: VerificationResult | None,
        existing: list[DictionaryEntry],
        sources: list[WebSource] | None = None,
    ) -> tuple[DictionaryEntry | None, str | None]:
        skipped_verification = verification is None
        if skipped_verification:
            if not self.can_skip_verification(candidate, research, sources or []):
                return None, (
                    "verification may only be skipped for a fetched official agency profile"
                )
            name = unicodedata.normalize("NFC", research.canonical_name or "")
            reading = unicodedata.normalize("NFC", research.reading or "")
        else:
            assert verification is not None
            if not verification.verified:
                return None, "verification agent did not verify the result"
            name = unicodedata.normalize("NFC", verification.canonical_name or "")
            reading = unicodedata.normalize("NFC", verification.reading or "")
        if not name or not reading:
            return None, "canonical name or reading is missing"
        research_name = unicodedata.normalize("NFC", research.canonical_name or "")
        research_reading = unicodedata.normalize("NFC", research.reading or "")
        if research.status == "resolved" and (research_name != name or research_reading != reading):
            return None, "research and verification results disagree"
        if not READING_PATTERN.fullmatch(reading):
            return None, "reading must consist of hiragana and prolonged-sound mark"
        if not skipped_verification and verification is not None and not verification.evidence:
            return None, "verification evidence is missing"
        if not research.evidence:
            return None, "research evidence is missing"
        evidence = research.evidence + (verification.evidence if verification is not None else [])
        if not any(item.source_type in PRIMARY_SOURCE_TYPES for item in evidence):
            return None, "no primary official evidence was supplied"
        if any(item.canonical_id == candidate.canonical_id for item in existing):
            return None, "canonical identity already has an entry"
        if any(item.reading == reading and item.canonical_name != name for item in existing):
            return None, "reading collides with a different canonical name"
        if any(item.canonical_name == name and item.reading != reading for item in existing):
            return None, "canonical name has a conflicting reading"
        urls = list(dict.fromkeys([e.url for e in evidence]))
        return DictionaryEntry(
            canonical_id=candidate.canonical_id,
            reading=reading,
            canonical_name=name,
            agency=candidate.agency,
            youtube_channel_id=candidate.youtube_channel_id,
            twitch_user_id=candidate.twitch_user_id,
            source_urls=urls,
        ), None
