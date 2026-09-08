"""Conservative official-site discovery for agency talent lists and sitemaps."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .domain import Agency, Candidate
from .platforms import ApiError, RetryingHttpClient, valid_twitch_login


@dataclass(frozen=True)
class _Link:
    url: str
    name: str


class _Links(HTMLParser):
    """Collect anchors, including accessible labels and nested image alt text."""

    def __init__(self) -> None:
        super().__init__()
        self.current_url: str | None = None
        self.current_text: list[str] = []
        self.links: list[_Link] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a":
            value = attributes.get("href")
            if value:
                self.current_url = value
                self.current_text = [
                    value
                    for value in (attributes.get("aria-label"), attributes.get("title"))
                    if value
                ]
        elif self.current_url and tag == "img" and (alt := attributes.get("alt")):
            self.current_text.append(alt)

    def handle_data(self, data: str) -> None:
        if self.current_url:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_url:
            self.links.append(_Link(self.current_url, " ".join(self.current_text).strip()))
            self.current_url = None


class _Title(HTMLParser):
    """Read a profile's official title when its list card has no visible text."""

    def __init__(self) -> None:
        super().__init__()
        self.values: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") in {"og:title", "twitter:title"}:
            if content := attributes.get("content"):
                self.values.append(content)
        elif tag == "title":
            self.in_title = True

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.values.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def first(self) -> str | None:
        for value in self.values:
            if normalized := " ".join(value.split()):
                return normalized
        return None


class _RosterCards(HTMLParser):
    """Collect name/YouTube pairs from official image-card rosters."""

    _VOID_TAGS = {"area", "base", "br", "embed", "hr", "img", "input", "link", "meta", "source"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.card_depth: int | None = None
        self.name_depth: int | None = None
        self.name: str | None = None
        self.youtube_url: str | None = None
        self.cards: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag not in self._VOID_TAGS:
            self.stack.append(tag)
        depth = len(self.stack)
        classes = set((attributes.get("class") or "").split())
        if self.card_depth is None and "member__profile" in classes:
            self.card_depth, self.name, self.youtube_url = depth, None, None
        if self.card_depth is None:
            return
        if "member__name" in classes:
            self.name_depth = depth
        if tag == "img" and self.name_depth is not None and (alt := attributes.get("alt")):
            self.name = alt.strip()
        if tag == "a" and (href := attributes.get("href")):
            parsed = urlparse(href)
            if parsed.netloc.casefold().removeprefix("www.") in {"youtube.com", "m.youtube.com"}:
                self.youtube_url = href

    def handle_endtag(self, tag: str) -> None:
        depth = len(self.stack)
        if self.card_depth is not None and depth == self.card_depth and tag == self.stack[-1]:
            if self.name and self.youtube_url:
                self.cards.append((self.name, self.youtube_url))
            self.card_depth, self.name_depth = None, None
        elif self.name_depth is not None and depth == self.name_depth and tag == self.stack[-1]:
            self.name_depth = None
        if tag not in self._VOID_TAGS and self.stack:
            self.stack.pop()


class AgencyPageTalentSource:
    """Extract official talent profiles without treating navigation as candidates.

    A number of official rosters use image-only cards or client-side rendering.
    We first inspect ordinary anchors, then a public Next.js roster payload, and
    finally the agency's published sitemap. Every fallback remains constrained
    to the configured official host and profile URL pattern.
    """

    _MAX_SITEMAPS = 20

    def __init__(self, http: RetryingHttpClient | None = None) -> None:
        self.http = http or RetryingHttpClient()

    async def list_talents(self, agency: Agency) -> list[Candidate]:
        body = await self.http.get_text(agency.talent_list_url)
        profiles = self._profile_links(agency, self._links(body))
        profiles = self._merge_profile_links(profiles, self._next_data_profile_links(agency, body))
        if not profiles:
            if card_candidates := self._roster_card_candidates(agency, body):
                return card_candidates
            profiles = await self._sitemap_profile_links(agency)

        candidates: list[Candidate] = []
        for profile, name in profiles.items():
            candidate = Candidate(
                display_name=name or profile,
                agency=agency.name,
                official_profile_url=profile,
            )
            title = await self._add_official_platform_links(candidate)
            if not name:
                if title is None:
                    continue
                candidate.display_name = title
            candidates.append(candidate)
        return candidates

    @staticmethod
    def _links(body: str) -> list[_Link]:
        parser = _Links()
        parser.feed(body)
        return parser.links

    @staticmethod
    def _matches_profile(agency: Agency, profile: str) -> bool:
        if any(ord(character) < 32 or ord(character) == 127 for character in profile):
            return False
        if urlparse(profile).netloc.casefold() != urlparse(agency.official_url).netloc.casefold():
            return False
        if agency.profile_url_pattern is None:
            return True
        return re.fullmatch(agency.profile_url_pattern, urlparse(profile).path) is not None

    def _profile_links(self, agency: Agency, links: list[_Link]) -> dict[str, str]:
        profiles: dict[str, str] = {}
        for link in links:
            profile = urljoin(agency.talent_list_url, link.url)
            if self._matches_profile(agency, profile):
                profiles.setdefault(profile, link.name)
                if link.name:
                    profiles[profile] = link.name
        return profiles

    @staticmethod
    def _merge_profile_links(*groups: dict[str, str]) -> dict[str, str]:
        merged: dict[str, str] = {}
        for group in groups:
            for profile, name in group.items():
                if profile not in merged or (name and not merged[profile]):
                    merged[profile] = name
        return merged

    @staticmethod
    def _roster_card_candidates(agency: Agency, body: str) -> list[Candidate]:
        parser = _RosterCards()
        parser.feed(body)
        candidates: list[Candidate] = []
        for name, youtube_url in parser.cards:
            url = youtube_url.rstrip("/")
            parts = [part for part in urlparse(url).path.split("/") if part]
            channel_id = parts[1] if len(parts) >= 2 and parts[0] == "channel" else None
            candidates.append(
                Candidate(
                    display_name=name,
                    agency=agency.name,
                    youtube_channel_id=channel_id,
                    youtube_channel_url=url,
                )
            )
        return candidates

    def _next_data_profile_links(self, agency: Agency, body: str) -> dict[str, str]:
        """Use the public `allLivers` payload emitted by Nijisanji's Next.js page."""
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', body, re.DOTALL
        )
        if not match:
            return {}
        try:
            payload = json.loads(unescape(match.group(1)))
            livers = payload["props"]["pageProps"]["allLivers"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return {}
        if not isinstance(livers, list):
            return {}

        profiles: dict[str, str] = {}
        for liver in livers:
            if not isinstance(liver, dict):
                continue
            slug, name = liver.get("slug"), liver.get("name")
            if not isinstance(slug, str) or not isinstance(name, str) or not slug or not name:
                continue
            profile = urljoin(agency.official_url, f"/talents/l/{slug}")
            if self._matches_profile(agency, profile):
                profiles[profile] = name.strip()
        return profiles

    async def _sitemap_profile_links(self, agency: Agency) -> dict[str, str]:
        """Find profile URLs from an official sitemap when roster cards are not in HTML."""
        profiles: dict[str, str] = {}
        for sitemap_url in await self._sitemap_urls(agency):
            try:
                body = await self.http.get_text(sitemap_url)
                root = ET.fromstring(body)
            except (ApiError, ET.ParseError):
                continue
            if root.tag.rsplit("}", 1)[-1] == "sitemapindex":
                continue
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] != "loc" or not element.text:
                    continue
                location = element.text.strip()
                if self._matches_profile(agency, location):
                    profiles.setdefault(location, "")
        return profiles

    async def _sitemap_urls(self, agency: Agency) -> list[str]:
        try:
            robots = await self.http.get_text(urljoin(agency.official_url, "/robots.txt"))
        except ApiError:
            robots = ""
        discovered = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
        if not discovered:
            discovered = [
                urljoin(agency.official_url, "/sitemap.xml"),
                urljoin(agency.official_url, "/wp-sitemap.xml"),
                urljoin(agency.official_url, "/sitemap_index.xml"),
            ]

        pending = list(dict.fromkeys(discovered))
        seen: set[str] = set()
        result: list[str] = []
        while pending and len(seen) < self._MAX_SITEMAPS:
            sitemap_url = pending.pop(0)
            if sitemap_url in seen:
                continue
            seen.add(sitemap_url)
            try:
                body = await self.http.get_text(sitemap_url)
                root = ET.fromstring(body)
            except (ApiError, ET.ParseError):
                continue
            result.append(sitemap_url)
            if root.tag.rsplit("}", 1)[-1] == "sitemapindex":
                pending.extend(
                    element.text.strip()
                    for element in root.iter()
                    if element.tag.rsplit("}", 1)[-1] == "loc" and element.text
                )
        return result

    async def _add_official_platform_links(self, candidate: Candidate) -> str | None:
        """Use profile links as identity evidence and return its official title."""
        if not candidate.official_profile_url:
            return None
        body = await self.http.get_text(candidate.official_profile_url)
        title_parser = _Title()
        title_parser.feed(body)
        for link in self._links(body):
            label = link.name.strip().casefold()
            url = urljoin(candidate.official_profile_url, link.url).rstrip("/")
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
        return title_parser.first()
