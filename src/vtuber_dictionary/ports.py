"""Interfaces for external services, designed to be replaced by test fakes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from .domain import Agency, AudienceMetrics, Candidate, ResearchResult, VerificationResult


class AgencyTalentSource(Protocol):
    async def list_talents(self, agency: Agency) -> list[Candidate]: ...


class TwitchStreamSource(Protocol):
    def streams(self, language: str | None) -> AsyncIterator[list[dict[str, object]]]: ...


class PlatformMetadataSource(Protocol):
    async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics: ...


class ReadingResearcher(Protocol):
    async def research(self, candidate: Candidate, metrics: AudienceMetrics) -> ResearchResult: ...


class Verifier(Protocol):
    async def verify(
        self, candidate: Candidate, research: ResearchResult, metrics: AudienceMetrics
    ) -> VerificationResult: ...
