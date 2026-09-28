#!/usr/bin/env python3
"""Run the repository verdict: every check in CHECKS, in order, then one summary line."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import Parser, ScriptError, run_script

ROOT = Path(__file__).resolve().parent.parent

EPILOG = """\
Each check is one row of CHECKS in scripts/check.py; add a row to add a check.
Checks run in order: formatting and lint, the test rules, types, isolated tests,
E2E tests, then the E2E artifact audit. A failing check does not stop the others.
--fast skips the E2E rows; lefthook runs it before each commit and runs the full
verdict before each push.

examples:
  just check
  just check --fast
  just check --list --verbose
  just check --only test-smells --only e2e-boundary"""

PYTHON = sys.executable

log = logging.getLogger("check")

Command = tuple[str, ...]


@dataclass(frozen=True)
class Check:
    """A named step whose commands run from the repository root and stop at the first failure."""

    name: str
    commands: tuple[Command, ...]
    fast: bool = True
    timeout: int = 120


def script(name: str) -> Command:
    return (PYTHON, f"scripts/{name}.py")


def takes_verbosity(command: Command) -> bool:
    """Repository scripts and the E2E runner take -v and --debug; ruff, pyright, and unittest
    read their own flags, so they never receive ours."""
    return command[1].startswith("scripts/") or command[1:] == ("-m", "tests.e2e")


# Timeouts stay inside the CI job's 20 minutes so the runner, not the job, reports a hang and
# kills its process group. The isolated and E2E rows take about 50 s and 3 min locally; the E2E
# row takes about 6 min on the Linux runner and longer on the macOS one
CHECKS = [
    Check("format", ((PYTHON, "-m", "ruff", "format", "--check", "."),)),
    Check("lint", ((PYTHON, "-m", "ruff", "check", "."),)),
    Check("test-layout", (script("check_test_layout"),)),
    Check("e2e-boundary", (script("check_e2e_boundary"),)),
    Check("isolated-failure-modes", (script("check_isolated_failure_modes"),)),
    Check("test-smells", (script("check_test_smells"),)),
    Check("test-only-code", (script("check_test_only_code"),)),
    # --pythonpath pins import resolution to this interpreter's environment
    Check("typecheck", ((PYTHON, "-m", "pyright", "--pythonpath", PYTHON),), timeout=180),
    Check(
        "isolated",
        ((PYTHON, "-m", "unittest", "discover", "-s", "tests/isolated", "-t", "."),),
        timeout=240,
    ),
    Check("e2e", ((PYTHON, "-m", "tests.e2e"),), fast=False, timeout=900),
    Check("e2e-artifacts", (script("check_e2e_artifacts"),), fast=False),
]


def execute(command: Command, timeout: int, verbose: bool) -> tuple[int, str]:
    """Run one command in its own process group; kill the whole group when it times out or a
    signal interrupts the runner. A verbose run streams the command's output to stderr, since
    stdout carries only the runner's result."""
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=sys.stderr if verbose else subprocess.PIPE,
        stderr=None if verbose else subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # SIGTERM first, so the command can say where it hung before its group is killed
        os.killpg(process.pid, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        return 124, (output or "") + f"\ntimed out after {timeout} s\n"
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    return process.returncode, output or ""


def passes(check: Check) -> bool:
    """Run one check; quiet runs replay a failing command's output on stderr."""
    verbose = log.isEnabledFor(logging.INFO)
    forwarded = ("-v",) if verbose else ()
    if log.isEnabledFor(logging.DEBUG):
        forwarded += ("--debug",)
    for command in check.commands:
        if takes_verbosity(command):
            command = (*command, *forwarded)
        log.info("==> %s: %s", check.name, shlex.join(command))
        started = time.monotonic()
        code, output = execute(command, check.timeout, verbose)
        log.debug("==> %s: exit %d after %.1f s", check.name, code, time.monotonic() - started)
        if code != 0:
            if not verbose:
                print(f"==> {check.name}: {shlex.join(command)}", file=sys.stderr)
                sys.stderr.write(output)
            return False
    return True


def run(args: argparse.Namespace) -> str:
    selected = [
        check
        for check in CHECKS
        if (not args.only or check.name in args.only) and (check.fast or not args.fast)
    ]
    if args.list:
        for check in selected:
            print(check.name if check.fast else f"{check.name} (full only)", flush=True)
            for command in check.commands:
                log.info("  %s", shlex.join(command))
        return ""
    if not selected:
        raise ScriptError("no check selected; fix: drop --fast or pick a check from --list")

    if any(check.name == "e2e" for check in selected):
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        os.environ.setdefault("HTML_PUBLISH_E2E_RUN_ID", f"e2e-{stamp}-{os.getpid()}")

    failed = [check.name for check in selected if not passes(check)]
    if failed:
        raise ScriptError(*(f"{name} failed; rerun: just check --only {name}" for name in failed))
    return f"ok: {len(selected)} passed"


def main(argv: list[str] | None = None) -> int:
    parser = Parser(
        prog="just check",
        description="Run the repository verdict: the same checks CI runs",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=[check.name for check in CHECKS],
        metavar="NAME",
        help="run only this check; repeat for more (see --list)",
    )
    parser.add_argument(
        "--fast", action="store_true", help="skip the E2E checks (the pre-commit verdict)"
    )
    parser.add_argument("--list", action="store_true", help="print the check names and exit")
    return run_script(parser, run, argv, failure="a check failed")


if __name__ == "__main__":
    raise SystemExit(main())
