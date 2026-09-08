"""Orchestrates stages without coupling domain logic to service implementations."""

from __future__ import annotations

from dataclasses import dataclass

from .dictionary import DictionaryCompiler
from .domain import AgentAttempt, CandidateStatus, DictionaryEntry, ReviewRecord, VerificationResult
from .filtering import ExistingEntryFilter, KatakanaOrLatinNameFilter, ThresholdFilter
from .ports import PlatformMetadataSource, ReadingResearcher, Verifier, WebSourcePrefetcher
from .repository import CandidateRepository, EntryRepository, ReviewRepository
from .settings import Settings
from .validation import DeterministicValidator

MAX_RESEARCH_ATTEMPTS = 3


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
        artifact_paths = self.compiler.artifact_paths(self.settings.dist_dir)
        self.entries.recover_publication(artifact_paths)
        self.reviews.recover_checkpoint(self.candidates)
        existing = self.entries.all()
        candidates = self.candidates.all()
        name_filter = KatakanaOrLatinNameFilter()
        excluded_ids = {
            candidate.canonical_id
            for candidate in candidates
            if name_filter.excludes(candidate)
            or (
                candidate.pending_entry is not None
                and name_filter.excludes_name(candidate.pending_entry.canonical_name)
            )
        }
        excluded_entry_ids = {
            entry.canonical_id
            for entry in existing
            if name_filter.excludes_name(entry.canonical_name)
        }
        excluded_ids |= excluded_entry_ids
        if excluded_ids:
            for candidate in candidates:
                if candidate.canonical_id in excluded_ids:
                    candidate.status = CandidateStatus.REJECTED
                    candidate.pending_entry = None
            self.candidates.replace(candidates)
        retained_entries = [
            entry
            for entry in existing
            if entry.canonical_id not in excluded_ids | excluded_entry_ids
        ]
        if len(retained_entries) != len(existing):
            self._publish_entries(retained_entries)
            existing = retained_entries
        existing_filter = ExistingEntryFilter(existing)
        threshold = ThresholdFilter(
            self.settings.youtube_min_subscribers,
            self.settings.twitch_min_followers,
            self.settings.audience_threshold_mode,
        )
        additions = 0
        for candidate in candidates:
            # Validation completed before a previous interruption.  Publish the
            # saved result without re-contacting the platform or either agent.
            if candidate.pending_entry is not None:
                was_new = candidate.pending_entry.canonical_id not in {
                    entry.canonical_id for entry in existing
                }
                self._publish_entry(existing, candidate.pending_entry)
                existing = self.entries.all()
                existing_filter = ExistingEntryFilter(existing)
                candidate.pending_entry = None
                candidate.status = CandidateStatus.VERIFIED
                self.candidates.replace(candidates)
                additions += int(was_new)
                continue
            if candidate.status in {
                CandidateStatus.REJECTED,
                CandidateStatus.REVIEW_REQUIRED,
            } or not existing_filter.needs_research(candidate):
                continue
            metrics = await self.platforms.audience_metrics(candidate)
            if candidate.agency is None and not threshold.accepts(metrics):
                continue
            sources = await self.web_sources.fetch(candidate, metrics) if self.web_sources else []
            entry: DictionaryEntry | None = None
            reason: str | None = candidate.retry_reason
            while candidate.research_attempts < MAX_RESEARCH_ATTEMPTS:
                attempt = AgentAttempt(number=candidate.research_attempts + 1)
                candidate.research_attempts += 1
                candidate.agent_attempts.append(attempt)
                self.candidates.replace(candidates)

                research = await self.researcher.research(candidate, metrics, sources)
                candidate.research_raw_json = research.raw_json
                attempt.research_raw_json = research.raw_json
                self.candidates.replace(candidates)
                if research.canonical_name and name_filter.excludes_name(research.canonical_name):
                    candidate.status = CandidateStatus.REJECTED
                    self.candidates.replace(candidates)
                    break

                verification = await self.verifier.verify(candidate, research, metrics, sources)
                candidate.verification_raw_json = verification.raw_json
                attempt.verification_raw_json = verification.raw_json
                self.candidates.replace(candidates)
                if (
                    verification.canonical_name
                    and name_filter.excludes_name(verification.canonical_name)
                ):
                    candidate.status = CandidateStatus.REJECTED
                    self.candidates.replace(candidates)
                    break

                prior = [
                    entry for entry in existing if entry.canonical_id != candidate.canonical_id
                ]
                entry, reason = self.validator.validate(candidate, research, verification, prior)
                if entry is not None:
                    break

                candidate.retry_reason = self._retry_reason(reason, verification)
                attempt.failure_reason = candidate.retry_reason
                self.candidates.replace(candidates)

            if candidate.status == CandidateStatus.REJECTED:
                continue
            if entry is None:
                candidate.status = CandidateStatus.REVIEW_REQUIRED
                self.reviews.checkpoint_review(
                    self.candidates,
                    candidates,
                    ReviewRecord(
                        canonical_id=candidate.canonical_id,
                        reason=candidate.retry_reason or reason or "unknown",
                        candidate=candidate,
                    ),
                )
                continue
            # Persist the agent result before attempting the multi-file
            # publication.  The next run can resume from this exact entry.
            candidate.pending_entry = entry
            self.candidates.replace(candidates)
            self._publish_entry(existing, entry)
            existing = self.entries.all()
            existing_filter = ExistingEntryFilter(existing)
            candidate.pending_entry = None
            candidate.status = CandidateStatus.VERIFIED
            self.candidates.replace(candidates)
            additions += 1
        if not all(path.exists() for path in artifact_paths):
            self._publish_entries(existing)
        return additions

    def _publish_entry(self, existing: list[DictionaryEntry], entry: DictionaryEntry) -> None:
        by_identity = {item.canonical_id: item for item in existing}
        by_identity[entry.canonical_id] = entry
        self._publish_entries(list(by_identity.values()))

    def _publish_entries(self, entries: list[DictionaryEntry]) -> None:
        artifacts = {
            self.settings.dist_dir / artifact.filename: artifact.payload
            for artifact in self.compiler.artifacts(entries)
        }
        self.entries.publish(entries, artifacts)

    @staticmethod
    def _retry_reason(reason: str | None, verification: VerificationResult) -> str:
        if not verification.verified:
            issues = "; ".join(verification.issues)
            return f"verification rejected: {issues or reason or 'no reason supplied'}"
        return reason or "unknown validation failure"
