from __future__ import annotations

import sys

import pytest

from vtuber_dictionary import cli


@pytest.mark.parametrize(
    ("arguments", "expected_sources"),
    [
        (["update"], set()),
        (["update", "--source", "agency"], {"agency"}),
        (["--source", "agency", "--source", "twitch"], {"agency", "twitch"}),
    ],
)
def test_main_passes_selected_discovery_sources(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], expected_sources: set[str]
) -> None:
    received: list[set[str]] = []

    async def fake_update(settings: object, discovery_sources: set[str] | None = None) -> int:
        received.append(discovery_sources or set())
        return 0

    monkeypatch.setattr(cli, "update", fake_update)
    monkeypatch.setattr(sys, "argv", ["vtuber-dictionary", *arguments])

    cli.main()

    assert received == [expected_sources]
