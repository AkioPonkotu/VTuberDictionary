from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from vtuber_dictionary.dictionary import DictionaryCompiler
from vtuber_dictionary.domain import (
    AudienceMetrics,
    Candidate,
    DictionaryEntry,
    Evidence,
    NameReadingParts,
    ResearchResult,
    VerificationResult,
)
from vtuber_dictionary.repository import CandidateRepository, EntryRepository, ReviewRepository
from vtuber_dictionary.settings import Settings
from vtuber_dictionary.validation import DeterministicValidator
from vtuber_dictionary.workflow import Pipeline


def _parts(name: str, reading: str) -> NameReadingParts:
    return NameReadingParts(
        family_name=None, given_name=name, family_reading=None, given_reading=reading
    )


class _RecordingEntries(EntryRepository):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.publications: list[list[str]] = []

    def publish(self, entries: list[DictionaryEntry], artifacts: dict[Path, bytes]) -> None:
        self.publications.append([entry.canonical_id for entry in entries])
        super().publish(entries, artifacts)


@pytest.mark.asyncio
async def test_parallel_workers_are_committed_when_they_finish(tmp_path: Path) -> None:
    active = 0
    maximum = 0
    calls: list[str] = []
    name_a, name_b = f"{chr(0x540D)}A", f"{chr(0x540D)}B"
    reading_a = "".join(map(chr, (0x306A, 0x307E, 0x3048)))
    reading_b = "".join(map(chr, (0x306A, 0x307E, 0x3048, 0x306B)))

    class Metrics:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if candidate.display_name == name_a:
                await asyncio.sleep(0.04)
            active -= 1
            return AudienceMetrics(youtube_subscribers=1)

    class Researcher:
        async def research(self, candidate: Candidate, *_: object) -> ResearchResult:
            calls.append(f"research:{candidate.display_name}")
            reading = reading_a if candidate.display_name == name_a else reading_b
            return ResearchResult(
                canonical_name=candidate.display_name,
                reading=reading,
                name_parts=_parts(candidate.display_name, reading),
                confidence=1,
                evidence=[
                    Evidence(
                        url="https://official.example", source_type="official_profile", claim="x"
                    )
                ],
                status="resolved",
                raw_json="{}",
            )

    class Verifier:
        async def verify(
            self, candidate: Candidate, research: ResearchResult, *_: object
        ) -> VerificationResult:
            calls.append(f"verify:{candidate.display_name}")
            return VerificationResult(
                verified=True,
                canonical_name=research.canonical_name,
                reading=research.reading,
                name_parts=research.name_parts,
                confidence=1,
                evidence=research.evidence,
                raw_json="{}",
            )

    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    start = datetime(2024, 1, 1, tzinfo=UTC)
    a = Candidate(display_name=name_a, agency="test", first_discovered_at=start)
    b = Candidate(
        display_name=name_b, agency="test", first_discovered_at=start + timedelta(seconds=1)
    )
    candidates.replace([a, b])
    entries = _RecordingEntries(data_dir / "entries.jsonl")
    pipeline = Pipeline(
        candidates,
        entries,
        ReviewRepository(data_dir / "review_required.jsonl"),
        Metrics(),
        Researcher(),
        Verifier(),
        DeterministicValidator(),
        DictionaryCompiler(),
        Settings(data_dir=data_dir, dist_dir=dist_dir, processing_concurrency=2),
    )

    assert await pipeline.run() == 2
    assert maximum == 2
    assert calls.index(f"research:{name_b}") < calls.index(f"verify:{name_a}")
    assert entries.publications == [[b.canonical_id], [b.canonical_id, a.canonical_id]]


def test_processing_concurrency_environment_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROCESSING_CONCURRENCY", raising=False)
    assert Settings(_env_file=None).processing_concurrency == 6
    monkeypatch.setenv("PROCESSING_CONCURRENCY", "1")
    assert Settings(_env_file=None).processing_concurrency == 1
    monkeypatch.setenv("PROCESSING_CONCURRENCY", "128")
    assert Settings(_env_file=None).processing_concurrency == 128
    monkeypatch.setenv("PROCESSING_CONCURRENCY", "129")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_openai_concurrency_environment_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_CONCURRENCY", raising=False)
    assert Settings(_env_file=None).openai_concurrency == 2
    monkeypatch.setenv("OPENAI_CONCURRENCY", "96")
    assert Settings(_env_file=None).openai_concurrency == 96
    monkeypatch.setenv("OPENAI_CONCURRENCY", "129")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_openai_request_interval_environment_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_MIN_REQUEST_INTERVAL_SECONDS", raising=False)
    assert Settings(_env_file=None).openai_min_request_interval_seconds == 1
    monkeypatch.setenv("OPENAI_MIN_REQUEST_INTERVAL_SECONDS", "0.5")
    assert Settings(_env_file=None).openai_min_request_interval_seconds == 0.5
    monkeypatch.setenv("OPENAI_MIN_REQUEST_INTERVAL_SECONDS", "-0.1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_openai_output_and_rate_limit_recovery_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("OPENAI_RATE_LIMIT_RETRY_SECONDS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.openai_max_output_tokens == 384
    assert settings.openai_rate_limit_retry_seconds == 900
    monkeypatch.setenv("OPENAI_MAX_OUTPUT_TOKENS", "127")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("OPENAI_MAX_OUTPUT_TOKENS", "384")
    monkeypatch.setenv("OPENAI_RATE_LIMIT_RETRY_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
