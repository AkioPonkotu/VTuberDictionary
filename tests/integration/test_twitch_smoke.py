"""Opt-in integration checks; never included in the normal unit-test suite."""

import os

import pytest

from vtuber_dictionary.domain import Candidate
from vtuber_dictionary.platforms import TwitchHelixClient, twitch_app_access_token


@pytest.mark.integration
@pytest.mark.asyncio
async def test_twitch_followers_for_configured_broadcaster() -> None:
    client_id = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")
    broadcaster_id = os.getenv("TWITCH_SMOKE_BROADCASTER_ID")
    if not all((client_id, client_secret, broadcaster_id)):
        pytest.skip("Set Twitch credentials and TWITCH_SMOKE_BROADCASTER_ID to run this smoke test")
    token = await twitch_app_access_token(client_id, client_secret)
    metrics = await TwitchHelixClient(client_id, token).audience_metrics(
        Candidate(display_name="smoke", twitch_user_id=broadcaster_id)
    )
    assert metrics.twitch_followers is not None
