"""Canonical-entry and IME artifact compilation."""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

from .domain import DictionaryEntry


class TsvExporter:
    def export(self, entries: list[DictionaryEntry]) -> str:
        return "".join(
            f"{entry.reading}\t{entry.canonical_name}\n"
            for entry in sorted(entries, key=lambda x: (x.reading, x.canonical_name))
        )


class DictionaryCompiler:
    def __init__(self, exporter: TsvExporter | None = None) -> None:
        self.exporter = exporter or TsvExporter()

    def compile(self, entries: list[DictionaryEntry], destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=destination.parent, delete=False
        ) as tmp:
            tmp.write(self.exporter.export(entries))
            temporary_path = Path(tmp.name)
        temporary_path.replace(destination)
