"""Orchestrates stages without coupling domain logic to service implementations."""

from __future__ import annotations

from dataclasses import dataclass

from .dictionary import DictionaryCompiler
from .domain import CandidateStatus, DictionaryEntry, ReviewRecord
from .filtering import ExistingEntryFilter, ThresholdFilter
from .ports import PlatformMetadataSource, ReadingResearcher, Verifier
from .repository import CandidateRepository, EntryRepository, ReviewRepository
from .settings import Settings
from .validation import DeterministicValidator


@dataclass
class Pipeline:
    candidates: CandidateRepository
    entries: EntryRepository
    reviews: ReviewRepository
    platforms: PlatformMetadataSource
    researcher: ReadingResearcher
    verifier: Verifier
    validator: DeterministicValidator
    compiler: DictionaryCompiler
    settings: Settings

    async def run(self) -> int:
        """Return count of accepted additions; compile only after all stages succeed."""
        existing = self.entries.all()
        existing_filter = ExistingEntryFilter(existing, self.settings.reverify_after_days)
        threshold = ThresholdFilter(
            self.settings.youtube_min_subscribers,
            self.settings.twitch_min_followers,
            self.settings.audience_threshold_mode,
        )
        additions: list[DictionaryEntry] = []
        candidates = self.candidates.all()
        for candidate in candidates:
            if (
                candidate.status == CandidateStatus.REVIEW_REQUIRED
                or not existing_filter.needs_research(candidate)
            ):
                continue
            metrics = await self.platforms.audience_metrics(candidate)
            if candidate.agency is None and not threshold.accepts(metrics):
                continue
            research = await self.researcher.research(candidate, metrics)
            verification = await self.verifier.verify(candidate, research, metrics)
            prior = [
                entry
                for entry in existing + additions
                if entry.canonical_id != candidate.canonical_id
            ]
            entry, reason = self.validator.validate(candidate, research, verification, prior)
            if entry is None:
                candidate.status = CandidateStatus.REVIEW_REQUIRED
                self.reviews.append(
                    ReviewRecord(
                        canonical_id=candidate.canonical_id,
                        reason=reason or "unknown",
                        candidate=candidate,
                    )
                )
                continue
            candidate.status = CandidateStatus.VERIFIED
            additions.append(entry)
        by_identity = {entry.canonical_id: entry for entry in existing}
        by_identity.update({entry.canonical_id: entry for entry in additions})
        all_entries = list(by_identity.values())
        artifact = self.settings.dist_dir / "vtuber_dictionary.tsv"
        if additions:
            self.entries.replace(all_entries)
        if additions or not artifact.exists():
            self.compiler.compile(all_entries, artifact)
        self.candidates.replace(candidates)
        return len(additions)
