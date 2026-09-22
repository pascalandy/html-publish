from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NewType

Name = NewType("Name", str)
Revision = NewType("Revision", str)
Commit = NewType("Commit", str)
RelativePath = NewType("RelativePath", str)


def _empty_details() -> Mapping[str, object]:
    return {}


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 100 * 1024 * 1024
    max_files: int = 2_000
    command_seconds: float = 120.0
    lock_seconds: float = 30.0
    verification_seconds: float = 60.0


@dataclass(frozen=True)
class Config:
    archive: Path
    runtime: Path
    base_url: str
    allow_http: bool
    object_format: Literal["sha1", "sha256"]
    limits: Limits


@dataclass(frozen=True)
class Deadline:
    expires_at: float

    @classmethod
    def start(cls, seconds: float) -> Deadline:
        return cls(time.monotonic() + seconds)

    def remaining(self, ceiling: float | None = None) -> float:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise PublishError(
                "command_timeout",
                "timeout",
                "The command exceeded its total time budget",
                "retry",
            )
        return min(remaining, ceiling) if ceiling is not None else remaining


@dataclass(frozen=True)
class FileEntry:
    path: RelativePath
    size: int
    blob: str


@dataclass(frozen=True)
class CapturedSite:
    root: Path
    entries: tuple[FileEntry, ...]
    revision: Revision
    total_bytes: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class StoredSite:
    revision: Revision
    entries: tuple[FileEntry, ...]


@dataclass(frozen=True)
class SavedPage:
    site: StoredSite
    commit: Commit


SelectionKind = Literal["absent", "selected", "degraded", "unobserved"]


@dataclass(frozen=True)
class Selection:
    kind: SelectionKind
    revision: Revision | None = None
    release: Path | None = None
    integrity_checked: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class LocalState:
    saved: SavedPage | None
    selection: Selection


@dataclass(frozen=True)
class StatusEntry:
    name: Name
    state: LocalState


@dataclass(frozen=True)
class HistoryEntry:
    archive_commit: Commit
    revision: Revision
    changes: Mapping[str, list[str]]


@dataclass(frozen=True)
class Effects:
    archive_advanced: bool | None = False
    activated: bool | None = False


@dataclass(frozen=True)
class Verification:
    result: Literal["passed", "failed", "not_checked"] = "not_checked"
    revision: Revision | None = None
    checked_at: str | None = None
    probe_location: str | None = None
    files_checked: int = 0
    bytes_checked: int = 0
    scope: tuple[str, ...] = ()
    detail: str | None = None


@dataclass(frozen=True)
class Failure:
    code: str
    phase: str
    message: str
    next_action: str
    required_inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Report:
    operation: Literal["plan", "publish", "status", "verify", "history", "restore"]
    outcome: Literal["planned", "published", "unchanged", "observed", "verified", "error"]
    target: str | None
    name: Name | None
    url: str | None
    request_id: str | None = None
    expected_revision: Revision | None = None
    requested_revision: Revision | None = None
    state: LocalState | None = None
    effects: Effects = field(default_factory=Effects)
    verification: Verification = field(default_factory=Verification)
    warnings: tuple[str, ...] = ()
    error: Failure | None = None
    details: Mapping[str, object] = field(default_factory=_empty_details)
    status_entries: tuple[StatusEntry, ...] = ()


class PublishError(Exception):
    def __init__(
        self,
        code: str,
        phase: str,
        message: str,
        next_action: str,
        required_inputs: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.failure = Failure(code, phase, message, next_action, required_inputs)
