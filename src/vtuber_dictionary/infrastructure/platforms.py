"""Official YouTube/Twitch HTTP clients with pagination, retry and timeouts."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, Literal, cast
from urllib.parse import urlparse

import httpx

from ..application.ports import AccessDeniedError, TwitchStreamPage
from ..domain import AudienceMetrics, Candidate

LOG = logging.getLogger(__name__)
TWITCH_LOGIN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{3,24}$", re.IGNORECASE)


def valid_twitch_login(value: str) -> str | None:
    login = value.strip()
    return login if TWITCH_LOGIN_PATTERN.fullmatch(login) else None


class ApiError(RuntimeError):
    def __init__(
        self, message: str, *, retryable: bool = False, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class AuthenticationError(ApiError, AccessDeniedError):
    """The credentials lack access or are invalid."""


class RateLimitError(ApiError):
    """The service explicitly asked the client to slow down."""


class RemoteServiceError(ApiError):
    """A remote server returned a retryable 5xx response."""


class ClientRequestError(ApiError):
    """A non-retryable request failed before reaching a useful response."""


class RetryingHttpClient:
    """HTTP retry policy with global and per-origin request limits."""

    def __init__(
        self,
        timeout_seconds: float = 20,
        retries: int = 3,
        *,
        max_concurrency: int = 8,
        per_host_concurrency: int = 2,
    ) -> None:
        self.client = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True)
        self.retries = retries
        self._requests = asyncio.Semaphore(max_concurrency)
        self._per_host_concurrency = per_host_concurrency
        self._host_locks: dict[str, asyncio.Semaphore] = {}

    def _host_lock(self, url: str) -> asyncio.Semaphore:
        host = urlparse(url).netloc.casefold()
        return self._host_locks.setdefault(host, asyncio.Semaphore(self._per_host_concurrency))

    @staticmethod
    def _raise_classified(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code in {401, 403}:
            raise AuthenticationError(f"authentication failed for {response.url}")
        retry_after: float | None = None
        if value := response.headers.get("Retry-After"):
            with suppress(ValueError):
                retry_after = max(0.0, float(value))
        if response.status_code == 429:
            raise RateLimitError(
                f"rate limited by {response.url}", retryable=True, retry_after=retry_after
            )
        if response.status_code >= 500:
            raise RemoteServiceError(
                f"service error from {response.url}", retryable=True, retry_after=retry_after
            )
        raise ClientRequestError(f"request rejected by {response.url}: {response.status_code}")

    async def _request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        for attempt in range(self.retries):
            try:
                async with self._requests, self._host_lock(url):
                    response = await self.client.request(method, url, **kwargs)
                self._raise_classified(response)
                return response
            except httpx.TimeoutException as exc:
                error = ApiError(f"request timed out: {url}", retryable=True)
                error.__cause__ = exc
            except httpx.RequestError as exc:
                # Connection resets and incomplete reads are transient on
                # public search endpoints.  They need the same bounded retry
                # path as timeouts rather than cancelling the whole pipeline.
                error = ApiError(f"request failed: {url}", retryable=True)
                error.__cause__ = exc
            except ApiError as exc:
                error = exc
            if not error.retryable or attempt == self.retries - 1:
                LOG.warning(
                    "api_request_failed",
                    extra={"url": url, "attempt": attempt + 1, "error_type": type(error).__name__},
                )
                raise error
            # Retry-After is a lower bound. Jitter prevents a fleet of workers
            # from issuing a synchronized follow-up request.
            await asyncio.sleep(max(error.retry_after or 0.0, (2**attempt) + random.random()))
        raise AssertionError("unreachable")

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._request("GET", url, params=params, headers=headers)
        return cast(dict[str, Any], response.json())

    async def post_json(
        self,
        url: str,
        *,
        data: dict[str, str],
    ) -> dict[str, Any]:
        response = await self._request("POST", url, data=data)
        return cast(dict[str, Any], response.json())

    async def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        return (await self._request("GET", url, headers=headers)).text

    async def aclose(self) -> None:
        await self.client.aclose()


class TwitchHelixClient:
    def __init__(
        self,
        client_id: str,
        access_token: str,
        http: RetryingHttpClient | None = None,
        max_pages: int = 20,
        max_concurrency: int = 2,
    ) -> None:
        self.headers = {"Client-Id": client_id, "Authorization": f"Bearer {access_token}"}
        self.http, self.max_pages = http or RetryingHttpClient(), max_pages
        self._requests = asyncio.Semaphore(max_concurrency)

    async def stream_pages(
        self, language: str | None, cursor: str | None = None
    ) -> AsyncIterator[TwitchStreamPage]:
        for _ in range(self.max_pages):
            params = {"first": "100"}
            if language:
                params["language"] = language
            if cursor:
                params["after"] = cursor
            async with self._requests:
                payload = await self.http.get_json(
                    "https://api.twitch.tv/helix/streams", params=params, headers=self.headers
                )
            next_cursor = payload.get("pagination", {}).get("cursor")
            yield TwitchStreamPage(
                streams=list(payload.get("data", [])),
                next_cursor=str(next_cursor) if next_cursor else None,
            )
            cursor = str(next_cursor) if next_cursor else None
            if not cursor:
                return

    async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
        async with self._requests:
            return await self._audience_metrics(candidate)

    async def _audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
        if not candidate.twitch_user_id and candidate.twitch_login:
            login = valid_twitch_login(candidate.twitch_login)
            if login is None:
                return AudienceMetrics()
            candidate.twitch_login = login
            lookup = await self.http.get_json(
                "https://api.twitch.tv/helix/users",
                params={"login": login},
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
            twitch_display_name=data[0].get("display_name") if data else None,
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
    def __init__(
        self, api_key: str, http: RetryingHttpClient | None = None, max_concurrency: int = 2
    ) -> None:
        self.api_key, self.http = api_key, http or RetryingHttpClient()
        self._requests = asyncio.Semaphore(max_concurrency)

    async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
        async with self._requests:
            return await self._audience_metrics(candidate)

    async def _audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
        params = {"part": "snippet,statistics", "key": self.api_key}
        if candidate.youtube_channel_id:
            params["id"] = candidate.youtube_channel_id
        elif candidate.youtube_channel_url:
            parsed = urlparse(candidate.youtube_channel_url)
            if "/@" not in parsed.path:
                return AudienceMetrics()
            params["forHandle"] = parsed.path.rsplit("/@", maxsplit=1)[1]
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
            youtube_title=item.get("snippet", {}).get("title"),
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
        payload = youtube.model_dump()
        payload.update(
            {
                "twitch_followers": twitch.twitch_followers,
                "twitch_display_name": twitch.twitch_display_name,
                "twitch_description": twitch.twitch_description,
            }
        )
        return AudienceMetrics(**payload)


async def _empty() -> AudienceMetrics:
    return AudienceMetrics()
