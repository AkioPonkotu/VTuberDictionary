"""A durable, ordered pipeline with parallel external I/O workers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from .dictionary import DictionaryCompiler
from .domain import (
    AgentAttempt,
    Candidate,
    CandidateStatus,
    DictionaryEntry,
    ResearchResult,
    ReviewRecord,
    VerificationResult,
    WebSource,
)
from .filtering import ExistingEntryFilter, KatakanaOrLatinNameFilter, ThresholdFilter
from .ports import PlatformMetadataSource, ReadingResearcher, Verifier, WebSourcePrefetcher
from .repository import CandidateRepository, EntryRepository, ReviewRepository
from .settings import Settings
from .validation import DeterministicValidator

LOG = logging.getLogger(__name__)
MAX_RESEARCH_ATTEMPTS = 3


@dataclass(frozen=True)
class SaveEvent:
    """An immutable candidate-state transition acknowledged by the save actor."""

    kind: Literal[
        "attempt_started",
        "research_saved",
        "verification_saved",
        "retry_saved",
        "candidate_rejected",
        "pending_entry_saved",
        "published",
        "review_required",
    ]
    candidate_id: str
    attempt_number: int | None = None
    raw_json: str | None = None
    reason: str | None = None
    entry: DictionaryEntry | None = None


@dataclass
class _SaveRequest:
    event: SaveEvent
    completed: asyncio.Future[Candidate]


@dataclass(frozen=True)
class _Work:
    index: int
    candidate: Candidate
    skip_external: bool = False


@dataclass(frozen=True)
class WorkerResult:
    index: int
    candidate: Candidate
    research: ResearchResult | None = None
    verification: VerificationResult | None = None
    rejected: bool = False
    skipped: bool = False


@dataclass(frozen=True)
class _WorkerOutcome:
    research: ResearchResult | None = None
    verification: VerificationResult | None = None
    rejected: bool = False
    skipped: bool = False


@dataclass
class _PipelineStats:
    processed: int = 0
    queued: int = 0
    in_flight: int = 0
    ready_to_commit: int = 0
    retries: int = 0
    published: int = 0
    review_required: int = 0


class _CandidateSaver:
    """The sole caller of ``CandidateRepository.replace`` while processing."""

    def __init__(
        self,
        repository: CandidateRepository,
        reviews: ReviewRepository,
        candidates: list[Candidate],
        stats: _PipelineStats,
        concurrency: int,
    ) -> None:
        self.repository, self.reviews, self.candidates, self.stats = (
            repository,
            reviews,
            candidates,
            stats,
        )
        self.concurrency = concurrency
        self.queue: asyncio.Queue[_SaveRequest | None] = asyncio.Queue()
        self._by_id = {candidate.canonical_id: candidate for candidate in candidates}

    async def save(self, event: SaveEvent) -> Candidate:
        completed: asyncio.Future[Candidate] = asyncio.get_running_loop().create_future()
        await self.queue.put(_SaveRequest(event, completed))
        return await completed

    async def run(self) -> None:
        try:
            while request := await self.queue.get():
                try:
                    candidate = self._apply(request.event)
                    if request.event.kind == "review_required":
                        self.reviews.checkpoint_review(
                            self.repository,
                            self.candidates,
                            ReviewRecord(
                                canonical_id=candidate.canonical_id,
                                reason=request.event.reason or "unknown",
                                candidate=candidate,
                            ),
                        )
                    else:
                        self.repository.replace(self.candidates)
                    request.completed.set_result(candidate.model_copy(deep=True))
                    self._log_progress()
                except BaseException as exc:
                    request.completed.set_exception(exc)
        finally:
            self._log_progress(final=True)

    def _apply(self, event: SaveEvent) -> Candidate:
        candidate = self._by_id[event.candidate_id]
        if event.kind == "attempt_started":
            if event.attempt_number is None:
                raise RuntimeError("attempt_started requires an attempt number")
            if candidate.research_attempts < event.attempt_number:
                candidate.research_attempts = event.attempt_number
                candidate.agent_attempts.append(AgentAttempt(number=event.attempt_number))
        elif event.kind == "research_saved":
            attempt = self._attempt(candidate, event)
            candidate.research_raw_json = event.raw_json
            attempt.research_raw_json = event.raw_json
        elif event.kind == "verification_saved":
            attempt = self._attempt(candidate, event)
            candidate.verification_raw_json = event.raw_json
            attempt.verification_raw_json = event.raw_json
        elif event.kind == "retry_saved":
            self._attempt(candidate, event).failure_reason = event.reason
            candidate.retry_reason = event.reason
            self.stats.retries += 1
        elif event.kind == "candidate_rejected":
            candidate.status, candidate.pending_entry = CandidateStatus.REJECTED, None
        elif event.kind == "pending_entry_saved":
            if event.entry is None:
                raise RuntimeError("pending_entry_saved requires an entry")
            candidate.pending_entry = event.entry
        elif event.kind == "published":
            candidate.pending_entry, candidate.status = None, CandidateStatus.VERIFIED
            self.stats.published += 1
        elif event.kind == "review_required":
            candidate.status = CandidateStatus.REVIEW_REQUIRED
            self.stats.review_required += 1
        return candidate

    @staticmethod
    def _attempt(candidate: Candidate, event: SaveEvent) -> AgentAttempt:
        if event.attempt_number is None:
            raise RuntimeError(f"{event.kind} requires an attempt number")
        for attempt in candidate.agent_attempts:
            if attempt.number == event.attempt_number:
                return attempt
        raise RuntimeError("attempt was not durably started")

    def _log_progress(self, *, final: bool = False) -> None:
        if final or self.stats.processed % 10 == 0:
            LOG.info(
                "pipeline_progress processed=%s queued=%s in_flight=%s ready_to_commit=%s "
                "concurrency=%s retries=%s rate_limited=%s published=%s review_required=%s",
                self.stats.processed,
                self.stats.queued,
                self.stats.in_flight,
                self.stats.ready_to_commit,
                self.concurrency,
                self.stats.retries,
                0,
                self.stats.published,
                self.stats.review_required,
            )


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
    candidate_source_filter: set[str] | None = None
    web_sources: WebSourcePrefetcher | None = None

    async def run(self) -> int:
        """Process one stable candidate snapshot, checkpointing every I/O boundary."""
        artifact_paths = self.compiler.artifact_paths(self.settings.dist_dir)
        self.entries.recover_publication(artifact_paths)
        self.reviews.recover_checkpoint(self.candidates)
        all_candidates = self.candidates.all()
        selected = [
            candidate
            for candidate in all_candidates
            if self.candidate_source_filter is None
            or candidate.discovery_sources & self.candidate_source_filter
        ]
        selected.sort(key=lambda candidate: (candidate.first_discovered_at, candidate.canonical_id))
        stats = _PipelineStats()
        saver = _CandidateSaver(
            self.candidates,
            self.reviews,
            all_candidates,
            stats,
            self.settings.processing_concurrency,
        )
        jobs: asyncio.Queue[_Work | None] = asyncio.Queue()
        results: asyncio.Queue[WorkerResult] = asyncio.Queue(
            maxsize=self.settings.processing_concurrency * 2
        )
        try:
            async with asyncio.TaskGroup() as group:
                save_task = group.create_task(saver.run())
                workers = [
                    group.create_task(self._worker(jobs, results, saver, stats))
                    for _ in range(self.settings.processing_concurrency)
                ]
                additions = await self._adopt_in_order(selected, jobs, results, saver, stats)
                for _ in workers:
                    await jobs.put(None)
                await asyncio.gather(*workers)
                await saver.queue.put(None)
                await save_task
        except* OSError as errors:
            # Keep the public failure mode of the pre-parallel pipeline.  The
            # journal has already made this publication safely recoverable.
            raise errors.exceptions[0] from None
        if not all(path.exists() for path in artifact_paths):
            self._publish_entries(self.entries.all())
        return additions

    async def _adopt_in_order(
        self,
        selected: list[Candidate],
        jobs: asyncio.Queue[_Work | None],
        results: asyncio.Queue[WorkerResult],
        saver: _CandidateSaver,
        stats: _PipelineStats,
    ) -> int:
        name_filter = KatakanaOrLatinNameFilter()
        existing = self.entries.all()
        selected_ids = {candidate.canonical_id for candidate in selected}
        excluded_ids = {
            candidate.canonical_id
            for candidate in selected
            if name_filter.excludes(candidate)
            or (
                candidate.pending_entry is not None
                and name_filter.excludes_name(candidate.pending_entry.canonical_name)
            )
        }
        excluded_ids |= {
            entry.canonical_id
            for entry in existing
            if name_filter.excludes_name(entry.canonical_name)
        }
        for candidate_id in excluded_ids & selected_ids:
            await saver.save(SaveEvent("candidate_rejected", candidate_id))
        retained = [entry for entry in existing if entry.canonical_id not in excluded_ids]
        if len(retained) != len(existing):
            self._publish_entries(retained)
        existing = retained

        additions = 0
        # Resume durable publication before any network access, in snapshot order.
        for candidate in selected:
            if candidate.canonical_id in excluded_ids or candidate.pending_entry is None:
                continue
            pending_entry = candidate.pending_entry
            was_new = pending_entry.canonical_id not in {item.canonical_id for item in existing}
            self._publish_entry(existing, pending_entry)
            existing = self.entries.all()
            await saver.save(SaveEvent("published", candidate.canonical_id))
            additions += int(was_new)

        existing_filter = ExistingEntryFilter(existing)
        snapshot = [
            candidate
            for candidate in selected
            if candidate.pending_entry is None and candidate.canonical_id not in excluded_ids
        ]
        for index, candidate in enumerate(snapshot):
            await jobs.put(
                _Work(
                    index,
                    candidate.model_copy(deep=True),
                    skip_external=(
                        candidate.status
                        in {CandidateStatus.REJECTED, CandidateStatus.REVIEW_REQUIRED}
                        or not existing_filter.needs_research(candidate)
                    ),
                )
            )
        stats.queued = len(snapshot)
        ready: dict[int, WorkerResult] = {}
        next_index = 0
        while next_index < len(snapshot):
            result = await results.get()
            stats.in_flight = max(0, stats.in_flight - 1)
            ready[result.index] = result
            stats.ready_to_commit = len(ready)
            while (ordered := ready.pop(next_index, None)) is not None:
                stats.ready_to_commit = len(ready)
                if ordered.rejected or ordered.skipped:
                    next_index += 1
                    stats.processed += 1
                    continue
                if ordered.research is None or ordered.verification is None:
                    raise RuntimeError("worker completed without an agent result")
                prior = [
                    entry
                    for entry in existing
                    if entry.canonical_id != ordered.candidate.canonical_id
                ]
                entry, reason = self.validator.validate(
                    ordered.candidate, ordered.research, ordered.verification, prior
                )
                if entry is None and ordered.candidate.research_attempts < MAX_RESEARCH_ATTEMPTS:
                    retry_reason = self._retry_reason(reason, ordered.verification)
                    candidate = await saver.save(
                        SaveEvent(
                            "retry_saved",
                            ordered.candidate.canonical_id,
                            attempt_number=ordered.candidate.research_attempts,
                            reason=retry_reason,
                        )
                    )
                    await jobs.put(_Work(next_index, candidate))
                    stats.queued += 1
                    break
                if entry is None:
                    await saver.save(
                        SaveEvent(
                            "review_required",
                            ordered.candidate.canonical_id,
                            reason=ordered.candidate.retry_reason
                            or self._retry_reason(reason, ordered.verification),
                        )
                    )
                else:
                    await saver.save(
                        SaveEvent(
                            "pending_entry_saved", ordered.candidate.canonical_id, entry=entry
                        )
                    )
                    self._publish_entry(existing, entry)
                    existing = self.entries.all()
                    await saver.save(SaveEvent("published", ordered.candidate.canonical_id))
                    additions += 1
                next_index += 1
                stats.processed += 1
        return additions

    async def _worker(
        self,
        jobs: asyncio.Queue[_Work | None],
        results: asyncio.Queue[WorkerResult],
        saver: _CandidateSaver,
        stats: _PipelineStats,
    ) -> None:
        while (work := await jobs.get()) is not None:
            stats.in_flight += 1
            candidate = work.candidate.model_copy(deep=True)
            try:
                if work.skip_external:
                    await results.put(WorkerResult(work.index, candidate, skipped=True))
                    continue
                saved_candidate, outcome = await self._perform_external_work(candidate, saver)
                await results.put(
                    WorkerResult(
                        work.index,
                        saved_candidate,
                        outcome.research,
                        outcome.verification,
                        outcome.rejected,
                        outcome.skipped,
                    )
                )
            except asyncio.CancelledError:
                raise

    async def _perform_external_work(
        self, candidate: Candidate, saver: _CandidateSaver
    ) -> tuple[Candidate, _WorkerOutcome]:
        latest = candidate.agent_attempts[-1] if candidate.agent_attempts else None
        if (
            latest
            and latest.research_raw_json
            and latest.verification_raw_json
            and not latest.failure_reason
        ):
            return candidate, _WorkerOutcome(
                ResearchResult.model_validate_json(latest.research_raw_json).model_copy(
                    update={"raw_json": latest.research_raw_json}
                ),
                VerificationResult.model_validate_json(latest.verification_raw_json).model_copy(
                    update={"raw_json": latest.verification_raw_json}
                ),
            )
        metrics = await self.platforms.audience_metrics(candidate)
        threshold = ThresholdFilter(
            self.settings.youtube_min_subscribers,
            self.settings.twitch_min_followers,
            self.settings.audience_threshold_mode,
        )
        if latest is None and candidate.agency is None and not threshold.accepts(metrics):
            return candidate, _WorkerOutcome(skipped=True)
        sources: list[WebSource] = (
            await self.web_sources.fetch(candidate, metrics) if self.web_sources else []
        )
        name_filter = KatakanaOrLatinNameFilter()
        if latest and latest.research_raw_json and not latest.verification_raw_json:
            attempt_number = latest.number
            research = ResearchResult.model_validate_json(latest.research_raw_json).model_copy(
                update={"raw_json": latest.research_raw_json}
            )
        else:
            attempt_number = candidate.research_attempts + 1
            candidate = await saver.save(
                SaveEvent("attempt_started", candidate.canonical_id, attempt_number=attempt_number)
            )
            research = await self.researcher.research(
                candidate.model_copy(deep=True), metrics, sources
            )
            candidate = await saver.save(
                SaveEvent(
                    "research_saved",
                    candidate.canonical_id,
                    attempt_number=attempt_number,
                    raw_json=research.raw_json,
                )
            )
        if research.canonical_name and name_filter.excludes_name(research.canonical_name):
            await saver.save(SaveEvent("candidate_rejected", candidate.canonical_id))
            return candidate, _WorkerOutcome(rejected=True)
        verification = await self.verifier.verify(
            candidate.model_copy(deep=True), research, metrics, sources
        )
        candidate = await saver.save(
            SaveEvent(
                "verification_saved",
                candidate.canonical_id,
                attempt_number=attempt_number,
                raw_json=verification.raw_json,
            )
        )
        if verification.canonical_name and name_filter.excludes_name(verification.canonical_name):
            await saver.save(SaveEvent("candidate_rejected", candidate.canonical_id))
            return candidate, _WorkerOutcome(rejected=True)
        return candidate, _WorkerOutcome(research, verification)

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
