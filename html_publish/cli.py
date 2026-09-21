from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
import signal
import sys
from collections.abc import Generator, Mapping
from pathlib import Path
from types import FrameType
from typing import Literal, NoReturn, cast
from urllib.parse import urlparse

from html_publish import __version__
from html_publish.delivery import publication_url
from html_publish.model import (
    Config,
    Deadline,
    Effects,
    Failure,
    Limits,
    LocalState,
    Name,
    PublishError,
    Report,
    Revision,
    Selection,
    Verification,
)
from html_publish.store import PublicationStore

NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


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


def _parser() -> Parser:
    parser = Parser(
        prog="html-publish",
        description="Publish private static HTML with a stable URL and local Git history",
    )
    parser.add_argument("--config", type=Path, help="JSON configuration file")
    parser.add_argument("--json", action="store_true", help="emit one versioned JSON object")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="operation", required=True)

    plan = commands.add_parser("plan", help="capture and inspect without persistent writes")
    plan.add_argument("--name", required=True, type=_name)
    plan.add_argument("--source", required=True, type=Path)
    plan.add_argument("--target", required=True)
    plan.add_argument("--expected-revision", type=Revision)
    plan.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    publish = commands.add_parser("publish", help="archive, activate, and verify an artifact")
    publish.add_argument("--name", required=True, type=_name)
    publish.add_argument("--source", required=True, type=Path)
    publish.add_argument("--target", required=True)
    publish.add_argument("--expected-revision", type=Revision)
    publish.add_argument("--request-id")
    publish.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    status = commands.add_parser("status", help="observe bounded local state")
    status.add_argument("--name", type=_name)
    status.add_argument("--after", type=_name)
    status.add_argument("--limit", type=_positive_int, default=100)
    status.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
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
            f"The configuration could not be read: {error}",
            "fix_config",
        ) from error
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
    if report.status_entries:
        payload["entries"] = [
            {
                "name": entry.name,
                "url": publication_url(report.target or "", entry.name),
                "observation": _state_dict(entry.state),
            }
            for entry in report.status_entries
        ]
    return payload


def _usage_report(operation: str | None, failure: Failure) -> dict[str, object]:
    selected_operation = operation if operation in {"plan", "publish", "status"} else "usage"
    return {
        "schema_version": 1,
        "operation": selected_operation,
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


def _print_report(report: Report, json_output: bool) -> int:
    payload = report_dict(report)
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    elif report.error:
        print(f"html-publish: {report.error.message}", file=sys.stderr)
    elif report.operation == "publish":
        assert report.url is not None
        print(report.url)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if report.error else 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in arguments
    operation = next((value for value in arguments if value in {"plan", "publish", "status"}), None)
    try:
        parsed = _parser().parse_args(arguments)
        if parsed.config is None:
            raise UsageFailure("--config is required")
        config = load_config(parsed.config)
        deadline = Deadline.start(config.limits.command_seconds)
        store = PublicationStore(config, deadline)
        with _command_alarm(config.limits.command_seconds):
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
            else:
                report = store.status(parsed.name, parsed.after, parsed.limit)
        return _print_report(report, parsed.json)
    except UsageFailure as error:
        failure = Failure("invalid_usage", "usage", str(error), "fix_arguments")
        if json_output:
            print(json.dumps(_usage_report(operation, failure), separators=(",", ":")))
        else:
            print(f"html-publish: {error}", file=sys.stderr)
        return 2
    except PublishError as error:
        error_operation: Literal["plan", "publish", "status"]
        if operation == "plan":
            error_operation = "plan"
        elif operation == "publish":
            error_operation = "publish"
        else:
            error_operation = "status"
        report = Report(
            error_operation,
            "error",
            None,
            None,
            None,
            error=error.failure,
        )
        return _print_report(report, json_output)


def entrypoint() -> NoReturn:
    raise SystemExit(main())
