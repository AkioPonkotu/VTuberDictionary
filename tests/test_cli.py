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


@pytest.mark.asyncio
async def test_update_checkpoints_discovery_before_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_discover(settings: object, discovery_sources: set[str] | None = None) -> None:
        assert discovery_sources == {"agency"}
        calls.append("discover")

    async def fake_process(settings: object, processing_sources: set[str] | None = None) -> int:
        assert processing_sources == {"agency"}
        calls.append("process")
        return 2

    monkeypatch.setattr(cli, "discover", fake_discover)
    monkeypatch.setattr(cli, "process", fake_process)

    assert await cli.update(object(), {"agency"}) == 2  # type: ignore[arg-type]
    assert calls == ["discover", "process"]


def test_main_runs_discovery_without_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[set[str]] = []

    async def fake_discover(settings: object, discovery_sources: set[str] | None = None) -> None:
        received.append(discovery_sources or set())

    monkeypatch.setattr(cli, "discover", fake_discover)
    monkeypatch.setattr(sys, "argv", ["vtuber-dictionary", "discover", "--source", "twitch"])

    cli.main()

    assert received == [{"twitch"}]


def test_main_passes_selected_processing_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[set[str]] = []

    async def fake_process(settings: object, processing_sources: set[str] | None = None) -> int:
        received.append(processing_sources or set())
        return 0

    monkeypatch.setattr(cli, "process", fake_process)
    monkeypatch.setattr(
        sys, "argv", ["vtuber-dictionary", "process", "--source", "twitch"]
    )

    cli.main()

    assert received == [{"twitch"}]
