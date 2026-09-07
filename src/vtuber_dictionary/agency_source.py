"""Conservative HTML-based agency discovery for official talent-list pages."""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .domain import Agency, Candidate
from .platforms import RetryingHttpClient


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
        response = await self.http.client.get(agency.talent_list_url)
        response.raise_for_status()
        parser = _Links()
        parser.feed(response.text)
        allowed_host = urlparse(agency.official_url).netloc
        candidates: list[Candidate] = []
        for href, name in parser.links:
            profile = urljoin(agency.talent_list_url, href)
            if not name or urlparse(profile).netloc != allowed_host:
                continue
            candidates.append(
                Candidate(display_name=name, agency=agency.name, official_profile_url=profile)
            )
        return candidates
