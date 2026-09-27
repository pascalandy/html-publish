#!/usr/bin/env python3
"""Check that every test lives in tests/e2e/ or tests/isolated/."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ScriptError, run_script
from _test_tree import BUCKETS, add_root_argument, python_files, test_functions, test_sources

EPILOG = """\
rule:
  Tests come in two buckets. tests/e2e/ drives shipped executables against real
  Git, files, and loopback HTTP. tests/isolated/ tests one system alone and opens
  with its failure list. There is no third bucket for after-the-fact unit tests.
  Inside a bucket, test modules are test_*.py and support modules are _*.py.
  See docs/testing.md.

examples:
  just check --only test-layout
  uv run python scripts/check_test_layout.py --verbose

exit codes: 0 ok, 1 layout errors found, 2 bad usage, 130 interrupted"""

SKIPPED_TREES = frozenset({"tests", ".venv", "venv", "build", "dist", "node_modules"})
FIX_BUCKET = (
    "fix: move it to tests/e2e/ if it drives a shipped executable, "
    "otherwise to tests/isolated/ with a failure list (docs/testing.md)"
)

log = logging.getLogger("check-test-layout")


def stray_test_files(root: Path) -> list[str]:
    """Test modules outside tests/, where discovery would miss them."""
    strays: list[str] = []
    for path in python_files(root):
        relative = path.relative_to(root)
        if relative.parts[0] in SKIPPED_TREES:
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            strays.append(
                f"{relative.as_posix()}: [test-layout] test module outside tests/; {FIX_BUCKET}"
            )
    return strays


def check(root: Path) -> str:
    sources = test_sources(root)
    errors = stray_test_files(root)
    counts = dict.fromkeys(BUCKETS, 0)

    for bucket in BUCKETS:
        if not (root / "tests" / bucket / "__init__.py").is_file():
            errors.append(f"tests/{bucket}/__init__.py: [test-layout] missing package marker")

    for source in sources:
        name = source.path.name
        log.debug("check %s", source.label)
        if source.bucket is None:
            if source.label != "tests/__init__.py":
                errors.append(
                    f"{source.label}: [test-layout] file outside a test bucket; {FIX_BUCKET}"
                )
            continue
        if source.path.parent != root / "tests" / source.bucket:
            errors.append(
                f"{source.label}: [test-layout] nested directory; "
                f"fix: keep modules directly in tests/{source.bucket}/"
            )
        if source.is_test_module:
            counts[source.bucket] += 1
        elif name.startswith("_"):
            for owner, function in test_functions(source.tree):
                errors.append(
                    f"{source.label}:{function.lineno}: [test-layout] "
                    f"{source.test_id(owner, function)} sits in a support module that "
                    "discovery skips; fix: move it into a test_*.py module"
                )
        else:
            errors.append(
                f"{source.label}: [test-layout] name must be test_*.py or _*.py; "
                "fix: rename the module"
            )

    if errors:
        raise ScriptError(*errors)
    return f"ok: {counts['e2e']} e2e and {counts['isolated']} isolated test modules"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_test_layout.py",
        description="Check that every test lives in tests/e2e/ or tests/isolated/",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_root_argument(parser)
    return run_script(parser, lambda args: check(args.root.resolve()), argv)


if __name__ == "__main__":
    raise SystemExit(main())
