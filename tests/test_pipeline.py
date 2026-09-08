from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vtuber_dictionary.agency_source import AgencyPageTalentSource
from vtuber_dictionary.agents import ReadingResearchAgent, VerificationAgent
from vtuber_dictionary.dictionary import DictionaryCompiler
from vtuber_dictionary.discovery import AgencyDiscovery, TwitchDiscovery
from vtuber_dictionary.domain import (
    Agency,
    AudienceMetrics,
    Candidate,
    DictionaryEntry,
    Evidence,
    ResearchResult,
    VerificationResult,
    WebSource,
)
from vtuber_dictionary.filtering import ExistingEntryFilter, ThresholdFilter
from vtuber_dictionary.platforms import (
    AuthenticationError,
    CombinedPlatformClient,
    RetryingHttpClient,
    TwitchHelixClient,
    YouTubeDataClient,
)
from vtuber_dictionary.repository import CandidateRepository, EntryRepository, ReviewRepository
from vtuber_dictionary.settings import Settings
from vtuber_dictionary.validation import DeterministicValidator
from vtuber_dictionary.web_sources import OfficialSourcePrefetcher, visible_text
from vtuber_dictionary.workflow import Pipeline


class FakeAgencySource:
    async def list_talents(self, agency: Agency) -> list[Candidate]:
        return [Candidate(display_name="公式タレント", youtube_channel_id="channel-1")]


class FakeStreams:
    def streams(self, language: str | None) -> AsyncIterator[list[dict[str, object]]]:
        async def pages() -> AsyncIterator[list[dict[str, object]]]:
            assert language == "ja"
            yield [
                {"user_id": "1", "user_login": "one", "user_name": "One", "tags": ["vTuBeR"]},
                {"user_id": "2", "user_login": "two", "user_name": "Two", "tags": ["gaming"]},
            ]

        return pages()


class FakeRunner:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.response_models: list[type[object]] = []
        self.prompts: list[str] = []

    async def run_json(self, instructions: str, prompt: str, response_model: type[object]) -> str:
        self.response_models.append(response_model)
        self.prompts.append(prompt)
        return self.responses.pop(0)


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
        display_name="candidate", youtube_channel_url="https://youtube.com/@candidate?sub_confirmation=1"
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


def test_existing_entry_filter_uses_canonical_id_and_reverify_window() -> None:
    candidate = Candidate(display_name="表示名")
    entry = DictionaryEntry(
        canonical_id=candidate.canonical_id,
        reading="ひょうじめい",
        canonical_name="表示名",
        source_urls=["https://official.example"],
        verified_at=datetime.now(UTC),
    )
    filter_ = ExistingEntryFilter([entry], 180)
    assert not filter_.needs_research(candidate)
    assert filter_.needs_research(candidate, entry.verified_at + timedelta(days=181))


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
    assert not ExistingEntryFilter([entry], 180).needs_research(candidate)


def test_settings_keeps_model_unset_when_env_value_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "")
    assert Settings(_env_file=None).openai_model is None


@pytest.mark.asyncio
async def test_research_and_verification_parse_structured_output() -> None:
    runner = FakeRunner(
        [
            '{"canonical_name":"星街すいせい","reading":"ほしまちすいせい","confidence":0.9,"evidence":[{"url":"https://official.example","source_type":"official_profile","claim":"reading"}],"status":"resolved"}',
            '{"verified":true,"canonical_name":"星街すいせい","reading":"ほしまちすいせい","confidence":0.95,"evidence":[{"url":"https://official.example","source_type":"official_profile","claim":"reading"}],"issues":[]}',
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


def test_validator_allows_fetched_agency_profile_without_verification() -> None:
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
    assert validator.can_skip_verification(candidate, research, [source])
    entry, reason = validator.validate(candidate, research, None, [], [source])
    assert reason is None and entry is not None


def test_deterministic_validator_and_compiler(tmp_path: Path) -> None:
    candidate = Candidate(display_name="星街すいせい")
    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="reading")
    ]
    research = ResearchResult(
        canonical_name="星街すいせい",
        reading="ほしまちすいせい",
        confidence=0.9,
        evidence=evidence,
        status="resolved",
    )
    verification = VerificationResult(
        verified=True,
        canonical_name="星街すいせい",
        reading="ほしまちすいせい",
        confidence=0.9,
        evidence=evidence,
    )
    entry, reason = DeterministicValidator().validate(candidate, research, verification, [])
    assert reason is None and entry is not None
    output = tmp_path / "dictionary.tsv"
    DictionaryCompiler().compile([entry], output)
    assert output.read_text("utf-8") == "ほしまちすいせい\t星街すいせい\n"


def test_validator_rejects_unverified_and_invalid_reading() -> None:
    candidate = Candidate(display_name="X")
    invalid = VerificationResult(
        verified=True, canonical_name="X", reading="invalid", confidence=1, evidence=[]
    )
    research = ResearchResult(confidence=0, status="unresolved")
    entry, reason = DeterministicValidator().validate(candidate, research, invalid, [])
    assert entry is None and reason == "reading must consist of hiragana and prolonged-sound mark"


def test_validator_rejects_conflicting_duplicate_reading() -> None:
    candidate = Candidate(display_name="X")
    evidence = [
        Evidence(url="https://official.example", source_type="official_profile", claim="reading")
    ]
    research = ResearchResult(
        canonical_name="X", reading="えっくす", confidence=1, evidence=evidence, status="resolved"
    )
    verification = VerificationResult(
        verified=True, canonical_name="X", reading="えっくす", confidence=1, evidence=evidence
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
        canonical_name="X", reading="えっくす", confidence=1, evidence=evidence, status="resolved"
    )
    verification = VerificationResult(
        verified=True, canonical_name="X", reading="えくす", confidence=1, evidence=evidence
    )
    entry, reason = DeterministicValidator().validate(candidate, research, verification, [])
    assert entry is None and reason == "research and verification results disagree"


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


@pytest.mark.asyncio
async def test_pipeline_skips_verification_for_prefetched_agency_profile(tmp_path: Path) -> None:
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

    class MustNotVerify:
        async def verify(
            self,
            candidate: Candidate,
            research: ResearchResult,
            metrics: AudienceMetrics,
            sources: list[WebSource],
        ) -> VerificationResult:
            raise AssertionError("official agency evidence must skip verification")

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
    pipeline = Pipeline(
        candidates=candidates,
        entries=EntryRepository(data_dir / "entries.jsonl"),
        reviews=ReviewRepository(data_dir / "review_required.jsonl"),
        platforms=Metrics(),
        researcher=Researcher(),
        verifier=MustNotVerify(),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=Settings(data_dir=data_dir, dist_dir=dist_dir),
        web_sources=Sources(),
    )
    assert await pipeline.run() == 1


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
