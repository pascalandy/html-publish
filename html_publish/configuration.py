from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from urllib.parse import urlsplit

from html_publish.model import Config, PublishError

Role = Literal["publisher", "client"]
Execution = Literal["local", "remote"]


def _invalid(message: str, code: str = "invalid_config") -> PublishError:
    return PublishError(code, "config", message, "fix_config")


def _xdg_root(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    if value:
        root = Path(value).expanduser()
        if not root.is_absolute():
            raise _invalid(f"{variable} must be an absolute directory")
        return root
    return Path(fallback).expanduser()


def config_root() -> Path:
    return _xdg_root("XDG_CONFIG_HOME", "~/.config")


def data_root() -> Path:
    return _xdg_root("XDG_DATA_HOME", "~/.local/share")


def selected_path(role: Role, explicit: Path | None) -> tuple[Path, str]:
    if explicit is not None:
        return explicit.expanduser(), "argument"
    return config_root() / "html-publish" / f"{role}.json", "user_default"


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid(f"{label} must be a JSON object")
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise _invalid(f"{label} must be a JSON object")
    return cast(dict[str, object], raw)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{label} must be a non-empty string")
    return value


def _unknown(value: dict[str, object], allowed: set[str], label: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise _invalid(f"Unknown {label} fields: {', '.join(sorted(extra))}")


def _url(value: object) -> str:
    url = _text(value, "target.base_url")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise _invalid(f"target.base_url is malformed: {error}") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise _invalid("target.base_url must be an absolute HTTP(S) URL ending in /")
    return url


@dataclass(frozen=True)
class TargetIdentity:
    kind: Execution
    host: str
    base_url: str


@dataclass(frozen=True)
class Executor:
    kind: Execution
    command: tuple[str, ...]
    publisher_config: str | None
    host: str
    remote_executable: str | None
    remote_config: str | None
    incoming_root: str | None
    connect_timeout: int | None


@dataclass(frozen=True)
class ClientLimits:
    max_bytes: int
    max_files: int
    copy_seconds: float
    command_seconds: float
    lock_seconds: float
    output_bytes: int


@dataclass(frozen=True)
class ClientConfig:
    target: TargetIdentity
    executor: Executor
    limits: ClientLimits
    fingerprint: str


def parse_client(raw: dict[str, object]) -> ClientConfig:
    _unknown(raw, {"schema_version", "target", "execution", "limits"}, "client config")
    if raw.get("schema_version") != 1:
        raise _invalid("client config schema_version must be 1")
    target = _object(raw.get("target"), "target")
    _unknown(target, {"id", "base_url"}, "target")
    target_id = _text(target.get("id"), "target.id")
    base_url = _url(target.get("base_url"))
    execution = _object(raw.get("execution"), "execution")
    kind = execution.get("kind")
    if not isinstance(kind, str) or kind not in {"local", "remote"}:
        raise _invalid("execution.kind must be local or remote")
    kind = cast(Execution, kind)
    command_raw = execution.get("command")
    if not isinstance(command_raw, list) or not command_raw:
        raise _invalid("execution.command must be a non-empty string array")
    command_items = cast(list[object], command_raw)
    if not all(isinstance(item, str) and item for item in command_items):
        raise _invalid("execution.command must be a non-empty string array")
    command = tuple(cast(list[str], command_items))
    if kind == "local":
        _unknown(execution, {"kind", "command", "publisher_config"}, "execution")
        publisher_config = _text(execution.get("publisher_config"), "execution.publisher_config")
        host = target_id
        remote_executable = remote_config = incoming_root = None
        connect_timeout = None
    else:
        _unknown(
            execution,
            {
                "kind",
                "command",
                "host",
                "remote_executable",
                "remote_config",
                "incoming_root",
                "connect_timeout",
            },
            "execution",
        )
        publisher_config = None
        host = _text(execution.get("host"), "execution.host")
        if host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@-]+", host):
            raise _invalid("execution.host has invalid SSH destination characters")
        optional_fields = (
            ("remote_executable", execution.get("remote_executable")),
            ("remote_config", execution.get("remote_config")),
            ("incoming_root", execution.get("incoming_root")),
        )
        for label, value in optional_fields:
            if value is not None and (not isinstance(value, str) or not value):
                raise _invalid(f"execution.{label} must be a non-empty string")
        remote_executable = cast(str | None, execution.get("remote_executable"))
        remote_config = cast(str | None, execution.get("remote_config"))
        incoming_root = cast(str | None, execution.get("incoming_root"))
        timeout = execution.get("connect_timeout")
        if timeout is not None and (
            not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300
        ):
            raise _invalid("execution.connect_timeout must be between 1 and 300")
        connect_timeout = timeout
    limits = _object(raw.get("limits", {}), "limits")
    _unknown(
        limits,
        {
            "max_bytes",
            "max_files",
            "copy_seconds",
            "command_seconds",
            "lock_seconds",
            "output_bytes",
        },
        "limit",
    )
    for label, value in limits.items():
        minimum = (
            0
            if label == "lock_seconds"
            else 0.1
            if label in {"copy_seconds", "command_seconds"}
            else 1024
            if label == "output_bytes"
            else 1
        )
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < minimum
            or (label in {"max_bytes", "max_files", "output_bytes"} and not isinstance(value, int))
        ):
            raise _invalid(f"limits.{label} must be >= {minimum}")
    client_limits = ClientLimits(
        cast(int, limits.get("max_bytes", 100 * 1024 * 1024)),
        cast(int, limits.get("max_files", 2_000)),
        float(cast(int | float, limits.get("copy_seconds", 30.0))),
        float(cast(int | float, limits.get("command_seconds", 150.0))),
        float(cast(int | float, limits.get("lock_seconds", 5.0))),
        cast(int, limits.get("output_bytes", 1024 * 1024)),
    )
    identity = {
        "schema_version": 1,
        "target": {"kind": kind, "host": host, "base_url": base_url},
        "execution": {
            "kind": kind,
            "command": command,
            "publisher_config": publisher_config,
            "host": host,
            "remote_executable": remote_executable,
            "remote_config": remote_config,
            "incoming_root": incoming_root,
        },
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return ClientConfig(
        TargetIdentity(kind, host, base_url),
        Executor(
            kind,
            command,
            publisher_config,
            host,
            remote_executable,
            remote_config,
            incoming_root,
            connect_timeout,
        ),
        client_limits,
        digest,
    )


def parse_document(role: Role, raw: dict[str, object]) -> Config | ClientConfig:
    if role == "client":
        return parse_client(raw)
    from html_publish.cli import parse_publisher

    return parse_publisher(raw)


def read_document(role: Role, path: Path) -> tuple[dict[str, object], Config | ClientConfig]:
    try:
        raw = _object(json.loads(path.read_text(encoding="utf-8")), "configuration")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _invalid(
            f"The selected {role} configuration could not be read at {path}: {error}"
        ) from error
    return raw, parse_document(role, raw)


def load_client_config(path: Path) -> ClientConfig:
    _, config = read_document("client", path)
    assert isinstance(config, ClientConfig)
    return config


def init_document(path: Path, raw: dict[str, object], role: Role) -> str:
    parsed = parse_document(role, raw)
    if isinstance(parsed, ClientConfig):
        parsed_url = urlsplit(parsed.target.base_url)
        if parsed_url.scheme == "http" and parsed_url.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise _invalid("HTTP is allowed only for a loopback test target in new config")
        if parsed.executor.kind == "local":
            assert parsed.executor.publisher_config is not None
            if not Path(parsed.executor.publisher_config).is_absolute():
                raise _invalid("execution.publisher_config must be an absolute path in new config")
        else:
            for label, remote_path in (
                ("remote_executable", parsed.executor.remote_executable),
                ("remote_config", parsed.executor.remote_config),
                ("incoming_root", parsed.executor.incoming_root),
            ):
                if remote_path is None:
                    raise _invalid(f"execution.{label} is required in new remote config")
                path_value = PurePosixPath(remote_path)
                if (
                    not path_value.is_absolute()
                    or ".." in path_value.parts
                    or not re.fullmatch(r"/[A-Za-z0-9._/-]+", remote_path)
                ):
                    raise _invalid(f"execution.{label} must be an absolute remote POSIX path")
    content = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise _invalid(f"Configuration already exists at {path}", "config_exists")
    if path.exists():
        try:
            if path.read_bytes() == content:
                return "unchanged"
        except OSError as error:
            raise _invalid(
                f"Existing configuration at {path} cannot be read: {error}", "config_exists"
            ) from error
        raise _invalid(f"Different configuration already exists at {path}", "config_exists")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _invalid(
            f"Configuration parent could not be created at {path.parent}: {error}"
        ) from error
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".html-publish-config-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            try:
                if not path.is_symlink() and path.is_file() and path.read_bytes() == content:
                    return "unchanged"
            except OSError:
                pass
            raise _invalid(
                f"Different configuration already exists at {path}", "config_exists"
            ) from error
        return "config_written"
    except OSError as error:
        raise _invalid(f"Configuration could not be installed at {path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
