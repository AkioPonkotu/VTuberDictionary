"""Pure domain models; these do not depend on HTTP clients or agents."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(UTC)


class CandidateStatus(StrEnum):
    DISCOVERED = "discovered"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"
    VERIFIED = "verified"


class AgentAttempt(BaseModel):
    """Durable record of one research/verification attempt for a candidate."""

    number: int
    research_raw_json: str | None = None
    verification_raw_json: str | None = None
    failure_reason: str | None = None


class Candidate(BaseModel):
    canonical_id: str = Field(default_factory=lambda: str(uuid4()))
    display_name: str
    agency: str | None = None
    official_profile_url: str | None = None
    youtube_channel_id: str | None = None
    youtube_channel_url: str | None = None
    twitch_user_id: str | None = None
    twitch_login: str | None = None
    twitch_url: str | None = None
    discovery_sources: set[str] = Field(default_factory=set)
    first_discovered_at: datetime = Field(default_factory=now)
    last_seen_at: datetime = Field(default_factory=now)
    status: CandidateStatus = CandidateStatus.DISCOVERED
    # A durable result checkpoint.  It is cleared only after the entry and TSV
    # have been published together, so a restart never has to call an agent again.
    pending_entry: DictionaryEntry | None = None
    # The exact structured responses returned by the two agents.  These are
    # retained even when deterministic validation sends the candidate to review.
    research_raw_json: str | None = None
    verification_raw_json: str | None = None
    research_attempts: int = 0
    retry_reason: str | None = None
    agent_attempts: list[AgentAttempt] = Field(default_factory=list)

    def identity_keys(self) -> set[str]:
        """Keys supported by explicit identity evidence, never a display name."""
        return {
            key
            for key in (
                f"youtube:{self.youtube_channel_id}" if self.youtube_channel_id else None,
                f"twitch:{self.twitch_user_id}" if self.twitch_user_id else None,
                f"profile:{self.official_profile_url}" if self.official_profile_url else None,
                f"youtube-url:{self.youtube_channel_url.rstrip('/')}"
                if self.youtube_channel_url
                else None,
                f"twitch-url:{self.twitch_url.rstrip('/')}" if self.twitch_url else None,
            )
            if key
        }


class Agency(BaseModel):
    name: str
    official_url: str
    talent_list_url: str
    profile_url_pattern: str | None = None
    last_checked_at: datetime | None = None
    active: bool = True


class AudienceMetrics(BaseModel):
    youtube_subscribers: int | None = None
    youtube_hidden: bool = False
    youtube_title: str | None = None
    twitch_followers: int | None = None
    twitch_display_name: str | None = None
    youtube_description: str | None = None
    twitch_description: str | None = None


class Evidence(BaseModel):
    url: str
    source_type: str
    claim: str


class WebSource(BaseModel):
    """A bounded, application-fetched public source supplied to an agent."""

    url: str
    source_type: str
    content: str


class NameReadingParts(BaseModel):
    """The source-backed surname/given-name breakdown returned by an agent.

    A one-component stage name uses ``null`` for the family-name pair and puts
    the complete name and reading in the given-name pair.  Keeping the pairs
    nullable rather than guessing a split is important for mononyms.
    """

    family_name: str | None = Field(
        ..., description="Official surname spelling, or null for a one-component stage name."
    )
    given_name: str | None = Field(
        ..., description="Official given-name spelling, or null when no name was resolved."
    )
    family_reading: str | None = Field(
        ..., description="Hiragana reading of family_name, or null with family_name."
    )
    given_reading: str | None = Field(
        ..., description="Hiragana reading of given_name, or null with given_name."
    )


class ResearchResult(BaseModel):
    canonical_name: str | None = None
    reading: str | None = None
    name_parts: NameReadingParts
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)
    status: Literal["resolved", "unresolved"]
    raw_json: str | None = Field(default=None, exclude=True)


class VerificationResult(BaseModel):
    verified: bool
    canonical_name: str | None = None
    reading: str | None = None
    name_parts: NameReadingParts
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    raw_json: str | None = Field(default=None, exclude=True)


class DictionaryEntry(BaseModel):
    canonical_id: str
    reading: str
    canonical_name: str
    agency: str | None = None
    youtube_channel_id: str | None = None
    twitch_user_id: str | None = None
    source_urls: list[str]
    verified_at: datetime = Field(default_factory=now)


class ReviewRecord(BaseModel):
    canonical_id: str
    reason: str
    candidate: Candidate
    created_at: datetime = Field(default_factory=now)
