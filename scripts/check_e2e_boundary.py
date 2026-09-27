#!/usr/bin/env python3
"""Check that E2E tests reach the product only through shipped executables."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ScriptError, run_script
from _test_tree import add_root_argument, imported_modules, is_product_module, test_sources

EPILOG = """\
rule:
  Everything under tests/e2e/ drives html-publish the way a user does: as a
  process against real Git, files, and loopback HTTP. It never imports
  html_publish or scripts, never imports unittest.mock, and never borrows
  helpers from tests/isolated/. A test that needs any of those is an isolated
  test and starts from a failure list. See docs/testing.md.

examples:
  just check --only e2e-boundary
  uv run python scripts/check_e2e_boundary.py --verbose

exit codes: 0 ok, 1 boundary errors found, 2 bad usage, 130 interrupted"""

MOCK_MODULES = frozenset({"mock", "unittest.mock"})

log = logging.getLogger("check-e2e-boundary")


def problem(module: str) -> str | None:
    if is_product_module(module):
        return (
            f"imports {module}; fix: drive the executable (python -m html_publish, "
            "html_publish.remote, html_publish.server, or the installed wheel) instead"
        )
    if module in MOCK_MODULES:
        return "imports a mock library; fix: mocks belong in tests/isolated/ with a failure list"
    if module == "tests.isolated" or module.startswith("tests.isolated."):
        return f"imports {module}; fix: keep E2E support in tests/e2e/"
    return None


def check(root: Path) -> str:
    sources = [source for source in test_sources(root) if source.bucket == "e2e"]
    errors: list[str] = []
    for source in sources:
        log.debug("check %s", source.label)
        reported: set[int] = set()
        for node, module in imported_modules(source.tree):
            message = problem(module)
            if message and node.lineno not in reported:
                reported.add(node.lineno)
                errors.append(f"{source.label}:{node.lineno}: [e2e-boundary] {message}")
    if errors:
        raise ScriptError(*errors)
    return f"ok: {len(sources)} e2e files stay behind the executable boundary"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_e2e_boundary.py",
        description="Check that E2E tests reach the product only through shipped executables",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_root_argument(parser)
    return run_script(parser, lambda args: check(args.root.resolve()), argv)


if __name__ == "__main__":
    raise SystemExit(main())
