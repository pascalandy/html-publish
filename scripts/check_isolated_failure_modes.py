#!/usr/bin/env python3
"""Check that each isolated test module starts from its failure list and covers it."""

from __future__ import annotations

import argparse
import ast
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import Parser, ScriptError, run_script
from _test_tree import SourceFile, add_root_argument, test_functions, test_sources

EPILOG = """\
rule:
  Test a system in isolation only after writing down how it can fail. Each
  tests/isolated/test_*.py module docstring holds a "Failure modes:" section
  with one numbered line per failure:

      Failure modes:
      F1: store: an fsync failure after selection is reported as success
      F2: store: a kill before the ref advances blocks the retry

  Each test's docstring names the failure IDs it proves, such as \"\"\"Proves F2.\"\"\".
  Every listed failure needs at least one test, and every test cites a listed
  failure. See docs/testing.md.

examples:
  just check --only isolated-failure-modes
  uv run python scripts/check_isolated_failure_modes.py --verbose"""

HEADING = "Failure modes:"
ENTRY_RE = re.compile(r"^\s*(F[1-9][0-9]*)[:.]\s+\S")
CITATION_RE = re.compile(r"\bF[1-9][0-9]*\b")

log = logging.getLogger("check-isolated-failure-modes")


def failure_list(source: SourceFile) -> tuple[set[str], list[str]]:
    """Return the failure IDs the module docstring lists, plus problems with the list."""
    docstring = ast.get_docstring(source.tree, clean=False)
    if docstring is None or HEADING not in docstring:
        return set(), [
            f'{source.label}:1: [isolated-failure-modes] module docstring has no "{HEADING}" '
            "section; fix: list how the system under test can fail (F1, F2, ...) before its tests"
        ]
    entries: set[str] = set()
    problems: list[str] = []
    for line in docstring.split(HEADING, 1)[1].splitlines():
        match = ENTRY_RE.match(line)
        if match is None:
            continue
        identifier = match.group(1)
        if identifier in entries:
            problems.append(
                f"{source.label}:1: [isolated-failure-modes] {identifier} is listed twice; "
                "fix: give each failure its own ID"
            )
        entries.add(identifier)
    if not entries:
        problems.append(
            f'{source.label}:1: [isolated-failure-modes] "{HEADING}" lists no entries; '
            'fix: add lines such as "F1: store: <how it fails>"'
        )
    return entries, problems


def check_module(source: SourceFile) -> tuple[int, list[str]]:
    entries, errors = failure_list(source)
    covered: set[str] = set()
    tests = 0
    for owner, function in test_functions(source.tree):
        tests += 1
        test_id = source.test_id(owner, function)
        citations = set(CITATION_RE.findall(ast.get_docstring(function) or ""))
        if not citations:
            errors.append(
                f"{source.label}:{function.lineno}: [isolated-failure-modes] {test_id} cites "
                'no failure; fix: open its docstring with the IDs it proves, e.g. """Proves F1."""'
            )
        for identifier in sorted(citations - entries):
            errors.append(
                f"{source.label}:{function.lineno}: [isolated-failure-modes] {test_id} cites "
                f"{identifier}, which the module failure list does not define"
            )
        covered |= citations
    for identifier in sorted(entries - covered, key=lambda value: int(value[1:])):
        errors.append(
            f"{source.label}:1: [isolated-failure-modes] {identifier} has no test; "
            "fix: prove it with a test or remove it from the list"
        )
    return tests, errors


def check(root: Path) -> str:
    modules = [
        source
        for source in test_sources(root)
        if source.bucket == "isolated" and source.is_test_module
    ]
    errors: list[str] = []
    tests = 0
    for source in modules:
        log.info("check %s", source.label)
        count, problems = check_module(source)
        tests += count
        errors.extend(problems)
    if errors:
        raise ScriptError(*errors)
    return f"ok: {tests} isolated tests cite the failures listed in {len(modules)} modules"


def main(argv: list[str] | None = None) -> int:
    parser = Parser(
        prog="check_isolated_failure_modes.py",
        description="Check that isolated test modules start from a failure list and cover it",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_root_argument(parser)
    return run_script(
        parser, lambda args: check(args.root.resolve()), argv, failure="failure-list errors found"
    )


if __name__ == "__main__":
    raise SystemExit(main())
