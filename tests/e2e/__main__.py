"""Run the E2E suite and leave a verifiable, repeatable artifact for every test.

Usage: uv run python -m tests.e2e

Each run writes $HTML_PUBLISH_E2E_ROOT/<run id>/artifacts/. The root defaults to
/tmp/html-publish-verify, which CI uploads, and the run ID comes from
$HTML_PUBLISH_E2E_RUN_ID or is generated.

  manifest.json          commit, the source fingerprint taken before and after
                         the suite, rerun commands, each test's outcome, and
                         the sha256 of every record file
  tests/<test id>.jsonl  one line per process the test started: argv, cwd, and
                         exit code; subprocess.run calls also get stdout and
                         stderr digests with a readable head. A Popen process's
                         exit code is read when its test ends, and a process
                         still running then records null

scripts/check_e2e_artifacts.py verifies a run against its files and the test tree.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from tests.e2e._fingerprint import source_fingerprint

ROOT = Path(__file__).resolve().parents[2]
SPAWN_EVENTS = frozenset({"subprocess.Popen", "os.system"})
HEAD_CHARACTERS = 2000
SCHEMA_VERSION = 2


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def digest(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    data = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "head": data[:HEAD_CHARACTERS].decode("utf-8", errors="replace"),
    }


def command_line(event: str, args: tuple[Any, ...]) -> list[str]:
    if event == "os.system":
        return [os.fsdecode(args[0])]
    arguments = args[1]
    if isinstance(arguments, str | bytes | os.PathLike):
        return [os.fsdecode(arguments)]  # pyright: ignore[reportUnknownArgumentType]
    return [os.fsdecode(argument) for argument in arguments]


class Recorder:
    """Collect the processes each test starts and the outcome it reaches."""

    def __init__(self, run_id: str, artifacts: Path) -> None:
        self.run_id = run_id
        self.artifacts = artifacts
        self.tests: dict[str, dict[str, Any]] = {}
        self.processes: dict[str, list[dict[str, Any]]] = {}
        self.popens: dict[str, list[tuple[dict[str, Any], subprocess.Popen[Any]]]] = {}
        self.current: str | None = None
        self.started: float = 0.0

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if event in SPAWN_EVENTS and self.current is not None:
            cwd = args[2] if event == "subprocess.Popen" and len(args) > 2 else None
            self.processes[self.current].append(
                {
                    "event": event,
                    "argv": command_line(event, args),
                    "cwd": os.fsdecode(cwd) if cwd is not None else None,
                }
            )

    def wrap_run(self) -> None:
        original: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run

        def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
            owner = self.current
            index = len(self.processes[owner]) if owner is not None else 0
            started = time.monotonic()
            result = original(*args, **kwargs)
            if owner is not None and len(self.processes[owner]) > index:
                self.processes[owner][index].update(
                    returncode=result.returncode,
                    seconds=round(time.monotonic() - started, 3),
                    stdout=digest(result.stdout),
                    stderr=digest(result.stderr),
                )
            return result

        setattr(subprocess, "run", run)  # noqa: B010

    def track_popen(self) -> None:
        """Keep each Popen a test starts, so its exit code can be read when the test ends."""
        recorder = self

        class RecordingPopen(subprocess.Popen[Any]):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                owner = recorder.current
                index = len(recorder.processes[owner]) if owner is not None else 0
                super().__init__(*args, **kwargs)
                if owner is not None and len(recorder.processes[owner]) > index:
                    entry = recorder.processes[owner][index]
                    recorder.popens.setdefault(owner, []).append((entry, self))

        setattr(subprocess, "Popen", RecordingPopen)  # noqa: B010

    def begin(self, test_id: str) -> None:
        self.current = test_id
        self.started = time.monotonic()
        self.processes.setdefault(test_id, [])
        self.tests.setdefault(test_id, {"id": test_id, "outcome": None, "reason": None})

    def settle(self, test_id: str, outcome: str, reason: str | None = None) -> None:
        entry = self.tests.setdefault(test_id, {"id": test_id, "outcome": None, "reason": None})
        self.processes.setdefault(test_id, [])
        if entry["outcome"] not in {"failed", "error"}:
            entry["outcome"] = outcome
            entry["reason"] = reason

    def end(self, test_id: str) -> None:
        self.tests[test_id]["seconds"] = round(time.monotonic() - self.started, 3)
        for entry, process in self.popens.pop(test_id, []):
            if "returncode" not in entry:
                entry["returncode"] = process.poll()
        self.current = None

    def write(self, started_at: str, source: dict[str, str | None]) -> Path:
        records = self.artifacts / "tests"
        records.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        tests: list[dict[str, Any]] = []
        for test_id, entry in sorted(self.tests.items()):
            record = f"tests/{test_id}.jsonl"
            lines = "".join(
                json.dumps(item, sort_keys=True) + "\n" for item in self.processes[test_id]
            )
            (self.artifacts / record).write_text(lines, encoding="utf-8")
            files[record] = hashlib.sha256(lines.encode("utf-8")).hexdigest()
            tests.append(
                {
                    **entry,
                    "processes": len(self.processes[test_id]),
                    "record": record,
                    "rerun": f"uv run python -m unittest {test_id}",
                }
            )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "started_at": started_at,
            "finished_at": now(),
            **git_state(),
            "source_fingerprint": source,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "rerun": "just check --only e2e",
            "tests": tests,
            "files": files,
        }
        path = self.artifacts / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path


class RecordingResult(unittest.TextTestResult):
    recorder: Recorder

    def startTest(self, test: unittest.TestCase) -> None:
        self.recorder.begin(test.id())
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:
        super().stopTest(test)
        self.recorder.end(test.id())

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        self.recorder.settle(test.id(), "passed")

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:
        super().addFailure(test, err)
        self.recorder.settle(test.id(), "failed")

    def addError(self, test: unittest.TestCase, err: Any) -> None:
        super().addError(test, err)
        self.recorder.settle(test.id(), "error")

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self.recorder.settle(test.id(), "skipped", reason)

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:
        super().addExpectedFailure(test, err)
        self.recorder.settle(test.id(), "failed", "expected failures hide broken behavior")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self.recorder.settle(test.id(), "failed", "unexpected success")

    def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err: Any) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            failed = issubclass(err[0], test.failureException)
            self.recorder.settle(test.id(), "failed" if failed else "error")


def git_state() -> dict[str, object]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}


def fingerprint() -> str | None:
    try:
        return source_fingerprint(ROOT)
    except (OSError, subprocess.CalledProcessError):
        return None


class RecordingRunner(unittest.TextTestRunner):
    def __init__(self, recorder: Recorder, stream: TextIO) -> None:
        super().__init__(stream=stream, verbosity=2)
        self.recorder = recorder

    def _makeResult(self) -> unittest.TextTestResult:
        result = RecordingResult(self.stream, self.descriptions, self.verbosity)
        result.recorder = self.recorder
        return result


def main(stream: TextIO = sys.stderr) -> int:
    run_id = os.environ.get("HTML_PUBLISH_E2E_RUN_ID") or (
        f"e2e-{datetime.datetime.now(datetime.UTC):%Y%m%dT%H%M%SZ}-{os.getpid()}"
    )
    artifacts = Path(os.environ.get("HTML_PUBLISH_E2E_ROOT", "/tmp/html-publish-verify"))
    artifacts = artifacts / run_id / "artifacts"
    if artifacts.exists():
        print(
            f"error: {artifacts} already exists; choose a fresh HTML_PUBLISH_E2E_RUN_ID",
            file=stream,
        )
        return 2
    artifacts.mkdir(parents=True)
    started_at = now()
    source_at_start = fingerprint()

    recorder = Recorder(run_id, artifacts)
    sys.addaudithook(recorder.audit)
    recorder.wrap_run()
    recorder.track_popen()
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests" / "e2e"), pattern="test_*.py", top_level_dir=str(ROOT)
    )
    result = RecordingRunner(recorder, stream).run(suite)
    manifest = recorder.write(started_at, {"start": source_at_start, "end": fingerprint()})
    print(f"E2E_ARTIFACTS={manifest.parent}", file=stream)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
