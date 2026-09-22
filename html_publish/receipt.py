#!/usr/bin/env python3
"""Publish HTML through the existing publisher with a durable caller receipt."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn, Protocol, cast

from html_publish.configuration import (
    ClientConfig,
    TargetIdentity,
    load_client_config,
)
from html_publish.configuration import (
    ClientLimits as Limits,
)
from html_publish.model import PublishError

DEFAULT_CONFIG = Path("~/.config/html-publish/client.json").expanduser()
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")

PendingState = Literal["uncertain", "retryable", "conflict"]
InputKind = Literal["file", "directory"]
ExecutorKind = Literal["local", "remote"]


class UsageFailure(Exception):
    pass


class ReceiptFailure(Exception):
    def __init__(self, code: str, message: str, next_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_action = next_action


class PersistenceFailure(ReceiptFailure):
    def __init__(self, message: str) -> None:
        super().__init__("receipt_persistence_failed", message, "retry_after_inspection")


@dataclass(frozen=True)
class CommandBudget:
    expires_at: float

    def remaining(self, ceiling: float | None = None) -> float:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise ReceiptFailure(
                "command_timeout", "The artifact command exceeded its total time budget", "retry"
            )
        return min(remaining, ceiling) if ceiling is not None else remaining


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageFailure(message)


@dataclass(frozen=True)
class Binding:
    association_id: str
    name: str
    target: TargetIdentity
    config_fingerprint: str


@dataclass(frozen=True)
class Expectation:
    kind: Literal["accepted", "reviewed"]
    revision: str | None
    replaces_attempt: str | None


@dataclass(frozen=True)
class FrozenInput:
    path: str
    kind: InputKind
    digest: str
    file_count: int
    byte_count: int


@dataclass(frozen=True)
class PublishIntent:
    id: str
    expectation: Expectation
    input: FrozenInput


@dataclass(frozen=True)
class RestoreIntent:
    id: str
    expectation: Expectation
    archive_commit: str


Intent = PublishIntent | RestoreIntent


@dataclass(frozen=True)
class Pending:
    intent: Intent
    dispatch_generation: int
    state: PendingState


@dataclass(frozen=True)
class Observation:
    operation: str
    outcome: str
    name: str | None
    target: str | None
    url: str | None
    request_id: str | None
    expected_revision: str | None
    requested_revision: str | None
    active_revision: str | None
    verification: str | None
    error_code: str | None


@dataclass(frozen=True)
class Completion:
    attempt_id: str
    dispatch_generation: int
    result_digest: str


@dataclass(frozen=True)
class Receipt:
    version: Literal[1, 2]
    binding: Binding
    accepted_revision: str | None
    pending: Pending | None
    last_observation: Observation | None
    completion: Completion | None


@dataclass(frozen=True)
class SavedResult:
    attempt_id: str
    dispatch_generation: int
    result_digest: str
    exit_code: int
    timed_out: bool
    output_limited: bool
    stdout: str
    stderr: str
    payload: Mapping[str, object] | None
    group_stopped: bool | None = False
    cancelled: bool = False


@dataclass(frozen=True)
class Classification:
    receipt: Receipt
    kind: Literal[
        "completed",
        "delivery_failed",
        "conflict",
        "retryable",
        "uncertain",
        "rejected",
    ]
    message: str


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    timed_out: bool
    output_limited: bool
    stdout: bytes
    stderr: bytes
    group_stopped: bool | None = False
    cancelled: bool = False


@dataclass(frozen=True)
class Recovery:
    receipt: Receipt
    classification: Classification | None
    local_completion: bool
    cleanup_pending: Path | None


@dataclass(frozen=True)
class DispatchResult:
    classification: Classification
    visible_receipt: Receipt
    pending: Pending
    saved_result: SavedResult
    result_path: Path
    cleanup_pending: Path | None
    persistence_error: PersistenceFailure | None


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ReceiptFailure("invalid_state", f"{label} must be a JSON object", "inspect")
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise ReceiptFailure("invalid_state", f"{label} must be a JSON object", "inspect")
    return cast(Mapping[str, object], raw)


def _string(value: object, label: str) -> str | None:
    if not isinstance(value, str) or not value:
        raise ReceiptFailure("invalid_state", f"{label} must be a non-empty string", "inspect")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ReceiptFailure("invalid_state", f"{label} must be an integer >= {minimum}", "inspect")
    return value


def _optional_revision(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ReceiptFailure("invalid_state", f"{label} is invalid", "inspect")
    return value


def _name(value: str) -> str:
    if len(value) > 80 or not NAME_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "name must use lowercase letters, digits, and single hyphens, up to 80 characters"
        )
    return value


def _revision(value: str) -> str:
    if not value or len(value) > 256:
        raise argparse.ArgumentTypeError("revision must contain 1 to 256 characters")
    return value


def _attempt_id(value: str) -> str:
    if (
        not value
        or len(value) > 128
        or value in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in value)
    ):
        raise argparse.ArgumentTypeError(
            "attempt ID must be a safe identifier of 1 to 128 characters"
        )
    return value


def _stored_attempt_id(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ReceiptFailure("invalid_state", f"{label} is invalid", "inspect")
    try:
        return _attempt_id(value)
    except argparse.ArgumentTypeError as error:
        raise ReceiptFailure("invalid_state", f"{label} is invalid", "inspect") from error


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_config(path: Path, command_seconds: float | None = None) -> ClientConfig:
    try:
        config = load_client_config(path)
        if command_seconds is not None:
            return dataclasses.replace(
                config,
                limits=dataclasses.replace(config.limits, command_seconds=command_seconds),
            )
        return config
    except PublishError as error:
        raise ReceiptFailure(error.failure.code, error.failure.message, "fix_config") from error


def receipt_dict(receipt: Receipt) -> dict[str, object]:
    raw = cast(dict[str, object], dataclasses.asdict(receipt))
    if receipt.version == 2 and receipt.pending is not None:
        pending = cast(dict[str, object], raw["pending"])
        intent = cast(dict[str, object], pending["intent"])
        intent["operation"] = (
            "publish" if isinstance(receipt.pending.intent, PublishIntent) else "restore"
        )
    return raw


def _parse_target(value: object) -> TargetIdentity:
    raw = _object(value, "binding.target")
    if set(raw) != {"kind", "host", "base_url"}:
        raise ReceiptFailure("invalid_state", "binding.target has unexpected fields", "inspect")
    kind = raw.get("kind")
    if kind not in {"local", "remote"}:
        raise ReceiptFailure("invalid_state", "binding.target.kind is invalid", "inspect")
    host = _string(raw.get("host"), "binding.target.host")
    base_url = _string(raw.get("base_url"), "binding.target.base_url")
    assert host is not None and base_url is not None
    return TargetIdentity(cast(ExecutorKind, kind), host, base_url)


def _parse_expectation(value: object) -> Expectation:
    raw = _object(value, "pending.intent.expectation")
    if set(raw) != {"kind", "revision", "replaces_attempt"}:
        raise ReceiptFailure(
            "invalid_state", "pending expectation has unexpected fields", "inspect"
        )
    kind = raw.get("kind")
    if kind not in {"accepted", "reviewed"}:
        raise ReceiptFailure("invalid_state", "pending expectation kind is invalid", "inspect")
    revision = _optional_revision(raw.get("revision"), "pending expectation revision")
    replaces = raw.get("replaces_attempt")
    if replaces is not None and (not isinstance(replaces, str) or not replaces):
        raise ReceiptFailure("invalid_state", "replaces_attempt is invalid", "inspect")
    if kind == "accepted" and replaces is not None:
        raise ReceiptFailure(
            "invalid_state", "accepted expectation cannot replace an attempt", "inspect"
        )
    if kind == "reviewed" and revision is None:
        raise ReceiptFailure("invalid_state", "reviewed expectation requires a revision", "inspect")
    return Expectation(cast(Literal["accepted", "reviewed"], kind), revision, replaces)


def _parse_frozen(value: object) -> FrozenInput:
    raw = _object(value, "pending.intent.input")
    if set(raw) != {"path", "kind", "digest", "file_count", "byte_count"}:
        raise ReceiptFailure("invalid_state", "frozen input has unexpected fields", "inspect")
    path = _string(raw.get("path"), "frozen input path")
    kind = raw.get("kind")
    digest = _string(raw.get("digest"), "frozen input digest")
    if kind not in {"file", "directory"}:
        raise ReceiptFailure("invalid_state", "frozen input kind is invalid", "inspect")
    assert path is not None and digest is not None
    pure = Path(path)
    if pure.is_absolute() or ".." in pure.parts:
        raise ReceiptFailure(
            "invalid_state", "frozen input path must stay inside the receipt", "inspect"
        )
    return FrozenInput(
        path,
        cast(InputKind, kind),
        digest,
        _integer(raw.get("file_count"), "frozen input file_count"),
        _integer(raw.get("byte_count"), "frozen input byte_count"),
    )


def _parse_observation(value: object) -> Observation | None:
    if value is None:
        return None
    raw = _object(value, "last_observation")
    expected = {field.name for field in dataclasses.fields(Observation)}
    if set(raw) != expected:
        raise ReceiptFailure("invalid_state", "last_observation has unexpected fields", "inspect")
    return Observation(
        cast(str, raw["operation"]),
        cast(str, raw["outcome"]),
        cast(str | None, raw["name"]),
        cast(str | None, raw["target"]),
        cast(str | None, raw["url"]),
        cast(str | None, raw["request_id"]),
        cast(str | None, raw["expected_revision"]),
        cast(str | None, raw["requested_revision"]),
        cast(str | None, raw["active_revision"]),
        cast(str | None, raw["verification"]),
        cast(str | None, raw["error_code"]),
    )


def _parse_completion(value: object) -> Completion | None:
    if value is None:
        return None
    raw = _object(value, "completion")
    if set(raw) != {"attempt_id", "dispatch_generation", "result_digest"}:
        raise ReceiptFailure("invalid_state", "completion has unexpected fields", "inspect")
    attempt_id = _stored_attempt_id(raw.get("attempt_id"), "completion.attempt_id")
    digest = _string(raw.get("result_digest"), "completion.result_digest")
    assert digest is not None
    return Completion(
        attempt_id,
        _integer(raw.get("dispatch_generation"), "completion.dispatch_generation", minimum=1),
        digest,
    )


def parse_receipt(value: object) -> Receipt:
    raw = _object(value, "receipt")
    if set(raw) != {
        "version",
        "binding",
        "accepted_revision",
        "pending",
        "last_observation",
        "completion",
    }:
        raise ReceiptFailure("invalid_state", "receipt has unexpected fields", "inspect")
    version = raw.get("version")
    if type(version) is not int or version not in (1, 2):
        raise ReceiptFailure("invalid_state", "receipt version must be 1 or 2", "inspect")
    binding_raw = _object(raw.get("binding"), "binding")
    if set(binding_raw) != {"association_id", "name", "target", "config_fingerprint"}:
        raise ReceiptFailure("invalid_state", "binding has unexpected fields", "inspect")
    association_id = _string(binding_raw.get("association_id"), "binding.association_id")
    name = _string(binding_raw.get("name"), "binding.name")
    fingerprint = _string(binding_raw.get("config_fingerprint"), "binding.config_fingerprint")
    assert association_id is not None and name is not None and fingerprint is not None
    if not NAME_PATTERN.fullmatch(name):
        raise ReceiptFailure("invalid_state", "binding.name is invalid", "inspect")
    pending_raw = raw.get("pending")
    pending: Pending | None = None
    if pending_raw is not None:
        pending_object = _object(pending_raw, "pending")
        if set(pending_object) != {"intent", "dispatch_generation", "state"}:
            raise ReceiptFailure("invalid_state", "pending has unexpected fields", "inspect")
        intent_raw = _object(pending_object.get("intent"), "pending.intent")
        operation = intent_raw.get("operation", "publish") if version == 2 else "publish"
        expected_fields = (
            {"id", "expectation", "input"}
            if version == 1
            else {"id", "expectation", "input", "operation"}
            if operation == "publish"
            else {"id", "expectation", "archive_commit", "operation"}
        )
        if set(intent_raw) != expected_fields or operation not in {"publish", "restore"}:
            raise ReceiptFailure("invalid_state", "pending.intent has unexpected fields", "inspect")
        attempt_id = _stored_attempt_id(intent_raw.get("id"), "pending.intent.id")
        state = pending_object.get("state")
        if state not in {"uncertain", "retryable", "conflict"}:
            raise ReceiptFailure("invalid_state", "pending.state is invalid", "inspect")
        expectation = _parse_expectation(intent_raw.get("expectation"))
        if operation == "publish":
            intent: Intent = PublishIntent(
                attempt_id, expectation, _parse_frozen(intent_raw.get("input"))
            )
        else:
            commit = _string(intent_raw.get("archive_commit"), "pending.intent.archive_commit")
            assert commit is not None
            intent = RestoreIntent(attempt_id, expectation, commit)
        pending = Pending(
            intent,
            _integer(
                pending_object.get("dispatch_generation"),
                "pending.dispatch_generation",
                minimum=1,
            ),
            cast(PendingState, state),
        )
    return Receipt(
        version,
        Binding(
            association_id,
            name,
            _parse_target(binding_raw.get("target")),
            fingerprint,
        ),
        _optional_revision(raw.get("accepted_revision"), "accepted_revision"),
        pending,
        _parse_observation(raw.get("last_observation")),
        _parse_completion(raw.get("completion")),
    )


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReceiptFailure(
            "invalid_state", f"JSON state is unreadable at {path}: {error}", "inspect"
        ) from error


def load_receipt(receipt_dir: Path) -> Receipt:
    path = receipt_dir / "receipt.json"
    try:
        return parse_receipt(_read_json(path))
    except FileNotFoundError as error:
        raise ReceiptFailure(
            "receipt_missing", f"Receipt does not exist: {path}", "bind_receipt"
        ) from error


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: object) -> None:
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            raise
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except OSError as error:
        raise PersistenceFailure(f"Could not persist {path}: {error}") from error
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _write_receipt(receipt_dir: Path, receipt: Receipt) -> None:
    _atomic_json(receipt_dir / "receipt.json", receipt_dict(receipt))


def _ensure_receipt_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=True)
        _sync_directory(path.parent)
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReceiptFailure(
            "unsafe_receipt",
            f"Receipt must be a real directory: {path}",
            "choose_receipt",
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077 or mode & 0o700 != 0o700:
        raise ReceiptFailure(
            "unsafe_receipt",
            f"Receipt directory must have mode 0700: {path}",
            "fix_permissions",
        )


@contextlib.contextmanager
def receipt_lock(
    receipt_dir: Path, seconds: float, budget: CommandBudget | None = None
) -> Generator[None, None, None]:
    if budget is not None:
        budget.remaining()
    _ensure_receipt_dir(receipt_dir)
    lock_path = receipt_dir / "lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    lock_info = os.fstat(descriptor)
    if not stat.S_ISREG(lock_info.st_mode) or stat.S_IMODE(lock_info.st_mode) & 0o077:
        os.close(descriptor)
        raise ReceiptFailure(
            "unsafe_receipt",
            f"Receipt lock must be a private regular file: {lock_path}",
            "fix_permissions",
        )
    deadline = time.monotonic() + seconds
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if budget is not None:
                    budget.remaining()
                if time.monotonic() >= deadline:
                    raise ReceiptFailure(
                        "receipt_busy",
                        f"Receipt is locked: {receipt_dir}",
                        "retry_later",
                    ) from error
                time.sleep(min(0.05, budget.remaining()) if budget is not None else 0.05)
        if budget is not None:
            budget.remaining()
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _is_within(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        return False


def _check_disjoint(source: Path, receipt_dir: Path) -> None:
    source_real = source.resolve(strict=True)
    receipt_real = receipt_dir.resolve(strict=False)
    if _is_within(source_real, receipt_real) or _is_within(receipt_real, source_real):
        raise ReceiptFailure(
            "overlapping_paths",
            "The source and receipt bundle must be disjoint",
            "choose_receipt",
        )


@dataclass
class CopyBudget:
    limits: Limits
    deadline: float
    command_budget: CommandBudget | None = None
    files: int = 0
    bytes: int = 0

    def check(self) -> None:
        if self.command_budget is not None:
            self.command_budget.remaining()
        if time.monotonic() > self.deadline:
            raise ReceiptFailure(
                "copy_timeout",
                "The immutable copy exceeded its time budget",
                "reduce_input",
            )

    def add(self, size: int) -> None:
        self.check()
        self.files += 1
        self.bytes += size
        if self.files > self.limits.max_files or self.bytes > self.limits.max_bytes:
            raise ReceiptFailure(
                "copy_limit",
                "The immutable copy exceeded its file or byte budget",
                "reduce_input",
            )


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


def _hash_record(digest: _Digest, kind: bytes, relative: str, data: bytes = b"") -> None:
    encoded = relative.encode("utf-8")
    digest.update(kind)
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def _copy_regular(
    source: Path,
    destination: Path,
    relative: str,
    digest: _Digest,
    budget: CopyBudget,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReceiptFailure(
                "unsafe_input", f"Input is not a regular file: {source}", "fix_input"
            )
        budget.add(before.st_size)
        output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            file_digest = hashlib.sha256()
            with os.fdopen(output, "wb", closefd=True) as target:
                while True:
                    budget.check()
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    file_digest.update(chunk)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(output)
            raise
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
        )
        if identity_before != identity_after or after.st_size != destination.stat().st_size:
            raise ReceiptFailure(
                "input_changed", f"Input changed while being copied: {source}", "retry"
            )
        os.chmod(destination, 0o400)
        _hash_record(digest, b"F", relative, file_digest.digest())
    finally:
        os.close(descriptor)


def _copy_directory(
    source: Path,
    destination: Path,
    relative: str,
    digest: _Digest,
    budget: CopyBudget,
) -> None:
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise ReceiptFailure("unsafe_input", f"Input is not a directory: {source}", "fix_input")
    destination.mkdir(mode=0o700)
    _hash_record(digest, b"D", relative)
    try:
        entries = sorted(os.scandir(source), key=lambda entry: os.fsencode(entry.name))
    except OSError as error:
        raise ReceiptFailure(
            "copy_failed", f"Could not read input directory {source}: {error}", "retry"
        ) from error
    for entry in entries:
        budget.check()
        child_relative = entry.name if not relative else f"{relative}/{entry.name}"
        child_source = Path(entry.path)
        child_destination = destination / entry.name
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise ReceiptFailure(
                "unsafe_input", f"Input contains a symlink: {child_source}", "fix_input"
            )
        if stat.S_ISDIR(info.st_mode):
            _copy_directory(child_source, child_destination, child_relative, digest, budget)
        elif stat.S_ISREG(info.st_mode):
            _copy_regular(child_source, child_destination, child_relative, digest, budget)
        else:
            raise ReceiptFailure(
                "unsafe_input",
                f"Input contains a special file: {child_source}",
                "fix_input",
            )
    after = source.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_mode) != (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
        after.st_mode,
    ):
        raise ReceiptFailure(
            "input_changed",
            f"Input directory changed while being copied: {source}",
            "retry",
        )
    os.chmod(destination, 0o500)
    _sync_directory(destination)


def freeze_input(
    source: Path,
    receipt_dir: Path,
    attempt_id: str,
    limits: Limits,
    command_budget: CommandBudget | None = None,
) -> FrozenInput:
    if command_budget is not None:
        command_budget.remaining()
    try:
        source_info = source.lstat()
    except OSError as error:
        raise ReceiptFailure(
            "input_unavailable", f"Input is unavailable: {error}", "fix_input"
        ) from error
    if stat.S_ISLNK(source_info.st_mode):
        raise ReceiptFailure("unsafe_input", f"Input cannot be a symlink: {source}", "fix_input")
    if not stat.S_ISREG(source_info.st_mode) and not stat.S_ISDIR(source_info.st_mode):
        raise ReceiptFailure(
            "unsafe_input",
            f"Input must be a regular file or directory: {source}",
            "fix_input",
        )
    _check_disjoint(source, receipt_dir)
    final_attempt = receipt_dir / f"attempt-{attempt_id}"
    if final_attempt.exists():
        raise ReceiptFailure(
            "attempt_exists",
            f"Attempt directory already exists: {final_attempt}",
            "inspect",
        )
    temporary = receipt_dir / f".attempt-{attempt_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    input_path = temporary / "input"
    digest = hashlib.sha256()
    budget = CopyBudget(
        limits,
        time.monotonic() + limits.copy_seconds,
        command_budget,
    )
    try:
        if stat.S_ISREG(source_info.st_mode):
            _copy_regular(source, input_path, "", digest, budget)
            kind: InputKind = "file"
        else:
            _copy_directory(source, input_path, "", digest, budget)
            kind = "directory"
        budget.check()
        _sync_directory(temporary)
        os.replace(temporary, final_attempt)
        _sync_directory(receipt_dir)
        budget.check()
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return FrozenInput(
        str(final_attempt.relative_to(receipt_dir) / "input"),
        kind,
        digest.hexdigest(),
        budget.files,
        budget.bytes,
    )


def _scan_snapshot(
    path: Path, kind: InputKind, limits: Limits, command_budget: CommandBudget | None = None
) -> FrozenInput:
    digest = hashlib.sha256()
    budget = CopyBudget(
        limits,
        time.monotonic() + limits.copy_seconds,
        command_budget,
    )

    def scan(current: Path, relative: str) -> None:
        budget.check()
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ReceiptFailure(
                "snapshot_changed", f"Snapshot contains a symlink: {current}", "inspect"
            )
        if stat.S_ISREG(info.st_mode):
            budget.add(info.st_size)
            file_digest = hashlib.sha256()
            descriptor = os.open(current, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                while True:
                    budget.check()
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    file_digest.update(chunk)
            finally:
                os.close(descriptor)
            _hash_record(digest, b"F", relative, file_digest.digest())
            return
        if not stat.S_ISDIR(info.st_mode):
            raise ReceiptFailure(
                "snapshot_changed",
                f"Snapshot contains a special file: {current}",
                "inspect",
            )
        _hash_record(digest, b"D", relative)
        for entry in sorted(os.scandir(current), key=lambda item: os.fsencode(item.name)):
            child_relative = entry.name if not relative else f"{relative}/{entry.name}"
            scan(Path(entry.path), child_relative)

    scan(path, "")
    actual_kind: InputKind = "file" if path.is_file() else "directory"
    if actual_kind != kind:
        raise ReceiptFailure("snapshot_changed", "Snapshot kind changed", "inspect")
    return FrozenInput("", kind, digest.hexdigest(), budget.files, budget.bytes)


def verify_snapshot(
    receipt_dir: Path,
    frozen: FrozenInput,
    limits: Limits,
    command_budget: CommandBudget | None = None,
) -> Path:
    if command_budget is not None:
        command_budget.remaining()
    path = receipt_dir / frozen.path
    try:
        resolved = path.resolve(strict=True)
        receipt_real = receipt_dir.resolve(strict=True)
    except OSError as error:
        raise ReceiptFailure(
            "snapshot_missing", f"Snapshot is unavailable: {error}", "inspect"
        ) from error
    if not _is_within(resolved, receipt_real):
        raise ReceiptFailure("snapshot_escape", "Snapshot escapes the receipt bundle", "inspect")
    scanned = _scan_snapshot(path, frozen.kind, limits, command_budget)
    if (
        scanned.digest != frozen.digest
        or scanned.file_count != frozen.file_count
        or scanned.byte_count != frozen.byte_count
    ):
        raise ReceiptFailure(
            "snapshot_changed",
            "Snapshot bytes differ from the pending intent",
            "inspect",
        )
    return path


def _executor_command(
    config: ClientConfig,
    operation: Literal["publish", "restore", "status"],
    name: str,
    source: Path | None = None,
    expected_revision: str | None = None,
    request_id: str | None = None,
    archive_commit: str | None = None,
) -> list[str]:
    executor = config.executor
    command = list(executor.command)
    if executor.kind == "local":
        assert executor.publisher_config is not None
        command.extend(["--config", executor.publisher_config, "--json"])
    else:
        command.extend(["--host", executor.host, "--target", config.target.base_url])
        if executor.remote_executable is not None:
            command.extend(["--remote-executable", executor.remote_executable])
        if executor.remote_config is not None:
            command.extend(["--remote-config", executor.remote_config])
        if executor.incoming_root is not None:
            command.extend(["--incoming-root", executor.incoming_root])
        if executor.connect_timeout is not None:
            command.extend(["--connect-timeout", str(executor.connect_timeout)])
    command.extend([operation, "--name", name])
    if operation in {"publish", "restore"}:
        assert request_id is not None
        if operation == "publish":
            assert source is not None
            command.extend(["--source", str(source)])
        else:
            assert archive_commit is not None
            command.extend(["--archive-commit", archive_commit])
        if executor.kind == "local":
            command.extend(["--target", config.target.base_url])
        if expected_revision is not None:
            command.extend(["--expected-revision", expected_revision])
        command.extend(["--request-id", request_id])
    return command


def _live_group_members(pgid: int) -> bool | None:
    try:
        observed = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,stat="],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if observed.returncode != 0 or len(observed.stdout) > 1024 * 1024:
        return None
    for line in observed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            return None
        try:
            group = int(fields[1])
        except ValueError:
            return None
        if group == pgid and not fields[2].startswith(("Z", "X")):
            return True
    return False


def _stop_uninspectable_group(process: subprocess.Popen[bytes]) -> None:
    for group_signal in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, group_signal)
        if group_signal == signal.SIGTERM:
            time.sleep(0.2)
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.2)


def _stop_process_group(process: subprocess.Popen[bytes]) -> bool | None:
    if process.returncode is not None:
        return False if _live_group_members(process.pid) is False else None
    live = _live_group_members(process.pid)
    if live is None:
        _stop_uninspectable_group(process)
        return None
    if live is False:
        process.wait(timeout=1)
        return False
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        live = _live_group_members(process.pid)
        if live is not False:
            _stop_uninspectable_group(process)
            return None
        process.wait(timeout=1)
        return False
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        live = _live_group_members(process.pid)
        if live is None:
            _stop_uninspectable_group(process)
            return None
        if live is False:
            process.wait(timeout=1)
            return True
        time.sleep(0.01)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        live = _live_group_members(process.pid)
        if live is not False:
            _stop_uninspectable_group(process)
            return None
        process.wait(timeout=1)
        return True
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        live = _live_group_members(process.pid)
        if live is None:
            _stop_uninspectable_group(process)
            return None
        if live is False:
            process.wait(timeout=1)
            return True
        time.sleep(0.01)
    _stop_uninspectable_group(process)
    return None


def _run_process(command: Sequence[str], seconds: float, output_bytes: int) -> ProcessResult:
    cancelled = False

    def cancel(_signal: int, _frame: object) -> None:
        nonlocal cancelled
        cancelled = True

    previous_handler = signal.signal(signal.SIGTERM, cancel)
    try:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            return ProcessResult(
                127,
                False,
                False,
                b"",
                str(error).encode("utf-8", "replace"),
                cancelled=cancelled,
            )
        assert process.stdout is not None and process.stderr is not None
        streams = (process.stdout, process.stderr)
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno()
        selector = selectors.DefaultSelector()
        buffers = {stream.fileno(): bytearray() for stream in streams}
        deadline = time.monotonic() + seconds
        timed_out = False
        output_limited = False
        try:
            for stream in streams:
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() and not cancelled:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(timeout=min(remaining_time, 0.1)):
                    descriptor = key.fd
                    buffer = buffers[descriptor]
                    remaining_bytes = output_bytes - len(buffer)
                    chunk = os.read(descriptor, min(64 * 1024, remaining_bytes + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer.extend(chunk[:remaining_bytes])
                    if len(chunk) > remaining_bytes:
                        output_limited = True
                        break
                if timed_out or output_limited:
                    break
            while not timed_out and not output_limited and not cancelled:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    timed_out = True
                else:
                    if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
                        exited = os.waitid(
                            os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                        )
                        if exited is not None:
                            break
                        time.sleep(min(remaining_time, 0.01))
                    else:
                        try:
                            process.wait(timeout=min(remaining_time, 0.1))
                            break
                        except subprocess.TimeoutExpired:
                            continue
        finally:
            try:
                group_stopped = _stop_process_group(process)
                return_code = process.returncode if process.returncode is not None else -1
            finally:
                selector.close()
                for stream in streams:
                    stream.close()
        return ProcessResult(
            return_code,
            timed_out,
            output_limited,
            bytes(buffers[stdout_fd]),
            bytes(buffers[stderr_fd]),
            group_stopped,
            cancelled,
        )

    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def _saved_result(
    pending: Pending,
    process: ProcessResult,
) -> SavedResult:
    stdout = process.stdout.decode("utf-8", "replace")
    stderr = process.stderr.decode("utf-8", "replace")
    payload: Mapping[str, object] | None = None
    if not process.output_limited:
        try:
            decoded: object = json.loads(stdout)
            if isinstance(decoded, dict):
                payload = _object(cast(object, decoded), "publisher result")
        except (json.JSONDecodeError, UnicodeError):
            pass
    digest_value = {
        "attempt_id": pending.intent.id,
        "dispatch_generation": pending.dispatch_generation,
        "exit_code": process.exit_code,
        "timed_out": process.timed_out,
        "output_limited": process.output_limited,
        "group_stopped": process.group_stopped,
        "cancelled": process.cancelled,
        "stdout": stdout,
        "stderr": stderr,
    }
    return SavedResult(
        pending.intent.id,
        pending.dispatch_generation,
        _canonical_digest(digest_value),
        process.exit_code,
        process.timed_out,
        process.output_limited,
        stdout,
        stderr,
        payload,
        process.group_stopped,
        process.cancelled,
    )


def saved_result_dict(result: SavedResult) -> dict[str, object]:
    return {
        "version": 1,
        "attempt_id": result.attempt_id,
        "dispatch_generation": result.dispatch_generation,
        "result_digest": result.result_digest,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "output_limited": result.output_limited,
        "group_stopped": result.group_stopped,
        "cancelled": result.cancelled,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "payload": result.payload,
    }


def parse_saved_result(value: object) -> SavedResult:
    raw = _object(value, "saved result")
    if (
        set(raw)
        != {
            "version",
            "attempt_id",
            "dispatch_generation",
            "result_digest",
            "exit_code",
            "timed_out",
            "output_limited",
            "group_stopped",
            "cancelled",
            "stdout",
            "stderr",
            "payload",
        }
        or raw.get("version") != 1
    ):
        raise ReceiptFailure("invalid_state", "saved result has an invalid shape", "inspect")
    attempt_id = _stored_attempt_id(raw.get("attempt_id"), "saved result attempt_id")
    digest = _string(raw.get("result_digest"), "saved result digest")
    stdout = raw.get("stdout")
    stderr = raw.get("stderr")
    if (
        not isinstance(stdout, str)
        or not isinstance(stderr, str)
        or not isinstance(raw.get("timed_out"), bool)
        or not isinstance(raw.get("output_limited"), bool)
        or (raw.get("group_stopped") is not None and not isinstance(raw.get("group_stopped"), bool))
        or not isinstance(raw.get("cancelled"), bool)
    ):
        raise ReceiptFailure("invalid_state", "saved result fields are invalid", "inspect")
    payload_raw = raw.get("payload")
    payload = None if payload_raw is None else _object(payload_raw, "saved result payload")
    exit_code = raw.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ReceiptFailure("invalid_state", "saved result exit_code is invalid", "inspect")
    assert digest is not None
    return SavedResult(
        attempt_id,
        _integer(raw.get("dispatch_generation"), "saved result generation", minimum=1),
        digest,
        exit_code,
        cast(bool, raw.get("timed_out")),
        cast(bool, raw.get("output_limited")),
        stdout,
        stderr,
        payload,
        cast(bool | None, raw.get("group_stopped")),
        cast(bool, raw.get("cancelled")),
    )


def _result_path(receipt_dir: Path, attempt_id: str) -> Path:
    return receipt_dir / f"attempt-{attempt_id}" / "result.json"


def _save_result(receipt_dir: Path, result: SavedResult) -> Path:
    path = _result_path(receipt_dir, result.attempt_id)
    _atomic_json(path, saved_result_dict(result))
    return path


def _load_result(path: Path) -> SavedResult:
    return parse_saved_result(_read_json(path))


def _payload_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _verification_result(payload: Mapping[str, object]) -> str | None:
    verification = payload.get("verification")
    if not isinstance(verification, dict):
        return None
    result = _object(cast(object, verification), "verification").get("result")
    return result if isinstance(result, str) else None


def _error_code(payload: Mapping[str, object]) -> str | None:
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = _object(cast(object, error), "publisher error").get("code")
    return code if isinstance(code, str) else None


def observation_from(payload: Mapping[str, object]) -> Observation:
    return Observation(
        _payload_string(payload, "operation") or "unknown",
        _payload_string(payload, "outcome") or "unknown",
        _payload_string(payload, "name"),
        _payload_string(payload, "target"),
        _payload_string(payload, "url"),
        _payload_string(payload, "request_id"),
        _payload_string(payload, "expected_revision"),
        _payload_string(payload, "requested_revision"),
        _payload_string(payload, "active_revision"),
        _verification_result(payload),
        _error_code(payload),
    )


def _completion(result: SavedResult) -> Completion:
    return Completion(result.attempt_id, result.dispatch_generation, result.result_digest)


def reduce_result(receipt: Receipt, result: SavedResult) -> Classification:
    pending = receipt.pending
    if pending is None:
        return Classification(receipt, "rejected", "No pending attempt owns this result")
    if (
        result.attempt_id != pending.intent.id
        or result.dispatch_generation != pending.dispatch_generation
    ):
        return Classification(
            receipt,
            "rejected",
            "The saved result belongs to another attempt or generation",
        )
    payload = result.payload
    if payload is None:
        return Classification(
            receipt, "uncertain", "Publisher output is missing, oversized, or malformed"
        )

    expected = pending.intent.expectation.revision
    schema = payload.get("schema_version")
    operation = _payload_string(payload, "operation")
    request_id = _payload_string(payload, "request_id")
    target = _payload_string(payload, "target")
    name = _payload_string(payload, "name")
    reported_expected = payload.get("expected_revision")
    correlated = (
        schema == 1
        and operation == ("publish" if isinstance(pending.intent, PublishIntent) else "restore")
        and request_id == pending.intent.id
        and target == receipt.binding.target.base_url
        and name == receipt.binding.name
        and reported_expected == expected
    )
    if not correlated:
        return Classification(receipt, "rejected", "Publisher result failed receipt correlation")

    error = payload.get("error")
    valid_error = "error" in payload and error is None
    if isinstance(error, dict):
        error_raw = _object(cast(object, error), "publisher error")
        action = error_raw.get("next_action")
        action_raw: Mapping[str, object] = (
            _object(cast(object, action), "publisher next action")
            if isinstance(action, dict)
            else {}
        )
        required_inputs = action_raw.get("required_inputs")
        valid_error = (
            all(isinstance(error_raw.get(key), str) for key in ("code", "phase", "message"))
            and isinstance(action_raw.get("kind"), str)
            and isinstance(required_inputs, list)
            and all(isinstance(item, str) for item in cast(list[object], required_inputs))
        )
    if not valid_error:
        return Classification(receipt, "uncertain", "Publisher error envelope is malformed")

    observation = observation_from(payload)
    requested = observation.requested_revision
    active = observation.active_revision
    verification = observation.verification
    effects = payload.get("effects")
    effects_raw: Mapping[str, object] = (
        _object(cast(object, effects), "publisher effects") if isinstance(effects, dict) else {}
    )
    activated = effects_raw.get("activated") is True
    effects_false = (
        isinstance(effects, dict)
        and effects_raw.get("activated") is False
        and effects_raw.get("archive_advanced") is False
    )
    outcome = observation.outcome
    process_finished = (
        not result.timed_out
        and not result.output_limited
        and result.group_stopped is False
        and not result.cancelled
    )
    verification_payload = payload.get("verification")
    same_revision_success = (
        process_finished
        and result.exit_code == 0
        and outcome in {"published", "unchanged"}
        and "error" in payload
        and payload["error"] is None
        and requested is not None
        and requested == active
        and verification == "passed"
        and isinstance(verification_payload, dict)
        and _object(cast(object, verification_payload), "verification").get("revision") == requested
    )
    completion = _completion(result)
    qualifies_activation = requested is not None and activated and active == requested

    if qualifies_activation:
        accepted = dataclasses.replace(
            receipt,
            accepted_revision=requested,
            last_observation=observation,
            completion=completion,
        )
        if same_revision_success:
            return Classification(
                dataclasses.replace(accepted, pending=None),
                "completed",
                "The publisher activated and verified the requested revision",
            )
        retryable = dataclasses.replace(pending, state="retryable")
        return Classification(
            dataclasses.replace(accepted, pending=retryable),
            "delivery_failed",
            "Activation was proven, but the publisher operation did not complete successfully",
        )

    if (
        outcome == "error"
        and effects_false
        and process_finished
        and observation.error_code != "revision_conflict"
    ):
        retryable = dataclasses.replace(pending, state="retryable")
        return Classification(
            dataclasses.replace(
                receipt,
                pending=retryable,
                last_observation=observation,
                completion=completion,
            ),
            "retryable",
            "The correlated result proves that publication made no host changes",
        )

    if outcome == "unchanged" and same_revision_success:
        return Classification(
            dataclasses.replace(
                receipt,
                accepted_revision=requested,
                pending=None,
                last_observation=observation,
                completion=completion,
            ),
            "completed",
            "The publisher verified the identical active revision",
        )
    if observation.error_code == "revision_conflict" and process_finished:
        conflict = dataclasses.replace(pending, state="conflict")
        return Classification(
            dataclasses.replace(
                receipt,
                pending=conflict,
                last_observation=observation,
                completion=completion,
            ),
            "conflict",
            "The host rejected the original expectation",
        )
    return Classification(
        dataclasses.replace(receipt, last_observation=observation, completion=completion),
        "uncertain",
        "The correlated result does not prove activation or verified identity",
    )


def _validate_binding(receipt: Receipt, config: ClientConfig) -> None:
    if (
        receipt.binding.target != config.target
        or receipt.binding.config_fingerprint != config.fingerprint
    ):
        raise ReceiptFailure(
            "target_drift",
            "The configured execution target differs from the receipt binding",
            "use_original_config_or_new_association",
        )


def _status_payload(
    receipt: Receipt, config: ClientConfig, budget: CommandBudget | None = None
) -> tuple[ProcessResult, Mapping[str, object] | None]:
    process = _run_process(
        _executor_command(config, "status", receipt.binding.name),
        budget.remaining(config.limits.command_seconds)
        if budget is not None
        else config.limits.command_seconds,
        config.limits.output_bytes,
    )
    if (
        process.timed_out
        or process.output_limited
        or process.group_stopped is not False
        or process.cancelled
    ):
        return process, None
    stdout = process.stdout.decode("utf-8", "replace")
    try:
        payload: object = json.loads(stdout)
    except json.JSONDecodeError:
        return process, None
    if not isinstance(payload, dict):
        return process, None
    typed = _object(cast(object, payload), "status result")
    if (
        typed.get("schema_version") != 1
        or _payload_string(typed, "operation") != "status"
        or _payload_string(typed, "name") != receipt.binding.name
        or _payload_string(typed, "target") != receipt.binding.target.base_url
    ):
        return process, None
    return process, typed


def _cleanup_completed_attempt(receipt_dir: Path, classification: Classification) -> Path | None:
    completion = classification.receipt.completion
    if completion is None:
        return None
    attempt = _result_path(receipt_dir, completion.attempt_id).parent
    try:
        if classification.receipt.pending is None:
            for root, directories, files in os.walk(attempt):
                for name in directories:
                    os.chmod(Path(root) / name, 0o700)
                for name in files:
                    os.chmod(Path(root) / name, 0o600)
            os.chmod(attempt, 0o700)
            shutil.rmtree(attempt)
        else:
            with contextlib.suppress(FileNotFoundError):
                (attempt / "result.json").unlink()
            _sync_directory(attempt)
        _sync_directory(receipt_dir)
    except OSError:
        return attempt
    return None


def _recover_saved_result(receipt_dir: Path, receipt: Receipt) -> Recovery:
    completion = receipt.completion
    if completion is not None:
        completion_path = _result_path(receipt_dir, completion.attempt_id)
        if completion_path.exists():
            saved = _load_result(completion_path)
            if (
                saved.attempt_id == completion.attempt_id
                and saved.dispatch_generation == completion.dispatch_generation
                and saved.result_digest == completion.result_digest
            ):
                _sync_directory(receipt_dir)
                recovery_kind = "delivery_failed" if receipt.pending is not None else "completed"
                classification = Classification(
                    receipt,
                    cast(
                        Literal[
                            "completed",
                            "delivery_failed",
                            "conflict",
                            "retryable",
                            "uncertain",
                            "rejected",
                        ],
                        recovery_kind,
                    ),
                    "Recovered a receipt that was renamed before its directory sync completed",
                )
                cleanup_pending = _cleanup_completed_attempt(receipt_dir, classification)
                return Recovery(receipt, classification, True, cleanup_pending)
    pending = receipt.pending
    if pending is None:
        return Recovery(receipt, None, False, None)
    result_path = _result_path(receipt_dir, pending.intent.id)
    if not result_path.exists():
        return Recovery(receipt, None, False, None)
    saved = _load_result(result_path)
    classification = reduce_result(receipt, saved)
    if classification.kind == "rejected":
        return Recovery(receipt, classification, False, None)
    _write_receipt(receipt_dir, classification.receipt)
    cleanup_pending = _cleanup_completed_attempt(receipt_dir, classification)
    return Recovery(classification.receipt, classification, True, cleanup_pending)


def _handoff(
    operation: str,
    receipt_dir: Path | None,
    receipt: Receipt | None,
    *,
    outcome: str,
    message: str,
    receipt_persisted: bool | None,
    publisher_calls: int,
    error_code: str | None = None,
    next_action: str | None = None,
    source: Path | None = None,
    dispatch: DispatchResult | None = None,
) -> dict[str, object]:
    visible_pending = receipt.pending if receipt is not None else None
    intent_pending = dispatch.pending if dispatch is not None else visible_pending
    result_observation = (
        observation_from(dispatch.saved_result.payload)
        if dispatch is not None and dispatch.saved_result.payload is not None
        else None
    )
    observation = result_observation or (receipt.last_observation if receipt is not None else None)
    binding = receipt.binding if receipt is not None else None
    return {
        "schema_version": 1,
        "operation": operation,
        "outcome": outcome,
        "message": message,
        "receipt": str(receipt_dir) if receipt_dir is not None else None,
        "source": str(source) if source is not None else None,
        "name": binding.name if binding else None,
        "target": binding.target.base_url if binding else None,
        "url": observation.url if observation else None,
        "attempt_id": intent_pending.intent.id
        if intent_pending
        else (receipt.completion.attempt_id if receipt and receipt.completion else None),
        "original_expectation": intent_pending.intent.expectation.revision
        if intent_pending
        else None,
        "requested_revision": observation.requested_revision if observation else None,
        "active_revision": observation.active_revision if observation else None,
        "accepted_revision": receipt.accepted_revision if receipt else None,
        "pending_state": visible_pending.state if visible_pending else None,
        "snapshot": str(receipt_dir / intent_pending.intent.input.path)
        if receipt_dir is not None
        and intent_pending
        and isinstance(intent_pending.intent, PublishIntent)
        else None,
        "archive_commit": intent_pending.intent.archive_commit
        if intent_pending and isinstance(intent_pending.intent, RestoreIntent)
        else None,
        "result": str(dispatch.result_path) if dispatch is not None else None,
        "cleanup_pending": str(dispatch.cleanup_pending)
        if dispatch is not None and dispatch.cleanup_pending is not None
        else None,
        "receipt_persisted": receipt_persisted,
        "publisher_calls": publisher_calls,
        "verification": observation.verification if observation else None,
        "effects": dispatch.saved_result.payload.get("effects")
        if dispatch is not None and dispatch.saved_result.payload is not None
        else None,
        "publisher": None
        if dispatch is None
        else {
            "outcome": result_observation.outcome if result_observation else None,
            "error_code": result_observation.error_code if result_observation else None,
            "exit_code": dispatch.saved_result.exit_code,
            "timed_out": dispatch.saved_result.timed_out,
            "output_limited": dispatch.saved_result.output_limited,
            "group_stopped": dispatch.saved_result.group_stopped,
            "cancelled": dispatch.saved_result.cancelled,
            "effects": dispatch.saved_result.payload.get("effects")
            if dispatch.saved_result.payload is not None
            else None,
        },
        "error": None
        if error_code is None
        else {"code": error_code, "message": message, "next_action": next_action},
    }


def _emit(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _dispatch(
    receipt_dir: Path,
    receipt: Receipt,
    config: ClientConfig,
    budget: CommandBudget | None = None,
) -> DispatchResult:
    pending = receipt.pending
    assert pending is not None
    source = (
        verify_snapshot(receipt_dir, pending.intent.input, config.limits, budget)
        if isinstance(pending.intent, PublishIntent)
        else None
    )
    command = _executor_command(
        config,
        "publish" if isinstance(pending.intent, PublishIntent) else "restore",
        receipt.binding.name,
        source,
        pending.intent.expectation.revision,
        pending.intent.id,
        pending.intent.archive_commit if isinstance(pending.intent, RestoreIntent) else None,
    )
    process = _run_process(
        command,
        budget.remaining(config.limits.command_seconds)
        if budget is not None
        else config.limits.command_seconds,
        config.limits.output_bytes,
    )
    saved = _saved_result(pending, process)
    result_path = _result_path(receipt_dir, saved.attempt_id)
    classification = reduce_result(receipt, saved)
    try:
        _save_result(receipt_dir, saved)
    except PersistenceFailure as error:
        return DispatchResult(classification, receipt, pending, saved, result_path, None, error)
    if classification.kind == "rejected":
        return DispatchResult(classification, receipt, pending, saved, result_path, None, None)
    try:
        _write_receipt(receipt_dir, classification.receipt)
    except PersistenceFailure as error:
        try:
            visible = load_receipt(receipt_dir)
        except ReceiptFailure:
            visible = receipt
        return DispatchResult(classification, visible, pending, saved, result_path, None, error)
    cleanup_pending = _cleanup_completed_attempt(receipt_dir, classification)
    return DispatchResult(
        classification,
        classification.receipt,
        pending,
        saved,
        result_path,
        cleanup_pending,
        None,
    )


def _finish_dispatch(
    operation: Literal["publish", "restore", "retry"],
    receipt_dir: Path,
    source: Path | None,
    dispatch: DispatchResult,
    publisher_calls: int,
) -> int:
    if dispatch.persistence_error is not None:
        error = dispatch.persistence_error
        _emit(
            _handoff(
                operation,
                receipt_dir,
                dispatch.visible_receipt,
                outcome="error",
                message=error.message,
                receipt_persisted=False,
                publisher_calls=publisher_calls,
                error_code=error.code,
                next_action=error.next_action,
                source=source,
                dispatch=dispatch,
            )
        )
        return 1

    classification = dispatch.classification
    success = classification.kind == "completed"
    result_observation = (
        observation_from(dispatch.saved_result.payload)
        if dispatch.saved_result.payload is not None
        else None
    )
    if dispatch.saved_result.cancelled:
        error_code = "interrupted"
    elif dispatch.saved_result.timed_out:
        error_code = "publisher_timeout"
    elif dispatch.saved_result.output_limited:
        error_code = "publisher_output_limit"
    elif dispatch.saved_result.group_stopped is None:
        error_code = "publisher_process_group_unknown"
    elif dispatch.saved_result.group_stopped:
        error_code = "publisher_process_group"
    elif result_observation is not None and result_observation.error_code is not None:
        error_code = result_observation.error_code
    else:
        error_code = classification.kind
    _emit(
        _handoff(
            operation,
            receipt_dir,
            dispatch.visible_receipt,
            outcome=classification.kind,
            message=classification.message,
            receipt_persisted=True,
            publisher_calls=publisher_calls,
            error_code=None if success else error_code,
            next_action=None if success else "retry_or_review",
            source=source,
            dispatch=dispatch,
        )
    )
    return 0 if success else 1


def _new_pending(
    source: Path,
    receipt_dir: Path,
    receipt: Receipt,
    expectation: Expectation,
    limits: Limits,
    budget: CommandBudget | None = None,
) -> Receipt:
    attempt_id = uuid.uuid4().hex
    frozen = freeze_input(source, receipt_dir, attempt_id, limits, budget)
    pending = Pending(PublishIntent(attempt_id, expectation, frozen), 1, "uncertain")
    prepared = dataclasses.replace(receipt, pending=pending, completion=None)
    _write_receipt(receipt_dir, prepared)
    return prepared


def _reviewed_replacement(
    receipt: Receipt,
    reviewed_revision: str | None,
    replaces_attempt: str | None,
) -> Expectation | None:
    if reviewed_revision is None and replaces_attempt is None:
        return None
    if reviewed_revision is None or replaces_attempt is None:
        raise ReceiptFailure(
            "invalid_review",
            "--reviewed-revision and --replaces-attempt must be supplied together",
            "review_conflict",
        )
    pending = receipt.pending
    if pending is None or pending.state != "conflict" or pending.intent.id != replaces_attempt:
        raise ReceiptFailure(
            "stale_review",
            "The reviewed replacement does not match the stored conflict attempt",
            "inspect_receipt",
        )
    observation = receipt.last_observation
    if observation is None or observation.active_revision != reviewed_revision:
        raise ReceiptFailure(
            "stale_review",
            "The reviewed revision does not match the stored conflict observation",
            "inspect_receipt",
        )
    return Expectation("reviewed", reviewed_revision, replaces_attempt)


def _publish(arguments: argparse.Namespace, config_path: Path, started_at: float) -> int:
    source = Path(arguments.source).expanduser().absolute()
    receipt_dir = (
        Path(arguments.receipt).expanduser().absolute()
        if arguments.receipt
        else Path(f"{source}.publish")
    )
    try:
        _check_disjoint(source, receipt_dir)
    except OSError as error:
        raise ReceiptFailure(
            "input_unavailable", f"Input is unavailable: {error}", "fix_input"
        ) from error
    if arguments.local_only:
        try:
            info = source.lstat()
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
            ):
                raise ReceiptFailure(
                    "unsafe_input",
                    f"Input must be a regular file or directory: {source}",
                    "fix_input",
                )
        except OSError as error:
            failure = ReceiptFailure("input_unavailable", str(error), "fix_input")
            _emit(
                _handoff(
                    "publish",
                    receipt_dir,
                    None,
                    outcome="error",
                    message=failure.message,
                    receipt_persisted=False,
                    publisher_calls=0,
                    error_code=failure.code,
                    next_action=failure.next_action,
                    source=source,
                )
            )
            return 1
        _emit(
            _handoff(
                "publish",
                receipt_dir,
                None,
                outcome="local_only",
                message="No publisher or receipt operation ran",
                receipt_persisted=False,
                publisher_calls=0,
                source=source,
            )
        )
        return 0

    config = load_config(config_path, getattr(arguments, "command_seconds", None))
    budget = CommandBudget(started_at + config.limits.command_seconds)
    with receipt_lock(receipt_dir, config.limits.lock_seconds, budget):
        receipt_path = receipt_dir / "receipt.json"
        creating = arguments.new is not None or arguments.adopt is not None
        if creating and receipt_path.exists():
            raise ReceiptFailure(
                "receipt_exists",
                f"Receipt already exists: {receipt_path}",
                "use_existing_or_choose_receipt",
            )
        if not creating:
            receipt = load_receipt(receipt_dir)
            _validate_binding(receipt, config)
            recovery = _recover_saved_result(receipt_dir, receipt)
            receipt = recovery.receipt
            if receipt.pending is not None:
                reviewed = _reviewed_replacement(
                    receipt, arguments.reviewed_revision, arguments.replaces_attempt
                )
                if reviewed is None:
                    raise ReceiptFailure(
                        "pending_attempt",
                        f"Receipt has unresolved attempt {receipt.pending.intent.id}",
                        "retry_or_review",
                    )
                expectation = reviewed
            else:
                if (
                    arguments.reviewed_revision is not None
                    or arguments.replaces_attempt is not None
                ):
                    raise ReceiptFailure(
                        "invalid_review",
                        "No stored conflict can be replaced",
                        "publish",
                    )
                expectation = Expectation("accepted", receipt.accepted_revision, None)
        else:
            name = arguments.new or arguments.adopt
            assert name is not None
            receipt = Receipt(
                1,
                Binding(uuid.uuid4().hex, name, config.target, config.fingerprint),
                None,
                None,
                None,
                None,
            )
            expectation = Expectation("accepted", None, None)
            if arguments.adopt is not None:
                if arguments.reviewed_revision is None or arguments.replaces_attempt is not None:
                    raise ReceiptFailure(
                        "invalid_adoption",
                        "--adopt requires --reviewed-revision and accepts no --replaces-attempt",
                        "review_status",
                    )
                process, payload = _status_payload(receipt, config, budget)
                if process.exit_code != 0 or payload is None:
                    raise ReceiptFailure(
                        "adoption_observation_failed",
                        "Could not obtain a correlated status for adoption",
                        "inspect_target",
                    )
                observation = observation_from(payload)
                if observation.active_revision != arguments.reviewed_revision:
                    raise ReceiptFailure(
                        "stale_review",
                        "The reviewed revision does not match current status",
                        "review_status",
                    )
                receipt = dataclasses.replace(receipt, last_observation=observation)
                expectation = Expectation("reviewed", arguments.reviewed_revision, None)

        prepared = _new_pending(source, receipt_dir, receipt, expectation, config.limits, budget)
        return _finish_dispatch(
            "publish",
            receipt_dir,
            source,
            _dispatch(receipt_dir, prepared, config, budget),
            1,
        )


def _restore(arguments: argparse.Namespace, config_path: Path, started_at: float) -> int:
    config = load_config(config_path, getattr(arguments, "command_seconds", None))
    budget = CommandBudget(started_at + config.limits.command_seconds)
    receipt_dir = Path(arguments.receipt).expanduser().absolute()
    with receipt_lock(receipt_dir, config.limits.lock_seconds, budget):
        receipt = load_receipt(receipt_dir)
        _validate_binding(receipt, config)
        receipt = _recover_saved_result(receipt_dir, receipt).receipt
        if receipt.pending is not None:
            reviewed = _reviewed_replacement(
                receipt, arguments.reviewed_revision, arguments.replaces_attempt
            )
            if reviewed is None:
                raise ReceiptFailure(
                    "pending_attempt",
                    f"Receipt has unresolved attempt {receipt.pending.intent.id}",
                    "retry_or_review",
                )
            expectation = reviewed
        else:
            if arguments.reviewed_revision is not None or arguments.replaces_attempt is not None:
                raise ReceiptFailure(
                    "invalid_review", "No stored conflict can be replaced", "restore"
                )
            expectation = Expectation("accepted", receipt.accepted_revision, None)
        attempt_id = uuid.uuid4().hex
        attempt_dir = _result_path(receipt_dir, attempt_id).parent
        attempt_dir.mkdir(mode=0o700)
        _sync_directory(receipt_dir)
        intent = RestoreIntent(attempt_id, expectation, arguments.archive_commit)
        prepared = dataclasses.replace(
            receipt,
            version=2,
            pending=Pending(intent, 1, "uncertain"),
            completion=None,
        )
        _write_receipt(receipt_dir, prepared)
        return _finish_dispatch(
            "restore", receipt_dir, None, _dispatch(receipt_dir, prepared, config, budget), 1
        )


def _retry(arguments: argparse.Namespace, config_path: Path, started_at: float) -> int:
    config = load_config(config_path, getattr(arguments, "command_seconds", None))
    budget = CommandBudget(started_at + config.limits.command_seconds)
    receipt_dir = Path(arguments.receipt).expanduser().absolute()
    with receipt_lock(receipt_dir, config.limits.lock_seconds, budget):
        receipt = load_receipt(receipt_dir)
        _validate_binding(receipt, config)
        recovery = _recover_saved_result(receipt_dir, receipt)
        receipt = recovery.receipt
        if recovery.local_completion and recovery.classification is not None:
            classification = recovery.classification
            if classification.kind == "completed":
                _emit(
                    _handoff(
                        "retry",
                        receipt_dir,
                        receipt,
                        outcome="completed",
                        message=classification.message,
                        receipt_persisted=True,
                        publisher_calls=0,
                    )
                )
                return 0
        pending = receipt.pending
        if pending is None:
            raise ReceiptFailure("nothing_to_retry", "Receipt has no pending attempt", "publish")
        if pending.state == "conflict":
            raise ReceiptFailure(
                "review_required",
                "The pending attempt conflicts with a host revision",
                "review_conflict",
            )
        calls = 0
        if pending.state == "uncertain":
            process, payload = _status_payload(receipt, config, budget)
            calls += 1
            if process.exit_code != 0 or payload is None:
                raise ReceiptFailure(
                    "inspection_failed",
                    "Mandatory status inspection did not return a correlated result",
                    "retry_status",
                )
            receipt = dataclasses.replace(receipt, last_observation=observation_from(payload))
            _write_receipt(receipt_dir, receipt)
            pending = receipt.pending
            assert pending is not None
        if isinstance(pending.intent, PublishIntent):
            verify_snapshot(receipt_dir, pending.intent.input, config.limits, budget)
        next_pending = dataclasses.replace(
            pending,
            dispatch_generation=pending.dispatch_generation + 1,
            state="uncertain",
        )
        receipt = dataclasses.replace(receipt, pending=next_pending, completion=None)
        _write_receipt(receipt_dir, receipt)
        return _finish_dispatch(
            "retry",
            receipt_dir,
            None,
            _dispatch(receipt_dir, receipt, config, budget),
            calls + 1,
        )


def _status(arguments: argparse.Namespace, config_path: Path, started_at: float) -> int:
    receipt_dir = Path(arguments.receipt).expanduser().absolute()
    if arguments.local_only:
        receipt = load_receipt(receipt_dir)
        _emit(
            _handoff(
                "status",
                receipt_dir,
                receipt,
                outcome="observed_local",
                message="Read the local receipt without host invocation",
                receipt_persisted=True,
                publisher_calls=0,
            )
        )
        return 0
    config = load_config(config_path, getattr(arguments, "command_seconds", None))
    budget = CommandBudget(started_at + config.limits.command_seconds)
    with receipt_lock(receipt_dir, config.limits.lock_seconds, budget):
        receipt = load_receipt(receipt_dir)
        _validate_binding(receipt, config)
        process, payload = _status_payload(receipt, config, budget)
        if process.exit_code != 0 or payload is None:
            raise ReceiptFailure(
                "status_failed",
                "Publisher status did not return a correlated result",
                "inspect_target",
            )
        updated = dataclasses.replace(receipt, last_observation=observation_from(payload))
        _write_receipt(receipt_dir, updated)
        _emit(
            _handoff(
                "status",
                receipt_dir,
                updated,
                outcome="observed",
                message="Recorded the host observation without changing accepted_revision",
                receipt_persisted=True,
                publisher_calls=1,
            )
        )
        return 0


def _parser() -> Parser:
    parser = Parser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="operation", required=True)

    publish = commands.add_parser("publish")
    publish.add_argument("source")
    publish.add_argument("--receipt")
    identity = publish.add_mutually_exclusive_group()
    identity.add_argument("--new", type=_name)
    identity.add_argument("--adopt", type=_name)
    publish.add_argument("--reviewed-revision", type=_revision)
    publish.add_argument("--replaces-attempt", type=_attempt_id)
    publish.add_argument("--local-only", action="store_true")

    retry = commands.add_parser("retry")
    retry.add_argument("--receipt", required=True)

    status = commands.add_parser("status")
    status.add_argument("--receipt", required=True)
    status.add_argument("--local-only", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("--receipt", required=True)
    restore.add_argument("--archive-commit", required=True)
    restore.add_argument("--reviewed-revision", type=_revision)
    restore.add_argument("--replaces-attempt", type=_attempt_id)
    return parser


def run(parsed: argparse.Namespace, config_path: Path, started_at: float | None = None) -> int:
    operation = parsed.artifact_action
    started_at = time.monotonic() if started_at is None else started_at
    try:
        if operation == "publish":
            return _publish(parsed, config_path, started_at)
        if operation == "retry":
            return _retry(parsed, config_path, started_at)
        if operation == "restore":
            return _restore(parsed, config_path, started_at)
        return _status(parsed, config_path, started_at)
    except ReceiptFailure as error:
        _emit(
            _handoff(
                operation,
                None,
                None,
                outcome="error",
                message=error.message,
                receipt_persisted=None,
                publisher_calls=0,
                error_code=error.code,
                next_action=error.next_action,
            )
        )
        return 1
    except OSError as error:
        _emit(
            _handoff(
                operation,
                None,
                None,
                outcome="error",
                message=str(error),
                receipt_persisted=None,
                publisher_calls=0,
                error_code="local_io_failure",
                next_action="inspect_local_state",
            )
        )
        return 1
    except KeyboardInterrupt:
        _emit(
            _handoff(
                operation,
                None,
                None,
                outcome="error",
                message="Interrupted",
                receipt_persisted=False,
                publisher_calls=0,
                error_code="interrupted",
                next_action="inspect_receipt",
            )
        )
        return 130


def usage_error(action: str, message: str) -> int:
    _emit(
        _handoff(
            action,
            None,
            None,
            outcome="error",
            message=message,
            receipt_persisted=False,
            publisher_calls=0,
            error_code="invalid_usage",
            next_action="fix_arguments",
        )
    )
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    started_at = time.monotonic()
    arguments = list(sys.argv[1:] if argv is None else argv)
    operation = next(
        (item for item in arguments if item in {"publish", "retry", "status", "restore"}), "usage"
    )
    try:
        parsed = _parser().parse_args(arguments)
    except UsageFailure as error:
        return usage_error(operation, str(error))
    parsed.artifact_action = parsed.operation
    config_path = cast(Path, parsed.config).expanduser().absolute()
    return run(parsed, config_path, started_at)


if __name__ == "__main__":
    raise SystemExit(main())
