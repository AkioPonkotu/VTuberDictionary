from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vtuber_dictionary.agency_source import AgencyPageTalentSource
from vtuber_dictionary.agents import ReadingResearchAgent, VerificationAgent
from vtuber_dictionary.dictionary import DictionaryCompiler, MacOsImeExporter, MicrosoftImeExporter
from vtuber_dictionary.discovery import AgencyDiscovery, TwitchDiscovery
from vtuber_dictionary.domain import (
    Agency,
    AudienceMetrics,
    Candidate,
    CandidateStatus,
    DictionaryEntry,
    Evidence,
    NameReadingParts,
    ResearchResult,
    ReviewRecord,
    VerificationResult,
    WebSource,
)
from vtuber_dictionary.filtering import (
    ExistingEntryFilter,
    KatakanaOrLatinNameFilter,
    ThresholdFilter,
)
from vtuber_dictionary.infrastructure import persistence as repository
from vtuber_dictionary.platforms import (
    ApiError,
    AuthenticationError,
    CombinedPlatformClient,
    RateLimitError,
    RetryingHttpClient,
    TwitchHelixClient,
    YouTubeDataClient,
)
from vtuber_dictionary.ports import TwitchStreamPage
from vtuber_dictionary.repository import (
    CandidateRepository,
    EntryRepository,
    ReviewRepository,
    TwitchDiscoveryCheckpointRepository,
)
from vtuber_dictionary.settings import Settings
from vtuber_dictionary.validation import DeterministicValidator
from vtuber_dictionary.web_sources import (
    OfficialSourcePrefetcher,
    TwitchSearchSourcePrefetcher,
    visible_text,
)
from vtuber_dictionary.workflow import Pipeline


def name_parts(name: str | None = None, reading: str | None = None) -> NameReadingParts:
    return NameReadingParts(
        family_name=None,
        given_name=name,
        family_reading=None,
        given_reading=reading,
    )


class FakeAgencySource:
    async def list_talents(self, agency: Agency) -> list[Candidate]:
        return [Candidate(display_name="公式タレント", youtube_channel_id="channel-1")]


class FakeStreams:
    def stream_pages(
        self, language: str | None, cursor: str | None = None
    ) -> AsyncIterator[TwitchStreamPage]:
        async def pages() -> AsyncIterator[TwitchStreamPage]:
            assert language == "ja"
            assert cursor is None
            yield TwitchStreamPage(
                streams=[
                    {
                        "user_id": "1",
                        "user_login": "one",
                        "user_name": "One",
                        "tags": ["vTuBeR"],
                    },
                    {"user_id": "2", "user_login": "two", "user_name": "Two", "tags": ["gaming"]},
                    {"user_id": "3", "user_login": "three", "user_name": "Three", "tags": None},
                ],
                next_cursor=None,
            )

        return pages()


class FakeRunner:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.returned: list[str] = []
        self.response_models: list[type[object]] = []
        self.prompts: list[str] = []

    async def run_json(self, instructions: str, prompt: str, response_model: type[object]) -> str:
        self.response_models.append(response_model)
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        self.returned.append(response)
        return response


@pytest.mark.asyncio
async def test_agency_discovery_marks_candidates_without_verifying() -> None:
    agency = Agency(
        name="Agency", official_url="https://example.com", talent_list_url="https://example.com/t"
    )
    found = await AgencyDiscovery(FakeAgencySource()).discover([agency])
    assert found[0].agency == "Agency"
    assert found[0].discovery_sources == {"agency"}


@pytest.mark.asyncio
async def test_agency_discovery_discards_shared_platform_links() -> None:
    class SharedChannelAgencySource:
        async def list_talents(self, agency: Agency) -> list[Candidate]:
            return [
                Candidate(display_name="A", youtube_channel_id="shared"),
                Candidate(display_name="B", youtube_channel_id="shared"),
            ]

    agency = Agency(
        name="Agency", official_url="https://example.com", talent_list_url="https://example.com/t"
    )
    found = await AgencyDiscovery(SharedChannelAgencySource()).discover([agency])
    assert [(item.youtube_channel_id, item.youtube_channel_url) for item in found] == [
        (None, None),
        (None, None),
    ]


@pytest.mark.asyncio
async def test_agency_page_source_extracts_official_profile_platform_links() -> None:
    class FakeHttp:
        async def get_text(self, url: str) -> str:
            pages = {
                "https://agency.example/talents": (
                    '<a href="/news">お知らせ</a><a href="/talents/a">公式 タレント</a>'
                ),
                "https://agency.example/talents/a": (
                    '<a href="https://www.youtube.com/channel/UC123">YouTube</a>'
                    '<a href="https://www.youtube.com/channel/WRONG">Featured video</a>'
                    '<a href="https://www.twitch.tv/example">Twitch</a>'
                ),
            }
            return pages[url]

    source = AgencyPageTalentSource(http=FakeHttp())  # type: ignore[arg-type]
    agency = Agency(
        name="Agency",
        official_url="https://agency.example",
        talent_list_url="https://agency.example/talents",
        profile_url_pattern=r"^/talents/[^/]+$",
    )
    [candidate] = await source.list_talents(agency)
    assert (candidate.youtube_channel_id, candidate.twitch_login) == ("UC123", "example")


@pytest.mark.asyncio
async def test_agency_page_source_keeps_named_profile_when_enrichment_is_rate_limited() -> None:
    class FakeHttp:
        async def get_text(self, url: str) -> str:
            pages = {
                "https://agency.example/talents": (
                    '<a href="/talents/available">利用可能 タレント</a>'
                    '<a href="/talents/limited">制限中 タレント</a>'
                ),
                "https://agency.example/talents/available": "<title>利用可能 タレント</title>",
            }
            if url == "https://agency.example/talents/limited":
                raise RateLimitError("rate limited", retryable=True)
            return pages[url]

    source = AgencyPageTalentSource(http=FakeHttp())  # type: ignore[arg-type]
    agency = Agency(
        name="Agency",
        official_url="https://agency.example",
        talent_list_url="https://agency.example/talents",
        profile_url_pattern=r"^/talents/[^/]+$",
    )

    candidates = await source.list_talents(agency)

    assert [(item.display_name, item.official_profile_url) for item in candidates] == [
        ("利用可能 タレント", "https://agency.example/talents/available"),
        ("制限中 タレント", "https://agency.example/talents/limited"),
    ]


@pytest.mark.asyncio
async def test_agency_page_source_skips_unavailable_roster() -> None:
    class FakeHttp:
        async def get_text(self, url: str) -> str:
            raise ApiError("request timed out", retryable=True)

    source = AgencyPageTalentSource(http=FakeHttp())  # type: ignore[arg-type]
    agency = Agency(
        name="Agency",
        official_url="https://agency.example",
        talent_list_url="https://agency.example/talents",
    )

    assert await source.list_talents(agency) == []


def test_agency_page_source_rejects_profile_links_with_control_characters() -> None:
    agency = Agency(
        name="Agency",
        official_url="https://agency.example",
        talent_list_url="https://agency.example/talents",
        profile_url_pattern=r"^/talents/[^/]+$",
    )

    assert not AgencyPageTalentSource._matches_profile(
        agency, "https://agency.example/talents/invalid\x00"
    )


@pytest.mark.asyncio
async def test_agency_page_source_uses_image_alt_text_for_profile_name() -> None:
    class FakeHttp:
        async def get_text(self, url: str) -> str:
            pages = {
                "https://agency.example/talents": (
                    '<a href="/talents/a"><img alt="画像タレント"></a>'
                ),
                "https://agency.example/talents/a": "<title>画像タレント | Agency</title>",
            }
            return pages[url]

    source = AgencyPageTalentSource(http=FakeHttp())  # type: ignore[arg-type]
    agency = Agency(
        name="Agency",
        official_url="https://agency.example",
        talent_list_url="https://agency.example/talents",
        profile_url_pattern=r"^/talents/[^/]+$",
    )
    [candidate] = await source.list_talents(agency)
    assert candidate.display_name == "画像タレント"


@pytest.mark.asyncio
async def test_agency_page_source_discovers_image_only_rosters_from_sitemap() -> None:
    class FakeHttp:
        async def get_text(self, url: str) -> str:
            pages = {
                "https://agency.example/talents": "<main></main>",
                "https://agency.example/robots.txt": "Sitemap: https://agency.example/sitemap.xml",
                "https://agency.example/sitemap.xml": (
                    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<sitemap><loc>https://agency.example/talents-sitemap.xml</loc></sitemap>"
                    "</sitemapindex>"
                ),
                "https://agency.example/talents-sitemap.xml": (
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<url><loc>https://agency.example/talents/a</loc></url>"
                    "</urlset>"
                ),
                "https://agency.example/talents/a": (
                    '<meta property="og:title" content="サイトマップ タレント">'
                ),
            }
            return pages[url]

    source = AgencyPageTalentSource(http=FakeHttp())  # type: ignore[arg-type]
    agency = Agency(
        name="Agency",
        official_url="https://agency.example",
        talent_list_url="https://agency.example/talents",
        profile_url_pattern=r"^/talents/[^/]+$",
    )
    [candidate] = await source.list_talents(agency)
    assert (candidate.display_name, candidate.official_profile_url) == (
        "サイトマップ タレント",
        "https://agency.example/talents/a",
    )


@pytest.mark.asyncio
async def test_agency_page_source_reads_public_nextjs_roster_payload() -> None:
    class FakeHttp:
        async def get_text(self, url: str) -> str:
            pages = {
                "https://agency.example/talents": (
                    '<script id="__NEXT_DATA__" type="application/json">'
                    '{"props":{"pageProps":{"allLivers":['
                    '{"slug":"talent-a","name":"構造化タレント"}]}}}</script>'
                ),
                "https://agency.example/talents/l/talent-a": "<title>構造化タレント</title>",
            }
            return pages[url]

    source = AgencyPageTalentSource(http=FakeHttp())  # type: ignore[arg-type]
    agency = Agency(
        name="Agency",
        official_url="https://agency.example",
        talent_list_url="https://agency.example/talents",
        profile_url_pattern=r"^/talents/l/[^/]+$",
    )
    [candidate] = await source.list_talents(agency)
    assert (candidate.display_name, candidate.official_profile_url) == (
        "構造化タレント",
        "https://agency.example/talents/l/talent-a",
    )


@pytest.mark.asyncio
async def test_agency_page_source_reads_official_image_card_roster() -> None:
    class FakeHttp:
        async def get_text(self, url: str) -> str:
            assert url == "https://agency.example/members"
            return (
                '<div class="member__profile"><p class="member__name">'
                '<img alt="カード タレント"></p>'
                '<a href="https://www.youtube.com/channel/UC123">YouTube</a></div>'
            )

    source = AgencyPageTalentSource(http=FakeHttp())  # type: ignore[arg-type]
    agency = Agency(
        name="Agency",
        official_url="https://agency.example",
        talent_list_url="https://agency.example/members",
        profile_url_pattern=r"^/members/[^/]+$",
    )
    [candidate] = await source.list_talents(agency)
    assert (candidate.display_name, candidate.youtube_channel_id) == ("カード タレント", "UC123")


@pytest.mark.asyncio
async def test_twitch_discovery_filters_tag_case_insensitively() -> None:
    found = await TwitchDiscovery(FakeStreams(), "VTuber", "ja").discover()
    assert [(item.twitch_user_id, item.twitch_login) for item in found] == [("1", "one")]


@pytest.mark.asyncio
async def test_youtube_client_collects_official_channel_metadata() -> None:
    class FakeHttp:
        async def get_json(self, url: str, **kwargs: object) -> dict[str, object]:
            return {
                "items": [
                    {
                        "id": "channel-id",
                        "snippet": {"title": "公式名", "description": "official description"},
                        "statistics": {"subscriberCount": "10000", "hiddenSubscriberCount": False},
                    }
                ]
            }

    candidate = Candidate(display_name="candidate", youtube_channel_id="channel-id")
    metrics = await YouTubeDataClient("key", http=FakeHttp()).audience_metrics(candidate)  # type: ignore[arg-type]
    assert (metrics.youtube_title, metrics.youtube_subscribers) == ("公式名", 10_000)


@pytest.mark.asyncio
async def test_youtube_client_strips_query_from_official_handle() -> None:
    class FakeHttp:
        def __init__(self) -> None:
            self.params: dict[str, str] | None = None

        async def get_json(self, url: str, **kwargs: object) -> dict[str, object]:
            self.params = kwargs["params"]  # type: ignore[assignment]
            return {"items": []}

    http = FakeHttp()
    candidate = Candidate(
        display_name="candidate",
        youtube_channel_url="https://youtube.com/@candidate?sub_confirmation=1",
    )
    await YouTubeDataClient("key", http=http).audience_metrics(candidate)  # type: ignore[arg-type]
    assert http.params is not None and http.params["forHandle"] == "candidate"


@pytest.mark.asyncio
async def test_twitch_client_skips_invalid_login_without_request() -> None:
    class FakeHttp:
        async def get_json(self, url: str, **kwargs: object) -> dict[str, object]:
            raise AssertionError("invalid login must not call Twitch")

    metrics = await TwitchHelixClient("id", "token", http=FakeHttp()).audience_metrics(  # type: ignore[arg-type]
        Candidate(display_name="candidate", twitch_login="invalid+login")
    )
    assert metrics.twitch_followers is None


@pytest.mark.asyncio
async def test_http_client_classifies_authentication_failure() -> None:
    import httpx

    client = RetryingHttpClient(retries=1)
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, request=request, json={"message": "invalid"})
        )
    )
    with pytest.raises(AuthenticationError):
        await client.get_json("https://example.test")
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_retries_transient_read_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    attempts = 0

    async def transport(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("connection reset", request=request)
        return httpx.Response(200, request=request, json={"ok": True})

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr("vtuber_dictionary.platforms.asyncio.sleep", no_wait)
    client = RetryingHttpClient(retries=2)
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))

    assert await client.get_json("https://example.test") == {"ok": True}
    assert attempts == 2
    await client.aclose()


def test_http_client_follows_official_site_redirects() -> None:
    client = RetryingHttpClient()
    assert client.client.follow_redirects


@pytest.mark.asyncio
async def test_combined_metrics_preserves_platform_specific_fields() -> None:
    class YouTube:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            return AudienceMetrics(youtube_subscribers=10_000, youtube_hidden=False)

    class Twitch:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            return AudienceMetrics(twitch_followers=5_000, twitch_display_name="Twitch Name")

    metrics = await CombinedPlatformClient(YouTube(), Twitch()).audience_metrics(  # type: ignore[arg-type]
        Candidate(display_name="candidate")
    )
    assert (metrics.youtube_subscribers, metrics.twitch_followers) == (10_000, 5_000)


def test_candidate_repository_deduplicates_only_explicit_identity(tmp_path: Path) -> None:
    repo = CandidateRepository(tmp_path / "candidates.jsonl")
    original = repo.upsert(Candidate(display_name="同名", youtube_channel_id="a"))
    assert (
        repo.upsert(Candidate(display_name="別表記", youtube_channel_id="a")).canonical_id
        == original.canonical_id
    )
    assert (
        repo.upsert(Candidate(display_name="同名", youtube_channel_id="b")).canonical_id
        != original.canonical_id
    )


def test_candidate_repository_batch_upserts_once(tmp_path: Path) -> None:
    repo = CandidateRepository(tmp_path / "candidates.jsonl")
    stored = repo.upsert_many(
        [
            Candidate(display_name="first", youtube_channel_id="channel-1"),
            Candidate(display_name="same identity", youtube_channel_id="channel-1"),
            Candidate(display_name="second", youtube_channel_id="channel-2"),
        ]
    )
    assert [item.display_name for item in repo.all()] == ["first", "second"]
    assert stored[0].canonical_id == stored[1].canonical_id


def test_ambiguous_identity_is_review_required(tmp_path: Path) -> None:
    repo = CandidateRepository(tmp_path / "candidates.jsonl")
    repo.upsert(Candidate(display_name="A", youtube_channel_id="youtube-a"))
    repo.upsert(Candidate(display_name="A", twitch_user_id="twitch-a"))
    ambiguous = repo.upsert(
        Candidate(display_name="A", youtube_channel_id="youtube-a", twitch_user_id="twitch-a")
    )
    assert ambiguous.status == "review_required"


def test_threshold_filter_supports_youtube_twitch_and_or() -> None:
    filter_ = ThresholdFilter(10_000, 5_000)
    assert filter_.accepts(AudienceMetrics(youtube_subscribers=10_000))
    assert filter_.accepts(AudienceMetrics(twitch_followers=5_000))
    assert filter_.accepts(AudienceMetrics(youtube_subscribers=1, twitch_followers=5_000))
    assert not filter_.accepts(AudienceMetrics(youtube_subscribers=9_999, twitch_followers=4_999))


@pytest.mark.parametrize(
    "display_name",
    ["アルス・アルマル", "Gawr Gura", "Ａｚｒｉ", "クレイジー・オリー Kureiji Ollie"],
)
def test_katakana_or_latin_name_filter_excludes_non_japanese_names(display_name: str) -> None:
    assert KatakanaOrLatinNameFilter().excludes(Candidate(display_name=display_name))


@pytest.mark.parametrize("display_name", ["星街すいせい", "ときのそら Tokino Sora", "友人A"])
def test_katakana_or_latin_name_filter_keeps_names_with_japanese_characters(
    display_name: str,
) -> None:
    assert not KatakanaOrLatinNameFilter().excludes(Candidate(display_name=display_name))


def test_validator_rejects_katakana_or_latin_canonical_name() -> None:
    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="name")
    ]
    research = ResearchResult(
        canonical_name="Gawr Gura",
        reading="がうるぐら",
        name_parts=name_parts("Gawr Gura", "がうるぐら"),
        confidence=1,
        evidence=evidence,
        status="resolved",
    )
    verification = VerificationResult(
        verified=True,
        canonical_name="Gawr Gura",
        reading="がうるぐら",
        name_parts=name_parts("Gawr Gura", "がうるぐら"),
        confidence=1,
        evidence=evidence,
    )

    entry, reason = DeterministicValidator().validate(
        Candidate(display_name="がうる・ぐら"), research, verification, []
    )

    assert entry is None
    assert reason == "canonical name must include hiragana or kanji"


def test_existing_entry_filter_excludes_canonical_id_regardless_of_verification_age() -> None:
    candidate = Candidate(display_name="表示名")
    entry = DictionaryEntry(
        canonical_id=candidate.canonical_id,
        reading="ひょうじめい",
        canonical_name="表示名",
        source_urls=["https://official.example"],
        verified_at=datetime.now(UTC) - timedelta(days=365),
    )
    filter_ = ExistingEntryFilter([entry])
    assert not filter_.needs_research(candidate)


def test_existing_entry_filter_uses_platform_metadata_when_id_changes() -> None:
    entry = DictionaryEntry(
        canonical_id="prior-identity",
        reading="よみ",
        canonical_name="正式名",
        youtube_channel_id="UC123",
        source_urls=["https://official.example"],
        verified_at=datetime.now(UTC),
    )
    candidate = Candidate(display_name="別表示", youtube_channel_id="UC123")
    assert not ExistingEntryFilter([entry]).needs_research(candidate)


@pytest.mark.asyncio
async def test_pipeline_does_not_contact_services_for_an_existing_entry(tmp_path: Path) -> None:
    class MustNotContactPlatforms:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            raise AssertionError("existing entries must not request platform metrics")

    class MustNotResearch:
        async def research(
            self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
        ) -> ResearchResult:
            raise AssertionError("existing entries must not be researched")

    class MustNotVerify:
        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            raise AssertionError("existing entries must not be verified")

    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidate = Candidate(display_name="既存候補", youtube_channel_id="UC123")
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    candidates.upsert(candidate)
    entries = EntryRepository(data_dir / "entries.jsonl")
    entries.replace(
        [
            DictionaryEntry(
                canonical_id=candidate.canonical_id,
                canonical_name="既存候補",
                reading="きぞんこうほ",
                youtube_channel_id="UC123",
                source_urls=["https://official.example"],
                verified_at=datetime.now(UTC) - timedelta(days=365),
            )
        ]
    )
    pipeline = Pipeline(
        candidates=candidates,
        entries=entries,
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=MustNotContactPlatforms(),
        researcher=MustNotResearch(),
        verifier=MustNotVerify(),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
    )

    assert await pipeline.run() == 0


@pytest.mark.asyncio
async def test_pipeline_processes_only_candidates_matching_source_filter(tmp_path: Path) -> None:
    evidence = [
        Evidence(url="https://twitch.tv/streamer", source_type="twitch_about", claim="name")
    ]
    processed: list[str] = []

    class Metrics:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            assert candidate.display_name == "星街すいせい"
            return AudienceMetrics(twitch_followers=5_000)

    class Researcher:
        async def research(
            self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
        ) -> ResearchResult:
            processed.append(candidate.display_name)
            return ResearchResult(
                canonical_name="星街すいせい",
                reading="ほしまちすいせい",
                name_parts=NameReadingParts(
                    family_name="星街",
                    given_name="すいせい",
                    family_reading="ほしまち",
                    given_reading="すいせい",
                ),
                confidence=1,
                evidence=evidence,
                status="resolved",
            )

    class Verifier:
        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            return VerificationResult(
                verified=True,
                canonical_name=research.canonical_name,
                reading=research.reading,
                name_parts=research.name_parts,
                confidence=1,
                evidence=evidence,
            )

    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    agency = candidates.upsert(
        Candidate(display_name="白星あわわ", agency="Agency", discovery_sources={"agency"})
    )
    twitch = candidates.upsert(
        Candidate(
            display_name="星街すいせい",
            twitch_user_id="1",
            discovery_sources={"twitch_vtuber_tag"},
        )
    )
    pipeline = Pipeline(
        candidates=candidates,
        entries=EntryRepository(data_dir / "entries.jsonl"),
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=Metrics(),
        researcher=Researcher(),
        verifier=Verifier(),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
        candidate_source_filter={"twitch_vtuber_tag"},
    )

    assert await pipeline.run() == 1
    assert processed == ["星街すいせい"]
    stored = {candidate.canonical_id: candidate for candidate in candidates.all()}
    assert stored[agency.canonical_id].status == CandidateStatus.DISCOVERED
    assert stored[twitch.canonical_id].status == CandidateStatus.VERIFIED


def test_settings_keeps_model_unset_when_env_value_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "")
    assert Settings(_env_file=None).openai_model is None


@pytest.mark.asyncio
async def test_research_and_verification_parse_structured_output() -> None:
    runner = FakeRunner(
        [
            '{"canonical_name":"星街すいせい","reading":"ほしまちすいせい","name_parts":{"family_name":"星街","given_name":"すいせい","family_reading":"ほしまち","given_reading":"すいせい"},"confidence":0.9,"evidence":[{"url":"https://official.example","source_type":"official_profile","claim":"reading"}],"status":"resolved"}',
            '{"verified":true,"canonical_name":"星街すいせい","reading":"ほしまちすいせい","name_parts":{"family_name":"星街","given_name":"すいせい","family_reading":"ほしまち","given_reading":"すいせい"},"confidence":0.95,"evidence":[{"url":"https://official.example","source_type":"official_profile","claim":"reading"}],"issues":[]}',
        ]
    )
    candidate, metrics = Candidate(display_name="星街すいせい"), AudienceMetrics()
    sources = [
        WebSource(
            url="https://official.example",
            source_type="official_profile",
            content="星街すいせい（ほしまちすいせい）",
        )
    ]
    research = await ReadingResearchAgent(runner).research(candidate, metrics, sources)
    verified = await VerificationAgent(runner).verify(candidate, research, metrics, sources)
    assert research.status == "resolved" and verified.verified
    assert research.raw_json == runner.returned[0]
    assert verified.raw_json == runner.returned[1]
    assert research.name_parts.family_reading == "ほしまち"
    assert verified.name_parts.given_reading == "すいせい"
    assert runner.response_models == [ResearchResult, VerificationResult]
    assert "prefetched_sources" in runner.prompts[0]
    assert "https://official.example" in runner.prompts[0]


@pytest.mark.asyncio
async def test_prefetcher_collects_visible_official_and_platform_source_material() -> None:
    class FakeHttp:
        async def get_text(self, url: str) -> str:
            assert url == "https://agency.example/talents/a"
            return "<script>ignore()</script><h1>公式 タレント</h1><p>読み: こうしき</p>"

    candidate = Candidate(
        display_name="公式タレント",
        agency="Agency",
        official_profile_url="https://agency.example/talents/a",
        youtube_channel_url="https://youtube.com/@official",
    )
    sources = await OfficialSourcePrefetcher(http=FakeHttp()).fetch(  # type: ignore[arg-type]
        candidate, AudienceMetrics(youtube_description="公式チャンネル概要")
    )
    assert [(item.url, item.source_type) for item in sources] == [
        ("https://agency.example/talents/a", "official_agency_profile"),
        ("https://youtube.com/@official", "youtube_about"),
    ]
    assert sources[0].content == "公式 タレント 読み: こうしき"
    assert visible_text("<style>x</style><p>表示</p>", 20) == "表示"


@pytest.mark.asyncio
async def test_twitch_prefetcher_crawls_search_results_but_obeys_robots() -> None:
    class FakeHttp:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> str:
            self.calls.append(url)
            assert headers and "User-Agent" in headers
            if url.startswith("https://html.duckduckgo.com/html/"):
                return """
                    <a class=\"result__a\" href=\"https://official.example/profile\">公式</a>
                    <a class=\"result__a\" href=\"https://blocked.example/profile\">除外</a>
                """
            if url == "https://official.example/robots.txt":
                return "User-agent: *\nAllow: /\n"
            if url == "https://blocked.example/robots.txt":
                return "User-agent: *\nDisallow: /\n"
            if url == "https://official.example/profile":
                return "<h1>配信者の公式サイト</h1><p>読みは はいしんしゃ</p>"
            raise AssertionError(f"unexpected request: {url}")

    http = FakeHttp()
    candidate = Candidate(
        display_name="配信者",
        twitch_login="streamer",
        discovery_sources={"twitch_vtuber_tag"},
    )
    sources = await TwitchSearchSourcePrefetcher(
        http=http,  # type: ignore[arg-type]
        max_results=2,
        minimum_delay_seconds=0,
    ).fetch(candidate, AudienceMetrics())

    assert [(source.url, source.source_type) for source in sources] == [
        ("https://official.example/profile", "other")
    ]
    assert "https://blocked.example/profile" not in http.calls
    assert "https://official.example/robots.txt" in http.calls


def test_validator_requires_verification_for_fetched_agency_profile() -> None:
    candidate = Candidate(
        display_name="星街すいせい",
        agency="Agency",
        official_profile_url="https://agency.example/talents/suisei/",
    )
    source = WebSource(
        url="https://agency.example/talents/suisei",
        source_type="official_agency_profile",
        content="星街すいせい（ほしまちすいせい）",
    )
    research = ResearchResult(
        canonical_name="星街すいせい",
        reading="ほしまちすいせい",
        name_parts=name_parts("星街すいせい", "ほしまちすいせい"),
        confidence=1,
        evidence=[
            Evidence(
                url=source.url,
                source_type="official_agency_profile",
                claim="読みはほしまちすいせい",
            )
        ],
        status="resolved",
    )
    validator = DeterministicValidator()
    entry, reason = validator.validate(candidate, research, None, [])
    assert entry is None and reason == "verification is required"

    verification = VerificationResult(
        verified=True,
        canonical_name="星街すいせい",
        reading="ほしまちすいせい",
        name_parts=name_parts("星街すいせい", "ほしまちすいせい"),
        confidence=1,
        evidence=[
            Evidence(
                url=source.url,
                source_type="official_agency_profile",
                claim="読みはほしまちすいせい",
            )
        ],
    )
    entry, reason = validator.validate(candidate, research, verification, [])
    assert reason is None and entry is not None


def test_deterministic_validator_and_compiler(tmp_path: Path) -> None:
    candidate = Candidate(display_name="星街すいせい")
    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="reading")
    ]
    research = ResearchResult(
        canonical_name="星街すいせい",
        reading="ほしまちすいせい",
        name_parts=name_parts("星街すいせい", "ほしまちすいせい"),
        confidence=0.9,
        evidence=evidence,
        status="resolved",
    )
    verification = VerificationResult(
        verified=True,
        canonical_name="星街すいせい",
        reading="ほしまちすいせい",
        name_parts=name_parts("星街すいせい", "ほしまちすいせい"),
        confidence=0.9,
        evidence=evidence,
    )
    entry, reason = DeterministicValidator().validate(candidate, research, verification, [])
    assert reason is None and entry is not None
    output = tmp_path / "dictionary.tsv"
    DictionaryCompiler().compile([entry], output)
    assert output.read_text("utf-8") == "ほしまちすいせい\t星街すいせい\n"


def test_validator_removes_unicode_whitespace_from_canonical_names() -> None:
    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="name")
    ]
    parts = NameReadingParts(
        family_name="雪花",
        given_name="ラミィ",
        family_reading="ゆきはな",
        given_reading="らみぃ",
    )
    research = ResearchResult(
        canonical_name="雪花 ラミィ",
        reading="ゆきはな らみぃ",
        name_parts=parts,
        confidence=1,
        evidence=evidence,
        status="resolved",
    )
    verification = VerificationResult(
        verified=True,
        canonical_name="雪花　ラミィ",
        reading="ゆきはな らみぃ",
        name_parts=parts,
        confidence=1,
        evidence=evidence,
    )

    entry, reason = DeterministicValidator().validate(
        Candidate(display_name="雪花ラミィ"), research, verification, []
    )

    assert reason is None
    assert entry is not None
    assert entry.canonical_name == "雪花ラミィ"


def test_platform_exporters_use_the_required_encoding_format_and_csv_escaping() -> None:
    entry = DictionaryEntry(
        canonical_id="quoted",
        reading="よみ",
        canonical_name='語句, "引用"',
        source_urls=[],
    )

    assert MicrosoftImeExporter().export([entry]) == (
        b"\xff\xfe" + "よみ\t語句, \"引用\"\t固有名詞\r\n".encode("utf-16-le")
    )
    assert MacOsImeExporter().export([entry]).decode("utf-8") == (
        'よみ,"語句, ""引用""",proper noun\n'
    )


def test_microsoft_ime_exporter_rejects_tsv_field_separators() -> None:
    entry = DictionaryEntry(
        canonical_id="invalid", reading="よみ", canonical_name="語句\t別列", source_urls=[]
    )

    with pytest.raises(ValueError, match="field separator"):
        MicrosoftImeExporter().export([entry])


@pytest.mark.parametrize(
    ("reading", "canonical_name", "message"),
    [
        ("あ" * 33, "語句", "reading exceeds 32"),
        ("よみ", "語" * 65, "word exceeds 64"),
        ("あ" * 32, '"' * 64, "line exceeds 127"),
    ],
)
def test_macos_exporter_enforces_professional_dictionary_limits(
    reading: str, canonical_name: str, message: str
) -> None:
    entry = DictionaryEntry(
        canonical_id="too-long", reading=reading, canonical_name=canonical_name, source_urls=[]
    )

    with pytest.raises(ValueError, match=message):
        MacOsImeExporter().export([entry])


def test_validator_rejects_unverified_and_invalid_reading() -> None:
    candidate = Candidate(display_name="X")
    invalid = VerificationResult(
        verified=True,
        canonical_name="X",
        reading="invalid",
        name_parts=name_parts("X", "invalid"),
        confidence=1,
        evidence=[],
    )
    research = ResearchResult(name_parts=name_parts(), confidence=0, status="unresolved")
    entry, reason = DeterministicValidator().validate(candidate, research, invalid, [])
    assert entry is None and reason == "reading must consist of hiragana and prolonged-sound mark"


def test_validator_rejects_conflicting_duplicate_reading() -> None:
    candidate = Candidate(display_name="X")
    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="reading")
    ]
    research = ResearchResult(
        canonical_name="X",
        reading="えっくす",
        name_parts=name_parts("X", "えっくす"),
        confidence=1,
        evidence=evidence,
        status="resolved",
    )
    verification = VerificationResult(
        verified=True,
        canonical_name="X",
        reading="えっくす",
        name_parts=name_parts("X", "えっくす"),
        confidence=1,
        evidence=evidence,
    )
    existing = DictionaryEntry(
        canonical_id="old",
        canonical_name="Y",
        reading="えっくす",
        source_urls=["https://old.example"],
    )
    entry, reason = DeterministicValidator().validate(candidate, research, verification, [existing])
    assert entry is None and reason == "reading collides with a different canonical name"


def test_validator_requires_agents_to_agree_on_resolved_reading() -> None:
    candidate = Candidate(display_name="X")
    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="reading")
    ]
    research = ResearchResult(
        canonical_name="X",
        reading="えっくす",
        name_parts=name_parts("X", "えっくす"),
        confidence=1,
        evidence=evidence,
        status="resolved",
    )
    verification = VerificationResult(
        verified=True,
        canonical_name="X",
        reading="えくす",
        name_parts=name_parts("X", "えくす"),
        confidence=1,
        evidence=evidence,
    )
    entry, reason = DeterministicValidator().validate(candidate, research, verification, [])
    assert entry is None and reason == "research and verification results disagree"


def test_validator_rejects_a_missing_family_reading() -> None:
    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="name")
    ]
    parts = NameReadingParts(
        family_name="本阿弥",
        given_name="あずさ",
        family_reading=None,
        given_reading="あずさ",
    )
    research = ResearchResult(
        canonical_name="本阿弥あずさ",
        reading="あずさ",
        name_parts=parts,
        confidence=1,
        evidence=evidence,
        status="resolved",
    )
    verification = VerificationResult(
        verified=True,
        canonical_name="本阿弥あずさ",
        reading="あずさ",
        name_parts=parts,
        confidence=1,
        evidence=evidence,
    )

    entry, reason = DeterministicValidator().validate(
        Candidate(display_name="本阿弥あずさ"), research, verification, []
    )

    assert entry is None
    assert reason == "family name and reading must both be present or absent"


@pytest.mark.asyncio
async def test_pipeline_persists_agent_raw_json_for_a_reviewed_candidate(tmp_path: Path) -> None:
    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="name")
    ]
    parts = NameReadingParts(
        family_name="本阿弥",
        given_name="あずさ",
        family_reading="ほんあみ",
        given_reading="あずさ",
    )

    class Metrics:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            return AudienceMetrics()

    class Researcher:
        async def research(
            self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
        ) -> ResearchResult:
            return ResearchResult(
                canonical_name="本阿弥あずさ",
                reading="あずさ",
                name_parts=parts,
                confidence=1,
                evidence=evidence,
                status="resolved",
                raw_json='{"agent":"research"}',
            )

    class Verifier:
        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            return VerificationResult(
                verified=True,
                canonical_name="本阿弥あずさ",
                reading="あずさ",
                name_parts=parts,
                confidence=1,
                evidence=evidence,
                raw_json='{"agent":"verification"}',
            )

    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    candidates.upsert(Candidate(display_name="本阿弥あずさ", agency="Agency"))
    pipeline = Pipeline(
        candidates=candidates,
        entries=EntryRepository(data_dir / "entries.jsonl"),
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=Metrics(),
        researcher=Researcher(),
        verifier=Verifier(),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
    )

    assert await pipeline.run() == 0
    stored = candidates.all()[0]
    assert stored.status == CandidateStatus.REVIEW_REQUIRED
    assert stored.research_raw_json == '{"agent":"research"}'
    assert stored.verification_raw_json == '{"agent":"verification"}'


@pytest.mark.asyncio
async def test_pipeline_researches_after_verification_rejection(tmp_path: Path) -> None:
    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="name")
    ]
    calls: list[str | None] = []

    class Metrics:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            return AudienceMetrics()

    class Researcher:
        async def research(
            self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
        ) -> ResearchResult:
            calls.append(candidate.retry_reason)
            return ResearchResult(
                canonical_name="星街すいせい",
                reading="ほしまちすいせい",
                name_parts=NameReadingParts(
                    family_name="星街",
                    given_name="すいせい",
                    family_reading="ほしまち",
                    given_reading="すいせい",
                ),
                confidence=1,
                evidence=evidence,
                status="resolved",
                raw_json=f'{{"research_attempt":{len(calls)}}}',
            )

    class Verifier:
        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            attempt = len(calls)
            return VerificationResult(
                verified=attempt == 3,
                canonical_name=research.canonical_name,
                reading=research.reading,
                name_parts=research.name_parts,
                confidence=1,
                evidence=evidence,
                issues=[] if attempt == 3 else ["confirm the split reading"],
                raw_json=f'{{"verification_attempt":{attempt}}}',
            )

    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    candidates.upsert(Candidate(display_name="星街すいせい", agency="Agency"))
    pipeline = Pipeline(
        candidates=candidates,
        entries=EntryRepository(data_dir / "entries.jsonl"),
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=Metrics(),
        researcher=Researcher(),
        verifier=Verifier(),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
    )

    assert await pipeline.run() == 1
    stored = candidates.all()[0]
    assert calls == [
        None,
        "verification rejected: confirm the split reading",
        "verification rejected: confirm the split reading",
    ]
    assert stored.status == CandidateStatus.VERIFIED
    assert stored.research_attempts == 3
    assert [attempt.failure_reason for attempt in stored.agent_attempts] == [
        "verification rejected: confirm the split reading",
        "verification rejected: confirm the split reading",
        None,
    ]


@pytest.mark.asyncio
async def test_pipeline_compiles_verified_agency_candidate_below_threshold(tmp_path: Path) -> None:
    class Metrics:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            return AudienceMetrics(youtube_subscribers=0)

    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="reading")
    ]

    class Researcher:
        async def research(
            self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
        ) -> ResearchResult:
            return ResearchResult(
                canonical_name="星街すいせい",
                reading="ほしまちすいせい",
                name_parts=name_parts("星街すいせい", "ほしまちすいせい"),
                confidence=1,
                evidence=evidence,
                status="resolved",
            )

    class Verifier:
        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            return VerificationResult(
                verified=True,
                canonical_name=research.canonical_name,
                reading=research.reading,
                name_parts=research.name_parts,
                confidence=1,
                evidence=evidence,
            )

    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    candidates.upsert(
        Candidate(display_name="星街すいせい", agency="Agency", youtube_channel_id="UC123")
    )
    pipeline = Pipeline(
        candidates=candidates,
        entries=EntryRepository(data_dir / "entries.jsonl"),
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=Metrics(),
        researcher=Researcher(),
        verifier=Verifier(),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
    )
    assert await pipeline.run() == 1
    assert (dist_dir / "vtuber_dictionary.tsv").read_text(
        "utf-8"
    ) == "ほしまちすいせい\t星街すいせい\n"
    assert (dist_dir / "vtuber_dictionary_msime.txt").read_bytes() == (
        b"\xff\xfe" + "ほしまちすいせい\t星街すいせい\t固有名詞\r\n".encode("utf-16-le")
    )
    assert (dist_dir / "vtuber_dictionary_macos.csv").read_text("utf-8") == (
        "ほしまちすいせい,星街すいせい,proper noun\n"
    )


@pytest.mark.asyncio
async def test_pipeline_verifies_prefetched_agency_profile(tmp_path: Path) -> None:
    profile_url = "https://agency.example/talents/suisei"
    source = WebSource(
        url=profile_url,
        source_type="official_agency_profile",
        content="星街すいせい（ほしまちすいせい）",
    )

    class Metrics:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            return AudienceMetrics(youtube_subscribers=0)

    class Sources:
        async def fetch(self, candidate: Candidate, metrics: AudienceMetrics) -> list[WebSource]:
            return [source]

    class Researcher:
        async def research(
            self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
        ) -> ResearchResult:
            return ResearchResult(
                canonical_name="星街すいせい",
                reading="ほしまちすいせい",
                name_parts=name_parts("星街すいせい", "ほしまちすいせい"),
                confidence=1,
                evidence=[
                    Evidence(
                        url=profile_url,
                        source_type="official_agency_profile",
                        claim="読みはほしまちすいせい",
                    )
                ],
                status="resolved",
            )

    class Verifier:
        def __init__(self) -> None:
            self.calls = 0

        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            self.calls += 1
            return VerificationResult(
                verified=True,
                canonical_name=research.canonical_name,
                reading=research.reading,
                name_parts=research.name_parts,
                confidence=1,
                evidence=research.evidence,
            )

    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    candidates.upsert(
        Candidate(
            display_name="星街すいせい",
            agency="Agency",
            official_profile_url=profile_url,
            youtube_channel_id="UC123",
        )
    )
    verifier = Verifier()
    pipeline = Pipeline(
        candidates=candidates,
        entries=EntryRepository(data_dir / "entries.jsonl"),
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=Metrics(),
        researcher=Researcher(),
        verifier=verifier,
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
        web_sources=Sources(),
    )
    assert await pipeline.run() == 1
    assert verifier.calls == 1


@pytest.mark.asyncio
async def test_pipeline_creates_empty_artifact_without_accepted_candidates(tmp_path: Path) -> None:
    class Metrics:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            return AudienceMetrics(youtube_subscribers=0)

    class ShouldNotRun:
        async def research(
            self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
        ) -> ResearchResult:
            raise AssertionError("threshold filter should prevent research")

        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            raise AssertionError("threshold filter should prevent verification")

    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    candidates.upsert(Candidate(display_name="below threshold", youtube_channel_id="UC123"))
    pipeline = Pipeline(
        candidates=candidates,
        entries=EntryRepository(data_dir / "entries.jsonl"),
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=Metrics(),
        researcher=ShouldNotRun(),
        verifier=ShouldNotRun(),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
    )
    assert await pipeline.run() == 0
    assert (dist_dir / "vtuber_dictionary.tsv").read_bytes() == b""


@pytest.mark.asyncio
async def test_pipeline_rejects_katakana_or_latin_candidates_before_external_calls(
    tmp_path: Path,
) -> None:
    class MustNotRun:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            raise AssertionError("script filter should prevent platform calls")

        async def research(
            self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
        ) -> ResearchResult:
            raise AssertionError("script filter should prevent research")

        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            raise AssertionError("script filter should prevent verification")

    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    candidate = candidates.upsert(Candidate(display_name="Gawr Gura", youtube_channel_id="UC123"))
    pipeline = Pipeline(
        candidates=candidates,
        entries=EntryRepository(data_dir / "entries.jsonl"),
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=MustNotRun(),
        researcher=MustNotRun(),
        verifier=MustNotRun(),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
    )

    assert await pipeline.run() == 0
    assert candidates.all()[0].canonical_id == candidate.canonical_id
    assert candidates.all()[0].status == CandidateStatus.REJECTED
    assert (dist_dir / "vtuber_dictionary.tsv").read_bytes() == b""


@pytest.mark.asyncio
async def test_pipeline_removes_previously_published_katakana_or_latin_candidate(
    tmp_path: Path,
) -> None:
    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    candidate = candidates.upsert(Candidate(display_name="アルス・アルマル"))
    entries = EntryRepository(data_dir / "entries.jsonl")
    entries.replace(
        [
            DictionaryEntry(
                canonical_id=candidate.canonical_id,
                reading="あるすあるまる",
                canonical_name="アルス・アルマル",
                source_urls=[],
            )
        ]
    )

    class MustNotRun:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            raise AssertionError("script filter should prevent platform calls")

        async def research(
            self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
        ) -> ResearchResult:
            raise AssertionError("script filter should prevent research")

        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            raise AssertionError("script filter should prevent verification")

    pipeline = Pipeline(
        candidates=candidates,
        entries=entries,
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=MustNotRun(),
        researcher=MustNotRun(),
        verifier=MustNotRun(),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
    )

    assert await pipeline.run() == 0
    assert entries.all() == []
    assert (dist_dir / "vtuber_dictionary.tsv").read_bytes() == b""


@pytest.mark.asyncio
async def test_pipeline_removes_existing_latin_entry_when_candidate_has_japanese_alias(
    tmp_path: Path,
) -> None:
    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    candidate = candidates.upsert(Candidate(display_name="さくらみこ Sakura Miko"))
    entries = EntryRepository(data_dir / "entries.jsonl")
    entries.replace(
        [
            DictionaryEntry(
                canonical_id=candidate.canonical_id,
                reading="さくらみこ",
                canonical_name="Sakura Miko",
                source_urls=[],
            )
        ]
    )

    class MustNotRun:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            raise AssertionError("canonical-name filter should prevent platform calls")

        async def research(
            self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
        ) -> ResearchResult:
            raise AssertionError("canonical-name filter should prevent research")

        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            raise AssertionError("canonical-name filter should prevent verification")

    pipeline = Pipeline(
        candidates=candidates,
        entries=entries,
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=MustNotRun(),
        researcher=MustNotRun(),
        verifier=MustNotRun(),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
    )

    assert await pipeline.run() == 0
    assert candidates.all()[0].status == CandidateStatus.REJECTED
    assert entries.all() == []


def test_entry_publication_recovers_after_the_first_file_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries_path = tmp_path / "data" / "entries.jsonl"
    first_artifact, second_artifact = tmp_path / "dist" / "dict.tsv", tmp_path / "dist" / "dict.txt"
    entries = EntryRepository(entries_path)
    entries.replace(
        [
            DictionaryEntry(
                canonical_id="old", reading="おーるど", canonical_name="Old", source_urls=[]
            )
        ]
    )
    first_artifact.parent.mkdir()
    first_artifact.write_text("おーるど\tOld\n", encoding="utf-8")
    second_artifact.write_text("old", encoding="utf-8")
    replacement = DictionaryEntry(
        canonical_id="new", reading="にゅー", canonical_name="New", source_urls=[]
    )
    original = repository._write_bytes_atomic
    failed = False

    def fail_once(path: Path, payload: bytes) -> None:
        nonlocal failed
        if path == second_artifact and not failed:
            failed = True
            raise OSError("simulated artifact failure")
        original(path, payload)

    monkeypatch.setattr(repository, "_write_bytes_atomic", fail_once)
    with pytest.raises(OSError, match="simulated"):
        entries.publish(
            [replacement], {first_artifact: "にゅー\tNew\n".encode(), second_artifact: b"new"}
        )
    monkeypatch.setattr(repository, "_write_bytes_atomic", original)

    entries.recover_publication([first_artifact, second_artifact])
    assert [entry.canonical_id for entry in entries.all()] == ["new"]
    assert first_artifact.read_text("utf-8") == "にゅー\tNew\n"
    assert second_artifact.read_bytes() == b"new"


def test_review_checkpoint_is_idempotent(tmp_path: Path) -> None:
    candidates = CandidateRepository(tmp_path / "data" / "candidates.jsonl")
    candidate = Candidate(display_name="ambiguous", youtube_channel_id="UC123")
    candidate.status = CandidateStatus.REVIEW_REQUIRED
    candidates.replace([candidate])
    reviews = ReviewRepository(tmp_path / "data" / "review_required.jsonl")
    record = ReviewRecord(
        canonical_id=candidate.canonical_id, reason="ambiguous", candidate=candidate
    )

    reviews.checkpoint_review(candidates, [candidate], record)
    reviews.checkpoint_review(candidates, [candidate], record)

    assert [item.canonical_id for item in reviews.all()] == [candidate.canonical_id]


@pytest.mark.asyncio
async def test_pipeline_resumes_a_checkpointed_entry_without_researching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Metrics:
        async def audience_metrics(self, candidate: Candidate) -> AudienceMetrics:
            return AudienceMetrics(youtube_subscribers=10_000)

    calls = 0
    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="reading")
    ]

    class Researcher:
        async def research(
            self, candidate: Candidate, metrics: AudienceMetrics, sources: list[WebSource]
        ) -> ResearchResult:
            nonlocal calls
            calls += 1
            return ResearchResult(
                canonical_name="再開タレント",
                reading="さいかいたれんと",
                name_parts=name_parts("再開タレント", "さいかいたれんと"),
                confidence=1,
                evidence=evidence,
                status="resolved",
            )

    class Verifier:
        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            return VerificationResult(
                verified=True,
                canonical_name=research.canonical_name,
                reading=research.reading,
                name_parts=research.name_parts,
                confidence=1,
                evidence=evidence,
            )

    data_dir, dist_dir = tmp_path / "data", tmp_path / "dist"
    candidates = CandidateRepository(data_dir / "candidates.jsonl")
    candidates.upsert(Candidate(display_name="再開タレント", youtube_channel_id="UC123"))
    entries = EntryRepository(data_dir / "entries.jsonl")
    artifact = dist_dir / "vtuber_dictionary.tsv"
    pipeline = Pipeline(
        candidates=candidates,
        entries=entries,
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=Metrics(),
        researcher=Researcher(),
        verifier=Verifier(),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
    )
    original = repository._write_bytes_atomic
    failed = False

    def fail_artifact_once(path: Path, payload: bytes) -> None:
        nonlocal failed
        if path == artifact and not failed:
            failed = True
            raise OSError("simulated artifact failure")
        original(path, payload)

    monkeypatch.setattr(repository, "_write_bytes_atomic", fail_artifact_once)
    with pytest.raises(OSError, match="simulated"):
        await pipeline.run()
    monkeypatch.setattr(repository, "_write_bytes_atomic", original)

    assert candidates.all()[0].pending_entry is not None
    assert await pipeline.run() == 0
    assert calls == 1
    assert candidates.all()[0].status == CandidateStatus.VERIFIED
    assert artifact.read_text("utf-8") == "さいかいたれんと\t再開タレント\n"


@pytest.mark.asyncio
async def test_twitch_discovery_resumes_from_a_saved_cursor(tmp_path: Path) -> None:
    class ResumableStreams:
        def __init__(self) -> None:
            self.cursors: list[str | None] = []
            self.fail_first_run = True

        def stream_pages(
            self, language: str | None, cursor: str | None = None
        ) -> AsyncIterator[TwitchStreamPage]:
            async def pages() -> AsyncIterator[TwitchStreamPage]:
                self.cursors.append(cursor)
                if cursor is None:
                    yield TwitchStreamPage(
                        streams=[
                            {
                                "user_id": "one",
                                "user_login": "one",
                                "user_name": "One",
                                "tags": ["VTuber"],
                            }
                        ],
                        next_cursor="next-page",
                    )
                    if self.fail_first_run:
                        raise OSError("network failure")
                if cursor == "next-page":
                    yield TwitchStreamPage(
                        streams=[
                            {
                                "user_id": "two",
                                "user_login": "two",
                                "user_name": "Two",
                                "tags": ["VTuber"],
                            }
                        ],
                        next_cursor=None,
                    )

            return pages()

    source = ResumableStreams()
    candidates = CandidateRepository(tmp_path / "data" / "candidates.jsonl")
    checkpoint = TwitchDiscoveryCheckpointRepository(
        tmp_path / "data" / "twitch_discovery_checkpoint.json"
    )
    discovery = TwitchDiscovery(source, "VTuber", "ja")
    with pytest.raises(OSError, match="network failure"):
        await discovery.discover(candidates, checkpoint)

    source.fail_first_run = False
    await discovery.discover(candidates, checkpoint)
    assert source.cursors == [None, "next-page"]
    assert {candidate.twitch_user_id for candidate in candidates.all()} == {"one", "two"}
    assert not checkpoint.path.exists()
