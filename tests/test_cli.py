"""Smoke tests for the package scaffold."""

from vtuber_dictionary.cli import main


def test_main_reports_ready(capsys: object) -> None:
    """The installed console entry point has an intentionally small smoke test."""
    main()
    assert "environment is ready" in capsys.readouterr().out
