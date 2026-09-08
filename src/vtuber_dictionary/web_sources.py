"""Application-side collection of bounded public source material for agents."""

from __future__ import annotations

from html.parser import HTMLParser

from .domain import AudienceMetrics, Candidate, WebSource
from .platforms import ApiError, RetryingHttpClient


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
