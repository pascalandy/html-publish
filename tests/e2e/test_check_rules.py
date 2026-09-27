from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULE_SCRIPTS = (
    "_common.py",
    "_test_tree.py",
    "check.py",
    "check_test_layout.py",
    "check_e2e_boundary.py",
    "check_isolated_failure_modes.py",
    "check_test_smells.py",
    "check_test_only_code.py",
    "check_e2e_artifacts.py",
)

CLEAN_TREE = {
    "pyproject.toml": '[project.scripts]\nhtml-publish = "html_publish.cli:main"\n',
    "html_publish/__init__.py": "",
    "html_publish/store.py": "def publish() -> None: ...\n\n\ndef _fsync() -> None: ...\n",
    "html_publish/cli.py": (
        "from html_publish.store import publish\n\n\ndef main() -> None:\n    publish()\n"
    ),
    "tests/__init__.py": "",
    "tests/e2e/__init__.py": "",
    "tests/isolated/__init__.py": "",
    "tests/e2e/test_cli.py": """\
        import subprocess
        import sys
        import unittest


        class CliTest(unittest.TestCase):
            def test_child_prints(self) -> None:
                result = subprocess.run(
                    [sys.executable, "-c", "print('hi')"], capture_output=True, text=True
                )
                self.assertEqual(result.stdout, "hi\\n")
        """,
    "tests/isolated/test_store.py": '''\
        """Store faults.

        Failure modes:
        F1: store: an fsync failure is reported as success
        """

        import unittest

        from html_publish import store


        class StoreTest(unittest.TestCase):
            def test_fsync_failure(self) -> None:
                """Proves F1."""
                self.assertIsNone(store.publish())
        ''',
}


def edited(relative: str, old: str, new: str) -> dict[str, str | None]:
    """The clean fixture file with one substitution; a missing `old` is a broken test."""
    content = CLEAN_TREE[relative]
    if old not in content:
        raise ValueError(f"{old!r} is not in the clean {relative}")
    return {relative: content.replace(old, new)}


class RuleScriptTest(unittest.TestCase):
    def fixture(self, changes: dict[str, str | None] | None = None) -> Path:
        """A throwaway repository holding the real rule scripts and a clean test tree."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-rules-")))
        (root / "scripts").mkdir()
        for name in RULE_SCRIPTS:
            shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
        (root / "tests" / "e2e").mkdir(parents=True)
        shutil.copy2(ROOT / "tests" / "e2e" / "__main__.py", root / "tests" / "e2e" / "__main__.py")
        for relative, content in {**CLEAN_TREE, **(changes or {})}.items():
            path = root / relative
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(content), encoding="utf-8")
        return root

    def run_script(self, root: Path, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, f"scripts/{name}.py", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_each_rule_accepts_the_clean_tree(self) -> None:
        root = self.fixture()
        for name, summary in (
            ("check_test_layout", "ok: 1 e2e and 1 isolated test modules\n"),
            ("check_e2e_boundary", "ok: 3 e2e files stay behind the executable boundary\n"),
            (
                "check_isolated_failure_modes",
                "ok: 1 isolated tests cite the failures listed in 1 modules\n",
            ),
            ("check_test_smells", "ok: 2 tests free of known smells\n"),
            ("check_test_only_code", "ok: 2 public definitions have a caller outside tests\n"),
        ):
            with self.subTest(name=name):
                result = self.run_script(root, name)
                self.assertEqual((result.returncode, result.stdout), (0, summary), result.stderr)

    def test_each_rule_rejects_its_planted_bad_test(self) -> None:
        cases: list[tuple[str, dict[str, str | None], str]] = [
            (
                "check_test_layout",
                {"tests/test_loose.py": "def test_x() -> None:\n    assert True\n"},
                "error: tests/test_loose.py: [test-layout] file outside a test bucket; "
                "fix: move it to tests/e2e/ if it drives a shipped executable, "
                "otherwise to tests/isolated/ with a failure list (docs/testing.md)\n",
            ),
            (
                "check_test_layout",
                {"tests/e2e/_support.py": "def test_hidden() -> None:\n    assert True\n"},
                "error: tests/e2e/_support.py:1: [test-layout] "
                "tests/e2e/_support.py::test_hidden sits in a support module that discovery "
                "skips; fix: move it into a test_*.py module\n",
            ),
            (
                "check_e2e_boundary",
                {"tests/e2e/test_cli.py": "from html_publish import store\n"},
                "error: tests/e2e/test_cli.py:1: [e2e-boundary] imports html_publish; "
                "fix: drive the executable (python -m html_publish, html_publish.remote, "
                "html_publish.server, or the installed wheel) instead\n",
            ),
            (
                "check_e2e_boundary",
                {"tests/e2e/test_cli.py": "from unittest import mock\n"},
                "error: tests/e2e/test_cli.py:1: [e2e-boundary] imports a mock library; "
                "fix: mocks belong in tests/isolated/ with a failure list\n",
            ),
            (
                "check_isolated_failure_modes",
                edited("tests/isolated/test_store.py", "F1: store", "Nothing listed: store"),
                'error: tests/isolated/test_store.py:1: [isolated-failure-modes] "Failure modes:" '
                'lists no entries; fix: add lines such as "F1: store: <how it fails>"\n',
            ),
            (
                "check_isolated_failure_modes",
                edited(
                    "tests/isolated/test_store.py", '"""Proves F1."""', '"""Checks the store."""'
                ),
                "error: tests/isolated/test_store.py:13: [isolated-failure-modes] "
                "tests/isolated/test_store.py::StoreTest::test_fsync_failure cites no failure; "
                'fix: open its docstring with the IDs it proves, e.g. """Proves F1."""\n',
            ),
            (
                "check_isolated_failure_modes",
                edited(
                    "tests/isolated/test_store.py",
                    "is reported as success\n",
                    "is reported as success\n        F2: store: lost lock\n",
                ),
                "error: tests/isolated/test_store.py:1: [isolated-failure-modes] F2 has no test; "
                "fix: prove it with a test or remove it from the list\n",
            ),
            (
                "check_test_smells",
                edited(
                    "tests/e2e/test_cli.py",
                    'self.assertEqual(result.stdout, "hi\\n")',
                    "print(result.stdout)",
                ),
                "error: tests/e2e/test_cli.py:7: [no-assertion] "
                "tests/e2e/test_cli.py::CliTest::test_child_prints asserts nothing; "
                "fix: assert the observable result, or delete the test\n",
            ),
            (
                "check_test_smells",
                edited(
                    "tests/e2e/test_cli.py",
                    'self.assertEqual(result.stdout, "hi\\n")',
                    "self.assertEqual(result.stdout, result.stdout)",
                ),
                "error: tests/e2e/test_cli.py:11: [self-comparison] "
                "tests/e2e/test_cli.py::CliTest::test_child_prints compares a value with itself; "
                "fix: compare with a literal expected value\n",
            ),
            (
                "check_test_smells",
                edited("tests/isolated/test_store.py", "store.publish()", "store._fsync()"),
                "error: tests/isolated/test_store.py:15: [private-access] calls private "
                "store._fsync; fix: assert through the public boundary; wrapping or patching a "
                "helper for fault injection is fine\n",
            ),
            (
                "check_test_smells",
                {"tests/isolated/test_store.py": "# pyright: reportPrivateUsage=false\n"},
                "error: tests/isolated/test_store.py:1: [private-pragma] silences "
                "reportPrivateUsage; fix: test through the public boundary instead\n",
            ),
            (
                "check_test_only_code",
                {
                    "html_publish/store.py": CLEAN_TREE["html_publish/store.py"]
                    + "\n\ndef seam() -> int:\n    return 1\n",
                    "tests/isolated/test_seam.py": "from html_publish.store import seam\n",
                },
                "error: html_publish/store.py:7: [test-only-code] seam is used only by tests; "
                "fix: delete it, or move its test to the boundary a user reaches\n",
            ),
        ]
        for name, changes, expected in cases:
            with self.subTest(name=name, expected=expected.split(": ", 2)[1]):
                result = self.run_script(self.fixture(changes), name)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertTrue(result.stderr.endswith("rerun with --verbose for details\n"))

    def test_the_runner_lists_checks_in_order_and_reports_every_failure(self) -> None:
        listed = self.run_script(ROOT, "check", "--list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(
            listed.stdout.splitlines(),
            [
                "format",
                "lint",
                "test-layout",
                "e2e-boundary",
                "isolated-failure-modes",
                "test-smells",
                "test-only-code",
                "typecheck",
                "isolated",
                "e2e (full only)",
                "e2e-artifacts (full only)",
            ],
        )
        fast = self.run_script(ROOT, "check", "--fast", "--list")
        self.assertEqual(fast.stdout.splitlines()[-1], "isolated")

        root = self.fixture({"tests/test_loose.py": "def test_x() -> None:\n    pass\n"})
        failed = self.run_script(
            root,
            "check",
            "--only",
            "test-layout",
            "--only",
            "test-smells",
            "--only",
            "e2e-boundary",
        )
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        self.assertTrue(
            failed.stderr.endswith(
                "error: test-layout failed; rerun: just check --only test-layout\n"
                "error: test-smells failed; rerun: just check --only test-smells\n"
                "rerun with --verbose for details\n"
            ),
            failed.stderr,
        )

    def test_e2e_artifacts_prove_each_run_and_catch_tampering(self) -> None:
        root = self.fixture()
        artifacts_root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-runs-")))
        for command in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-qm",
                "fixture",
            ],
        ):
            subprocess.run(command, cwd=root, check=True, capture_output=True, timeout=30)
        environment = {**os.environ, "HTML_PUBLISH_E2E_ROOT": str(artifacts_root)}

        def record(run_id: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "tests.e2e"],
                cwd=root,
                env={**environment, "HTML_PUBLISH_E2E_RUN_ID": run_id},
                capture_output=True,
                text=True,
                timeout=120,
            )

        def audit(run_id: str) -> subprocess.CompletedProcess[str]:
            return self.run_script(
                root,
                "check_e2e_artifacts",
                "--run-id",
                run_id,
                "--artifacts-root",
                str(artifacts_root),
            )

        recorded = record("e2e-good")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        run = artifacts_root / "e2e-good" / "artifacts"
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(manifest["commit"], head)
        [test] = manifest["tests"]
        self.assertEqual(
            (test["id"], test["outcome"], test["processes"], test["rerun"]),
            (
                "tests.e2e.test_cli.CliTest.test_child_prints",
                "passed",
                1,
                "uv run python -m unittest tests.e2e.test_cli.CliTest.test_child_prints",
            ),
        )
        [line] = (run / test["record"]).read_text(encoding="utf-8").splitlines()
        process = json.loads(line)
        self.assertEqual((process["returncode"], process["stdout"]["head"]), (0, "hi\n"))
        passed = audit("e2e-good")
        self.assertEqual(
            (passed.returncode, passed.stdout),
            (0, f"ok: 1 e2e tests left verified records in {run}\n"),
            passed.stderr,
        )

        (run / test["record"]).write_text("{}\n", encoding="utf-8")
        tampered = audit("e2e-good")
        self.assertEqual(tampered.returncode, 1)
        self.assertIn(
            f"error: {run / test['record']}: [e2e-artifacts] sha256 differs from the manifest; "
            "the record changed after the run\n",
            tampered.stderr,
        )

        (root / "tests" / "e2e" / "test_quiet.py").write_text(
            "import unittest\n\n\nclass QuietTest(unittest.TestCase):\n"
            "    def test_nothing_runs(self) -> None:\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self.assertEqual(record("e2e-quiet").returncode, 0)
        quiet = audit("e2e-quiet")
        self.assertEqual(quiet.returncode, 1)
        self.assertIn(
            "error: tests.e2e.test_quiet.QuietTest.test_nothing_runs: [e2e-artifacts] started no "
            "process; fix: drive a shipped executable, or move the test to tests/isolated/ with a "
            "failure list\n",
            quiet.stderr,
        )


if __name__ == "__main__":
    unittest.main()
