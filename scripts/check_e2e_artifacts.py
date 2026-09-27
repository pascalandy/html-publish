#!/usr/bin/env python3
"""Check that an E2E run left a complete, untampered artifact for every E2E test."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ScriptError, run_script
from _test_tree import add_root_argument, test_functions, test_sources

EPILOG = """\
rule:
  Every E2E test leaves a verifiable, repeatable artifact. `python -m tests.e2e`
  writes <artifacts root>/<run id>/artifacts/manifest.json plus one record per
  test. This check requires that the run
  - comes from the checked-out commit,
  - covers every E2E test in tests/e2e/,
  - reports no failed test and gives every skip a reason,
  - shows every test that ran starting at least one process, since an E2E test
    drives a shipped executable,
  - lists a rerun command per test, and
  - still matches the sha256 of every record file.
  See docs/testing.md.

examples:
  just check --only e2e --only e2e-artifacts
  uv run python scripts/check_e2e_artifacts.py --run-id e2e-20260927T120000Z-4242

exit codes: 0 ok, 1 artifact errors found, 2 bad usage, 130 interrupted"""

DEFAULT_ARTIFACTS_ROOT = "/tmp/html-publish-verify"
REQUIRED_KEYS = ("schema_version", "run_id", "commit", "rerun", "tests", "files")

log = logging.getLogger("check-e2e-artifacts")


def find_run(artifacts_root: Path, run_id: str | None) -> Path:
    if run_id:
        run = artifacts_root / run_id / "artifacts"
        if not (run / "manifest.json").is_file():
            raise ScriptError(
                f"{run}/manifest.json is missing; fix: run just check --only e2e first"
            )
        return run
    manifests = sorted(
        artifacts_root.glob("e2e-*/artifacts/manifest.json"), key=lambda path: path.stat().st_mtime
    )
    if not manifests:
        raise ScriptError(
            f"no E2E run under {artifacts_root}; fix: run just check --only e2e first"
        )
    return manifests[-1].parent


def expected_tests(root: Path) -> set[str]:
    """unittest IDs of every test method in tests/e2e/test_*.py."""
    identifiers: set[str] = set()
    for source in test_sources(root):
        if source.bucket != "e2e" or not source.is_test_module:
            continue
        module = source.label.removesuffix(".py").replace("/", ".")
        for owner, function in test_functions(source.tree):
            if owner is not None:
                identifiers.add(f"{module}.{owner.name}.{function.name}")
    return identifiers


def head_commit(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def check_files(run: Path, files: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for relative, expected in sorted(files.items()):
        path = run / relative
        if not path.is_file():
            errors.append(f"{path}: [e2e-artifacts] listed in the manifest but missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(
                f"{path}: [e2e-artifacts] sha256 differs from the manifest; "
                "the record changed after the run"
            )
    for path in sorted((run / "tests").glob("*")):
        if path.relative_to(run).as_posix() not in files:
            errors.append(f"{path}: [e2e-artifacts] record not listed in the manifest")
    return errors


def check_tests(tests: list[dict[str, Any]], expected: set[str]) -> list[str]:
    errors: list[str] = []
    recorded = {str(test.get("id")) for test in tests}
    for missing in sorted(expected - recorded):
        errors.append(f"{missing}: [e2e-artifacts] did not run; fix: rerun just check --only e2e")
    for test in tests:
        test_id = str(test.get("id"))
        outcome = test.get("outcome")
        if outcome in {"failed", "error"}:
            errors.append(
                f"{test_id}: [e2e-artifacts] {outcome}; its record is {test.get('record')}"
            )
        elif outcome == "skipped":
            if not test.get("reason"):
                errors.append(f"{test_id}: [e2e-artifacts] skipped without a reason")
        elif outcome != "passed":
            errors.append(f"{test_id}: [e2e-artifacts] has no outcome")
        elif int(test.get("processes") or 0) == 0:
            errors.append(
                f"{test_id}: [e2e-artifacts] started no process; fix: drive a shipped executable, "
                "or move the test to tests/isolated/ with a failure list"
            )
        if not test.get("rerun"):
            errors.append(f"{test_id}: [e2e-artifacts] has no rerun command")
    return errors


def check(root: Path, artifacts_root: Path, run_id: str | None) -> str:
    run = find_run(artifacts_root, run_id)
    log.debug("check %s", run)
    try:
        manifest = cast(
            dict[str, Any], json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        )
    except json.JSONDecodeError as error:
        raise ScriptError(f"{run}/manifest.json is not JSON: {error}") from error
    missing = [key for key in REQUIRED_KEYS if key not in manifest]
    if missing:
        raise ScriptError(f"{run}/manifest.json lacks {', '.join(missing)}")
    if manifest["schema_version"] != 1:
        raise ScriptError(
            f"{run}/manifest.json has schema_version {manifest['schema_version']}, expected 1"
        )

    errors: list[str] = []
    commit = head_commit(root)
    if manifest["commit"] != commit:
        errors.append(
            f"{run}: [e2e-artifacts] run is from commit {manifest['commit']}, "
            f"checkout is at {commit}; fix: rerun just check --only e2e"
        )
    errors += check_files(run, cast(dict[str, str], manifest["files"]))
    tests = cast(list[dict[str, Any]], manifest["tests"])
    errors += check_tests(tests, expected_tests(root))
    if errors:
        raise ScriptError(*errors)
    return f"ok: {len(tests)} e2e tests left verified records in {run}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_e2e_artifacts.py",
        description="Check that an E2E run left a complete, untampered artifact for every E2E test",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_root_argument(parser)
    parser.add_argument(
        "--run-id",
        default=os.environ.get("HTML_PUBLISH_E2E_RUN_ID"),
        help="run to check (default: $HTML_PUBLISH_E2E_RUN_ID, else the newest run)",
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path(os.environ.get("HTML_PUBLISH_E2E_ROOT", DEFAULT_ARTIFACTS_ROOT)),
        help="directory holding E2E runs "
        f"(default: $HTML_PUBLISH_E2E_ROOT, else {DEFAULT_ARTIFACTS_ROOT})",
    )
    return run_script(
        parser, lambda args: check(args.root.resolve(), args.artifacts_root, args.run_id), argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
