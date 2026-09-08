"""Interfaces for external services, designed to be replaced by test fakes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from .domain import (
    Agency,
    AudienceMetrics,
    Candidate,
    ResearchResult,
    VerificationResult,
    WebSource,
)


class AgencyTalentSource(Protocol):
    async def list_talents(self, agency: Agency) -> list[Candidate]: ...


class TwitchStreamSource(Protocol):
    def streams(self, language: str | None) -> AsyncIterator[list[dict[str, object]]]: ...


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
