from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
import signal
import sys
from collections.abc import Generator, Mapping
from dataclasses import asdict
from pathlib import Path
from types import FrameType
from typing import Literal, NoReturn, cast
from urllib.parse import urlparse

from html_publish import __version__, _git
from html_publish.configuration import (
    ClientConfig,
    data_root,
    init_document,
    read_document,
    selected_path,
)
from html_publish.delivery import publication_url
from html_publish.discovery import (
    command_schema,
    register_command,
    version_payload,
)
from html_publish.model import (
    Config,
    Deadline,
    Effects,
    Failure,
    Limits,
    LocalState,
    Name,
    Operation,
    PublishError,
    Report,
    Revision,
    Selection,
    Verification,
)
from html_publish.store import PublicationStore

NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


OPERATIONS = frozenset({"plan", "publish", "status", "verify", "history", "restore"})


class UsageFailure(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageFailure(message)


@contextlib.contextmanager
def _command_alarm(seconds: float) -> Generator[None, None, None]:
    if not hasattr(signal, "setitimer"):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def timeout(_signum: int, _frame: FrameType | None) -> NoReturn:
        raise PublishError(
            "command_timeout",
            "timeout",
            "The command exceeded its total time budget",
            "retry",
        )

    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _name(value: str) -> Name:
    if len(value) > 80 or not NAME_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "name must use lowercase letters, digits, and single hyphens, up to 80 characters"
        )
    return Name(value)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 100:
        raise argparse.ArgumentTypeError("value must be between 1 and 100")
    return parsed


def _positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("command seconds must be positive") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("command seconds must be positive")
    return parsed


def _globals(command: argparse.ArgumentParser, version: str) -> None:
    command.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="JSON configuration file; defaults to the role's user config",
    )
    command.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="write one JSON object to stdout",
    )
    command.add_argument(
        "--version", action="version", version=version, help="show installed version"
    )
    command.add_argument(
        "--command-seconds",
        type=_positive_seconds,
        default=argparse.SUPPRESS,
        help="override total command budget in seconds "
        "(default: configuration limit, normally 120)",
    )


def _parser(json_version: bool = False) -> Parser:
    version = (
        json.dumps(version_payload("html-publish"), separators=(",", ":"))
        if json_version
        else f"html-publish {__version__}"
    )
    parser = Parser(
        prog="html-publish",
        description="Publish private static HTML with a stable URL and local Git history. "
        "Preview with plan, publish under a revision guard, then inspect with status or verify.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="JSON configuration file; defaults to the role's user config",
    )
    parser.add_argument("--json", action="store_true", help="write one JSON object to stdout")
    parser.add_argument(
        "--version", action="version", version=version, help="show installed version"
    )
    parser.add_argument(
        "--command-seconds",
        type=_positive_seconds,
        help="override total command budget in seconds "
        "(default: configuration limit, normally 120)",
    )
    commands = parser.add_subparsers(dest="operation", required=True)

    plan = register_command(
        commands,
        "plan",
        "capture and inspect without persistent writes",
        examples=(
            "html-publish --config publisher.json plan --name release-notes "
            "--source ./page.html --target https://host.example/pages/",
        ),
        effects=("reads source and saved state", "does not change archive or selection"),
    )
    plan.add_argument(
        "--name",
        required=True,
        type=_name,
        help="publication name (lowercase letters, digits, single hyphens; max 80)",
    )
    plan.add_argument(
        "--source", required=True, type=Path, help="HTML file or directory to capture"
    )
    plan.add_argument(
        "--target", required=True, help="publication base URL (must match configuration)"
    )
    plan.add_argument(
        "--expected-revision",
        type=Revision,
        help="expected active revision used for the prediction; "
        "omit for a first publication or identical retry",
    )
    _globals(plan, version)

    publish = register_command(
        commands,
        "publish",
        "archive, activate, and verify an artifact",
        examples=(
            "html-publish --config publisher.json --json publish --name release-notes "
            "--source ./page.html --target https://host.example/pages/",
        ),
        effects=("may advance archive", "may activate page", "probes delivery"),
    )
    publish.add_argument(
        "--name",
        required=True,
        type=_name,
        help="publication name (lowercase letters, digits, single hyphens; max 80)",
    )
    publish.add_argument(
        "--source", required=True, type=Path, help="HTML file or directory to capture"
    )
    publish.add_argument(
        "--target", required=True, help="publication base URL (must match configuration)"
    )
    publish.add_argument(
        "--expected-revision",
        type=Revision,
        help="expected active revision to replace different content; "
        "omit for a first publication or identical retry",
    )
    publish.add_argument(
        "--request-id", help="caller attempt ID echoed in the result for reconciliation"
    )
    _globals(publish, version)

    status = register_command(
        commands,
        "status",
        "observe bounded local state",
        examples=("html-publish --config publisher.json --json status --name release-notes",),
        effects=(
            "reads state",
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
        type=_positive_int,
        default=100,
        help="page size, 1 to 100 (default: %(default)s)",
    )
    status.add_argument(
        "--host-check",
        action="store_true",
        help="also validate selected bytes and probe delivery for a named page",
    )
    _globals(status, version)

    verify = register_command(
        commands,
        "verify",
        "validate the selected export and probe delivery",
        examples=("html-publish --config publisher.json --json verify --name release-notes",),
        effects=("reads saved bytes and HTTP delivery", "does not activate"),
    )
    verify.add_argument("--name", required=True, type=_name, help="publication name")
    _globals(verify, version)

    history = register_command(
        commands,
        "history",
        "list bounded publication history for a name",
        examples=("html-publish --config publisher.json --json history --name release-notes",),
        effects=("reads Git history", "does not change archive or selection"),
    )
    history.add_argument("--name", required=True, type=_name, help="publication name")
    history.add_argument(
        "--limit", type=_positive_int, default=20, help="page size, 1 to 100 (default: %(default)s)"
    )
    history.add_argument("--after", help="opaque continuation from history for the same name")
    history.add_argument(
        "--diff",
        dest="diff_revision",
        help="reachable archived revision to compare with the latest page tree at HEAD "
        "(UTF-8 diff text capped at 64 KiB)",
    )
    _globals(history, version)

    restore = register_command(
        commands,
        "restore",
        "select a saved revision at a reachable commit",
        examples=(
            "html-publish --config publisher.json --json restore --name release-notes "
            "--archive-commit COMMIT --target https://host.example/pages/ "
            "--expected-revision REVISION",
        ),
        effects=("may append archive history", "may activate saved revision", "probes delivery"),
    )
    restore.add_argument("--name", required=True, type=_name, help="publication name")
    restore.add_argument(
        "--archive-commit",
        required=True,
        help="reachable commit containing the revision to restore",
    )
    restore.add_argument(
        "--target", required=True, help="publication base URL (must match configuration)"
    )
    restore.add_argument(
        "--expected-revision", type=Revision, help="expected active revision for guarded restore"
    )
    restore.add_argument(
        "--request-id", help="caller attempt ID echoed in the result for reconciliation"
    )
    _globals(restore, version)

    schema = register_command(
        commands,
        "schema",
        "print parser-derived command discovery as JSON",
        examples=("html-publish schema",),
        effects=("reads command definitions only",),
    )
    _globals(schema, version)

    config_command = register_command(
        commands,
        "config",
        "create or inspect explicit publisher and client configuration",
        examples=("html-publish config validate --role publisher",),
        effects=("init writes only the selected config file", "show and validate read only"),
    )
    _globals(config_command, version)
    config_actions = config_command.add_subparsers(dest="config_action", required=True)
    for action in ("init", "show", "validate"):
        item = register_command(
            config_actions,
            action,
            f"{action} a publisher or client configuration",
            examples=(
                "html-publish config init --role publisher --config publisher.json "
                "--base-url https://review.example/pages/"
                if action == "init"
                else f"html-publish config {action} --role publisher --config publisher.json",
            ),
            effects=("writes only the selected config file",)
            if action == "init"
            else ("reads configuration only",),
        )
        item.add_argument("--role", choices=("publisher", "client"), required=True)
        if action == "init":
            item.add_argument("--base-url", required=True, help="canonical target URL ending in /")
            item.add_argument("--archive", type=Path, help="publisher bare Git archive path")
            item.add_argument("--runtime", type=Path, help="publisher runtime directory")
            item.add_argument(
                "--allow-http", action="store_true", help="allow an HTTP loopback test target"
            )
            item.add_argument("--target-id", help="stable client target identity")
            item.add_argument(
                "--execution", choices=("local", "remote"), help="client execution kind"
            )
            item.add_argument(
                "--publisher-config", type=Path, help="absolute local publisher config path"
            )
            item.add_argument("--host", help="SSH destination as user@host")
            item.add_argument(
                "--remote-executable", help="absolute publisher executable path on SSH host"
            )
            item.add_argument("--remote-config", help="absolute publisher config path on SSH host")
            item.add_argument(
                "--incoming-root", help="absolute private incoming directory on SSH host"
            )
        _globals(item, version)

    doctor = register_command(
        commands,
        "doctor",
        "inspect local prerequisites; opt in to network reads",
        examples=("html-publish doctor --role publisher --config publisher.json --json",),
        effects=(
            "reads local configuration and prerequisites",
            "--network probes configured SSH and HTTP targets",
            "never repairs state",
        ),
    )
    doctor.add_argument("--role", choices=("publisher", "client"), required=True)
    doctor.add_argument("--network", action="store_true", help="allow bounded SSH and HTTP reads")
    _globals(doctor, version)
    return parser


def _require_object(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise PublishError(
            "invalid_config",
            "config",
            f"{label} must be a JSON object",
            "fix_config",
        )
    raw_dict = cast(dict[object, object], raw)
    if not all(isinstance(key, str) for key in raw_dict):
        raise PublishError(
            "invalid_config",
            "config",
            f"{label} keys must be strings",
            "fix_config",
        )
    return cast(dict[str, object], raw_dict)


def _number(raw: object, default: float, label: str) -> float:
    if raw is None:
        return default
    if (
        not isinstance(raw, (int, float))
        or isinstance(raw, bool)
        or not math.isfinite(raw)
        or raw <= 0
    ):
        raise PublishError(
            "invalid_config",
            "config",
            f"{label} must be a positive number",
            "fix_config",
        )
    return float(raw)


def _integer(raw: object, default: int, label: str) -> int:
    if raw is None:
        return default
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise PublishError(
            "invalid_config",
            "config",
            f"{label} must be a positive integer",
            "fix_config",
        )
    return raw


def load_config(path: Path) -> Config:
    try:
        raw = _require_object(json.loads(path.read_text(encoding="utf-8")), "configuration")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PublishError(
            "invalid_config",
            "config",
            f"The publisher configuration at {path} could not be read: {error}",
            "fix_config",
        ) from error
    return parse_publisher(raw)


def parse_publisher(raw: dict[str, object]) -> Config:
    allowed = {"archive", "runtime", "base_url", "allow_http", "object_format", "limits"}
    unknown = set(raw) - allowed
    if unknown:
        raise PublishError(
            "invalid_config",
            "config",
            f"Unknown configuration fields: {', '.join(sorted(unknown))}",
            "fix_config",
        )
    try:
        archive_value = raw["archive"]
        runtime_value = raw["runtime"]
        base_value = raw["base_url"]
    except KeyError as error:
        raise PublishError(
            "invalid_config",
            "config",
            f"Missing configuration field: {error.args[0]}",
            "fix_config",
        ) from error
    if not isinstance(archive_value, str):
        raise PublishError(
            "invalid_config",
            "config",
            "archive must be a string",
            "fix_config",
        )
    if not isinstance(runtime_value, str):
        raise PublishError(
            "invalid_config",
            "config",
            "runtime must be a string",
            "fix_config",
        )
    if not isinstance(base_value, str):
        raise PublishError(
            "invalid_config",
            "config",
            "base_url must be a string",
            "fix_config",
        )
    try:
        archive = Path(archive_value).expanduser()
        runtime = Path(runtime_value).expanduser()
    except (OSError, RuntimeError) as error:
        raise PublishError(
            "invalid_config",
            "config",
            f"The configured paths could not be resolved: {error}",
            "fix_config",
        ) from error
    if not archive.is_absolute() or not runtime.is_absolute():
        raise PublishError(
            "invalid_config",
            "config",
            "archive and runtime paths must be absolute",
            "fix_config",
        )
    archive = archive.resolve(strict=False)
    runtime = runtime.resolve(strict=False)
    if archive == runtime or archive.is_relative_to(runtime) or runtime.is_relative_to(archive):
        raise PublishError(
            "invalid_config",
            "config",
            "archive and runtime paths must not overlap",
            "fix_config",
        )

    base_url = base_value
    try:
        parsed = urlparse(base_url)
        parsed_hostname = parsed.hostname
        parsed_port = parsed.port
    except ValueError as error:
        raise PublishError(
            "invalid_config",
            "config",
            f"base_url is malformed: {error}",
            "fix_config",
        ) from error
    allow_http = raw.get("allow_http", False)
    if not isinstance(allow_http, bool):
        raise PublishError(
            "invalid_config",
            "config",
            "allow_http must be a boolean",
            "fix_config",
        )
    if (
        not parsed_hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/")
    ):
        raise PublishError(
            "invalid_config",
            "config",
            "base_url must be an absolute URL ending in / without credentials, query, or fragment",
            "fix_config",
        )
    if parsed.scheme == "http":
        if not allow_http or parsed_hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise PublishError(
                "invalid_config",
                "config",
                "HTTP is allowed only for an explicit loopback test target",
                "fix_config",
            )
    elif parsed.scheme != "https":
        raise PublishError(
            "invalid_config",
            "config",
            "Production base_url must use HTTPS",
            "fix_config",
        )

    raw_object_format = raw.get("object_format", "sha1")
    if raw_object_format == "sha1":
        object_format: Literal["sha1", "sha256"] = "sha1"
    elif raw_object_format == "sha256":
        object_format = "sha256"
    else:
        raise PublishError(
            "invalid_config",
            "config",
            "object_format must be sha1 or sha256",
            "fix_config",
        )
    raw_limits = _require_object(raw.get("limits", {}), "limits")
    limit_keys = {
        "max_bytes",
        "max_files",
        "command_seconds",
        "lock_seconds",
        "verification_seconds",
    }
    unknown_limits = set(raw_limits) - limit_keys
    if unknown_limits:
        raise PublishError(
            "invalid_config",
            "config",
            f"Unknown limit fields: {', '.join(sorted(unknown_limits))}",
            "fix_config",
        )
    if parsed_port is not None and not 1 <= parsed_port <= 65_535:
        raise PublishError(
            "invalid_config",
            "config",
            "base_url port must be between 1 and 65535",
            "fix_config",
        )
    max_bytes = _integer(raw_limits.get("max_bytes"), 100 * 1024 * 1024, "max_bytes")
    max_files = _integer(raw_limits.get("max_files"), 2_000, "max_files")
    return Config(
        archive,
        runtime,
        base_url,
        allow_http,
        object_format,
        Limits(
            max_bytes,
            max_files,
            _number(raw_limits.get("command_seconds"), 120, "command_seconds"),
            _number(raw_limits.get("lock_seconds"), 30, "lock_seconds"),
            _number(raw_limits.get("verification_seconds"), 60, "verification_seconds"),
        ),
    )


def _selection_dict(selection: Selection) -> dict[str, object]:
    return {
        "state": selection.kind,
        "revision": selection.revision,
        "integrity_checked": selection.integrity_checked,
        "detail": selection.detail,
    }


def _state_dict(state: LocalState) -> dict[str, object]:
    return {
        "saved": None
        if state.saved is None
        else {
            "revision": state.saved.site.revision,
            "archive_commit": state.saved.commit,
        },
        "selection": _selection_dict(state.selection),
    }


def _verification_dict(verification: Verification) -> dict[str, object]:
    return {
        "result": verification.result,
        "revision": verification.revision,
        "checked_at": verification.checked_at,
        "probe_location": verification.probe_location,
        "files_checked": verification.files_checked,
        "bytes_checked": verification.bytes_checked,
        "scope": list(verification.scope),
        "detail": verification.detail,
    }


def _effects_dict(effects: Effects) -> dict[str, object]:
    return {
        "archive_advanced": effects.archive_advanced,
        "activated": effects.activated,
    }


def _failure_dict(failure: Failure | None) -> dict[str, object] | None:
    if failure is None:
        return None
    return {
        "code": failure.code,
        "phase": failure.phase,
        "message": failure.message,
        "next_action": {
            "kind": failure.next_action,
            "required_inputs": list(failure.required_inputs),
        },
    }


def _details_dict(details: Mapping[str, object]) -> dict[str, object]:
    return dict(details)


def report_dict(report: Report) -> dict[str, object]:
    state = report.state
    payload: dict[str, object] = {
        "schema_version": 1,
        "operation": report.operation,
        "request_id": report.request_id,
        "outcome": report.outcome,
        "target": report.target,
        "name": report.name,
        "url": report.url,
        "expected_revision": report.expected_revision,
        "requested_revision": report.requested_revision,
        "archived_revision": state.saved.site.revision if state and state.saved else None,
        "archive_commit": state.saved.commit if state and state.saved else None,
        "active_revision": state.selection.revision
        if state and state.selection.kind == "selected"
        else None,
        "effects": _effects_dict(report.effects),
        "verification": _verification_dict(report.verification),
        "warnings": list(report.warnings),
        "error": _failure_dict(report.error),
        "observation": _state_dict(state) if state else None,
    }
    payload.update(_details_dict(report.details))
    if report.status_entries or (report.operation == "status" and report.name is None):
        payload["entries"] = [
            {
                "name": entry.name,
                "url": publication_url(report.target or "", entry.name),
                "observation": _state_dict(entry.state),
            }
            for entry in report.status_entries
        ]
    return payload


def usage_report(
    operation: str | None,
    failure: Failure,
    *,
    target: str | None = None,
    name: Name | None = None,
    expected_revision: Revision | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    if operation in OPERATIONS:
        return report_dict(
            Report(
                cast(Operation, operation),
                "error",
                target,
                name,
                publication_url(target, name) if target is not None and name is not None else None,
                request_id=request_id,
                expected_revision=expected_revision,
                error=failure,
            )
        )
    return {
        "schema_version": 1,
        "operation": "usage",
        "request_id": None,
        "outcome": "error",
        "target": None,
        "name": None,
        "url": None,
        "expected_revision": None,
        "requested_revision": None,
        "archived_revision": None,
        "archive_commit": None,
        "active_revision": None,
        "effects": {"archive_advanced": False, "activated": False},
        "verification": _verification_dict(Verification()),
        "warnings": [],
        "error": _failure_dict(failure),
        "observation": None,
    }


def emit_json(payload: Mapping[str, object], exit_code: Literal[0, 1, 2]) -> int:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


def _print_report(report: Report, json_output: bool) -> int:
    payload = report_dict(report)
    if json_output:
        return emit_json(payload, 1 if report.error else 0)
    elif report.error:
        print(f"html-publish: {report.error.message}", file=sys.stderr)
    elif report.operation in {"publish", "restore"}:
        assert report.url is not None
        print(report.url)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if report.error else 0


def _config_result(
    operation: str,
    role: str | None,
    path: Path | None,
    source: str | None,
    outcome: str,
    *,
    values: Mapping[str, object] | None = None,
    origins: dict[str, str] | None = None,
    checks: list[dict[str, str]] | None = None,
    network: bool = False,
    failure: Failure | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operation": operation,
        "role": role,
        "outcome": outcome,
        "config": {"path": str(path), "source": source} if path is not None else None,
        "values": values,
        "origins": origins,
        "scope": "network" if network else "local",
        "checks": checks,
        "effects": {"config_written": outcome == "config_written"},
        "error": _failure_dict(failure),
    }


def _emit_config(payload: dict[str, object], json_output: bool, exit_code: Literal[0, 1, 2]) -> int:
    if json_output:
        return emit_json(payload, exit_code)
    if payload["error"] is not None:
        error = cast(dict[str, object], payload["error"])
        print(f"html-publish: {error['message']}", file=sys.stderr)
        if payload["operation"] == "doctor" and payload["checks"] is not None:
            for check in cast(list[dict[str, str]], payload["checks"]):
                if check["status"] == "fail":
                    print(
                        f"  {check['id']}: {check['detail']}. {check['next_step']}",
                        file=sys.stderr,
                    )
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


def _init_raw(parsed: argparse.Namespace) -> dict[str, object]:
    if parsed.role == "publisher":
        invalid = (
            "target_id",
            "execution",
            "publisher_config",
            "host",
            "remote_executable",
            "remote_config",
            "incoming_root",
        )
        if any(getattr(parsed, name) is not None for name in invalid):
            raise UsageFailure("client options cannot be used for publisher config init")
        root = (
            data_root() / "html-publish"
            if parsed.archive is None or parsed.runtime is None
            else None
        )
        archive = parsed.archive or cast(Path, root) / "archive.git"
        runtime = parsed.runtime or cast(Path, root) / "runtime"
        return {
            "archive": str(archive.expanduser().absolute()),
            "runtime": str(runtime.expanduser().absolute()),
            "base_url": parsed.base_url,
            "allow_http": parsed.allow_http,
            "object_format": "sha1",
            "limits": {},
        }
    if parsed.archive is not None or parsed.runtime is not None or parsed.allow_http:
        raise UsageFailure("publisher options cannot be used for client config init")
    if parsed.target_id is None or parsed.execution is None:
        raise UsageFailure("client config init requires --target-id and --execution")
    if parsed.execution == "local":
        if parsed.publisher_config is None:
            raise UsageFailure("local client config init requires --publisher-config")
        if any(
            getattr(parsed, name) is not None
            for name in ("host", "remote_executable", "remote_config", "incoming_root")
        ):
            raise UsageFailure("SSH options cannot be used with local execution")
        execution: dict[str, object] = {
            "kind": "local",
            "command": ["html-publish"],
            "publisher_config": str(parsed.publisher_config.expanduser()),
        }
    else:
        if parsed.publisher_config is not None:
            raise UsageFailure("--publisher-config is only for local execution")
        missing = [
            flag
            for name, flag in (
                ("host", "--host"),
                ("remote_executable", "--remote-executable"),
                ("remote_config", "--remote-config"),
                ("incoming_root", "--incoming-root"),
            )
            if getattr(parsed, name) is None
        ]
        if missing:
            raise UsageFailure("remote client config init requires " + ", ".join(missing))
        execution = {
            "kind": "remote",
            "command": ["html-publish-remote"],
            "host": parsed.host,
            "remote_executable": parsed.remote_executable,
            "remote_config": parsed.remote_config,
            "incoming_root": parsed.incoming_root,
        }
    return {
        "schema_version": 1,
        "target": {"id": parsed.target_id, "base_url": parsed.base_url},
        "execution": execution,
        "limits": {},
    }


def _run_config(parsed: argparse.Namespace) -> int:
    role = cast(Literal["publisher", "client"], parsed.role)
    action = parsed.config_action if parsed.operation == "config" else "doctor"
    if action == "init" and parsed.config is None:
        raise UsageFailure("config init requires an explicit --config path")
    path, source = selected_path(role, parsed.config)
    parsed.selected_config_path = path
    parsed.config_source = source
    if action == "init":
        outcome = init_document(path, _init_raw(parsed), role)
        return _emit_config(
            _config_result("config.init", role, path, source, outcome), parsed.json, 0
        )
    raw, config = read_document(role, path)
    values: dict[str, object]
    origins: dict[str, str]
    raw_limits = cast(dict[str, object], raw.get("limits", {}))
    if isinstance(config, ClientConfig):
        raw_execution = cast(dict[str, object], raw["execution"])
        execution = asdict(config.executor)
        values = {
            **raw,
            "execution": execution,
            "limits": asdict(config.limits),
            "fingerprint": config.fingerprint,
        }
        origins = {
            "schema_version": "file",
            "target.id": "file",
            "target.base_url": "file",
            "fingerprint": "derived",
            **{
                f"execution.{name}": "file" if name in raw_execution else "default"
                for name in execution
            },
        }
        if config.executor.kind == "local":
            origins["execution.host"] = "derived"
    else:
        values = {
            "archive": str(config.archive),
            "runtime": str(config.runtime),
            "base_url": config.base_url,
            "allow_http": config.allow_http,
            "object_format": config.object_format,
            "limits": asdict(config.limits),
        }
        origins = {
            name: "file" if name in raw else "default" for name in values if name != "limits"
        }
    limits = cast(dict[str, object], values["limits"])
    origins.update(
        {f"limits.{name}": "file" if name in raw_limits else "default" for name in limits}
    )
    if parsed.command_seconds is not None:
        origins["limits.command_seconds"] = "argument"
        limits["command_seconds"] = parsed.command_seconds
    if action == "doctor":
        from html_publish.doctor import run_doctor

        checks = run_doctor(
            role, config, network=parsed.network, command_seconds=parsed.command_seconds
        )
        failed = any(check["status"] == "fail" for check in checks)
        result = _config_result(
            "doctor",
            role,
            path,
            source,
            "error" if failed else "diagnosed",
            values=values,
            origins=origins,
            checks=checks,
            network=parsed.network,
            failure=Failure(
                "diagnostic_failed",
                "doctor",
                "One or more prerequisite checks failed",
                "inspect_checks",
            )
            if failed
            else None,
        )
        return _emit_config(result, parsed.json, 1 if failed else 0)
    result = _config_result(
        f"config.{action}",
        role,
        path,
        source,
        "valid",
        values=values if action == "show" else None,
        origins=origins if action == "show" else None,
    )
    return _emit_config(result, parsed.json, 0)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in arguments
    parsed = argparse.Namespace()
    config: Config | None = None
    try:
        parser = _parser(json_output)
        parser.parse_args(arguments, namespace=parsed)
        if parsed.operation == "schema":
            return emit_json(command_schema(parser, "html-publish"), 0)
        if parsed.operation in {"config", "doctor"}:
            return _run_config(parsed)
        config_path, _ = selected_path("publisher", parsed.config)
        config = load_config(config_path)
        command_seconds = parsed.command_seconds or config.limits.command_seconds
        deadline = Deadline.start(command_seconds)
        with _command_alarm(command_seconds):
            _git.check_supported_version(deadline)
            store = PublicationStore(config, deadline)
            if parsed.operation == "plan":
                report = store.plan(
                    parsed.name,
                    parsed.source,
                    parsed.target,
                    parsed.expected_revision,
                )
            elif parsed.operation == "publish":
                report = store.publish(
                    parsed.name,
                    parsed.source,
                    parsed.target,
                    parsed.expected_revision,
                    parsed.request_id,
                )
            elif parsed.operation == "verify":
                report = store.verify_page(parsed.name)
            elif parsed.operation == "history":
                report = store.history(
                    parsed.name, parsed.limit, parsed.after, parsed.diff_revision
                )
            elif parsed.operation == "restore":
                report = store.restore(
                    parsed.name,
                    parsed.archive_commit,
                    parsed.target,
                    parsed.expected_revision,
                    parsed.request_id,
                )
            else:
                report = store.status(
                    parsed.name,
                    parsed.after,
                    parsed.limit,
                    getattr(parsed, "host_check", False),
                )
        return _print_report(report, parsed.json)
    except UsageFailure as error:
        failure = Failure("invalid_usage", "usage", str(error), "fix_arguments")
        operation = getattr(parsed, "operation", None)
        if operation in {"config", "doctor"} and not hasattr(parsed, "role"):
            return _emit_config(
                _config_result(operation, None, None, None, "error", failure=failure),
                json_output,
                2,
            )
        if operation in {"config", "doctor"}:
            action = parsed.config_action if parsed.operation == "config" else "doctor"
            return _emit_config(
                _config_result(
                    f"config.{action}" if action != "doctor" else "doctor",
                    getattr(parsed, "role", None),
                    getattr(parsed, "selected_config_path", getattr(parsed, "config", None)),
                    getattr(parsed, "config_source", None),
                    "error",
                    failure=failure,
                ),
                json_output,
                2,
            )
        if json_output:
            return emit_json(
                usage_report(
                    operation,
                    failure,
                    target=getattr(parsed, "target", None),
                    name=getattr(parsed, "name", None),
                    expected_revision=getattr(parsed, "expected_revision", None),
                    request_id=getattr(parsed, "request_id", None),
                ),
                2,
            )
        else:
            print(f"html-publish: {error}", file=sys.stderr)
        return 2
    except PublishError as error:
        operation = getattr(parsed, "operation", None)
        if operation in {"config", "doctor"}:
            action = parsed.config_action if parsed.operation == "config" else "doctor"
            return _emit_config(
                _config_result(
                    f"config.{action}" if action != "doctor" else "doctor",
                    getattr(parsed, "role", None),
                    getattr(parsed, "selected_config_path", getattr(parsed, "config", None)),
                    getattr(parsed, "config_source", None),
                    "error",
                    failure=error.failure,
                ),
                json_output,
                1,
            )
        error_operation: Operation
        error_operation = cast(
            Operation,
            operation if operation in OPERATIONS else "status",
        )
        target = getattr(parsed, "target", config.base_url if config else None)
        name = getattr(parsed, "name", None)
        report = Report(
            error_operation,
            "error",
            target,
            name,
            publication_url(target, name) if target is not None and name is not None else None,
            request_id=getattr(parsed, "request_id", None),
            expected_revision=getattr(parsed, "expected_revision", None),
            error=error.failure,
        )
        return _print_report(report, json_output)


def entrypoint() -> NoReturn:
    raise SystemExit(main())
