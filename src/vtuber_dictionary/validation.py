"""Rules that do not rely on an LLM's confidence."""

from __future__ import annotations

import re
import unicodedata

from .domain import Candidate, DictionaryEntry, NameReadingParts, ResearchResult, VerificationResult
from .filtering import KatakanaOrLatinNameFilter

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
    def validate(
        self,
        candidate: Candidate,
        research: ResearchResult,
        verification: VerificationResult | None,
        existing: list[DictionaryEntry],
    ) -> tuple[DictionaryEntry | None, str | None]:
        if verification is None:
            return None, "verification is required"
        if not verification.verified:
            return None, "verification agent did not verify the result"
        name = unicodedata.normalize("NFC", verification.canonical_name or "")
        reading = unicodedata.normalize("NFC", verification.reading or "")
        if not name or not reading:
            return None, "canonical name or reading is missing"
        if reason := self._name_parts_reason(verification.name_parts, name, reading):
            return None, reason
        research_name = unicodedata.normalize("NFC", research.canonical_name or "")
        research_reading = unicodedata.normalize("NFC", research.reading or "")
        if research.status == "resolved" and (research_name != name or research_reading != reading):
            return None, "research and verification results disagree"
        if research.status == "resolved":
            if reason := self._name_parts_reason(
                research.name_parts, research_name, research_reading
            ):
                return None, reason
            if research.name_parts != verification.name_parts:
                return None, "research and verification name parts disagree"
        if not READING_PATTERN.fullmatch(reading):
            return None, "reading must consist of hiragana and prolonged-sound mark"
        if not verification.evidence:
            return None, "verification evidence is missing"
        if not research.evidence:
            return None, "research evidence is missing"
        evidence = research.evidence + verification.evidence
        if not any(item.source_type in PRIMARY_SOURCE_TYPES for item in evidence):
            return None, "no primary official evidence was supplied"
        if any(item.canonical_id == candidate.canonical_id for item in existing):
            return None, "canonical identity already has an entry"
        if any(item.reading == reading and item.canonical_name != name for item in existing):
            return None, "reading collides with a different canonical name"
        if any(item.canonical_name == name and item.reading != reading for item in existing):
            return None, "canonical name has a conflicting reading"
        if KatakanaOrLatinNameFilter().excludes_name(name):
            return None, "canonical name must include hiragana or kanji"
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

    @staticmethod
    def _name_parts_reason(parts: NameReadingParts, name: str, reading: str) -> str | None:
        if (parts.family_name is None) != (parts.family_reading is None):
            return "family name and reading must both be present or absent"
        if (parts.given_name is None) != (parts.given_reading is None):
            return "given name and reading must both be present or absent"
        joined_name = "".join(part for part in (parts.family_name, parts.given_name) if part)
        joined_reading = "".join(
            part for part in (parts.family_reading, parts.given_reading) if part
        )
        if joined_name != name:
            return "name parts do not compose canonical name"
        if joined_reading != reading:
            return "reading parts do not compose reading"
        return None
