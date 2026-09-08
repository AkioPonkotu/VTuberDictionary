"""Orchestrates stages without coupling domain logic to service implementations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .dictionary import DictionaryCompiler
from .domain import CandidateStatus, DictionaryEntry, ReviewRecord
from .filtering import ExistingEntryFilter, ThresholdFilter
from .ports import PlatformMetadataSource, ReadingResearcher, Verifier, WebSourcePrefetcher
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
    web_sources: WebSourcePrefetcher | None = None

    async def run(self) -> int:
        """Return accepted additions while checkpointing every external-result boundary."""
        artifact = self.settings.dist_dir / "vtuber_dictionary.tsv"
        self.entries.recover_publication(artifact)
        self.reviews.recover_checkpoint(self.candidates)
        existing = self.entries.all()
        existing_filter = ExistingEntryFilter(existing, self.settings.reverify_after_days)
        threshold = ThresholdFilter(
            self.settings.youtube_min_subscribers,
            self.settings.twitch_min_followers,
            self.settings.audience_threshold_mode,
        )
        candidates = self.candidates.all()
        additions = 0
        for candidate in candidates:
            # Validation completed before a previous interruption.  Publish the
            # saved result without re-contacting the platform or either agent.
            if candidate.pending_entry is not None:
                was_new = candidate.pending_entry.canonical_id not in {
                    entry.canonical_id for entry in existing
                }
                self._publish_entry(existing, candidate.pending_entry, artifact)
                existing = self.entries.all()
                existing_filter = ExistingEntryFilter(
                    existing, self.settings.reverify_after_days
                )
                candidate.pending_entry = None
                candidate.status = CandidateStatus.VERIFIED
                self.candidates.replace(candidates)
                additions += int(was_new)
                continue
            if (
                candidate.status == CandidateStatus.REVIEW_REQUIRED
                or not existing_filter.needs_research(candidate)
            ):
                continue
            metrics = await self.platforms.audience_metrics(candidate)
            if candidate.agency is None and not threshold.accepts(metrics):
                continue
            sources = await self.web_sources.fetch(candidate, metrics) if self.web_sources else []
            research = await self.researcher.research(candidate, metrics, sources)
            verification = (
                None
                if self.validator.can_skip_verification(candidate, research, sources)
                else await self.verifier.verify(candidate, research, metrics, sources)
            )
            prior = [entry for entry in existing if entry.canonical_id != candidate.canonical_id]
            entry, reason = self.validator.validate(
                candidate, research, verification, prior, sources
            )
            if entry is None:
                candidate.status = CandidateStatus.REVIEW_REQUIRED
                self.reviews.checkpoint_review(
                    self.candidates,
                    candidates,
                    ReviewRecord(
                        canonical_id=candidate.canonical_id,
                        reason=reason or "unknown",
                        candidate=candidate,
                    ),
                )
                continue
            # Persist the agent result before attempting the multi-file
            # publication.  The next run can resume from this exact entry.
            candidate.pending_entry = entry
            self.candidates.replace(candidates)
            self._publish_entry(existing, entry, artifact)
            existing = self.entries.all()
            existing_filter = ExistingEntryFilter(existing, self.settings.reverify_after_days)
            candidate.pending_entry = None
            candidate.status = CandidateStatus.VERIFIED
            self.candidates.replace(candidates)
            additions += 1
        if not artifact.exists():
            self.entries.publish(existing, artifact, self.compiler.render(existing))
        return additions

    def _publish_entry(
        self, existing: list[DictionaryEntry], entry: DictionaryEntry, artifact: Path
    ) -> None:
        by_identity = {item.canonical_id: item for item in existing}
        by_identity[entry.canonical_id] = entry
        all_entries = list(by_identity.values())
        self.entries.publish(all_entries, artifact, self.compiler.render(all_entries))
