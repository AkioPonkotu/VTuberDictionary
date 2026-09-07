"""Candidate discovery deliberately produces unverified candidates only."""

from __future__ import annotations

from typing import cast

from .domain import Agency, Candidate, now
from .ports import AgencyTalentSource, TwitchStreamSource
from .repository import CandidateRepository


class AgencyDiscovery:
    def __init__(self, source: AgencyTalentSource) -> None:
        self.source = source

    async def discover(self, agencies: list[Agency]) -> list[Candidate]:
        found: list[Candidate] = []
        for agency in agencies:
            for candidate in await self.source.list_talents(agency):
                candidate.agency = candidate.agency or agency.name
                candidate.discovery_sources.add("agency")
                found.append(candidate)
            agency.last_checked_at = now()
        return found


class TwitchDiscovery:
    def __init__(self, source: TwitchStreamSource, tag: str, language: str | None) -> None:
        self.source, self.tag, self.language = source, tag.casefold(), language or None

    async def discover(self) -> list[Candidate]:
        by_user: dict[str, Candidate] = {}
        async for page in self.source.streams(self.language):
            for stream in page:
                tags = [str(item).casefold() for item in cast(list[object], stream.get("tags", []))]
                user_id = str(stream.get("user_id", ""))
                if self.tag not in tags or not user_id:
                    continue
                login = str(stream.get("user_login", "")) or None
                by_user[user_id] = Candidate(
                    display_name=str(stream.get("user_name", login or user_id)),
                    twitch_user_id=user_id,
                    twitch_login=login,
                    twitch_url=f"https://www.twitch.tv/{login}" if login else None,
                    discovery_sources={"twitch_vtuber_tag"},
                )
        return list(by_user.values())


async def persist_discoveries(
    repository: CandidateRepository, candidates: list[Candidate]
) -> list[Candidate]:
    return [repository.upsert(candidate) for candidate in candidates]
