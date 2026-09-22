#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

DEFAULT_REMOTE_COMMAND = "html-publish-remote"
DEFAULT_NAME = "om1-deployment-mvp"
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
OLD_IF_MODIFIED_SINCE = "Thu, 01 Jan 1970 00:00:00 GMT"


class VerificationFailure(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise VerificationFailure(message)


@dataclass(frozen=True)
class SourceArtifact:
    label: str
    path: Path
    content: bytes
    sha256: str


@dataclass(frozen=True)
class RemoteResult:
    step: str
    returncode: int
    payload: dict[str, object]

    @property
    def outcome(self) -> str:
        return _required_string(self.payload, "outcome", self.step)


def _bounded_timeout(value: str) -> int:
    try:
        timeout = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be an integer") from error
    if timeout < 1 or timeout > MAX_TIMEOUT:
        raise argparse.ArgumentTypeError(f"timeout must be between 1 and {MAX_TIMEOUT} seconds")
    return timeout


def _parser() -> Parser:
    parser = Parser(description="Verify the controlled om1 html-publish MVP end to end")
    parser.add_argument("--remote-command", default=DEFAULT_REMOTE_COMMAND)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--timeout", type=_bounded_timeout, default=DEFAULT_TIMEOUT)
    return parser


def _required_string(payload: dict[str, object], key: str, step: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise VerificationFailure(f"{step} returned no usable {key}")
    return value


def _optional_string(payload: dict[str, object], key: str, step: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise VerificationFailure(f"{step} returned an invalid {key}")
    return value


def _json_object(text: str, step: str) -> dict[str, object]:
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise VerificationFailure(f"{step} returned invalid JSON: {error.msg}") from error
    if not isinstance(value, dict):
        raise VerificationFailure(f"{step} returned JSON that is not an object")
    return cast(dict[str, object], value)


def _remote(
    command: tuple[str, ...],
    timeout: int,
    step: str,
    arguments: list[str],
    *,
    expect_success: bool = True,
) -> RemoteResult:
    try:
        result = subprocess.run(
            [*command, *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise VerificationFailure(f"{step} exceeded the {timeout} second timeout") from error
    except OSError as error:
        raise VerificationFailure(f"{step} could not start the remote command: {error}") from error

    if not result.stdout.strip():
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise VerificationFailure(f"{step} returned no JSON output: {detail}")
    payload = _json_object(result.stdout, step)
    if expect_success and result.returncode != 0:
        code = _error_code(payload) or "unknown_error"
        raise VerificationFailure(f"{step} failed with {code} (exit {result.returncode})")
    if expect_success and payload.get("outcome") == "error":
        raise VerificationFailure(f"{step} reported {_error_code(payload) or 'an error'}")
    return RemoteResult(step, result.returncode, payload)


def _error_code(payload: dict[str, object]) -> str | None:
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = cast(dict[str, object], error).get("code")
    return code if isinstance(code, str) else None


def _write_sources(root: Path) -> tuple[SourceArtifact, SourceArtifact, SourceArtifact]:
    os.chmod(root, 0o700)
    run_id = uuid.uuid4().hex
    artifacts: list[SourceArtifact] = []
    for label in ("A", "B", "C"):
        content = (
            "<!doctype html><meta charset=utf-8>"
            f"<title>om1 deployment MVP {label}</title>"
            f"<h1>om1 deployment MVP {label}</h1>"
            f"<p>verification run {run_id}</p>\n"
        ).encode()
        path = root / f"source-{label.lower()}.html"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        artifacts.append(SourceArtifact(label, path, content, hashlib.sha256(content).hexdigest()))
    return cast(tuple[SourceArtifact, SourceArtifact, SourceArtifact], tuple(artifacts))


def _hashes(artifacts: tuple[SourceArtifact, ...]) -> dict[str, str]:
    return {
        artifact.label: hashlib.sha256(artifact.path.read_bytes()).hexdigest()
        for artifact in artifacts
    }


def _assert_outcome(result: RemoteResult, expected: str) -> None:
    if result.outcome != expected:
        raise VerificationFailure(
            f"{result.step} returned outcome {result.outcome!r}, expected {expected!r}"
        )


def _assert_active(result: RemoteResult, expected: str) -> None:
    active = _optional_string(result.payload, "active_revision", result.step)
    if active != expected:
        raise VerificationFailure(
            f"{result.step} returned active revision {active!r}, expected {expected!r}"
        )


def _expected_arguments(revision: str | None) -> list[str]:
    return ["--expected-revision", revision] if revision is not None else []


def _artifact_arguments(
    operation: str,
    name: str,
    artifact: SourceArtifact,
    expected_revision: str | None,
) -> list[str]:
    return [
        operation,
        "--name",
        name,
        "--source",
        str(artifact.path),
        *_expected_arguments(expected_revision),
    ]


def _stable_url(results: tuple[RemoteResult, ...]) -> str:
    urls = {
        url
        for result in results
        if (url := _optional_string(result.payload, "url", result.step)) is not None
    }
    if len(urls) != 1:
        raise VerificationFailure(f"remote operations returned {len(urls)} distinct stable URLs")
    return urls.pop()


def _fetch(url: str, expected_content: bytes, timeout: int) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={"If-Modified-Since": OLD_IF_MODIFIED_SINCE},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            cache_control = response.headers.get("Cache-Control", "")
            body = response.read()
    except urllib.error.HTTPError as error:
        raise VerificationFailure(f"stable URL returned HTTP {error.code}") from error
    except (OSError, urllib.error.URLError) as error:
        raise VerificationFailure(f"stable URL request failed: {error}") from error
    if status != 200:
        raise VerificationFailure(f"stable URL returned HTTP {status}, expected 200")
    if body != expected_content:
        raise VerificationFailure("stable URL did not return the published B artifact")
    directives = {part.strip().lower() for part in cache_control.split(",")}
    if "no-store" not in directives:
        raise VerificationFailure("stable URL response did not include Cache-Control: no-store")
    return {
        "status": status,
        "cache_control": cache_control,
        "if_modified_since": OLD_IF_MODIFIED_SINCE,
        "content_sha256": hashlib.sha256(body).hexdigest(),
    }


def _operation_evidence(results: tuple[RemoteResult, ...]) -> list[dict[str, object]]:
    return [
        {
            "step": result.step,
            "outcome": result.outcome,
            "active_revision": result.payload.get("active_revision"),
        }
        for result in results
    ]


def _verify(
    command: tuple[str, ...],
    name: str,
    timeout: int,
    artifacts: tuple[SourceArtifact, SourceArtifact, SourceArtifact],
) -> dict[str, object]:
    source_a, source_b, source_c = artifacts
    initial = _remote(command, timeout, "status-initial", ["status", "--name", name])
    _assert_outcome(initial, "observed")
    initial_revision = _optional_string(initial.payload, "active_revision", initial.step)

    plan_a = _remote(
        command,
        timeout,
        "plan-a",
        _artifact_arguments("plan", name, source_a, initial_revision),
    )
    _assert_outcome(plan_a, "planned")
    expected_prediction = "create" if initial_revision is None else "update"
    if plan_a.payload.get("prediction") != expected_prediction:
        raise VerificationFailure(f"plan-a did not predict {expected_prediction}")
    publish_a = _remote(
        command,
        timeout,
        "publish-a",
        _artifact_arguments("publish", name, source_a, initial_revision),
    )
    _assert_outcome(publish_a, "published")
    revision_a = _required_string(publish_a.payload, "active_revision", publish_a.step)
    requested_a = _required_string(plan_a.payload, "requested_revision", plan_a.step)
    if revision_a != requested_a:
        raise VerificationFailure("plan-a and publish-a disagreed on revision A")

    status_a = _remote(command, timeout, "status-a", ["status", "--name", name])
    _assert_outcome(status_a, "observed")
    _assert_active(status_a, revision_a)

    plan_b = _remote(
        command,
        timeout,
        "plan-b",
        _artifact_arguments("plan", name, source_b, revision_a),
    )
    _assert_outcome(plan_b, "planned")
    if plan_b.payload.get("prediction") != "update":
        raise VerificationFailure("plan-b did not predict an update from revision A")
    publish_b = _remote(
        command,
        timeout,
        "publish-b",
        _artifact_arguments("publish", name, source_b, revision_a),
    )
    _assert_outcome(publish_b, "published")
    revision_b = _required_string(publish_b.payload, "active_revision", publish_b.step)
    requested_b = _required_string(plan_b.payload, "requested_revision", plan_b.step)
    if revision_b != requested_b or revision_b == revision_a:
        raise VerificationFailure("publication B did not advance from revision A")

    status_b = _remote(command, timeout, "status-b", ["status", "--name", name])
    _assert_outcome(status_b, "observed")
    _assert_active(status_b, revision_b)

    conflict = _remote(
        command,
        timeout,
        "publish-c-stale-a",
        _artifact_arguments("publish", name, source_c, revision_a),
        expect_success=False,
    )
    conflict_code = _error_code(conflict.payload)
    if (
        conflict.returncode == 0
        or conflict.outcome != "error"
        or conflict_code != "revision_conflict"
    ):
        raise VerificationFailure("stale publication C did not fail with revision_conflict")
    _assert_active(conflict, revision_b)

    results = (initial, plan_a, publish_a, status_a, plan_b, publish_b, status_b, conflict)
    url = _stable_url(results)
    http = _fetch(url, source_b.content, timeout)
    return {
        "url": url,
        "active_revision": revision_b,
        "revisions": {"initial": initial_revision, "a": revision_a, "b": revision_b},
        "operations": _operation_evidence(results),
        "conflict_code": conflict_code,
        "http": http,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        parsed = _parser().parse_args(argv)
        remote_command = cast(str, parsed.remote_command)
        name = cast(str, parsed.name)
        timeout = cast(int, parsed.timeout)
        command = tuple(shlex.split(remote_command))
        if not command:
            raise VerificationFailure("remote command must not be empty")
        if not name:
            raise VerificationFailure("name must not be empty")

        with tempfile.TemporaryDirectory(prefix="html-publish-om1-mvp-") as temporary:
            artifacts = _write_sources(Path(temporary))
            before = _hashes(artifacts)
            failure: VerificationFailure | None = None
            evidence: dict[str, object] | None = None
            try:
                evidence = _verify(command, name, timeout, artifacts)
            except VerificationFailure as error:
                failure = error
            after = _hashes(artifacts)
            preserved = before == after and all(
                before[artifact.label] == artifact.sha256 for artifact in artifacts
            )
            if not preserved:
                raise VerificationFailure("a source artifact changed during verification")
            if failure is not None:
                raise failure
            assert evidence is not None
            evidence["source_hash_preservation"] = {"verified": True, "sha256": before}
            print(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")))
            return 0
    except VerificationFailure as error:
        print(f"om1 MVP verification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
