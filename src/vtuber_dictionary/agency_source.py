"""Conservative HTML-based agency discovery for official talent-list pages."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .domain import Agency, Candidate
from .platforms import RetryingHttpClient, valid_twitch_login


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.current_url: str | None = None
        self.current_text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            value = dict(attrs).get("href")
            if value:
                self.current_url, self.current_text = value, []

    def handle_data(self, data: str) -> None:
        if self.current_url:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_url:
            self.links.append((self.current_url, " ".join(self.current_text).strip()))
            self.current_url = None


class AgencyPageTalentSource:
    """Extract same-site profile links from a configured official talent list.

    Agency pages vary widely, so links without useful visible text are ignored.
    Extra agency-specific parsers can implement ``AgencyTalentSource`` without
    altering the pipeline.
    """

    def __init__(self, http: RetryingHttpClient | None = None) -> None:
        self.http = http or RetryingHttpClient()

    async def list_talents(self, agency: Agency) -> list[Candidate]:
        body = await self.http.get_text(agency.talent_list_url)
        parser = _Links()
        parser.feed(body)
        allowed_host = urlparse(agency.official_url).netloc
        profile_pattern = (
            re.compile(agency.profile_url_pattern) if agency.profile_url_pattern else None
        )
        candidates: list[Candidate] = []
        for href, name in parser.links:
            profile = urljoin(agency.talent_list_url, href)
            if (
                not name
                or urlparse(profile).netloc != allowed_host
                or (
                    profile_pattern is not None
                    and not profile_pattern.fullmatch(urlparse(profile).path)
                )
            ):
                continue
            candidate = Candidate(
                display_name=name, agency=agency.name, official_profile_url=profile
            )
            await self._add_official_platform_links(candidate)
            candidates.append(candidate)
        return candidates

    async def _add_official_platform_links(self, candidate: Candidate) -> None:
        """Use links embedded in the official profile as identity evidence."""
        if not candidate.official_profile_url:
            return
        parser = _Links()
        parser.feed(await self.http.get_text(candidate.official_profile_url))
        for href, label in parser.links:
            label = label.strip().casefold()
            if label not in {"youtube", "twitch"}:
                continue
            url = urljoin(candidate.official_profile_url, href).rstrip("/")
            parsed = urlparse(url)
            host = parsed.netloc.casefold().removeprefix("www.")
            parts = [part for part in parsed.path.split("/") if part]
            if label == "youtube" and host in {"youtube.com", "m.youtube.com"}:
                candidate.youtube_channel_url = url
                if len(parts) >= 2 and parts[0] == "channel":
                    candidate.youtube_channel_id = parts[1]
            elif label == "twitch" and host == "twitch.tv" and parts:
                login = valid_twitch_login(parts[0])
                if login:
                    candidate.twitch_url = f"https://www.twitch.tv/{login}"
                    candidate.twitch_login = login
