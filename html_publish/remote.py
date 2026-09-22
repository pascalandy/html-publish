from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn, cast

from html_publish.artifact import capture
from html_publish.model import Deadline, Limits, PublishError

DEFAULT_HOST = "pascal@om1.donkey-arcturus.ts.net"
DEFAULT_EXECUTABLE = "/home/pascal/.local/share/html-publish/current/.venv/bin/html-publish"
DEFAULT_CONFIG = "/home/pascal/.config/html-publish/publisher.json"
DEFAULT_TARGET = "https://om1.donkey-arcturus.ts.net:8444/html-publish/"
DEFAULT_INCOMING_ROOT = "/home/pascal/.local/share/html-publish/incoming"
DEFAULT_CONNECT_TIMEOUT = 10

Operation = Literal["plan", "publish", "status"]
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
HOST_PATTERN = re.compile(r"[A-Za-z0-9_.@-]+\Z")
REMOTE_PATH_PATTERN = re.compile(r"/[A-Za-z0-9._/-]+\Z")


class UsageFailure(Exception):
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

    def ssh_options(self) -> tuple[str, ...]:
        return (
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
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
    name: str


Request = ArtifactRequest | StatusRequest


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


def _parser() -> Parser:
    parser = Parser(
        prog="python -m html_publish.remote",
        description="Capture a local artifact and run html-publish on the controlled om1 host",
    )
    parser.add_argument("--host", type=_host, default=DEFAULT_HOST)
    parser.add_argument("--remote-executable", default=DEFAULT_EXECUTABLE)
    parser.add_argument("--remote-config", default=DEFAULT_CONFIG)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument(
        "--incoming-root",
        type=_remote_path,
        default=PurePosixPath(DEFAULT_INCOMING_ROOT),
    )
    parser.add_argument(
        "--connect-timeout",
        type=_connect_timeout,
        default=DEFAULT_CONNECT_TIMEOUT,
    )
    commands = parser.add_subparsers(dest="operation", required=True)

    for operation in ("plan", "publish"):
        command = commands.add_parser(operation)
        command.add_argument("--name", required=True, type=_name)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("--expected-revision")
        if operation == "publish":
            command.add_argument("--request-id")

    status = commands.add_parser("status")
    status.add_argument("--name", required=True, type=_name)
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
    )
    operation = cast(Operation, parsed.operation)
    name = cast(str, parsed.name)
    if operation == "status":
        return settings, StatusRequest("status", name)
    request_id = cast(str | None, getattr(parsed, "request_id", None))
    if operation == "publish" and request_id is None:
        request_id = f"remote-{uuid.uuid4().hex}"
    return settings, ArtifactRequest(
        operation,
        name,
        cast(Path, parsed.source),
        cast(str | None, parsed.expected_revision),
        request_id,
    )


def _operation(arguments: list[str]) -> Operation | Literal["usage"]:
    for value in arguments:
        if value in {"plan", "publish", "status"}:
            return cast(Operation, value)
    return "usage"


def _error_payload(
    operation: Operation | Literal["usage"],
    code: str,
    phase: str,
    message: str,
    next_action: str,
    publication_may_have_started: bool,
    request_id: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operation": operation,
        "request_id": request_id,
        "outcome": "error",
        "publication_may_have_started": publication_may_have_started,
        "error": {
            "code": code,
            "phase": phase,
            "message": message,
            "next_action": next_action,
            "required_inputs": [],
        },
    }


def _print_error(payload: dict[str, object], exit_code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, capture_output=True, check=False)


def _write_stderr(data: bytes) -> None:
    if data:
        sys.stderr.buffer.write(data)
        sys.stderr.buffer.flush()


def _ssh(settings: RemoteSettings, remote_command: str) -> subprocess.CompletedProcess[bytes]:
    return _run(["ssh", *settings.ssh_options(), settings.host, remote_command])


def _transport_error(
    request: Request,
    phase: str,
    result: subprocess.CompletedProcess[bytes] | OSError,
) -> int:
    request_id = request.request_id if isinstance(request, ArtifactRequest) else None
    if isinstance(result, OSError):
        detail = str(result)
        exit_code = 1
    else:
        _write_stderr(result.stderr)
        detail = f"exit {result.returncode}"
        exit_code = result.returncode or 1
    return _print_error(
        _error_payload(
            request.operation,
            "transport_failure",
            phase,
            f"The remote transport failed before publication invocation: {detail}",
            "retry",
            False,
            request_id,
        ),
        exit_code,
    )


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
        "--name",
        request.name,
    ]
    if isinstance(request, StatusRequest):
        return arguments
    assert remote_source is not None
    arguments.extend(["--source", str(remote_source), "--target", settings.target])
    if request.expected_revision is not None:
        arguments.extend(["--expected-revision", request.expected_revision])
    if request.request_id is not None:
        arguments.extend(["--request-id", request.request_id])
    return arguments


def _relay_invocation(
    settings: RemoteSettings,
    request: Request,
    remote_command: str,
) -> int:
    request_id = request.request_id if isinstance(request, ArtifactRequest) else None
    try:
        result = _ssh(settings, remote_command)
    except OSError as error:
        return _print_error(
            _error_payload(
                request.operation,
                "transport_failure",
                "invoke",
                f"SSH could not start the remote invocation: {error}",
                "retry",
                False,
                request_id,
            ),
            1,
        )
    _write_stderr(result.stderr)
    if result.returncode == 255:
        may_have_started = request.operation == "publish"
        return _print_error(
            _error_payload(
                request.operation,
                "publication_outcome_unknown" if may_have_started else "transport_failure",
                "invoke",
                "SSH lost the remote command result after invocation started",
                "check_status" if may_have_started else "retry",
                may_have_started,
                request_id,
            ),
            result.returncode,
        )
    sys.stdout.buffer.write(result.stdout)
    sys.stdout.buffer.flush()
    return result.returncode


def _cleanup(settings: RemoteSettings, staging: PurePosixPath) -> None:
    cleanup = shlex.join(["rm", "-rf", "--", str(staging)])
    with contextlib.suppress(OSError):
        _ssh(settings, cleanup)


def _run_artifact(settings: RemoteSettings, request: ArtifactRequest) -> int:
    staging = settings.incoming_root / uuid.uuid4().hex
    remote_source = staging / "site"
    request_id = request.request_id
    try:
        with tempfile.TemporaryDirectory(prefix="html-publish-remote-") as temporary:
            workspace = Path(temporary)
            os.chmod(workspace, 0o700)
            captured = capture(
                request.source,
                workspace,
                "sha1",
                Limits(),
                Deadline.start(Limits().command_seconds),
            )
            mkdir = (
                shlex.join(["umask", "077"]) + " && " + shlex.join(["mkdir", "--", str(staging)])
            )
            try:
                mkdir_result = _ssh(settings, mkdir)
            except OSError as error:
                _cleanup(settings, staging)
                return _transport_error(request, "transfer", error)
            if mkdir_result.returncode != 0:
                _cleanup(settings, staging)
                return _transport_error(request, "transfer", mkdir_result)
            destination = f"{settings.host}:{staging}/"
            try:
                transfer = _run(
                    [
                        "scp",
                        *settings.ssh_options(),
                        "-r",
                        "--",
                        str(captured.root),
                        destination,
                    ]
                )
            except OSError as error:
                _cleanup(settings, staging)
                return _transport_error(request, "transfer", error)
            if transfer.returncode != 0:
                _cleanup(settings, staging)
                return _transport_error(request, "transfer", transfer)
            cleanup = shlex.join(["rm", "-rf", "--", str(staging)])
            invocation = shlex.join(_remote_arguments(settings, request, remote_source))
            remote_command = f"trap {shlex.quote(cleanup)} EXIT; {invocation}"
            return _relay_invocation(settings, request, remote_command)
    except (OSError, PublishError) as error:
        if isinstance(error, PublishError):
            failure = error.failure
            code = failure.code
            phase = failure.phase
            message = failure.message
            next_action = failure.next_action
        else:
            code = "capture_failure"
            phase = "capture"
            message = str(error)
            next_action = "fix_input"
        return _print_error(
            _error_payload(
                request.operation,
                code,
                phase,
                message,
                next_action,
                False,
                request_id,
            ),
            1,
        )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        settings, request = _parse(arguments)
    except UsageFailure as error:
        return _print_error(
            _error_payload(
                _operation(arguments),
                "invalid_usage",
                "usage",
                str(error),
                "fix_arguments",
                False,
            ),
            2,
        )
    if isinstance(request, ArtifactRequest):
        return _run_artifact(settings, request)
    remote_command = shlex.join(_remote_arguments(settings, request))
    return _relay_invocation(settings, request, remote_command)


if __name__ == "__main__":
    raise SystemExit(main())
