"""Read the test tree the rule checks share: each file's bucket, tests, and product imports."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent
PRODUCT_PACKAGES = frozenset({"html_publish", "scripts"})

Bucket = Literal["e2e", "isolated"]
BUCKETS: tuple[Bucket, ...] = ("e2e", "isolated")
TestFunction = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class SourceFile:
    """One parsed Python file under the checked root."""

    root: Path
    path: Path
    tree: ast.Module

    @property
    def label(self) -> str:
        return self.path.relative_to(self.root).as_posix()

    @property
    def bucket(self) -> Bucket | None:
        parts = self.path.relative_to(self.root / "tests").parts
        if len(parts) > 1 and parts[0] == "e2e":
            return "e2e"
        if len(parts) > 1 and parts[0] == "isolated":
            return "isolated"
        return None

    @property
    def is_test_module(self) -> bool:
        return self.path.name.startswith("test_")

    def test_id(self, owner: ast.ClassDef | None, function: TestFunction) -> str:
        parts = [self.label, *([owner.name] if owner else []), function.name]
        return "::".join(parts)


def project_root(value: str) -> Path:
    """Parse --root; a directory without tests/ and html_publish/ is bad usage, not a clean tree."""
    root = Path(value).resolve()
    missing = [name for name in ("tests", "html_publish") if not (root / name).is_dir()]
    if missing:
        names = " or ".join(f"{name}/" for name in missing)
        raise argparse.ArgumentTypeError(f"{root} has no {names}; pass the repository root")
    return root


def add_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=project_root,
        default=ROOT,
        help="repository root to check (default: this checkout)",
    )


def parse(root: Path, path: Path) -> SourceFile:
    source = path.read_text(encoding="utf-8")
    return SourceFile(root, path, ast.parse(source, filename=str(path)))


def python_files(directory: Path) -> list[Path]:
    """Every .py file below `directory`, skipping caches and hidden directories."""
    return sorted(
        path
        for path in directory.rglob("*.py")
        if not any(
            part.startswith(".") or part == "__pycache__"
            for part in path.relative_to(directory).parts
        )
    )


def test_sources(root: Path) -> list[SourceFile]:
    """Every Python file under tests/, parsed."""
    tests = root / "tests"
    return [parse(root, path) for path in python_files(tests)] if tests.is_dir() else []


def module_statements(nodes: Iterable[ast.AST]) -> Iterator[ast.stmt]:
    """Statements that run in the module namespace, including those inside if, try, with, for,
    while, and match blocks, but not inside a function or class body."""
    for node in nodes:
        if isinstance(node, ast.stmt):
            yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield from module_statements(ast.iter_child_nodes(node))


def test_functions(tree: ast.Module) -> Iterator[tuple[ast.ClassDef | None, TestFunction]]:
    """Module-level test functions and test methods of module-level classes."""
    for node in module_statements(tree.body):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test"
        ):
            yield None, node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(
                    item, ast.FunctionDef | ast.AsyncFunctionDef
                ) and item.name.startswith("test"):
                    yield node, item


def undiscovered_tests(source: SourceFile, check: str) -> list[str]:
    """One error per module-level test function in a test module; unittest runs only methods."""
    if not source.is_test_module:
        return []
    return [
        f"{source.label}:{function.lineno}: [{check}] {source.test_id(owner, function)} is a "
        "module-level function that unittest never runs; fix: make it a method of a "
        "unittest.TestCase subclass"
        for owner, function in test_functions(source.tree)
        if owner is None
    ]


def imported_modules(tree: ast.Module) -> Iterator[tuple[ast.stmt, str]]:
    """Each import statement with every module it names, including `from X import Y` as X.Y."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node, node.module
            for alias in node.names:
                yield node, f"{node.module}.{alias.name}"


def is_product_module(name: str) -> bool:
    return name.split(".", 1)[0] in PRODUCT_PACKAGES
