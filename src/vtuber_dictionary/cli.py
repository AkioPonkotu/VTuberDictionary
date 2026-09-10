"""Command-line entry point for safe dictionary updates."""

from __future__ import annotations

import argparse
import asyncio
import logging

from .agents import (
    AgentFrameworkJsonRunner,
    MissingOpenAICredentials,
    ReadingResearchAgent,
    VerificationAgent,
)
from .dictionary import DictionaryCompiler
from .discovery import AgencyDiscovery, TwitchDiscovery
from .platforms import (
    CombinedPlatformClient,
    TwitchHelixClient,
    YouTubeDataClient,
    twitch_app_access_token,
)
from .ports import ReadingResearcher, Verifier
from .repository import (
    AgencyRepository,
    CandidateRepository,
    EntryRepository,
    ReviewRepository,
    TwitchDiscoveryCheckpointRepository,
)
from .settings import Settings
from .validation import DeterministicValidator
from .web_sources import TwitchSearchSourcePrefetcher
from .workflow import Pipeline

PROCESSING_SOURCE_MARKERS = {
    "agency": "agency",
    "twitch": "twitch_vtuber_tag",
}


async def discover(settings: Settings, discovery_sources: set[str] | None = None) -> None:
    """Persist candidates discovered from the selected sources."""
    enabled_sources = discovery_sources or {"agency", "twitch"}
    candidate_repo = CandidateRepository(settings.data_dir / "candidates.jsonl")
    agency_repo = AgencyRepository(settings.data_dir / "agencies.json")
    # Agency discovery is optional when the versioned agency registry is initially empty.
    from .agency_source import AgencyPageTalentSource

    agencies = agency_repo.all()
    if "agency" in enabled_sources:
        await AgencyDiscovery(AgencyPageTalentSource()).discover(
            [agency for agency in agencies if agency.active],
            candidates_repository=candidate_repo,
            agencies_repository=agency_repo,
        )
    twitch: TwitchHelixClient | None = None
    if settings.twitch_client_id and settings.twitch_client_secret:
        token = await twitch_app_access_token(
            settings.twitch_client_id, settings.twitch_client_secret.get_secret_value()
        )
        twitch = TwitchHelixClient(
            settings.twitch_client_id, token, max_pages=settings.twitch_discovery_max_pages
        )
        if "twitch" in enabled_sources and settings.twitch_discovery_enabled:
            await TwitchDiscovery(
                twitch, settings.twitch_discovery_tag, settings.twitch_discovery_language
            ).discover(
                candidates_repository=candidate_repo,
                checkpoint_repository=TwitchDiscoveryCheckpointRepository(
                    settings.data_dir / "twitch_discovery_checkpoint.json"
                ),
            )


async def process(settings: Settings, processing_sources: set[str] | None = None) -> int:
    """Research and publish persisted candidates from the selected sources."""
    candidate_repo = CandidateRepository(settings.data_dir / "candidates.jsonl")
    twitch: TwitchHelixClient | None = None
    if settings.twitch_client_id and settings.twitch_client_secret:
        token = await twitch_app_access_token(
            settings.twitch_client_id, settings.twitch_client_secret.get_secret_value()
        )
        twitch = TwitchHelixClient(
            settings.twitch_client_id, token, max_pages=settings.twitch_discovery_max_pages
        )
    youtube = (
        YouTubeDataClient(settings.youtube_api_key.get_secret_value())
        if settings.youtube_api_key
        else None
    )
    researcher: ReadingResearcher
    verifier: Verifier
    if settings.openai_api_key:
        if not settings.openai_model:
            raise RuntimeError("OPENAI_MODEL is required when OPENAI_API_KEY is configured")
        runner = AgentFrameworkJsonRunner(
            settings.openai_api_key.get_secret_value(),
            settings.openai_model,
            max_concurrency=settings.openai_concurrency,
            min_request_interval_seconds=settings.openai_min_request_interval_seconds,
            rate_limit_retry_seconds=settings.openai_rate_limit_retry_seconds,
        )
        researcher = ReadingResearchAgent(runner)
        verifier = VerificationAgent(runner)
    else:
        missing_credentials = MissingOpenAICredentials()
        researcher = missing_credentials
        verifier = missing_credentials
    pipeline = Pipeline(
        candidates=candidate_repo,
        entries=EntryRepository(settings.data_dir / "entries.jsonl"),
        reviews=ReviewRepository(settings.data_dir / "review_required.jsonl"),
        platforms=CombinedPlatformClient(youtube, twitch),
        researcher=researcher,
        verifier=verifier,
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=settings,
        candidate_source_filter=(
            {PROCESSING_SOURCE_MARKERS[source] for source in processing_sources}
            if processing_sources
            else None
        ),
        web_sources=TwitchSearchSourcePrefetcher(
            enabled=settings.twitch_crawler_enabled,
            max_results=settings.twitch_crawler_max_results,
            minimum_delay_seconds=settings.twitch_crawler_delay_seconds,
        ),
    )
    return await pipeline.run()


async def update(settings: Settings, discovery_sources: set[str] | None = None) -> int:
    """Discover candidates first, then run the safe, threshold-gated pipeline."""
    await discover(settings, discovery_sources)
    return await process(settings, discovery_sources)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the verified Japanese VTuber IME dictionary."
    )
    parser.add_argument(
        "command", nargs="?", choices=["update", "discover", "process"], default="update"
    )
    parser.add_argument(
        "--source",
        action="append",
        choices=("agency", "twitch"),
        dest="sources",
        help="For update/discover/process: select sources; specify more than once to combine.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx includes query strings in its INFO request logs.  API keys must never be logged.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    sources = set(args.sources or [])
    if args.command == "discover":
        asyncio.run(discover(Settings(), sources))
        logging.getLogger(__name__).info("dictionary_discovery_complete")
        return
    if args.command == "process":
        count = asyncio.run(process(Settings(), sources))
    else:
        count = asyncio.run(update(Settings(), sources))
    logging.getLogger(__name__).info("dictionary_update_complete new_entries=%s", count)
