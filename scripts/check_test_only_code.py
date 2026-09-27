#!/usr/bin/env python3
"""Check that no public product definition exists only for tests."""

from __future__ import annotations

import argparse
import ast
import logging
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ScriptError, run_script
from _test_tree import add_root_argument, python_files

EPILOG = """\
rule:
  Every public top-level function, class, and constant in html_publish/ has a
  caller outside tests/: product code, repository scripts, skill scripts, or a
  [project.scripts] entry point. A definition only tests reach is a test-only
  seam; delete it, or move its test to the real boundary. Matching is by name,
  so a shared name anywhere in product code counts as a caller.
  See docs/testing.md.

examples:
  just check --only test-only-code
  uv run python scripts/check_test_only_code.py --verbose

exit codes: 0 ok, 1 test-only definitions found, 2 bad usage, 130 interrupted"""

PACKAGE = "html_publish"
CALLER_TREES = (PACKAGE, "scripts", ".agents")

log = logging.getLogger("check-test-only-code")


def definitions(tree: ast.Module) -> Iterator[tuple[str, int]]:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield node.name, node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target.id, node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            yield node.target.id, node.lineno


def references(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load | ast.Del):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


def entry_points(root: Path) -> set[str]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return set()
    scripts = (
        tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("scripts", {})
    )
    return {str(target).rsplit(":", 1)[-1] for target in dict(scripts).values()}


def parsed(paths: list[Path]) -> dict[Path, ast.Module]:
    return {path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path)) for path in paths}


def check(root: Path) -> str:
    product = parsed(python_files(root / PACKAGE))
    caller_paths = [
        path
        for tree in CALLER_TREES
        if (root / tree).is_dir()
        for path in python_files(root / tree)
    ]
    callers: set[str] = entry_points(root)
    for tree in parsed(caller_paths).values():
        callers |= references(tree)
    tested: set[str] = set()
    for tree in parsed(python_files(root / "tests") if (root / "tests").is_dir() else []).values():
        tested |= references(tree)

    errors: list[str] = []
    count = 0
    for path, tree in product.items():
        label = path.relative_to(root).as_posix()
        for name, line in definitions(tree):
            if name.startswith("_"):
                continue
            count += 1
            if name not in callers and name in tested:
                log.debug("test-only %s:%s", label, name)
                errors.append(
                    f"{label}:{line}: [test-only-code] {name} is used only by tests; "
                    "fix: delete it, or move its test to the boundary a user reaches"
                )
    if errors:
        raise ScriptError(*errors)
    return f"ok: {count} public definitions have a caller outside tests"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_test_only_code.py",
        description="Check that no public product definition exists only for tests",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_root_argument(parser)
    return run_script(parser, lambda args: check(args.root.resolve()), argv)


if __name__ == "__main__":
    raise SystemExit(main())
