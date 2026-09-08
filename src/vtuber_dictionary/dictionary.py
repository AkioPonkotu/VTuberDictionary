"""Canonical-entry and IME artifact compilation."""

from __future__ import annotations

from pathlib import Path

from .domain import DictionaryEntry
from .repository import _write_text_atomic


class TsvExporter:
    def export(self, entries: list[DictionaryEntry]) -> str:
        return "".join(
            f"{entry.reading}\t{entry.canonical_name}\n"
            for entry in sorted(entries, key=lambda x: (x.reading, x.canonical_name))
        )


class DictionaryCompiler:
    def __init__(self, exporter: TsvExporter | None = None) -> None:
        self.exporter = exporter or TsvExporter()

    def render(self, entries: list[DictionaryEntry]) -> str:
        return self.exporter.export(entries)

    def compile(self, entries: list[DictionaryEntry], destination: Path) -> None:
        _write_text_atomic(destination, self.render(entries))
