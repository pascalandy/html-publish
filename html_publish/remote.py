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

from html_publish.artifact import capture
from html_publish.cli import emit_json, report_dict, usage_report
from html_publish.delivery import publication_url
from html_publish.model import (
    Deadline,
    Effects,
    Failure,
    Limits,
    Name,
    Operation,
    PublishError,
    Report,
    Revision,
)

DEFAULT_HOST = "pascal@om1.donkey-arcturus.ts.net"
DEFAULT_EXECUTABLE = "/home/pascal/.local/share/html-publish/current/.venv/bin/html-publish"
DEFAULT_CONFIG = "/home/pascal/.config/html-publish/publisher.json"
DEFAULT_TARGET = "https://om1.donkey-arcturus.ts.net:8444/html-publish/"
DEFAULT_INCOMING_ROOT = "/home/pascal/.local/share/html-publish/incoming"
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_COMMAND_SECONDS = 120.0

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


@dataclass(frozen=True)
class StatusRequest:
    operation: Literal["status"]
    name: str | None
    after: str | None
    limit: int
    host_check: bool


@dataclass(frozen=True)
class VerifyRequest:
    operation: Literal["verify"]
    name: str


@dataclass(frozen=True)
class HistoryRequest:
    operation: Literal["history"]
    name: str
    after: str | None
    limit: int
    diff_revision: str | None


@dataclass(frozen=True)
class RestoreRequest:
    operation: Literal["restore"]
    name: str
    archive_commit: str
    expected_revision: str | None
    request_id: str


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


def _parser() -> Parser:
    parser = Parser(
        prog="html-publish-remote",
        description="Run the html-publish JSON contract through the controlled SSH transport",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  html-publish-remote publish --name release-notes --source ./release-notes.html
  html-publish-remote status --limit 20
  html-publish-remote verify --name release-notes""",
    )
    parser.add_argument(
        "--host", type=_host, default=DEFAULT_HOST, help="SSH destination as user@host"
    )
    parser.add_argument(
        "--remote-executable", default=DEFAULT_EXECUTABLE, help="host html-publish executable path"
    )
    parser.add_argument(
        "--remote-config", default=DEFAULT_CONFIG, help="host publisher JSON configuration path"
    )
    parser.add_argument(
        "--target",
        default=DEFAULT_TARGET,
        help="publication base URL matching the host configuration",
    )
    parser.add_argument(
        "--incoming-root",
        type=_remote_path,
        default=PurePosixPath(DEFAULT_INCOMING_ROOT),
        help="private host directory for unique plan and publish upload stages",
    )
    parser.add_argument(
        "--connect-timeout",
        type=_connect_timeout,
        default=DEFAULT_CONNECT_TIMEOUT,
        help="SSH connection budget in seconds, capped by remaining command time "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--command-seconds",
        type=_positive_seconds,
        default=DEFAULT_COMMAND_SECONDS,
        help="total client budget for capture, transport, and cleanup in seconds "
        "(default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="operation", required=True)

    for operation, help_text in (
        ("plan", "capture and inspect without publication changes"),
        ("publish", "archive, activate, and verify a finished artifact"),
    ):
        command = commands.add_parser(operation, help=help_text)
        command.add_argument("--name", required=True, type=_name)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("--expected-revision")
        if operation == "publish":
            command.add_argument("--request-id")

    status = commands.add_parser(
        "status",
        help="observe one publication or a paged list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""continuation example:
  html-publish-remote status --after '<continuation>' --limit 20""",
    )
    status.add_argument("--name", type=_name)
    status.add_argument(
        "--after", type=_name, help="opaque continuation from the prior status page"
    )
    status.add_argument("--limit", type=_positive_limit, default=100)
    status.add_argument("--host-check", action="store_true")

    verify = commands.add_parser("verify", help="check selected files and host HTTP delivery")
    verify.add_argument("--name", required=True, type=_name)

    history = commands.add_parser(
        "history",
        help="list per-name history and optional text differences",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""continuation example:
  html-publish-remote history --name release-notes --after '<continuation>' --limit 5""",
    )
    history.add_argument("--name", required=True, type=_name)
    history.add_argument("--after", help="opaque continuation from history for the same name")
    history.add_argument("--limit", type=_positive_limit, default=20)
    history.add_argument("--diff", dest="diff_revision")

    restore = commands.add_parser(
        "restore", help="select an archived revision under a revision guard"
    )
    restore.add_argument("--name", required=True, type=_name)
    restore.add_argument("--archive-commit", required=True)
    restore.add_argument("--expected-revision")
    restore.add_argument("--request-id")
    return parser


def _parse(arguments: list[str]) -> tuple[RemoteSettings, Request]:
    parsed = _parser().parse_args(arguments)
    settings = RemoteSettings(
        cast(str, parsed.host),
        cast(str, parsed.remote_executable),
        cast(str, parsed.remote_config),
        cast(str, parsed.target),
        cast(PurePosixPath, parsed.incoming_root),
        cast(int, parsed.connect_timeout),
        cast(float, parsed.command_seconds),
    )
    operation = cast(Operation, parsed.operation)
    if operation == "status":
        return settings, StatusRequest(
            "status",
            cast(str | None, parsed.name),
            cast(str | None, parsed.after),
            cast(int, parsed.limit),
            cast(bool, parsed.host_check),
        )
    name = cast(str, parsed.name)
    if operation == "verify":
        return settings, VerifyRequest("verify", name)
    if operation == "history":
        return settings, HistoryRequest(
            "history",
            name,
            cast(str | None, parsed.after),
            cast(int, parsed.limit),
            cast(str | None, parsed.diff_revision),
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
        )
    return settings, ArtifactRequest(
        operation,
        name,
        cast(Path, parsed.source),
        cast(str | None, parsed.expected_revision),
        request_id,
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
    raw_name = _argument_value(arguments, "--name")
    try:
        name = Name(_name(raw_name)) if raw_name is not None else None
    except argparse.ArgumentTypeError:
        name = None
    target = _argument_value(arguments, "--target") or DEFAULT_TARGET
    try:
        parsed_target = urlparse(target)
        valid_target = parsed_target.scheme in {"http", "https"} and bool(parsed_target.hostname)
    except ValueError:
        valid_target = False
    request_id = _argument_value(arguments, "--request-id")
    expected_revision = _argument_value(arguments, "--expected-revision")
    return usage_report(
        operation,
        failure,
        target=target if valid_target else None,
        name=name,
        request_id=request_id,
        expected_revision=Revision(expected_revision) if expected_revision is not None else None,
    )


def _request_id(request: Request) -> str | None:
    if isinstance(request, (ArtifactRequest, RestoreRequest)):
        return request.request_id
    return None


def _expected_revision(request: Request) -> str | None:
    if isinstance(request, (ArtifactRequest, RestoreRequest)):
        return request.expected_revision
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
            effects=effects or Effects(),
            error=failure,
            details=details or {},
        )
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


def _write_stderr(data: bytes) -> None:
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
    ]
    if isinstance(request, ArtifactRequest):
        assert remote_source is not None
        arguments.extend(
            ["--name", request.name, "--source", str(remote_source), "--target", settings.target]
        )
        if request.expected_revision is not None:
            arguments.extend(["--expected-revision", request.expected_revision])
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
    value: object, report: Mapping[str, object] | None = None
) -> dict[str, object] | None:
    saved_revision: object = None
    archive_commit: object = None
    active_revision: object = None
    selection: dict[str, object] | None = None
    if value is not None:
        observation = _report_object(value, {"saved", "selection"}, "observation")
        if observation["saved"] is not None:
            saved = _report_object(
                observation["saved"], {"revision", "archive_commit"}, "saved state"
            )
            if any(
                not isinstance(saved[key], str) or not saved[key]
                for key in ("revision", "archive_commit")
            ):
                raise ProtocolFailure(
                    "The host result saved revision and commit must be nonempty text"
                )
            saved_revision, archive_commit = saved["revision"], saved["archive_commit"]
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
        or report["archive_commit"] != archive_commit
        or report["active_revision"] != active_revision
    ):
        raise ProtocolFailure("The host result revisions disagree with its observation")
    return selection


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
    if any(
        payload[key] is not None and not isinstance(payload[key], str)
        for key in ("requested_revision", "archived_revision", "archive_commit", "active_revision")
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
    if outcome == "published" and effect_values["activated"] is not True:
        raise ProtocolFailure("The published host result does not report activation")
    if (outcome == "unchanged" or exit_code == 2) and any(
        effect_values[key] is not False for key in ("archive_advanced", "activated")
    ):
        raise ProtocolFailure("The host result effects disagree with its outcome")
    verification = _validate_verification(payload["verification"])
    selection = _validate_observation(payload["observation"], payload)
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
    _write_stderr(result.stderr)
    if result.returncode == 255:
        return _invocation_loss(settings, request, staging, "SSH exited 255")
    try:
        payload = _json_object(result.stdout)
        exit_code = _validate_host_payload(payload, result.returncode, settings, request)
    except ProtocolFailure as error:
        return _protocol_failure(settings, request, staging, str(error))
    return Invocation(payload, exit_code, True)


def _cleanup(
    settings: RemoteSettings,
    staging: PurePosixPath,
    deadline: Deadline,
) -> str | None:
    command = shlex.join(["rm", "-rf", "--", str(staging)])
    try:
        result = _ssh(settings, command, deadline)
    except (CommandExpired, KeyboardInterrupt):
        return "The command deadline expired before cleanup completed"
    except OSError as error:
        return f"Cleanup could not start: {error}"
    _write_stderr(result.stderr)
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
    updated["transport"] = {
        "cleanup": "failed",
        "staging": str(staging),
        "detail": detail,
    }
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
    remote_source = staging / "site"
    try:
        with tempfile.TemporaryDirectory(prefix="html-publish-remote-") as temporary:
            workspace = Path(temporary)
            os.chmod(workspace, 0o700)
            captured = capture(
                request.source,
                workspace,
                "sha1",
                Limits(command_seconds=settings.command_seconds),
                deadline,
            )
            transport_deadline = _transport_deadline(deadline, settings.command_seconds)
            mkdir = (
                shlex.join(["umask", "077"]) + " && " + shlex.join(["mkdir", "--", str(staging)])
            )
            try:
                setup = _ssh(settings, mkdir, transport_deadline)
            except (CommandExpired, OSError, KeyboardInterrupt) as error:
                payload = _transfer_failure(settings, request, str(error))
                cleanup_error = _cleanup(settings, staging, deadline)
                if cleanup_error is not None:
                    payload = _add_cleanup_warning(payload, staging, cleanup_error)
                return emit_json(payload, 1)
            _write_stderr(setup.stderr)
            if setup.returncode != 0:
                payload = _transfer_failure(settings, request, f"setup exited {setup.returncode}")
                cleanup_error = _cleanup(settings, staging, deadline)
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
                cleanup_error = _cleanup(settings, staging, deadline)
                if cleanup_error is not None:
                    payload = _add_cleanup_warning(payload, staging, cleanup_error)
                return emit_json(payload, 1)
            _write_stderr(transfer.stderr)
            if transfer.returncode != 0:
                payload = _transfer_failure(settings, request, f"scp exited {transfer.returncode}")
                cleanup_error = _cleanup(settings, staging, deadline)
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
                cleanup_error = _cleanup(settings, staging, deadline)
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
        settings, request = _parse(arguments)
    except UsageFailure as error:
        failure = Failure("invalid_usage", "usage", str(error), "fix_arguments")
        return emit_json(_usage_payload(arguments, failure), 2)
    deadline = Deadline.start(settings.command_seconds)
    if isinstance(request, ArtifactRequest):
        return _run_artifact(settings, request, deadline)
    invocation = _invoke(settings, request, deadline)
    return emit_json(invocation.payload, invocation.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
