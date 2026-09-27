#!/usr/bin/env python3
"""Check that an E2E run left a complete, untampered artifact for every E2E test."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import logging
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from _common import ScriptError, run_script
from _test_tree import add_root_argument, test_functions, test_sources, undiscovered_tests

from tests.e2e._fingerprint import git_environment, source_fingerprint

EPILOG = """\
rule:
  Every E2E test leaves a verifiable, repeatable artifact. `python -m tests.e2e`
  writes <artifacts root>/<run id>/artifacts/manifest.json plus one record per
  test. This check requires that the run
  - comes from the checked-out commit,
  - tested the source files the checkout holds now: the fingerprints of tracked
    and non-ignored untracked files taken before the suite, after it, and from
    the checkout all match, so a dirty checkout passes until a file changes,
  - covers every E2E test in tests/e2e/, none of them a module-level function
    that unittest never runs,
  - reports no failed test and gives every skip a reason,
  - shows every test that passed starting html-publish, the html_publish
    package, or a repository script, since an E2E test drives the product,
  - counts a skip raised in setUpClass or setUpModule for the tests it covers,
  - lists a rerun command per test, and
  - still matches the sha256 of every record file.
  See docs/testing.md.

examples:
  just check --only e2e --only e2e-artifacts
  uv run python scripts/check_e2e_artifacts.py --run-id e2e-20260927T120000Z-4242

exit codes: 0 ok, 1 artifact errors found, 2 bad usage, 130 interrupted"""

DEFAULT_ARTIFACTS_ROOT = "/tmp/html-publish-verify"
SCHEMA_VERSION = 2
REQUIRED_KEYS = ("run_id", "commit", "source_fingerprint", "rerun", "tests", "files")
RERUN = "fix: rerun just check --only e2e"
PRODUCT_MODULES = ("html_publish", "tests.e2e")
FIXTURE_HOLDER_RE = re.compile(r"^(?:setUpClass|setUpModule) \((?P<scope>[\w.]+)\)$")
SHELLS = frozenset({"bash", "sh"})


def executables(root: Path) -> frozenset[str]:
    """Command names from [project.scripts], such as html-publish and html-publish-remote."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return frozenset()
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("scripts")
    return frozenset(str(name) for name in dict(scripts or {}))


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


def expected_tests(root: Path) -> tuple[set[str], list[str]]:
    """unittest IDs of every test method in tests/e2e/test_*.py, and errors for the test
    functions unittest never runs, which a run cannot record."""
    identifiers: set[str] = set()
    undiscovered: list[str] = []
    for source in test_sources(root):
        if source.bucket != "e2e" or not source.is_test_module:
            continue
        undiscovered += undiscovered_tests(source, "e2e-artifacts")
        module = source.label.removesuffix(".py").replace("/", ".")
        for owner, function in test_functions(source.tree):
            # A class inside an if or try block may not exist on this platform, so only a class
            # at the top of the module is sure to run
            if owner is not None and owner in source.tree.body:
                identifiers.add(f"{module}.{owner.name}.{function.name}")
    return identifiers, undiscovered


def head_commit(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        env=git_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def check_source(run: Path, root: Path, recorded: object) -> list[str]:
    """Whether the run tested, from start to finish, the source files `root` holds now."""
    fingerprint = cast(dict[str, object], recorded) if isinstance(recorded, dict) else {}
    start, end = fingerprint.get("start"), fingerprint.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return [f"{run}: [e2e-artifacts] records no source fingerprint; {RERUN}"]
    if start != end:
        return [
            f"{run}: [e2e-artifacts] source files changed while the suite ran; "
            f"{RERUN} and leave the checkout unchanged until it ends"
        ]
    try:
        current = source_fingerprint(root)
    except (OSError, subprocess.CalledProcessError) as error:
        return [
            f"{root}: [e2e-artifacts] cannot fingerprint the checkout: {str(error).rstrip('.')}; "
            "fix: pass a Git checkout as --root and put git on PATH"
        ]
    if current != start:
        return [
            f"{run}: [e2e-artifacts] run tested other source files than the checkout holds; {RERUN}"
        ]
    return []


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


def invokes_product(argv: list[str], commands: frozenset[str]) -> bool:
    """Whether a recorded command runs a product executable, package, or repository script."""
    if not argv:
        return False
    if any(Path(argument).name in commands for argument in argv):
        return True
    for flag, value in itertools.pairwise(argv):
        if flag == "-m" and value.startswith(PRODUCT_MODULES):
            return True
    if len(argv) > 1 and (argv[1].startswith("scripts/") or "/scripts/" in argv[1]):
        return True
    return (
        Path(argv[0]).name in SHELLS
        and "-c" in argv
        and any(word in commands for word in argv[-1].split())
    )


def product_processes(run: Path, test: dict[str, Any], commands: frozenset[str]) -> int:
    record = run / str(test.get("record"))
    if not record.is_file():
        return 0
    return sum(
        invokes_product(cast(list[str], json.loads(line).get("argv", [])), commands)
        for line in record.read_text(encoding="utf-8").splitlines()
    )


def check_tests(
    run: Path, tests: list[dict[str, Any]], expected: set[str], commands: frozenset[str]
) -> list[str]:
    errors: list[str] = []
    recorded = {str(test.get("id")) for test in tests}
    skipped_scopes = tuple(
        f"{match['scope']}."
        for test in tests
        if test.get("outcome") == "skipped"
        and (match := FIXTURE_HOLDER_RE.match(str(test.get("id"))))
    )
    for missing in sorted(expected - recorded):
        if not missing.startswith(skipped_scopes) or not skipped_scopes:
            errors.append(
                f"{missing}: [e2e-artifacts] did not run; fix: rerun just check --only e2e"
            )
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
        elif product_processes(run, test, commands) == 0:
            errors.append(
                f"{test_id}: [e2e-artifacts] started no product process; fix: drive "
                "html-publish or a repository script, or move the test to tests/isolated/ "
                "with a failure list"
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
    version = manifest.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ScriptError(
            f"{run}/manifest.json has schema_version {version}, expected {SCHEMA_VERSION}; {RERUN}"
        )
    missing = [key for key in REQUIRED_KEYS if key not in manifest]
    if missing:
        raise ScriptError(f"{run}/manifest.json lacks {', '.join(missing)}; {RERUN}")

    errors: list[str] = []
    commit = head_commit(root)
    if manifest["commit"] != commit:
        errors.append(
            f"{run}: [e2e-artifacts] run is from commit {manifest['commit']}, "
            f"checkout is at {commit}; {RERUN}"
        )
    errors += check_source(run, root, manifest["source_fingerprint"])
    errors += check_files(run, cast(dict[str, str], manifest["files"]))
    tests = cast(list[dict[str, Any]], manifest["tests"])
    expected, undiscovered = expected_tests(root)
    errors += undiscovered
    errors += check_tests(run, tests, expected, executables(root))
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
