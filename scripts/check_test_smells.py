#!/usr/bin/env python3
"""Check tests for low-signal patterns that pass without protecting behavior."""

from __future__ import annotations

import argparse
import ast
import io
import logging
import re
import sys
import tokenize
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ScriptError, run_script
from _test_tree import (
    SourceFile,
    add_root_argument,
    is_product_module,
    test_functions,
    test_sources,
)

EPILOG = """\
rules:
  no-assertion      a test must assert something, directly or through a helper
  self-comparison   an equality assertion must not compare a value with itself
  private-access    tests must not import or call private (_name) product helpers;
                    reading one to wrap or patch it for fault injection is fine
  private-pragma    tests must not silence pyright's reportPrivateUsage
  See docs/testing.md.

examples:
  just check --only test-smells
  uv run python scripts/check_test_smells.py --verbose

exit codes: 0 ok, 1 smells found, 2 bad usage, 130 interrupted"""

EQUALITY_ASSERTIONS = frozenset(
    {
        "assertEqual",
        "assertIs",
        "assertListEqual",
        "assertDictEqual",
        "assertTupleEqual",
        "assertSetEqual",
        "assertSequenceEqual",
        "assertCountEqual",
        "assertMultiLineEqual",
    }
)
PRIVATE_PRAGMA_RE = re.compile(r"reportPrivateUsage\s*=\s*false")

log = logging.getLogger("check-test-smells")

Function = ast.FunctionDef | ast.AsyncFunctionDef


def callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def asserts_directly(function: Function) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Raise) and node.exc is not None:
            raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(raised, ast.Name) and raised.id == "AssertionError":
                return True
        if isinstance(node, ast.Call):
            name = callee_name(node)
            if name is not None and (name.startswith("assert") or name == "fail"):
                return True
    return False


def called_names(function: Function) -> set[str]:
    return {
        name
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and (name := callee_name(node)) is not None
    }


def asserting_helpers(sources: list[SourceFile]) -> set[str]:
    """Names of test-side functions that assert, directly or through other such functions."""
    functions = [
        node
        for source in sources
        for node in ast.walk(source.tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    asserting = {function.name for function in functions if asserts_directly(function)}
    changed = True
    while changed:
        changed = False
        for function in functions:
            if function.name not in asserting and called_names(function) & asserting:
                asserting.add(function.name)
                changed = True
    return asserting


def contains_call(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Call) for child in ast.walk(node))


def self_comparisons(function: Function) -> Iterator[ast.expr]:
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and callee_name(node) in EQUALITY_ASSERTIONS
            and len(node.args) >= 2
            and ast.dump(node.args[0]) == ast.dump(node.args[1])
            and not contains_call(node.args[0])
        ) or (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq | ast.Is)
            and ast.dump(node.left) == ast.dump(node.comparators[0])
            and not contains_call(node.left)
        ):
            yield node


def private_pragmas(source: SourceFile) -> Iterator[int]:
    """Lines whose comments silence reportPrivateUsage; string literals do not count."""
    text = source.path.read_text(encoding="utf-8")
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT and PRIVATE_PRAGMA_RE.search(token.string):
            yield token.start[0]


def is_private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def is_module(root: Path, dotted: str) -> bool:
    path = root.joinpath(*dotted.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def private_access(root: Path, source: SourceFile) -> Iterator[tuple[int, str]]:
    """Private product names a test imports or calls."""
    aliases: set[str] = set()
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_product_module(alias.name):
                    aliases.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module and is_product_module(node.module):
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if is_module(root, dotted):
                    aliases.add(alias.asname or alias.name)
                elif is_private(alias.name):
                    yield node.lineno, f"imports private {dotted}"
    for node in ast.walk(source.tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and is_private(node.func.attr)
            and (base := dotted_name(node.func.value)) is not None
            and base.split(".", 1)[0] in aliases
        ):
            yield node.lineno, f"calls private {base}.{node.func.attr}"
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "__dict__"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and is_private(node.slice.value)
            and (base := dotted_name(node.value.value)) is not None
            and base.split(".", 1)[0] in aliases
        ):
            yield node.lineno, f"reads private {base}.{node.slice.value} through __dict__"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and is_private(node.args[1].value)
            and (base := dotted_name(node.args[0])) is not None
            and base.split(".", 1)[0] in aliases
        ):
            yield node.lineno, f"reads private {base}.{node.args[1].value} through getattr"


def check(root: Path) -> str:
    sources = test_sources(root)
    asserting = asserting_helpers(sources)
    errors: list[str] = []
    tests = 0
    for source in sources:
        log.debug("check %s", source.label)
        for line_number in private_pragmas(source):
            errors.append(
                f"{source.label}:{line_number}: [private-pragma] silences reportPrivateUsage; "
                "fix: test through the public boundary instead"
            )
        for line_number, what in private_access(root, source):
            errors.append(
                f"{source.label}:{line_number}: [private-access] {what}; fix: assert through the "
                "public boundary; wrapping or patching a helper for fault injection is fine"
            )
        if not source.is_test_module:
            continue
        for owner, function in test_functions(source.tree):
            tests += 1
            test_id = source.test_id(owner, function)
            if not asserts_directly(function) and not called_names(function) & asserting:
                errors.append(
                    f"{source.label}:{function.lineno}: [no-assertion] {test_id} asserts nothing; "
                    "fix: assert the observable result, or delete the test"
                )
            for node in self_comparisons(function):
                errors.append(
                    f"{source.label}:{node.lineno}: [self-comparison] "
                    f"{test_id} compares a value with itself; "
                    "fix: compare with a literal expected value"
                )
    if errors:
        raise ScriptError(*errors)
    return f"ok: {tests} tests free of known smells"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_test_smells.py",
        description="Check tests for low-signal patterns that pass without protecting behavior",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_root_argument(parser)
    return run_script(parser, lambda args: check(args.root.resolve()), argv)


if __name__ == "__main__":
    raise SystemExit(main())
