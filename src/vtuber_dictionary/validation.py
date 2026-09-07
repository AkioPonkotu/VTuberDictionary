"""Rules that do not rely on an LLM's confidence."""

from __future__ import annotations

import re
import unicodedata

from .domain import Candidate, DictionaryEntry, ResearchResult, VerificationResult

READING_PATTERN = re.compile(r"^[ぁ-ゖゝゞー]+$")


class DeterministicValidator:
    def validate(
        self,
        candidate: Candidate,
        research: ResearchResult,
        verification: VerificationResult,
        existing: list[DictionaryEntry],
    ) -> tuple[DictionaryEntry | None, str | None]:
        name = unicodedata.normalize("NFC", verification.canonical_name or "")
        reading = unicodedata.normalize("NFC", verification.reading or "")
        if not verification.verified:
            return None, "verification agent did not verify the result"
        if not name or not reading:
            return None, "canonical name or reading is missing"
        if not READING_PATTERN.fullmatch(reading):
            return None, "reading must consist of hiragana and prolonged-sound mark"
        if not verification.evidence:
            return None, "verification evidence is missing"
        if not research.evidence:
            return None, "research evidence is missing"
        if any(item.canonical_id == candidate.canonical_id for item in existing):
            return None, "canonical identity already has an entry"
        if any(item.reading == reading and item.canonical_name != name for item in existing):
            return None, "reading collides with a different canonical name"
        if any(item.canonical_name == name and item.reading != reading for item in existing):
            return None, "canonical name has a conflicting reading"
        urls = list(dict.fromkeys([e.url for e in research.evidence + verification.evidence]))
        return DictionaryEntry(
            canonical_id=candidate.canonical_id,
            reading=reading,
            canonical_name=name,
            agency=candidate.agency,
            youtube_channel_id=candidate.youtube_channel_id,
            twitch_user_id=candidate.twitch_user_id,
            source_urls=urls,
        ), None
