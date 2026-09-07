"""Command-line entry point for safe dictionary updates."""

from __future__ import annotations

import argparse
import asyncio
import logging

from .agents import AgentFrameworkJsonRunner, ReadingResearchAgent, VerificationAgent
from .dictionary import DictionaryCompiler
from .discovery import AgencyDiscovery, TwitchDiscovery, persist_discoveries
from .platforms import (
    CombinedPlatformClient,
    TwitchHelixClient,
    YouTubeDataClient,
    twitch_app_access_token,
)
from .repository import AgencyRepository, CandidateRepository, EntryRepository, ReviewRepository
from .settings import Settings
from .validation import DeterministicValidator
from .workflow import Pipeline


async def update(settings: Settings) -> int:
    """Discover candidates first, then run the safe, threshold-gated pipeline."""
    candidate_repo = CandidateRepository(settings.data_dir / "candidates.jsonl")
    agency_repo = AgencyRepository(settings.data_dir / "agencies.json")
    # Agency discovery is optional when the versioned agency registry is initially empty.
    from .agency_source import AgencyPageTalentSource

    agencies = agency_repo.all()
    await persist_discoveries(
        candidate_repo,
        await AgencyDiscovery(AgencyPageTalentSource()).discover(
            [agency for agency in agencies if agency.active]
        ),
    )
    agency_repo.replace(agencies)
    twitch: TwitchHelixClient | None = None
    if settings.twitch_client_id and settings.twitch_client_secret:
        token = await twitch_app_access_token(
            settings.twitch_client_id, settings.twitch_client_secret.get_secret_value()
        )
        twitch = TwitchHelixClient(
            settings.twitch_client_id, token, max_pages=settings.twitch_discovery_max_pages
        )
        if settings.twitch_discovery_enabled:
            await persist_discoveries(
                candidate_repo,
                await TwitchDiscovery(
                    twitch, settings.twitch_discovery_tag, settings.twitch_discovery_language
                ).discover(),
            )
    youtube = (
        YouTubeDataClient(settings.youtube_api_key.get_secret_value())
        if settings.youtube_api_key
        else None
    )
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required for research and verification")
    runner = AgentFrameworkJsonRunner(
        settings.openai_api_key.get_secret_value(), settings.openai_model
    )
    pipeline = Pipeline(
        candidates=candidate_repo,
        entries=EntryRepository(settings.data_dir / "entries.jsonl"),
        reviews=ReviewRepository(settings.data_dir / "review_required.jsonl"),
        platforms=CombinedPlatformClient(youtube, twitch),
        researcher=ReadingResearchAgent(runner),
        verifier=VerificationAgent(runner),
        validator=DeterministicValidator(),
        compiler=DictionaryCompiler(),
        settings=settings,
    )
    return await pipeline.run()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the verified Japanese VTuber IME dictionary."
    )
    parser.add_argument("command", nargs="?", choices=["update"], default="update")
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    count = asyncio.run(update(Settings()))
    logging.getLogger(__name__).info("dictionary_update_complete", extra={"new_entries": count})
