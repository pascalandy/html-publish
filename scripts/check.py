#!/usr/bin/env python3
"""Run the repository verdict: every check in CHECKS, in order, then one summary line."""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ScriptError, run_script

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
  just check --only test-smells --only e2e-boundary

exit codes: 0 ok, 1 a check failed, 2 bad usage, 130 interrupted"""

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


CHECKS = [
    Check("format", ((PYTHON, "-m", "ruff", "format", "--check", "."),)),
    Check("lint", ((PYTHON, "-m", "ruff", "check", "."),)),
    Check("test-layout", (script("check_test_layout"),)),
    Check("e2e-boundary", (script("check_e2e_boundary"),)),
    Check("isolated-failure-modes", (script("check_isolated_failure_modes"),)),
    Check("test-smells", (script("check_test_smells"),)),
    Check("test-only-code", (script("check_test_only_code"),)),
    # --pythonpath pins import resolution to this interpreter's environment
    Check("typecheck", ((PYTHON, "-m", "pyright", "--pythonpath", PYTHON),), timeout=600),
    Check(
        "isolated",
        ((PYTHON, "-m", "unittest", "discover", "-s", "tests/isolated", "-t", "."),),
        timeout=900,
    ),
    Check("e2e", ((PYTHON, "-m", "tests.e2e"),), fast=False, timeout=1800),
    Check("e2e-artifacts", (script("check_e2e_artifacts"),), fast=False),
]


def execute(command: Command, timeout: int, verbose: bool) -> tuple[int, str]:
    """Run one command in its own process group; kill the whole group when it times out."""
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=None if verbose else subprocess.PIPE,
        stderr=None if verbose else subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        return 124, (output or "") + f"\ntimed out after {timeout} s\n"
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    return process.returncode, output or ""


def passes(check: Check, verbose: bool) -> bool:
    """Run one check; quiet runs replay a failing command's output on stderr."""
    for command in check.commands:
        log.info("==> %s: %s", check.name, shlex.join(command))
        code, output = execute(command, check.timeout, verbose)
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
        lines: list[str] = []
        for check in selected:
            lines.append(check.name if check.fast else f"{check.name} (full only)")
            if args.verbose:
                lines.extend(f"  {shlex.join(command)}" for command in check.commands)
        return "\n".join(lines)
    if not selected:
        raise ScriptError("no check selected; fix: drop --fast or pick a check from --list")

    if any(check.name == "e2e" for check in selected):
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        os.environ.setdefault("HTML_PUBLISH_E2E_RUN_ID", f"e2e-{stamp}-{os.getpid()}")

    failed = [check.name for check in selected if not passes(check, args.verbose)]
    if failed:
        raise ScriptError(*(f"{name} failed; rerun: just check --only {name}" for name in failed))
    return f"ok: {len(selected)} passed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
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
    return run_script(parser, run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
