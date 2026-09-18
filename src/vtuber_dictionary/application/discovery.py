"""Candidate discovery deliberately produces unverified candidates only."""

from __future__ import annotations

import logging
from collections import Counter

from ..domain import Agency, Candidate, now
from .ports import (
    AccessDeniedError,
    AgencyStore,
    AgencyTalentSource,
    CandidateStore,
    TwitchDiscoveryCheckpointStore,
    TwitchStreamSource,
)

LOG = logging.getLogger(__name__)


class AgencyDiscovery:
    def __init__(self, source: AgencyTalentSource) -> None:
        self.source = source

    async def discover(
        self,
        agencies: list[Agency],
        candidates_repository: CandidateStore | None = None,
        agencies_repository: AgencyStore | None = None,
    ) -> list[Candidate]:
        found: list[Candidate] = []
        for agency in agencies:
            try:
                candidates = await self.source.list_talents(agency)
            except AccessDeniedError:
                # Public rosters can return 401/403 because of temporary WAF
                # rules or runner IP restrictions.  Leave last_checked_at
                # unchanged so this active agency is retried on the next run.
                LOG.warning(
                    "agency_discovery_access_denied agency=%s url=%s retry_on_next_run=true",
                    agency.name,
                    agency.talent_list_url,
                )
                continue
            except Exception as error:
                # Each agency is an independent public source.  Network,
                # server, parsing, and other source-specific failures must
                # not prevent the remaining agencies from being discovered.
                # Keep last_checked_at unchanged so the failed agency is
                # retried on the next scheduled run.
                LOG.warning(
                    "agency_discovery_failed agency=%s url=%s error_type=%s retry_on_next_run=true",
                    agency.name,
                    agency.talent_list_url,
                    type(error).__name__,
                    exc_info=True,
                )
                continue
            self._discard_shared_platform_links(candidates)
            for candidate in candidates:
                candidate.agency = candidate.agency or agency.name
                candidate.discovery_sources.add("agency")
                found.append(candidate)
            # Store every agency's page before moving on.  Re-fetching a page
            # after a crash is safe because the candidate store upserts it.
            if candidates_repository:
                candidates_repository.upsert_many(candidates)
            agency.last_checked_at = now()
            if agencies_repository:
                agencies_repository.replace(agencies)
        return found

    @staticmethod
    def _discard_shared_platform_links(candidates: list[Candidate]) -> None:
        """Do not attribute an agency-wide platform account to individual talents."""
        for id_field, url_field in (
            ("youtube_channel_id", "youtube_channel_url"),
            ("twitch_user_id", "twitch_url"),
        ):
            keys = [
                getattr(candidate, id_field) or getattr(candidate, url_field)
                for candidate in candidates
            ]
            duplicated = {
                key for key, count in Counter(key for key in keys if key).items() if count > 1
            }
            for candidate in candidates:
                key = getattr(candidate, id_field) or getattr(candidate, url_field)
                if key in duplicated:
                    setattr(candidate, id_field, None)
                    setattr(candidate, url_field, None)


class TwitchDiscovery:
    def __init__(self, source: TwitchStreamSource, tag: str, language: str | None) -> None:
        self.source, self.tag, self.language = source, tag.casefold(), language or None

    async def discover(
        self,
        candidates_repository: CandidateStore | None = None,
        checkpoint_repository: TwitchDiscoveryCheckpointStore | None = None,
    ) -> list[Candidate]:
        by_user: dict[str, Candidate] = {}
        cursor = (
            checkpoint_repository.load(self.tag, self.language) if checkpoint_repository else None
        )
        async for page in self.source.stream_pages(self.language, cursor):
            discovered_on_page: list[Candidate] = []
            for stream in page.streams:
                raw_tags = stream.get("tags")
                tags = (
                    [str(item).casefold() for item in raw_tags]
                    if isinstance(raw_tags, list)
                    else []
                )
                user_id = str(stream.get("user_id", ""))
                if self.tag not in tags or not user_id:
                    continue
                login = str(stream.get("user_login", "")) or None
                candidate = Candidate(
                    display_name=str(stream.get("user_name", login or user_id)),
                    twitch_user_id=user_id,
                    twitch_login=login,
                    twitch_url=f"https://www.twitch.tv/{login}" if login else None,
                    discovery_sources={"twitch_vtuber_tag"},
                )
                by_user[user_id] = candidate
                discovered_on_page.append(candidate)
            # The candidate page is durable before its continuation token.  A
            # crash can at most replay this page, never skip it.
            if candidates_repository:
                candidates_repository.upsert_many(discovered_on_page)
            if checkpoint_repository:
                if page.next_cursor:
                    checkpoint_repository.save(self.tag, self.language, page.next_cursor)
                else:
                    checkpoint_repository.clear()
        return list(by_user.values())


async def persist_discoveries(
    repository: CandidateStore, candidates: list[Candidate]
) -> list[Candidate]:
    return repository.upsert_many(candidates)
