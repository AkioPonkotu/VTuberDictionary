"""Canonical-entry exporters for the supported IME dictionary formats."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .domain import DictionaryEntry
from .repository import _write_bytes_atomic


def _sorted(entries: Iterable[DictionaryEntry]) -> list[DictionaryEntry]:
    return sorted(entries, key=lambda entry: (entry.reading, entry.canonical_name))


@dataclass(frozen=True)
class DictionaryArtifact:
    """A named, fully encoded distribution artifact."""

    filename: str
    payload: bytes


class TsvExporter:
    """The project's original UTF-8, two-column TSV artifact."""

    filename = "vtuber_dictionary.tsv"

    def export(self, entries: Iterable[DictionaryEntry]) -> bytes:
        text = "".join(
            f"{entry.reading}\t{entry.canonical_name}\n" for entry in _sorted(entries)
        )
        return text.encode("utf-8")


class MicrosoftImeExporter:
    """Microsoft IME bulk-registration text (UTF-16 LE with a BOM)."""

    filename = "vtuber_dictionary_msime.txt"
    part_of_speech = "固有名詞"

    def export(self, entries: Iterable[DictionaryEntry]) -> bytes:
        text = "".join(
            f"{entry.reading}\t{entry.canonical_name}\t{self.part_of_speech}\r\n"
            for entry in _sorted(entries)
        )
        return b"\xff\xfe" + text.encode("utf-16-le")


class MacOsImeExporter:
    """macOS Japanese Input professional-dictionary CSV exporter."""

    filename = "vtuber_dictionary_macos.csv"
    part_of_speech = "proper noun"
    maximum_reading_length = 32
    maximum_word_length = 64
    maximum_line_length = 127

    def export(self, entries: Iterable[DictionaryEntry]) -> bytes:
        lines: list[str] = []
        for entry in _sorted(entries):
            self._validate_field_lengths(entry)
            output = io.StringIO(newline="")
            csv.writer(output, lineterminator="\n").writerow(
                (entry.reading, entry.canonical_name, self.part_of_speech)
            )
            line = output.getvalue()
            if len(line.removesuffix("\n")) > self.maximum_line_length:
                raise ValueError(
                    f"macOS dictionary line exceeds {self.maximum_line_length} characters: "
                    f"{entry.canonical_name!r}"
                )
            lines.append(line)
        return "".join(lines).encode("utf-8")

    def _validate_field_lengths(self, entry: DictionaryEntry) -> None:
        if "\r" in entry.reading or "\n" in entry.reading:
            raise ValueError(f"macOS dictionary reading contains a line break: {entry.reading!r}")
        if "\r" in entry.canonical_name or "\n" in entry.canonical_name:
            raise ValueError(
                f"macOS dictionary word contains a line break: {entry.canonical_name!r}"
            )
        if len(entry.reading) > self.maximum_reading_length:
            raise ValueError(
                f"macOS dictionary reading exceeds {self.maximum_reading_length} characters: "
                f"{entry.reading!r}"
            )
        if len(entry.canonical_name) > self.maximum_word_length:
            raise ValueError(
                f"macOS dictionary word exceeds {self.maximum_word_length} characters: "
                f"{entry.canonical_name!r}"
            )


class DictionaryCompiler:
    """Renders every distributable dictionary from the same canonical entries."""

    def __init__(
        self,
        exporters: tuple[TsvExporter | MicrosoftImeExporter | MacOsImeExporter, ...] | None = None,
    ) -> None:
        self.exporters = exporters or (TsvExporter(), MicrosoftImeExporter(), MacOsImeExporter())

    def artifacts(self, entries: Iterable[DictionaryEntry]) -> list[DictionaryArtifact]:
        entry_list = list(entries)
        return [
            DictionaryArtifact(exporter.filename, exporter.export(entry_list))
            for exporter in self.exporters
        ]

    def artifact_paths(self, destination_directory: Path) -> list[Path]:
        return [destination_directory / exporter.filename for exporter in self.exporters]

    def render(self, entries: Iterable[DictionaryEntry]) -> str:
        """Retain the previous public API for callers of the original TSV renderer."""
        tsv_exporter = next(
            exporter for exporter in self.exporters if isinstance(exporter, TsvExporter)
        )
        return tsv_exporter.export(entries).decode("utf-8")

    def compile(self, entries: Iterable[DictionaryEntry], destination: Path) -> None:
        """Write the original TSV artifact to an explicit caller-provided destination."""
        _write_bytes_atomic(destination, TsvExporter().export(entries))
