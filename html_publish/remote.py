from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import Literal, NoReturn, cast
from urllib.parse import urlparse

from html_publish import __version__
from html_publish.artifact import capture, capture_source
from html_publish.cli import ReportMode, bound_report_text, emit_json, report_dict, usage_report
from html_publish.configuration import ClientConfig, read_document, selected_path
from html_publish.delivery import publication_url
from html_publish.discovery import (
    command_schema,
    register_command,
    version_payload,
)
from html_publish.markdown import RENDER_PROFILE_ID
from html_publish.model import (
    Deadline,
    Effects,
    Failure,
    InputFormat,
    Limits,
    Name,
    Operation,
    PublishError,
    RecordRevision,
    Report,
    Revision,
)

DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_COMMAND_SECONDS = 120.0
REPORT_HELP = "report detail (default) or bounded summary with exact omission counts"

NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
HOST_PATTERN = re.compile(r"[A-Za-z0-9_.@-]+\Z")
REMOTE_PATH_PATTERN = re.compile(r"/[A-Za-z0-9._/-]+\Z")

ExitCode = Literal[0, 1, 2]


class UsageFailure(Exception):
    pass


class CommandExpired(Exception):
    def __init__(self, started: bool = False) -> None:
        super().__init__("The command deadline expired")
        self.started = started


class ProtocolFailure(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageFailure(message)


@dataclass(frozen=True)
class RemoteSettings:
    host: str
    executable: str
    config: str
    target: str
    incoming_root: PurePosixPath
    connect_timeout: int
    command_seconds: float

    def ssh_options(self, deadline: Deadline) -> tuple[str, ...]:
        connect_timeout = max(1, min(self.connect_timeout, math.ceil(deadline.remaining())))
        return (
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={connect_timeout}",
            "-o",
            "ForwardAgent=no",
        )


@dataclass(frozen=True)
class ArtifactRequest:
    operation: Literal["plan", "publish"]
    name: str
    source: Path
    expected_revision: str | None
    request_id: str | None
    report: ReportMode = "detail"
    input_format: InputFormat = "html"
    entry: str | None = None
    expected_record_revision: str | None = None


@dataclass(frozen=True)
class StatusRequest:
    operation: Literal["status"]
    name: str | None
    after: str | None
    limit: int
    host_check: bool
    report: ReportMode = "detail"


@dataclass(frozen=True)
class VerifyRequest:
    operation: Literal["verify"]
    name: str
    report: ReportMode = "detail"


@dataclass(frozen=True)
class HistoryRequest:
    operation: Literal["history"]
    name: str
    after: str | None
    limit: int
    diff_revision: str | None
    report: ReportMode = "detail"


@dataclass(frozen=True)
class RestoreRequest:
    operation: Literal["restore"]
    name: str
    archive_commit: str
    expected_revision: str | None
    request_id: str
    report: ReportMode = "detail"
    expected_record_revision: str | None = None


Request = ArtifactRequest | StatusRequest | VerifyRequest | HistoryRequest | RestoreRequest


@dataclass(frozen=True)
class Invocation:
    payload: dict[str, object]
    exit_code: ExitCode
    cleanup_allowed: bool


def _name(value: str) -> str:
    if len(value) > 80 or not NAME_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "name must use lowercase letters, digits, and single hyphens, up to 80 characters"
        )
    return value


def _host(value: str) -> str:
    if value.startswith("-") or not HOST_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("host must contain only ASCII host and user characters")
    return value


def _remote_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not REMOTE_PATH_PATTERN.fullmatch(value) or not path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError("incoming root must be an absolute ASCII path")
    return path


def _connect_timeout(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 300:
        raise argparse.ArgumentTypeError("connect timeout must be between 1 and 300 seconds")
    return parsed


def _positive_seconds(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("command seconds must be positive")
    return parsed


def _positive_limit(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return parsed


def _globals(parser: argparse.ArgumentParser, version: str, *, child: bool = False) -> None:
    def default(value: object) -> object:
        return argparse.SUPPRESS if child else value

    parser.add_argument(
        "--host",
        type=_host,
        default=default(None),
        help="SSH destination as user@host (required without client config)",
    )
    parser.add_argument(
        "--remote-executable",
        default=default(None),
        help="host html-publish executable path (required without client config)",
    )
    parser.add_argument(
        "--remote-config",
        default=default(None),
        help="host publisher JSON configuration path (required without client config)",
    )
    parser.add_argument(
        "--target",
        default=default(None),
        help="publication base URL matching the host config; required without client config",
    )
    parser.add_argument(
        "--incoming-root",
        type=_remote_path,
        default=default(None),
        help="private host directory for plan and publish uploads (required without client config)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default(None),
        help="client JSON config; defaults to user config when destination flags are absent",
    )
    parser.add_argument(
        "--connect-timeout",
        type=_connect_timeout,
        default=default(DEFAULT_CONNECT_TIMEOUT),
        help="SSH connection budget, 1 to 300 seconds, capped by remaining time "
        f"(default: {DEFAULT_CONNECT_TIMEOUT})",
    )
    parser.add_argument(
        "--command-seconds",
        type=_positive_seconds,
        default=default(DEFAULT_COMMAND_SECONDS),
        help="total client budget for capture, transport, and cleanup in seconds, positive "
        f"(default: {DEFAULT_COMMAND_SECONDS:g})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=default(True),
        help="write one JSON object to stdout (default for remote operations)",
    )
    parser.add_argument(
        "--version", action="version", version=version, help="show installed version"
    )


def _parser(json_version: bool = False) -> Parser:
    version = (
        json.dumps(version_payload("html-publish-remote"), separators=(",", ":"))
        if json_version
        else f"html-publish-remote {__version__}"
    )
    parser = Parser(
        prog="html-publish-remote",
        description="Run the six publisher operations through SSH. "
        "Publication results are JSON by default.",
        allow_abbrev=False,
    )
    _globals(parser, version)
    commands = parser.add_subparsers(dest="operation", required=True)

    for operation, help_text in (
        ("plan", "capture and inspect without publication changes"),
        ("publish", "archive, activate, and verify a finished artifact"),
    ):
        command = register_command(
            commands,
            operation,
            help_text,
            examples=(
                f"html-publish-remote --config client.json {operation} "
                "--name release-notes --source ./page.html",
            ),
            effects=("uploads a private source copy", "reads host state")
            if operation == "plan"
            else (
                "uploads a private source copy",
                "may advance archive and activate page",
                "probes host delivery",
            ),
        )
        command.add_argument(
            "--name",
            required=True,
            type=_name,
            help="publication name (lowercase letters, digits, single hyphens; max 80)",
        )
        command.add_argument(
            "--source",
            required=True,
            type=Path,
            help="HTML or Markdown file or directory to upload",
        )
        command.add_argument(
            "--format",
            dest="input_format",
            choices=("html", "markdown"),
            default="html",
            help="input format (default: html)",
        )
        command.add_argument(
            "--entry", help="Markdown directory entry file; otherwise index.md or README.md"
        )
        command.add_argument(
            "--expected-revision",
            help=(
                "expected active revision used for the prediction; "
                "omit for a first publication or identical retry"
                if operation == "plan"
                else "expected active revision to replace different content; "
                "omit for a first publication or identical retry"
            ),
        )
        command.add_argument(
            "--expected-record-revision",
            help="expected latest private source-record revision for Markdown publication",
        )
        if operation == "publish":
            command.add_argument("--request-id", help="caller attempt ID echoed in the result")
        command.add_argument(
            "--report", choices=("detail", "summary"), default="detail", help=REPORT_HELP
        )
        _globals(command, version, child=True)

    status = register_command(
        commands,
        "status",
        "observe one publication or a paged list",
        examples=(
            "html-publish-remote --config client.json status --name release-notes",
            "html-publish-remote --config client.json status --after '<continuation>' --limit 20",
        ),
        effects=(
            "reads host state",
            "--host-check validates saved bytes and delivery",
            "does not repair or activate",
        ),
    )
    status.add_argument("--name", type=_name, help="publication name; omit for a paged listing")
    status.add_argument(
        "--after", type=_name, help="opaque continuation from the prior status page"
    )
    status.add_argument(
        "--limit",
        type=_positive_limit,
        default=100,
        help="page size, 1 to 100 (default: %(default)s)",
    )
    status.add_argument(
        "--host-check",
        action="store_true",
        help="also validate selected bytes and probe delivery for a named page",
    )
    status.add_argument(
        "--report", choices=("detail", "summary"), default="detail", help=REPORT_HELP
    )
    _globals(status, version, child=True)

    verify = register_command(
        commands,
        "verify",
        "check selected files and host HTTP delivery",
        examples=("html-publish-remote --config client.json verify --name release-notes",),
        effects=("reads saved bytes and host delivery", "does not activate"),
    )
    verify.add_argument("--name", required=True, type=_name, help="publication name")
    verify.add_argument(
        "--report", choices=("detail", "summary"), default="detail", help=REPORT_HELP
    )
    _globals(verify, version, child=True)

    history = register_command(
        commands,
        "history",
        "list per-name history and optional text differences",
        examples=(
            "html-publish-remote --config client.json history --name release-notes",
            "html-publish-remote --config client.json history --name release-notes "
            "--after '<continuation>' --limit 5",
        ),
        effects=("reads host Git history", "does not change archive or selection"),
    )
    history.add_argument("--name", required=True, type=_name, help="publication name")
    history.add_argument("--after", help="opaque continuation from history for the same name")
    history.add_argument(
        "--limit",
        type=_positive_limit,
        default=20,
        help="page size, 1 to 100 (default: %(default)s)",
    )
    history.add_argument(
        "--diff",
        dest="diff_revision",
        help="reachable archived revision to compare with the latest page tree at HEAD "
        "(UTF-8 diff text capped at 64 KiB)",
    )
    history.add_argument(
        "--report", choices=("detail", "summary"), default="detail", help=REPORT_HELP
    )
    _globals(history, version, child=True)

    restore = register_command(
        commands,
        "restore",
        "select an archived revision under a revision guard",
        examples=(
            "html-publish-remote --config client.json restore --name release-notes "
            "--archive-commit COMMIT --expected-revision REVISION",
        ),
        effects=(
            "may append archive history",
            "may activate saved revision",
            "probes host delivery",
        ),
    )
    restore.add_argument("--name", required=True, type=_name, help="publication name")
    restore.add_argument(
        "--archive-commit",
        required=True,
        help="reachable commit containing the revision to restore",
    )
    restore.add_argument("--expected-revision", help="expected active revision for guarded restore")
    restore.add_argument("--expected-record-revision", help="expected private record revision")
    restore.add_argument("--request-id", help="caller attempt ID echoed in the result")
    restore.add_argument(
        "--report", choices=("detail", "summary"), default="detail", help=REPORT_HELP
    )
    _globals(restore, version, child=True)

    schema = register_command(
        commands,
        "schema",
        "print parser-derived command discovery as JSON",
        examples=("html-publish-remote schema",),
        effects=("reads command definitions only",),
    )
    _globals(schema, version, child=True)
    return parser


def _parse(arguments: list[str]) -> tuple[RemoteSettings, Request]:
    parsed = _parser().parse_args(arguments)
    fields = ("host", "remote_executable", "remote_config", "target", "incoming_root")
    supplied = {field: getattr(parsed, field) for field in fields}
    client: ClientConfig | None = None
    if parsed.config is not None or any(value is None for value in supplied.values()):
        selected, source = selected_path("client", parsed.config)
        if source == "argument" or selected.exists():
            _, loaded = read_document("client", selected)
            assert isinstance(loaded, ClientConfig)
            if loaded.executor.kind != "remote":
                raise PublishError(
                    "invalid_config",
                    "config",
                    "Selected client config must use remote execution",
                    "fix_config",
                )
            client = loaded
    effective = {
        "host": parsed.host or (client.executor.host if client else None),
        "remote_executable": parsed.remote_executable
        or (client.executor.remote_executable if client else None),
        "remote_config": parsed.remote_config
        or (client.executor.remote_config if client else None),
        "target": parsed.target or (client.target.base_url if client else None),
        "incoming_root": parsed.incoming_root
        or (
            PurePosixPath(client.executor.incoming_root)
            if client and client.executor.incoming_root
            else None
        ),
    }
    missing = ["--" + name.replace("_", "-") for name, value in effective.items() if value is None]
    if missing:
        if client is not None:
            raise PublishError(
                "invalid_config",
                "config",
                "Selected client config lacks remote destination fields: " + ", ".join(missing),
                "fix_config",
            )
        raise UsageFailure(
            "Remote destination requires " + ", ".join(missing) + " or an explicit client config"
        )
    settings = RemoteSettings(
        cast(str, effective["host"]),
        cast(str, effective["remote_executable"]),
        cast(str, effective["remote_config"]),
        cast(str, effective["target"]),
        cast(PurePosixPath, effective["incoming_root"]),
        cast(
            int,
            parsed.connect_timeout
            if _argument_value(arguments, "--connect-timeout") is not None or client is None
            else client.executor.connect_timeout or DEFAULT_CONNECT_TIMEOUT,
        ),
        cast(
            float,
            parsed.command_seconds
            if _argument_value(arguments, "--command-seconds") is not None or client is None
            else client.limits.command_seconds,
        ),
    )
    operation = cast(Operation, parsed.operation)
    if operation == "status":
        return settings, StatusRequest(
            "status",
            cast(str | None, parsed.name),
            cast(str | None, parsed.after),
            cast(int, parsed.limit),
            cast(bool, parsed.host_check),
            cast(ReportMode, parsed.report),
        )
    name = cast(str, parsed.name)
    if operation == "verify":
        return settings, VerifyRequest("verify", name, cast(ReportMode, parsed.report))
    if operation == "history":
        return settings, HistoryRequest(
            "history",
            name,
            cast(str | None, parsed.after),
            cast(int, parsed.limit),
            cast(str | None, parsed.diff_revision),
            cast(ReportMode, parsed.report),
        )
    request_id = cast(str | None, getattr(parsed, "request_id", None))
    if operation in {"publish", "restore"} and request_id is None:
        request_id = f"remote-{uuid.uuid4().hex}"
    if operation == "restore":
        assert request_id is not None
        return settings, RestoreRequest(
            "restore",
            name,
            cast(str, parsed.archive_commit),
            cast(str | None, parsed.expected_revision),
            request_id,
            cast(ReportMode, parsed.report),
            cast(str | None, parsed.expected_record_revision),
        )
    input_format = cast(InputFormat, parsed.input_format)
    entry = cast(str | None, parsed.entry)
    expected_record_revision = cast(str | None, parsed.expected_record_revision)
    if input_format == "html" and (entry is not None or expected_record_revision is not None):
        raise UsageFailure("--entry and --expected-record-revision require --format markdown")
    return settings, ArtifactRequest(
        operation,
        name,
        cast(Path, parsed.source),
        cast(str | None, parsed.expected_revision),
        request_id,
        cast(ReportMode, parsed.report),
        input_format,
        entry,
        expected_record_revision,
    )


def _operation(arguments: list[str]) -> Operation | None:
    for value in arguments:
        if value in {"plan", "publish", "status", "verify", "history", "restore"}:
            return cast(Operation, value)
    return None


def _argument_value(arguments: list[str], option: str) -> str | None:
    for index in range(len(arguments) - 1, -1, -1):
        value = arguments[index]
        if value.startswith(option + "="):
            return value[len(option) + 1 :]
        if value == option and index + 1 < len(arguments):
            following = arguments[index + 1]
            return following if not following.startswith("--") else None
    return None


def _usage_payload(arguments: list[str], failure: Failure) -> dict[str, object]:
    operation = _operation(arguments)
    if operation is None:
        return usage_report(None, failure)
    mode: ReportMode = (
        "summary" if _argument_value(arguments, "--report") == "summary" else "detail"
    )
    raw_name = _argument_value(arguments, "--name")
    try:
        name = Name(_name(raw_name)) if raw_name is not None else None
    except argparse.ArgumentTypeError:
        name = None
    target = _argument_value(arguments, "--target")
    try:
        parsed_target = urlparse(target) if target is not None else None
        valid_target = (
            parsed_target is not None
            and parsed_target.scheme in {"http", "https"}
            and bool(parsed_target.hostname)
        )
    except ValueError:
        valid_target = False
    request_id = _argument_value(arguments, "--request-id")
    expected_revision = _argument_value(arguments, "--expected-revision")
    expected_record_revision = _argument_value(arguments, "--expected-record-revision")
    return usage_report(
        operation,
        failure,
        target=target if valid_target else None,
        name=name,
        request_id=request_id,
        expected_revision=Revision(expected_revision) if expected_revision is not None else None,
        expected_record_revision=(
            RecordRevision(expected_record_revision)
            if expected_record_revision is not None
            else None
        ),
        mode=mode,
    )


def _request_id(request: Request) -> str | None:
    if isinstance(request, (ArtifactRequest, RestoreRequest)):
        return request.request_id
    return None


def _expected_revision(request: Request) -> str | None:
    if isinstance(request, (ArtifactRequest, RestoreRequest)):
        return request.expected_revision
    return None


def _expected_record_revision(request: Request) -> str | None:
    if isinstance(request, ArtifactRequest):
        return request.expected_record_revision
    if isinstance(request, RestoreRequest):
        return request.expected_record_revision
    return None


def _failure_payload(
    settings: RemoteSettings,
    request: Request,
    failure: Failure,
    effects: Effects | None = None,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    name = request.name
    expected_revision = _expected_revision(request)
    expected_record_revision = _expected_record_revision(request)
    return report_dict(
        Report(
            request.operation,
            "error",
            settings.target,
            Name(name) if name is not None else None,
            publication_url(settings.target, Name(name)) if name is not None else None,
            request_id=_request_id(request),
            expected_revision=Revision(expected_revision)
            if expected_revision is not None
            else None,
            expected_record_revision=RecordRevision(expected_record_revision)
            if expected_record_revision is not None
            else None,
            render_profile_id=(
                RENDER_PROFILE_ID
                if isinstance(request, ArtifactRequest) and request.input_format == "markdown"
                else None
            ),
            effects=effects or Effects(),
            error=failure,
            details=details or {},
        ),
        request.report,
    )


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.2)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


def _run(argv: list[str], deadline: Deadline) -> subprocess.CompletedProcess[bytes]:
    def cancel(_signum: int, _frame: FrameType | None) -> NoReturn:
        raise KeyboardInterrupt

    try:
        timeout = deadline.remaining()
    except PublishError as error:
        raise CommandExpired from error
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    previous = signal.signal(signal.SIGTERM, cancel)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _terminate_group(process)
        raise CommandExpired(started=True) from error
    except BaseException:
        _terminate_group(process)
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)
    _terminate_group(process)
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _write_stderr(data: bytes, mode: ReportMode = "detail") -> None:
    if mode == "summary":
        data = data[:4096]
    if data:
        sys.stderr.buffer.write(data)
        sys.stderr.buffer.flush()


def _ssh(
    settings: RemoteSettings,
    remote_command: str,
    deadline: Deadline,
) -> subprocess.CompletedProcess[bytes]:
    try:
        options = settings.ssh_options(deadline)
    except PublishError as error:
        raise CommandExpired from error
    return _run(["ssh", *options, settings.host, remote_command], deadline)


def _remote_arguments(
    settings: RemoteSettings,
    request: Request,
    remote_source: PurePosixPath | None = None,
) -> list[str]:
    arguments = [
        settings.executable,
        "--config",
        settings.config,
        "--json",
        request.operation,
        "--report",
        request.report,
    ]
    if isinstance(request, ArtifactRequest):
        assert remote_source is not None
        arguments.extend(
            ["--name", request.name, "--source", str(remote_source), "--target", settings.target]
        )
        if request.expected_revision is not None:
            arguments.extend(["--expected-revision", request.expected_revision])
        if request.input_format == "markdown":
            arguments.extend(["--format", "markdown"])
            if request.entry is not None:
                arguments.extend(["--entry", request.entry])
        if request.expected_record_revision is not None:
            arguments.extend(["--expected-record-revision", request.expected_record_revision])
        if request.request_id is not None:
            arguments.extend(["--request-id", request.request_id])
        return arguments
    if isinstance(request, StatusRequest):
        if request.name is not None:
            arguments.extend(["--name", request.name])
        if request.after is not None:
            arguments.extend(["--after", request.after])
        arguments.extend(["--limit", str(request.limit)])
        if request.host_check:
            arguments.append("--host-check")
        return arguments
    arguments.extend(["--name", request.name])
    if isinstance(request, HistoryRequest):
        arguments.extend(["--limit", str(request.limit)])
        if request.after is not None:
            arguments.extend(["--after", request.after])
        if request.diff_revision is not None:
            arguments.extend(["--diff", request.diff_revision])
    elif isinstance(request, RestoreRequest):
        arguments.extend(["--archive-commit", request.archive_commit, "--target", settings.target])
        if request.expected_revision is not None:
            arguments.extend(["--expected-revision", request.expected_revision])
        if request.expected_record_revision is not None:
            arguments.extend(["--expected-record-revision", request.expected_record_revision])
        arguments.extend(["--request-id", request.request_id])
    return arguments


def _json_object(data: bytes) -> dict[str, object]:
    try:
        decoded = data.decode("utf-8")
        raw: object = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolFailure("The host did not return one valid UTF-8 JSON object") from error
    if not isinstance(raw, dict):
        raise ProtocolFailure("The host result must be one JSON object with string keys")
    return cast(dict[str, object], raw)


def _report_object(value: object, fields: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProtocolFailure(f"The host result {label} must be an object")
    values = cast(dict[str, object], value)
    if not fields.issubset(values):
        raise ProtocolFailure(f"The host result {label} is missing required fields")
    return values


def _validate_verification(value: object) -> dict[str, object]:
    verification = _report_object(
        value,
        {
            "result",
            "revision",
            "checked_at",
            "probe_location",
            "files_checked",
            "bytes_checked",
            "scope",
            "detail",
        },
        "verification",
    )
    if verification["result"] not in ("passed", "failed", "not_checked"):
        raise ProtocolFailure("The host result verification result is invalid")
    for key in ("revision", "checked_at", "probe_location", "detail"):
        if verification[key] is not None and not isinstance(verification[key], str):
            raise ProtocolFailure(f"The host result verification {key} must be text or null")
    for key in ("files_checked", "bytes_checked"):
        count = verification[key]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ProtocolFailure(
                f"The host result verification {key} must be a nonnegative integer"
            )
    scope = verification["scope"]
    if not isinstance(scope, list) or not all(
        isinstance(item, str) for item in cast(list[object], scope)
    ):
        raise ProtocolFailure("The host result verification scope must be a list of strings")
    return verification


def _validate_observation(
    value: object,
    report: Mapping[str, object] | None = None,
    *,
    require_record_identity: bool = False,
) -> dict[str, object] | None:
    saved_revision: object = None
    saved_record_revision: object = None
    archive_commit: object = None
    active_revision: object = None
    selection: dict[str, object] | None = None
    if value is not None:
        observation = _report_object(value, {"saved", "selection"}, "observation")
        if observation["saved"] is not None:
            saved_raw = _report_object(
                observation["saved"],
                {"revision", "archive_commit"},
                "saved state",
            )
            if require_record_identity and "record_revision" not in saved_raw:
                raise ProtocolFailure("The host result saved state omits its record revision")
            saved = saved_raw
            if any(
                not isinstance(saved[key], str) or not saved[key]
                for key in ("revision", "archive_commit")
            ):
                raise ProtocolFailure(
                    "The host result saved revision and commit must be nonempty text"
                )
            saved_revision, archive_commit = saved["revision"], saved["archive_commit"]
            saved_record_revision = saved.get("record_revision")
            if saved_record_revision is not None and not isinstance(saved_record_revision, str):
                raise ProtocolFailure("The host result saved record revision is invalid")
        selection = _report_object(
            observation["selection"],
            {"state", "revision", "integrity_checked", "detail"},
            "selection",
        )
        state = selection["state"]
        if state not in ("absent", "selected", "degraded", "unobserved"):
            raise ProtocolFailure("The host result selection state is invalid")
        if type(selection["integrity_checked"]) is not bool:
            raise ProtocolFailure("The host result selection integrity_checked must be a boolean")
        for key in ("revision", "detail"):
            if selection[key] is not None and not isinstance(selection[key], str):
                raise ProtocolFailure(f"The host result selection {key} must be text or null")
        if state == "selected":
            if not selection["revision"]:
                raise ProtocolFailure("The selected host result has no revision")
            active_revision = selection["revision"]
        elif selection["integrity_checked"] is not False:
            raise ProtocolFailure("An unselected host result cannot claim checked integrity")
        if state in ("absent", "unobserved") and selection["revision"] is not None:
            raise ProtocolFailure("The absent or unobserved selection cannot claim a revision")
    if report is not None and (
        report["archived_revision"] != saved_revision
        or report.get("archived_record_revision") != saved_record_revision
        or report["archive_commit"] != archive_commit
        or report["active_revision"] != active_revision
    ):
        raise ProtocolFailure("The host result revisions disagree with its observation")
    return selection


def _validate_report_projection(payload: dict[str, object], request: Request) -> None:
    details = payload.get("warning_details")
    if request.report == "summary" and details is None:
        raise ProtocolFailure("The host summary has no warning details collection")
    if details is not None:
        if not isinstance(details, list):
            raise ProtocolFailure("The host warning details are invalid")
        for item in cast(list[object], details):
            values = _report_object(
                item, {"code", "source_path", "reference", "expected_path"}, "warning detail"
            )
            if not all(
                isinstance(values[key], str) for key in ("code", "source_path", "reference")
            ) or (
                values["expected_path"] is not None and not isinstance(values["expected_path"], str)
            ):
                raise ProtocolFailure("The host warning details are invalid")
            warnings = payload.get("warnings")
            if isinstance(warnings, list) and values["code"] not in warnings:
                raise ProtocolFailure("The host warning detail code is not reported")
    report = payload.get("report")
    if report is None:
        if request.report == "summary":
            raise ProtocolFailure("The host did not return the requested summary")
        return
    values = _report_object(report, {"mode", "collections", "text"}, "report")
    if values["mode"] != request.report:
        raise ProtocolFailure("The host report mode does not match the request")
    collections = values["collections"]
    text_fields = values["text"]
    if not isinstance(collections, dict) or not isinstance(text_fields, dict):
        raise ProtocolFailure("The host report metadata is invalid")
    collections = cast(dict[str, object], collections)
    text_fields = cast(dict[str, object], text_fields)
    expected: dict[str, list[object]] = {}
    if isinstance(details, list):
        expected["/warning_details"] = cast(list[object], details)
    differences = payload.get("differences")
    if isinstance(differences, dict):
        differences = cast(dict[str, object], differences)
        for key in ("added", "changed", "deleted"):
            paths = differences.get(key)
            if isinstance(paths, list):
                expected[f"/differences/{key}"] = cast(list[object], paths)
    record_differences = payload.get("record_differences")
    if isinstance(record_differences, dict):
        record_differences = cast(dict[str, object], record_differences)
        for key in ("added", "changed", "deleted"):
            paths = record_differences.get(key)
            if isinstance(paths, list):
                expected[f"/record_differences/{key}"] = cast(list[object], paths)
    entries = payload.get("entries")
    if request.operation == "history" and isinstance(entries, list):
        for index, entry in enumerate(cast(list[object], entries)):
            if isinstance(entry, dict):
                typed_entry = cast(dict[str, object], entry)
                if not isinstance(typed_entry.get("changes"), dict):
                    continue
                changes = cast(dict[str, object], typed_entry["changes"])
                for key in ("added", "changed", "deleted"):
                    paths = changes.get(key)
                    if isinstance(paths, list):
                        expected[f"/entries/{index}/changes/{key}"] = cast(list[object], paths)
                record_changes = typed_entry.get("record_changes")
                if isinstance(record_changes, dict):
                    record_changes = cast(dict[str, object], record_changes)
                    for key in ("added", "changed", "deleted"):
                        paths = record_changes.get(key)
                        if isinstance(paths, list):
                            expected[f"/entries/{index}/record_changes/{key}"] = cast(
                                list[object], paths
                            )
    if set(collections) != set(expected):
        raise ProtocolFailure("The host report collection metadata is incomplete")
    for path, paths in expected.items():
        counts = _report_object(collections[path], {"total", "included", "omitted"}, "report count")
        total, included, omitted = (counts[key] for key in ("total", "included", "omitted"))
        if any(type(value) is not int or value < 0 for value in (total, included, omitted)):
            raise ProtocolFailure("The host report counts are invalid")
        total, included, omitted = cast(tuple[int, int, int], (total, included, omitted))
        if (
            included != len(paths)
            or total != included + omitted
            or (request.report == "detail" and omitted != 0)
            or (request.report == "summary" and included != 0)
        ):
            raise ProtocolFailure("The host report counts disagree with its collections")
    expected_text: dict[str, str] = {}
    for path, container, key in (
        ("/error/message", payload.get("error"), "message"),
        ("/verification/detail", payload.get("verification"), "detail"),
        ("/transport/detail", payload.get("transport"), "detail"),
    ):
        if isinstance(container, dict):
            value = cast(dict[str, object], container).get(key)
            if isinstance(value, str):
                expected_text[path] = value
    observation = payload.get("observation")
    if isinstance(observation, dict):
        selection = cast(dict[str, object], observation).get("selection")
        if isinstance(selection, dict):
            value = cast(dict[str, object], selection).get("detail")
            if isinstance(value, str):
                expected_text["/observation/selection/detail"] = value
    if set(text_fields) != set(expected_text):
        raise ProtocolFailure("The host report text metadata is incomplete")
    for path, item in text_fields.items():
        counts = _report_object(
            item, {"total_bytes", "included_bytes", "omitted_bytes"}, "text count"
        )
        total, included, omitted = (
            counts[key] for key in ("total_bytes", "included_bytes", "omitted_bytes")
        )
        if any(type(value) is not int or value < 0 for value in (total, included, omitted)):
            raise ProtocolFailure("The host report text counts are invalid")
        total, included, omitted = cast(tuple[int, int, int], (total, included, omitted))
        if (
            total != included + omitted
            or included != len(expected_text[path].encode("utf-8"))
            or (request.report == "detail" and omitted != 0)
            or (request.report == "summary" and included > 4096)
        ):
            raise ProtocolFailure("The host report text counts disagree")


def _validate_host_payload(
    payload: dict[str, object],
    exit_code: int,
    settings: RemoteSettings,
    request: Request,
) -> ExitCode:
    required = {
        "schema_version",
        "operation",
        "target",
        "name",
        "url",
        "request_id",
        "expected_revision",
        "requested_revision",
        "archived_revision",
        "archive_commit",
        "active_revision",
        "outcome",
        "effects",
        "verification",
        "warnings",
        "error",
        "observation",
    }
    if not required.issubset(payload):
        raise ProtocolFailure("The host result is missing common envelope fields")
    if exit_code not in {0, 1, 2}:
        raise ProtocolFailure(f"The host returned unsupported exit code {exit_code}")
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1:
        raise ProtocolFailure("The host result has an unsupported schema version")
    if payload.get("operation") != request.operation:
        raise ProtocolFailure("The host result operation does not match the request")
    if payload.get("target") != settings.target:
        raise ProtocolFailure("The host result target does not match the request")
    if payload.get("name") != request.name:
        raise ProtocolFailure("The host result name does not match the request")
    expected_url = publication_url(settings.target, Name(request.name)) if request.name else None
    if payload.get("url") != expected_url:
        raise ProtocolFailure("The host result URL does not match the request")
    if payload.get("request_id") != _request_id(request):
        raise ProtocolFailure("The host result request ID does not match the request")
    if payload.get("expected_revision") != _expected_revision(request):
        raise ProtocolFailure("The host result expectation does not match the request")
    record_aware_request = isinstance(request, RestoreRequest) or (
        isinstance(request, ArtifactRequest) and request.input_format == "markdown"
    )
    record_identity_fields = {
        "expected_record_revision",
        "requested_record_revision",
        "archived_record_revision",
        "render_profile_id",
    }
    if record_aware_request and not record_identity_fields.issubset(payload):
        raise ProtocolFailure("The host result is missing Markdown record identity fields")
    if payload.get("expected_record_revision") != _expected_record_revision(request):
        raise ProtocolFailure("The host record expectation does not match the request")
    if any(
        payload.get(key) is not None and not isinstance(payload.get(key), str)
        for key in (
            "requested_revision",
            "requested_record_revision",
            "archived_revision",
            "archived_record_revision",
            "archive_commit",
            "active_revision",
            "render_profile_id",
        )
    ):
        raise ProtocolFailure("The host result revision fields are invalid")
    outcome = payload.get("outcome")
    allowed_outcomes: dict[Operation, set[str]] = {
        "plan": {"planned", "error"},
        "publish": {"published", "unchanged", "error"},
        "status": {"observed", "error"},
        "verify": {"verified", "error"},
        "history": {"observed", "error"},
        "restore": {"published", "unchanged", "error"},
    }
    if not isinstance(outcome, str) or outcome not in allowed_outcomes[request.operation]:
        raise ProtocolFailure("The host result outcome is invalid for the command")
    effects = payload.get("effects")
    if not isinstance(effects, dict):
        raise ProtocolFailure("The host result effects are invalid")
    effect_values = cast(dict[str, object], effects)
    if any(
        key not in effect_values
        or (effect_values[key] is not None and type(effect_values[key]) is not bool)
        for key in ("archive_advanced", "activated")
    ):
        raise ProtocolFailure("The host result effects are invalid")
    if request.operation not in {"publish", "restore"} and any(
        effect_values[key] is not False for key in ("archive_advanced", "activated")
    ):
        raise ProtocolFailure("The read-only host result claims mutation effects")
    if exit_code == 0 and any(
        effect_values[key] is None for key in ("archive_advanced", "activated")
    ):
        raise ProtocolFailure("The host success result has unknown effects")
    record_only_published = (
        outcome == "published"
        and record_aware_request
        and effect_values["archive_advanced"] is True
        and effect_values["activated"] is False
        and payload["requested_revision"] == payload["active_revision"]
        and payload["requested_record_revision"] == payload["archived_record_revision"]
    )
    if (
        outcome == "published"
        and effect_values["activated"] is not True
        and not record_only_published
    ):
        raise ProtocolFailure(
            "The published host result reports neither activation nor record-only archive"
        )
    if (outcome == "unchanged" or exit_code == 2) and any(
        effect_values[key] is not False for key in ("archive_advanced", "activated")
    ):
        raise ProtocolFailure("The host result effects disagree with its outcome")
    verification = _validate_verification(payload["verification"])
    selection = _validate_observation(
        payload["observation"], payload, require_record_identity=record_aware_request
    )
    if request.operation == "status" and request.name is None:
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise ProtocolFailure("The host status listing must contain entries")
        for entry in cast(list[object], entries):
            values = _report_object(entry, {"name", "url", "observation"}, "status entry")
            if not isinstance(values["name"], str) or not isinstance(values["url"], str):
                raise ProtocolFailure("The host status entry name and URL must be text")
            if _validate_observation(values["observation"]) is None:
                raise ProtocolFailure("The host status entry must contain an observation")
    warnings = payload.get("warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(item, str) for item in cast(list[object], warnings)
    ):
        raise ProtocolFailure("The host result warnings are invalid")
    _validate_report_projection(payload, request)
    error = payload.get("error")
    if exit_code == 0 and (outcome == "error" or error is not None):
        raise ProtocolFailure("The host success exit does not agree with its result")
    if exit_code in {1, 2} and (
        (outcome != "error" and not (request.operation == "status" and outcome == "observed"))
        or not isinstance(error, dict)
    ):
        raise ProtocolFailure("The host error exit does not agree with its result")
    error_values = cast(dict[str, object], error) if isinstance(error, dict) else {}
    if error_values:
        action = error_values.get("next_action")
        if not all(isinstance(error_values.get(key), str) for key in ("code", "phase", "message")):
            raise ProtocolFailure("The host result error is invalid")
        if not isinstance(action, dict):
            raise ProtocolFailure("The host result next action is invalid")
        action_values = cast(dict[str, object], action)
        required = action_values.get("required_inputs")
        if (
            not isinstance(action_values.get("kind"), str)
            or not isinstance(required, list)
            or not all(isinstance(item, str) for item in cast(list[object], required))
        ):
            raise ProtocolFailure("The host result next action is invalid")
    elif exit_code != 0:
        raise ProtocolFailure("The host result error is empty")
    if exit_code == 1 and error_values.get("phase") == "usage":
        raise ProtocolFailure("The host usage failure returned operational exit 1")
    if exit_code == 2 and error_values.get("phase") != "usage":
        raise ProtocolFailure("The host exit 2 does not describe invalid usage")
    if exit_code == 0 and request.operation in {"publish", "restore", "verify"}:
        if verification["result"] != "passed":
            raise ProtocolFailure("The successful host command has not passed verification")
        if (
            selection is None
            or selection["state"] != "selected"
            or not selection["integrity_checked"]
        ):
            raise ProtocolFailure("The successful host command has no integrity-checked selection")
        if verification["revision"] != payload["active_revision"]:
            raise ProtocolFailure(
                "The successful host verification does not match the selected revision"
            )
        if (
            not verification["checked_at"]
            or verification["probe_location"] != "host"
            or verification["files_checked"] == 0
            or not {
                "local_export",
                "directory_url",
                "index_html",
                "all_files",
                "missing_path",
            }.issubset(cast(list[str], verification["scope"]))
        ):
            raise ProtocolFailure(
                "The successful host verification lacks complete delivery evidence"
            )
        if request.operation in {"publish", "restore"}:
            if (
                payload["requested_revision"] != payload["active_revision"]
                or payload["archive_commit"] is None
            ):
                raise ProtocolFailure(
                    "The successful mutation does not select its requested revision"
                )
            if (
                outcome == "published"
                and payload["archived_revision"] != payload["requested_revision"]
            ):
                raise ProtocolFailure(
                    "The published host result does not archive its requested revision"
                )
            if (
                record_aware_request
                and payload["requested_record_revision"] != payload["archived_record_revision"]
            ):
                raise ProtocolFailure("The successful host result does not archive its record")
    return cast(ExitCode, exit_code)


def _retained_transport(staging: PurePosixPath | None, detail: str) -> dict[str, object]:
    transport: dict[str, object] = {"cleanup": "skipped", "detail": detail}
    if staging is not None:
        transport["staging"] = str(staging)
    return {"transport": transport}


def _invocation_loss(
    settings: RemoteSettings,
    request: Request,
    staging: PurePosixPath | None,
    detail: str,
) -> Invocation:
    mutating = request.operation in {"publish", "restore"}
    failure = Failure(
        "publication_outcome_unknown" if mutating else "transport_failure",
        "invoke",
        "SSH lost the publication result" if mutating else "SSH lost the remote command result",
        "inspect" if mutating else "retry",
        ("name", "request_id", "expected_revision") if mutating else ("name",),
    )
    payload = _failure_payload(
        settings,
        request,
        failure,
        Effects(None, None) if mutating else Effects(),
        details=_retained_transport(staging, detail),
    )
    return Invocation(payload, 1, False)


def _protocol_failure(
    settings: RemoteSettings,
    request: Request,
    staging: PurePosixPath | None,
    message: str,
) -> Invocation:
    mutating = request.operation in {"publish", "restore"}
    failure = Failure(
        "remote_protocol_failure",
        "invoke",
        message,
        "inspect" if mutating else "retry",
        ("name", "request_id", "expected_revision") if mutating else ("name",),
    )
    details = _retained_transport(staging, message) if mutating else None
    return Invocation(
        _failure_payload(
            settings,
            request,
            failure,
            Effects(None, None) if mutating else Effects(),
            details=details,
        ),
        1,
        not mutating,
    )


def _invoke(
    settings: RemoteSettings,
    request: Request,
    deadline: Deadline,
    staging: PurePosixPath | None = None,
    remote_source: PurePosixPath | None = None,
) -> Invocation:
    remote_command = shlex.join(_remote_arguments(settings, request, remote_source))
    try:
        result = _ssh(settings, remote_command, deadline)
    except CommandExpired as error:
        if not error.started:
            failure = Failure("command_timeout", "invoke", str(error), "retry")
            return Invocation(_failure_payload(settings, request, failure), 1, True)
        return _invocation_loss(settings, request, staging, "The command deadline expired")
    except KeyboardInterrupt:
        return _invocation_loss(settings, request, staging, "The caller cancelled the invocation")
    except OSError as error:
        failure = Failure(
            "transport_failure",
            "invoke",
            f"SSH could not start the remote invocation: {error}",
            "retry",
            ("name",),
        )
        return Invocation(_failure_payload(settings, request, failure), 1, True)
    _write_stderr(result.stderr, request.report)
    if result.returncode == 255:
        return _invocation_loss(settings, request, staging, "SSH exited 255")
    try:
        payload = _json_object(result.stdout)
        exit_code = _validate_host_payload(payload, result.returncode, settings, request)
    except ProtocolFailure as error:
        return _protocol_failure(settings, request, staging, str(error))
    if (
        request.report == "summary"
        and len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        >= 512 * 1024
    ):
        return _protocol_failure(
            settings, request, staging, "The host summary exceeds the supported transport bound"
        )
    return Invocation(payload, exit_code, True)


def _cleanup(
    settings: RemoteSettings,
    staging: PurePosixPath,
    deadline: Deadline,
    mode: ReportMode = "detail",
) -> str | None:
    command = shlex.join(["rm", "-rf", "--", str(staging)])
    try:
        result = _ssh(settings, command, deadline)
    except (CommandExpired, KeyboardInterrupt):
        return "The command deadline expired before cleanup completed"
    except OSError as error:
        return f"Cleanup could not start: {error}"
    _write_stderr(result.stderr, mode)
    if result.returncode != 0:
        return f"Cleanup exited {result.returncode}"
    return None


def _add_cleanup_warning(
    payload: dict[str, object],
    staging: PurePosixPath,
    detail: str,
) -> dict[str, object]:
    updated = dict(payload)
    warnings = cast(list[object], updated.get("warnings", []))
    updated["warnings"] = [
        *warnings,
        "Remote staging cleanup failed; inspect the retained incoming directory",
    ]
    transport: dict[str, object] = {
        "cleanup": "failed",
        "staging": str(staging),
        "detail": detail,
    }
    updated["transport"] = transport
    report = updated.get("report")
    if isinstance(report, dict):
        typed_report = cast(dict[str, object], report)
        mode = typed_report.get("mode")
        if mode in ("detail", "summary"):
            counts = bound_report_text(transport, "detail", mode)
            text_fields = typed_report.get("text")
            if counts is not None and isinstance(text_fields, dict):
                cast(dict[str, object], text_fields)["/transport/detail"] = counts
    return updated


def _transfer_failure(
    settings: RemoteSettings,
    request: ArtifactRequest,
    detail: str,
) -> dict[str, object]:
    required_inputs = (
        ("source", "request_id", "expected_revision")
        if request.operation == "publish"
        else ("source", "expected_revision")
    )
    return _failure_payload(
        settings,
        request,
        Failure(
            "transport_failure",
            "transfer",
            "Transfer failed before invocation",
            "retry",
            required_inputs,
        ),
        details={"transport": {"detail": detail}},
    )


def _transport_deadline(deadline: Deadline, command_seconds: float) -> Deadline:
    reserve = min(5.0, command_seconds / 2.0)
    return Deadline(deadline.expires_at - reserve)


def _run_artifact(
    settings: RemoteSettings,
    request: ArtifactRequest,
    deadline: Deadline,
) -> int:
    staging = settings.incoming_root / uuid.uuid4().hex
    remote_source = staging / ("source" if request.input_format == "markdown" else "site")
    try:
        with tempfile.TemporaryDirectory(prefix="html-publish-remote-") as temporary:
            workspace = Path(temporary)
            os.chmod(workspace, 0o700)
            limits = Limits(command_seconds=settings.command_seconds)
            if request.input_format == "markdown":
                captured_source = capture_source(request.source, workspace, limits, deadline)
                captured = captured_source
                if captured_source.kind == "file":
                    remote_source /= request.source.name
            else:
                captured = capture(request.source, workspace, "sha1", limits, deadline)
            transport_deadline = _transport_deadline(deadline, settings.command_seconds)
            mkdir = (
                shlex.join(["umask", "077"]) + " && " + shlex.join(["mkdir", "--", str(staging)])
            )
            try:
                setup = _ssh(settings, mkdir, transport_deadline)
            except (CommandExpired, OSError, KeyboardInterrupt) as error:
                payload = _transfer_failure(settings, request, str(error))
                cleanup_error = _cleanup(settings, staging, deadline, request.report)
                if cleanup_error is not None:
                    payload = _add_cleanup_warning(payload, staging, cleanup_error)
                return emit_json(payload, 1)
            _write_stderr(setup.stderr, request.report)
            if setup.returncode != 0:
                payload = _transfer_failure(settings, request, f"setup exited {setup.returncode}")
                cleanup_error = _cleanup(settings, staging, deadline, request.report)
                if cleanup_error is not None:
                    payload = _add_cleanup_warning(payload, staging, cleanup_error)
                return emit_json(payload, 1)
            destination = f"{settings.host}:{staging}/"
            try:
                transfer = _run(
                    [
                        "scp",
                        *settings.ssh_options(transport_deadline),
                        "-r",
                        "--",
                        str(captured.root),
                        destination,
                    ],
                    transport_deadline,
                )
            except (CommandExpired, PublishError, OSError, KeyboardInterrupt) as error:
                payload = _transfer_failure(settings, request, str(error))
                cleanup_error = _cleanup(settings, staging, deadline, request.report)
                if cleanup_error is not None:
                    payload = _add_cleanup_warning(payload, staging, cleanup_error)
                return emit_json(payload, 1)
            _write_stderr(transfer.stderr, request.report)
            if transfer.returncode != 0:
                payload = _transfer_failure(settings, request, f"scp exited {transfer.returncode}")
                cleanup_error = _cleanup(settings, staging, deadline, request.report)
                if cleanup_error is not None:
                    payload = _add_cleanup_warning(payload, staging, cleanup_error)
                return emit_json(payload, 1)
            invocation = _invoke(
                settings,
                request,
                transport_deadline,
                staging,
                remote_source,
            )
            payload = invocation.payload
            if invocation.cleanup_allowed:
                cleanup_error = _cleanup(settings, staging, deadline, request.report)
                if cleanup_error is not None:
                    payload = _add_cleanup_warning(payload, staging, cleanup_error)
            return emit_json(payload, invocation.exit_code)
    except (OSError, PublishError) as error:
        failure = (
            error.failure
            if isinstance(error, PublishError)
            else Failure("capture_failure", "capture", str(error), "fix_input")
        )
        return emit_json(_failure_payload(settings, request, failure), 1)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = _parser("--json" in arguments)
        parsed = parser.parse_args(arguments)
        if parsed.operation == "schema":
            return emit_json(command_schema(parser, "html-publish-remote"), 0)
        settings, request = _parse(arguments)
    except UsageFailure as error:
        failure = Failure("invalid_usage", "usage", str(error), "fix_arguments")
        return emit_json(_usage_payload(arguments, failure), 2)
    except PublishError as error:
        return emit_json(_usage_payload(arguments, error.failure), 1)
    deadline = Deadline.start(settings.command_seconds)
    if isinstance(request, ArtifactRequest):
        return _run_artifact(settings, request, deadline)
    invocation = _invoke(settings, request, deadline)
    return emit_json(invocation.payload, invocation.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
