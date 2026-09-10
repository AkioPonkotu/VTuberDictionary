"""Application-side collection of bounded public source material for agents."""

from __future__ import annotations

import asyncio
import ipaddress
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from .domain import AudienceMetrics, Candidate, WebSource
from .platforms import ApiError, ClientRequestError, RetryingHttpClient


class _VisibleText(HTMLParser):
    """Extract visible text while excluding executable and styling content."""

    _IGNORED = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._IGNORED:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def visible_text(html: str, maximum_characters: int) -> str:
    parser = _VisibleText()
    parser.feed(html)
    return parser.text()[:maximum_characters]


class _SearchResultLinks(HTMLParser):
    """Read only the outbound links in DuckDuckGo's no-JavaScript results page."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        href = attributes.get("href")
        if "result__a" in classes and href:
            self.hrefs.append(href)


class OfficialSourcePrefetcher:
    """Fetch candidate-owned official sources before making any model request.

    This intentionally does not provide a model-callable search tool. It only
    follows the profile URL established during agency discovery and the platform
    descriptions already obtained from official APIs.
    """

    def __init__(
        self, http: RetryingHttpClient | None = None, maximum_characters: int = 12_000
    ) -> None:
        self.http = http or RetryingHttpClient()
        self.maximum_characters = maximum_characters

    async def fetch(self, candidate: Candidate, metrics: AudienceMetrics) -> list[WebSource]:
        sources: list[WebSource] = []
        if candidate.official_profile_url:
            try:
                body = await self.http.get_text(candidate.official_profile_url)
            except ApiError:
                body = ""
            if content := visible_text(body, self.maximum_characters):
                sources.append(
                    WebSource(
                        url=candidate.official_profile_url,
                        source_type=(
                            "official_agency_profile" if candidate.agency else "official_profile"
                        ),
                        content=content,
                    )
                )
        if metrics.youtube_description and candidate.youtube_channel_url:
            sources.append(
                WebSource(
                    url=candidate.youtube_channel_url,
                    source_type="youtube_about",
                    content=metrics.youtube_description[: self.maximum_characters],
                )
            )
        if metrics.twitch_description and candidate.twitch_url:
            sources.append(
                WebSource(
                    url=candidate.twitch_url,
                    source_type="twitch_about",
                    content=metrics.twitch_description[: self.maximum_characters],
                )
            )
        return sources


class TwitchSearchSourcePrefetcher(OfficialSourcePrefetcher):
    """Add bounded, robots-aware web results for candidates discovered from Twitch.

    The crawler deliberately searches only for Twitch-tag discovery candidates. Agency
    candidates retain their direct, official-profile-only path. Search pages and result
    pages are fetched concurrently with bounded candidate and HTTP pools.
    """

    _SEARCH_URL = "https://html.duckduckgo.com/html/"
    _SEARCH_HOSTS = {"duckduckgo.com", "html.duckduckgo.com"}
    _USER_AGENT = "VTuberDictionary/0.1 (+https://github.com/AkioPonkotu/VTuberDictionary)"

    def __init__(
        self,
        http: RetryingHttpClient | None = None,
        maximum_characters: int = 12_000,
        *,
        enabled: bool = True,
        max_results: int = 3,
        minimum_delay_seconds: float = 0.0,
        max_concurrency: int = 32,
        search_source_maximum_characters: int = 3_000,
    ) -> None:
        super().__init__(
            http=http
            or RetryingHttpClient(
                max_concurrency=max_concurrency, per_host_concurrency=max_concurrency
            ),
            maximum_characters=maximum_characters,
        )
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        if minimum_delay_seconds < 0:
            raise ValueError("minimum_delay_seconds cannot be negative")
        self.enabled = enabled
        self.max_results = max_results
        self.minimum_delay_seconds = minimum_delay_seconds
        self.search_source_maximum_characters = search_source_maximum_characters
        self._robots: dict[str, RobotFileParser | bool] = {}
        self._crawl_slots = asyncio.Semaphore(max_concurrency)
        self._robots_lock = asyncio.Lock()

    async def fetch(self, candidate: Candidate, metrics: AudienceMetrics) -> list[WebSource]:
        sources = await super().fetch(candidate, metrics)
        if not self.enabled or "twitch_vtuber_tag" not in candidate.discovery_sources:
            return sources
        async with self._crawl_slots:
            for result_url in await self._search(candidate, metrics):
                if await self._can_fetch(result_url):
                    body = await self._get_text(result_url)
                    if content := visible_text(body, self.search_source_maximum_characters):
                        sources.append(
                            WebSource(url=result_url, source_type="other", content=content)
                        )
        return sources

    async def _search(self, candidate: Candidate, metrics: AudienceMetrics) -> list[str]:
        name = metrics.twitch_display_name or candidate.display_name
        query = f'"{name}" VTuber 公式 読み'
        if candidate.twitch_login:
            query = f"{query} {candidate.twitch_login}"
        search_url = f"{self._SEARCH_URL}?{urlencode({'q': query, 'kl': 'jp-jp'})}"
        body = await self._get_text(search_url)
        parser = _SearchResultLinks()
        parser.feed(body)
        urls: list[str] = []
        for href in parser.hrefs:
            if (url := self._result_url(href)) and url not in urls:
                urls.append(url)
            if len(urls) == self.max_results:
                break
        return urls

    async def _get_text(self, url: str) -> str:
        try:
            return await self.http.get_text(url, headers={"User-Agent": self._USER_AGENT})
        except ApiError:
            return ""

    async def _can_fetch(self, url: str) -> bool:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        async with self._robots_lock:
            policy = self._robots.get(origin)
        if policy is None:
            try:
                robots = await self._get_text_or_raise(urljoin(origin, "/robots.txt"))
            except ClientRequestError:
                # A missing robots.txt permits crawling under the standard robots
                # convention. Authentication, rate-limit, and transport errors deny it.
                policy = True
            except ApiError:
                policy = False
            else:
                parser = RobotFileParser()
                parser.parse(robots.splitlines())
                policy = parser
            async with self._robots_lock:
                self._robots.setdefault(origin, policy)
        return policy is True or (
            isinstance(policy, RobotFileParser) and policy.can_fetch(self._USER_AGENT, url)
        )

    async def _get_text_or_raise(self, url: str) -> str:
        return await self.http.get_text(url, headers={"User-Agent": self._USER_AGENT})

    @classmethod
    def _result_url(cls, href: str) -> str | None:
        absolute = urljoin(cls._SEARCH_URL, href)
        parsed = urlsplit(absolute)
        if parsed.hostname and parsed.hostname.casefold() in cls._SEARCH_HOSTS:
            destination = parse_qs(parsed.query).get("uddg", [None])[0]
            if destination:
                parsed = urlsplit(destination)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.hostname.casefold() in cls._SEARCH_HOSTS:
            return None
        try:
            if not ipaddress.ip_address(parsed.hostname).is_global:
                return None
        except ValueError:
            pass
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
