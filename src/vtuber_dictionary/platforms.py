"""Official YouTube/Twitch HTTP clients with pagination, retry and timeouts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

from .domain import AudienceMetrics, Candidate

LOG = logging.getLogger(__name__)


class ApiError(RuntimeError):
    pass


class RetryingHttpClient:
    def __init__(self, timeout_seconds: float = 20, retries: int = 3) -> None:
        self.client = httpx.AsyncClient(timeout=timeout_seconds)
        self.retries = retries

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(self.retries):
            try:
                response = await self.client.get(url, params=params, headers=headers)
                if response.status_code in {408, 429} or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "retryable response", request=response.request, response=response
                    )
                response.raise_for_status()
                return cast(dict[str, Any], response.json())
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if attempt == self.retries - 1:
                    LOG.warning("api_request_failed", extra={"url": url, "attempt": attempt + 1})
                    raise ApiError(f"request failed: {url}") from exc
                await asyncio.sleep(2**attempt)
        raise AssertionError("unreachable")

    async def post_json(
        self,
        url: str,
        *,
        data: dict[str, str],
    ) -> dict[str, Any]:
        for attempt in range(self.retries):
            try:
                response = await self.client.post(url, data=data)
                if response.status_code in {408, 429} or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "retryable response", request=response.request, response=response
                    )
                response.raise_for_status()
                return cast(dict[str, Any], response.json())
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if attempt == self.retries - 1:
                    raise ApiError(f"request failed: {url}") from exc
                await asyncio.sleep(2**attempt)
        raise AssertionError("unreachable")

    async def get_text(self, url: str) -> str:
        for attempt in range(self.retries):
            try:
                response = await self.client.get(url)
                if response.status_code in {408, 429} or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "retryable response", request=response.request, response=response
                    )
                response.raise_for_status()
                return response.text
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if attempt == self.retries - 1:
                    raise ApiError(f"request failed: {url}") from exc
                await asyncio.sleep(2**attempt)
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        await self.client.aclose()


class TwitchHelixClient:
    def __init__(
        self,
        client_id: str,
        access_token: str,
        http: RetryingHttpClient | None = None,
        max_pages: int = 20,
    ) -> None:
        self.headers = {"Client-Id": client_id, "Authorization": f"Bearer {access_token}"}
        self.http, self.max_pages = http or RetryingHttpClient(), max_pages

    async def streams(self, language: str | None) -> AsyncIterator[list[dict[str, object]]]:
        cursor: str | None = None
        for _ in range(self.max_pages):
            params = {"first": "100"}
            if language:
                params["language"] = language
            if cursor:
                params["after"] = cursor
            payload = await self.http.get_json(
                "https://api.twitch.tv/helix/streams", params=params, headers=self.headers
            )
            yield list(payload.get("data", []))
            cursor = payload.get("pagination", {}).get("cursor")
            if not cursor:
                return

    async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
        if not candidate.twitch_user_id and candidate.twitch_login:
            lookup = await self.http.get_json(
                "https://api.twitch.tv/helix/users",
                params={"login": candidate.twitch_login},
                headers=self.headers,
            )
            users = lookup.get("data", [])
            if users:
                candidate.twitch_user_id = str(users[0]["id"])
        if not candidate.twitch_user_id:
            return AudienceMetrics()
        user = await self.http.get_json(
            "https://api.twitch.tv/helix/users",
            params={"id": candidate.twitch_user_id},
            headers=self.headers,
        )
        follows = await self.http.get_json(
            "https://api.twitch.tv/helix/channels/followers",
            params={"broadcaster_id": candidate.twitch_user_id},
            headers=self.headers,
        )
        data = user.get("data", [])
        return AudienceMetrics(
            twitch_description=data[0].get("description") if data else None,
            twitch_followers=follows.get("total"),
        )


async def twitch_app_access_token(
    client_id: str, client_secret: str, http: RetryingHttpClient | None = None
) -> str:
    """Obtain an app token for official Helix endpoints."""
    transport = http or RetryingHttpClient()
    payload = await transport.post_json(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
    )
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise ApiError("Twitch did not return an app access token")
    return token


class YouTubeDataClient:
    def __init__(self, api_key: str, http: RetryingHttpClient | None = None) -> None:
        self.api_key, self.http = api_key, http or RetryingHttpClient()

    async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
        params = {"part": "snippet,statistics", "key": self.api_key}
        if candidate.youtube_channel_id:
            params["id"] = candidate.youtube_channel_id
        elif candidate.youtube_channel_url and "/@" in candidate.youtube_channel_url:
            params["forHandle"] = candidate.youtube_channel_url.rsplit("/@", maxsplit=1)[1]
        else:
            return AudienceMetrics()
        data = await self.http.get_json(
            "https://www.googleapis.com/youtube/v3/channels",
            params=params,
        )
        items = data.get("items", [])
        if not items:
            return AudienceMetrics()
        item = items[0]
        candidate.youtube_channel_id = str(item["id"])
        stats = item.get("statistics", {})
        hidden = bool(stats.get("hiddenSubscriberCount", False))
        return AudienceMetrics(
            youtube_subscribers=None
            if hidden
            else int(stats["subscriberCount"])
            if "subscriberCount" in stats
            else None,
            youtube_hidden=hidden,
            youtube_description=item.get("snippet", {}).get("description"),
        )


class CombinedPlatformClient:
    def __init__(self, youtube: YouTubeDataClient | None, twitch: TwitchHelixClient | None) -> None:
        self.youtube, self.twitch = youtube, twitch

    async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
        youtube, twitch = await asyncio.gather(
            self.youtube.audience_metrics(candidate) if self.youtube else _empty(),
            self.twitch.audience_metrics(candidate) if self.twitch else _empty(),
        )
        return AudienceMetrics(
            **youtube.model_dump(),
            **{key: value for key, value in twitch.model_dump().items() if value is not None},
        )


async def _empty() -> AudienceMetrics:
    return AudienceMetrics()
