"""Small JSON/JSONL repositories.  Writes are atomic to protect prior outputs."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel

from .domain import Agency, Candidate, CandidateStatus, DictionaryEntry, ReviewRecord


def _read_jsonl[T: BaseModel](path: Path, model: type[T]) -> list[T]:
    if not path.exists():
        return []
    return [
        model.model_validate_json(line) for line in path.read_text("utf-8").splitlines() if line
    ]


def _write_jsonl_atomic(path: Path, values: Iterable[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(f"{item.model_dump_json()}\n" for item in values)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        temporary_path = Path(tmp.name)
    temporary_path.replace(path)


class CandidateRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[Candidate]:
        return _read_jsonl(self.path, Candidate)

    def replace(self, candidates: Iterable[Candidate]) -> None:
        _write_jsonl_atomic(self.path, candidates)

    def upsert(self, incoming: Candidate) -> Candidate:
        candidates = self.all()
        matches = [item for item in candidates if item.identity_keys() & incoming.identity_keys()]
        if len(matches) > 1:
            incoming.status = CandidateStatus.REVIEW_REQUIRED
            candidates.append(incoming)
            self.replace(candidates)
            return incoming
        if not matches:
            candidates.append(incoming)
            self.replace(candidates)
            return incoming
        existing = matches[0]
        for field in (
            "agency",
            "official_profile_url",
            "youtube_channel_id",
            "youtube_channel_url",
            "twitch_user_id",
            "twitch_login",
            "twitch_url",
        ):
            if getattr(existing, field) is None and getattr(incoming, field) is not None:
                setattr(existing, field, getattr(incoming, field))
        existing.discovery_sources |= incoming.discovery_sources
        existing.last_seen_at = incoming.last_seen_at
        self.replace(candidates)
        return existing


class EntryRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[DictionaryEntry]:
        return _read_jsonl(self.path, DictionaryEntry)

    def replace(self, entries: Iterable[DictionaryEntry]) -> None:
        _write_jsonl_atomic(self.path, entries)


class ReviewRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: ReviewRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(record.model_dump_json() + "\n")


class AgencyRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[Agency]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text("utf-8"))
        return [Agency.model_validate(item) for item in raw]

    def all_active(self) -> list[Agency]:
        return [agency for agency in self.all() if agency.active]

    def replace(self, agencies: Iterable[Agency]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [agency.model_dump(mode="json") for agency in agencies], ensure_ascii=False
        )
        self.path.write_text(payload + "\n", encoding="utf-8")
