"""Small JSON/JSONL repositories.  Writes are atomic to protect prior outputs."""

from __future__ import annotations

import json
import os
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


def _jsonl_payload(values: Iterable[BaseModel]) -> str:
    return "".join(f"{item.model_dump_json()}\n" for item in values)


def _fsync_directory(path: Path) -> None:
    """Persist a rename's directory entry where the platform supports it."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        # Windows does not allow opening a directory this way.  os.replace is
        # still atomic there; the file fsync below supplies the available
        # durability guarantee.
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_text_atomic(path: Path, payload: str) -> None:
    """Atomically replace ``path`` with crash-durable file contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            temporary_path = Path(tmp.name)
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _remove_durable(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _write_jsonl_atomic(path: Path, values: Iterable[BaseModel]) -> None:
    _write_text_atomic(path, _jsonl_payload(values))


class CandidateRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[Candidate]:
        return _read_jsonl(self.path, Candidate)

    def replace(self, candidates: Iterable[Candidate]) -> None:
        _write_jsonl_atomic(self.path, candidates)

    def upsert(self, incoming: Candidate) -> Candidate:
        return self.upsert_many([incoming])[0]

    def upsert_many(self, incoming_values: Iterable[Candidate]) -> list[Candidate]:
        incoming_list = list(incoming_values)
        if not incoming_list:
            return []
        candidates = self.all()
        stored = [self._upsert(candidates, incoming) for incoming in incoming_list]
        self.replace(candidates)
        return stored

    @staticmethod
    def _upsert(candidates: list[Candidate], incoming: Candidate) -> Candidate:
        matches = [item for item in candidates if item.identity_keys() & incoming.identity_keys()]
        if len(matches) > 1:
            incoming.status = CandidateStatus.REVIEW_REQUIRED
            candidates.append(incoming)
            return incoming
        if not matches:
            candidates.append(incoming)
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
        return existing


class TwitchDiscoveryCheckpointRepository:
    """Persists the continuation cursor only after its page has been stored."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, tag: str, language: str | None) -> str | None:
        if not self.path.exists():
            return None
        try:
            checkpoint = json.loads(self.path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid Twitch discovery checkpoint") from exc
        if checkpoint.get("tag") != tag or checkpoint.get("language") != language:
            return None
        cursor = checkpoint.get("cursor")
        if not isinstance(cursor, str) or not cursor:
            raise RuntimeError("invalid Twitch discovery cursor")
        return cursor

    def save(self, tag: str, language: str | None, cursor: str) -> None:
        _write_text_atomic(
            self.path,
            json.dumps({"tag": tag, "language": language, "cursor": cursor}, separators=(",", ":")),
        )

    def clear(self) -> None:
        _remove_durable(self.path)


class EntryRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[DictionaryEntry]:
        return _read_jsonl(self.path, DictionaryEntry)

    def replace(self, entries: Iterable[DictionaryEntry]) -> None:
        _write_jsonl_atomic(self.path, entries)

    @property
    def _transaction_path(self) -> Path:
        return self.path.parent / ".dictionary-transaction.json"

    def recover_publication(self, artifact: Path) -> None:
        """Finish a previously journaled entry/TSV publication, if any."""
        if not self._transaction_path.exists():
            return
        try:
            transaction = json.loads(self._transaction_path.read_text("utf-8"))
            entries_payload = transaction["entries_jsonl"]
            artifact_payload = transaction["artifact_tsv"]
            expected_entries = transaction["entries_path"]
            expected_artifact = transaction["artifact_path"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid dictionary publication journal") from exc
        if not all(isinstance(item, str) for item in (entries_payload, artifact_payload)):
            raise RuntimeError("invalid dictionary publication journal payload")
        if (
            expected_entries != str(self.path.resolve())
            or expected_artifact != str(artifact.resolve())
        ):
            raise RuntimeError("dictionary publication journal targets a different output")
        _write_text_atomic(self.path, entries_payload)
        _write_text_atomic(artifact, artifact_payload)
        _remove_durable(self._transaction_path)

    def publish(
        self, entries: Iterable[DictionaryEntry], artifact: Path, artifact_payload: str
    ) -> None:
        """Publish canonical data and its TSV using a replayable write-ahead journal."""
        self.recover_publication(artifact)
        entries_payload = _jsonl_payload(entries)
        transaction = {
            "entries_path": str(self.path.resolve()),
            "artifact_path": str(artifact.resolve()),
            "entries_jsonl": entries_payload,
            "artifact_tsv": artifact_payload,
        }
        _write_text_atomic(
            self._transaction_path,
            json.dumps(transaction, ensure_ascii=False, separators=(",", ":")),
        )
        self.recover_publication(artifact)


class ReviewRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[ReviewRecord]:
        return _read_jsonl(self.path, ReviewRecord)

    def replace(self, records: Iterable[ReviewRecord]) -> None:
        _write_jsonl_atomic(self.path, records)

    @staticmethod
    def _upsert(records: Iterable[ReviewRecord], record: ReviewRecord) -> list[ReviewRecord]:
        by_candidate = {item.canonical_id: item for item in records}
        by_candidate[record.canonical_id] = record
        return list(by_candidate.values())

    def checkpoint_review(
        self,
        candidates: CandidateRepository,
        candidate_values: Iterable[Candidate],
        record: ReviewRecord,
    ) -> None:
        """Durably checkpoint the status and one review record as one replayable action."""
        self.recover_checkpoint(candidates)
        transaction_path = candidates.path.parent / ".review-transaction.json"
        candidate_payload = _jsonl_payload(candidate_values)
        review_payload = _jsonl_payload(self._upsert(self.all(), record))
        transaction = {
            "candidates_path": str(candidates.path.resolve()),
            "reviews_path": str(self.path.resolve()),
            "candidates_jsonl": candidate_payload,
            "reviews_jsonl": review_payload,
        }
        _write_text_atomic(
            transaction_path,
            json.dumps(transaction, ensure_ascii=False, separators=(",", ":")),
        )
        self.recover_checkpoint(candidates)

    def recover_checkpoint(self, candidates: CandidateRepository) -> None:
        transaction_path = candidates.path.parent / ".review-transaction.json"
        if not transaction_path.exists():
            return
        try:
            transaction = json.loads(transaction_path.read_text("utf-8"))
            candidate_payload = transaction["candidates_jsonl"]
            review_payload = transaction["reviews_jsonl"]
            expected_candidates = transaction["candidates_path"]
            expected_reviews = transaction["reviews_path"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid review checkpoint journal") from exc
        if not all(isinstance(item, str) for item in (candidate_payload, review_payload)):
            raise RuntimeError("invalid review checkpoint journal payload")
        if expected_candidates != str(candidates.path.resolve()) or expected_reviews != str(
            self.path.resolve()
        ):
            raise RuntimeError("review checkpoint journal targets a different output")
        _write_text_atomic(candidates.path, candidate_payload)
        _write_text_atomic(self.path, review_payload)
        _remove_durable(transaction_path)


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
        payload = json.dumps(
            [agency.model_dump(mode="json") for agency in agencies], ensure_ascii=False
        )
        _write_text_atomic(self.path, payload + "\n")
