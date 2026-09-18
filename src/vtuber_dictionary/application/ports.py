"""Interfaces for external services, designed to be replaced by test fakes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain import (
    Agency,
    AudienceMetrics,
    Candidate,
    DictionaryEntry,
    ResearchResult,
    ReviewRecord,
    VerificationResult,
    WebSource,
)


class AccessDeniedError(Exception):
    """A public source rejected access and should be retried in a later run."""


class CandidateStore(Protocol):
    path: Path

    def all(self) -> list[Candidate]: ...

    def replace(self, candidates: Iterable[Candidate]) -> None: ...

    def upsert_many(self, incoming_values: Iterable[Candidate]) -> list[Candidate]: ...


class AgencyStore(Protocol):
    def replace(self, agencies: Iterable[Agency]) -> None: ...


class TwitchDiscoveryCheckpointStore(Protocol):
    def load(self, tag: str, language: str | None) -> str | None: ...

    def save(self, tag: str, language: str | None, cursor: str) -> None: ...

    def clear(self) -> None: ...


class EntryStore(Protocol):
    def all(self) -> list[DictionaryEntry]: ...

    def recover_publication(self, artifacts: Iterable[Path]) -> None: ...

    def publish(self, entries: Iterable[DictionaryEntry], artifacts: dict[Path, bytes]) -> None: ...


class ReviewStore(Protocol):
    def checkpoint_review(
        self, candidates: CandidateStore, values: Iterable[Candidate], record: ReviewRecord
    ) -> None: ...

    def recover_checkpoint(self, candidates: CandidateStore) -> None: ...


@dataclass(frozen=True)
class DictionaryArtifact:
    """A rendered distributable artifact, independent of its storage mechanism."""

    filename: str
    payload: bytes


class DictionaryRenderer(Protocol):
    def artifacts(self, entries: Iterable[DictionaryEntry]) -> list[DictionaryArtifact]: ...

    def artifact_paths(self, destination_directory: Path) -> list[Path]: ...


class PipelineSettings(Protocol):
    audience_threshold_mode: str
    data_dir: Path
    dist_dir: Path
    processing_concurrency: int
    twitch_min_followers: int
    youtube_min_subscribers: int


class AgencyTalentSource(Protocol):
    async def list_talents(self, agency: Agency) -> list[Candidate]: ...


@dataclass(frozen=True)
class TwitchStreamPage:
    streams: list[dict[str, object]]
    next_cursor: str | None


class TwitchStreamSource(Protocol):
    def stream_pages(
        self, language: str | None, cursor: str | None = None
    ) -> AsyncIterator[TwitchStreamPage]: ...


class PlatformMetadataSource(Protocol):
    async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics: ...


class WebSourcePrefetcher(Protocol):
    async def fetch(self, candidate: Candidate, metrics: AudienceMetrics) -> list[WebSource]: ...


class ReadingResearcher(Protocol):
    async def research(
        self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
    ) -> ResearchResult: ...


class Verifier(Protocol):
    async def verify(
        self,
        candidate: Candidate,
        research: ResearchResult,
        metrics: AudienceMetrics,
        sources: list[WebSource],
    ) -> VerificationResult: ...
