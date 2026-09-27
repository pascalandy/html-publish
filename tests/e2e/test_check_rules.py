from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Callable
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
E2E_SUPPORT = ("__main__.py", "_fingerprint.py")
VERBOSE_HINT = "rerun with --verbose for details\n"
# A Git hook exports GIT_DIR and related variables. A fixture command that inherited them would
# act on the repository running the hook, not on the throwaway one, so none of them pass through
FIXTURE_ENVIRONMENT = {
    key: value for key, value in os.environ.items() if not key.startswith("GIT_")
}

CLEAN_TREE = {
    ".gitignore": "__pycache__/\n*.pyc\ndist/\n",
    "pyproject.toml": '[project.scripts]\nhtml-publish = "html_publish.cli:main"\n',
    "html_publish/__init__.py": "",
    "html_publish/__main__.py": 'print("hi")\n',
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
                result = subprocess.run(product(), capture_output=True, text=True)
                self.assertEqual(result.stdout, "hi\\n")


        def product() -> list[str]:
            return [sys.executable, "-m", "html_publish"]
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


def appended(relative: str, text: str) -> dict[str, str | None]:
    """The clean fixture file with `text` added at the end."""
    return {relative: textwrap.dedent(CLEAN_TREE[relative]) + text}


class RuleScriptTest(unittest.TestCase):
    def fixture(self, changes: dict[str, str | None] | None = None) -> Path:
        """A throwaway repository holding the real rule scripts and a clean test tree."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-rules-")))
        (root / "scripts").mkdir()
        for name in RULE_SCRIPTS:
            shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
        (root / "tests" / "e2e").mkdir(parents=True)
        for name in E2E_SUPPORT:
            shutil.copy2(ROOT / "tests" / "e2e" / name, root / "tests" / "e2e" / name)
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
            env=FIXTURE_ENVIRONMENT,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def git(self, root: Path, *args: str) -> str:
        return subprocess.run(
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
                *args,
            ],
            cwd=root,
            env=FIXTURE_ENVIRONMENT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

    def committed_fixture(self) -> tuple[Path, Path]:
        """The clean fixture committed to a new repository, and an empty directory for runs."""
        root = self.fixture()
        self.git(root, "init", "-q")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "fixture")
        return root, Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-runs-")))

    def record(self, root: Path, runs: Path, run_id: str) -> subprocess.CompletedProcess[str]:
        """Run the fixture's E2E suite through the recorder, as just check does."""
        return subprocess.run(
            [sys.executable, "-m", "tests.e2e"],
            cwd=root,
            env={
                **FIXTURE_ENVIRONMENT,
                "HTML_PUBLISH_E2E_ROOT": str(runs),
                "HTML_PUBLISH_E2E_RUN_ID": run_id,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )

    def audit(
        self, root: Path, runs: Path, run_id: str, *args: str
    ) -> subprocess.CompletedProcess[str]:
        return self.run_script(
            root, "check_e2e_artifacts", "--run-id", run_id, "--artifacts-root", str(runs), *args
        )

    def test_each_rule_accepts_the_clean_tree(self) -> None:
        root = self.fixture()
        for name, summary in (
            ("check_test_layout", "ok: 1 e2e and 1 isolated test modules\n"),
            ("check_e2e_boundary", "ok: 4 e2e files stay behind the executable boundary\n"),
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

    def test_a_root_without_the_project_is_bad_usage(self) -> None:
        empty = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-empty-")))
        for name in (
            "check_test_layout",
            "check_e2e_boundary",
            "check_isolated_failure_modes",
            "check_test_smells",
            "check_test_only_code",
            "check_e2e_artifacts",
        ):
            with self.subTest(name=name):
                result = self.run_script(ROOT, name, "--root", str(empty))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(
                    f"error: argument --root: {empty.resolve()} has no tests/ or html_publish/; "
                    "pass the repository root\n",
                    result.stderr,
                )

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
                "check_test_layout",
                appended(
                    "tests/e2e/test_cli.py", "\n\ndef test_unrun() -> None:\n    assert False\n"
                ),
                "error: tests/e2e/test_cli.py:16: [test-layout] tests/e2e/test_cli.py::test_unrun "
                "is a module-level function that unittest never runs; fix: make it a method of a "
                "unittest.TestCase subclass\n",
            ),
            (
                "check_test_layout",
                appended(
                    "tests/isolated/test_store.py",
                    "\n\ndef test_unrun() -> None:\n    assert False\n",
                ),
                "error: tests/isolated/test_store.py:18: [test-layout] "
                "tests/isolated/test_store.py::test_unrun is a module-level function that unittest "
                "never runs; fix: make it a method of a unittest.TestCase subclass\n",
            ),
            (
                "check_test_layout",
                appended(
                    "tests/e2e/test_cli.py",
                    "\n\nif True:\n\n    def test_in_a_block() -> None:\n        assert False\n",
                ),
                "error: tests/e2e/test_cli.py:18: [test-layout] "
                "tests/e2e/test_cli.py::test_in_a_block is a module-level function that unittest "
                "never runs; fix: make it a method of a unittest.TestCase subclass\n",
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
                    "self.failing_step(result)",
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
                "error: tests/e2e/test_cli.py:9: [self-comparison] "
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
                {
                    **edited(
                        "tests/e2e/test_cli.py",
                        'self.assertEqual(result.stdout, "hi\\n")',
                        "helper(result)",
                    ),
                    "tests/e2e/test_other.py": (
                        "def helper(value: object) -> None:\n    assert value\n"
                    ),
                },
                "error: tests/e2e/test_cli.py:7: [no-assertion] "
                "tests/e2e/test_cli.py::CliTest::test_child_prints asserts nothing; "
                "fix: assert the observable result, or delete the test\n",
            ),
            (
                "check_test_smells",
                edited(
                    "tests/isolated/test_store.py",
                    "self.assertIsNone(store.publish())",
                    'self.assertIsNotNone(getattr(store, "_fsync"))',
                ),
                "error: tests/isolated/test_store.py:15: [private-access] reads private "
                "store._fsync through getattr; fix: to wrap it for fault injection, read it as a "
                "plain attribute with # pyright: ignore[reportPrivateUsage]; getattr and __dict__ "
                "hide the read from pyright\n",
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
        root, artifacts_root = self.committed_fixture()
        recorded = self.record(root, artifacts_root, "e2e-good")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        run = artifacts_root / "e2e-good" / "artifacts"
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (manifest["schema_version"], manifest["commit"], manifest["dirty"]),
            (2, self.git(root, "rev-parse", "HEAD"), False),
        )
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
        passed = self.audit(root, artifacts_root, "e2e-good")
        self.assertEqual(
            (passed.returncode, passed.stdout),
            (0, f"ok: 1 e2e tests left verified records in {run}\n"),
            passed.stderr,
        )

        (run / test["record"]).write_text("{}\n", encoding="utf-8")
        tampered = self.audit(root, artifacts_root, "e2e-good")
        self.assertEqual(tampered.returncode, 1)
        self.assertIn(
            f"error: {run / test['record']}: [e2e-artifacts] sha256 differs from the manifest; "
            "the record changed after the run\n",
            tampered.stderr,
        )

        (root / "tests" / "e2e" / "test_quiet.py").write_text(
            textwrap.dedent(
                """\
                import subprocess
                import sys
                import unittest


                class QuietTest(unittest.TestCase):
                    def test_nothing_runs(self) -> None:
                        self.assertTrue(True)

                    def test_only_git_runs(self) -> None:
                        self.assertEqual(subprocess.run(["git", "--version"]).returncode, 0)

                    def test_popen_drives_the_product(self) -> None:
                        command = [sys.executable, "-m", "html_publish"]
                        process = subprocess.Popen(command, stdout=subprocess.DEVNULL)
                        self.assertEqual(process.wait(), 0)


                class SkippedClassTest(unittest.TestCase):
                    @classmethod
                    def setUpClass(cls) -> None:
                        raise unittest.SkipTest("needs systemd")

                    def test_never_runs(self) -> None:
                        self.fail("setUpClass skipped this class")


                def test_free_function_never_runs() -> None:
                    assert False


                if sys.platform == "no-such-platform":

                    class ElsewhereTest(unittest.TestCase):
                        def test_only_elsewhere(self) -> None:
                            self.fail("this class exists only on another platform")
                """
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.record(root, artifacts_root, "e2e-quiet").returncode, 0)
        quiet = self.audit(root, artifacts_root, "e2e-quiet")
        self.assertEqual(quiet.returncode, 1)
        for name in ("test_nothing_runs", "test_only_git_runs"):
            self.assertIn(
                f"error: tests.e2e.test_quiet.QuietTest.{name}: [e2e-artifacts] started no "
                "product process; fix: drive html-publish or a repository script, or move the "
                "test to tests/isolated/ with a failure list\n",
                quiet.stderr,
            )
        self.assertIn(
            "error: tests/e2e/test_quiet.py:28: [e2e-artifacts] "
            "tests/e2e/test_quiet.py::test_free_function_never_runs is a module-level function "
            "that unittest never runs; fix: make it a method of a unittest.TestCase subclass\n",
            quiet.stderr,
        )
        self.assertNotIn("test_popen_drives_the_product", quiet.stderr)
        self.assertNotIn("test_never_runs", quiet.stderr)
        self.assertNotIn("ElsewhereTest", quiet.stderr)
        quiet_run = artifacts_root / "e2e-quiet" / "artifacts"
        quiet_tests = json.loads((quiet_run / "manifest.json").read_text(encoding="utf-8"))["tests"]
        self.assertEqual(
            sorted(item["id"] for item in quiet_tests),
            [
                "setUpClass (tests.e2e.test_quiet.SkippedClassTest)",
                "tests.e2e.test_cli.CliTest.test_child_prints",
                "tests.e2e.test_quiet.QuietTest.test_nothing_runs",
                "tests.e2e.test_quiet.QuietTest.test_only_git_runs",
                "tests.e2e.test_quiet.QuietTest.test_popen_drives_the_product",
            ],
        )
        popen = next(item for item in quiet_tests if item["id"].endswith("drives_the_product"))
        [started] = (quiet_run / popen["record"]).read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(started)["returncode"], 0)

    def test_e2e_artifacts_bind_each_run_to_the_source_files_it_tested(self) -> None:
        root, runs = self.committed_fixture()
        (root / "dist").mkdir()
        (root / "dist" / "pinned.txt").write_text(
            "tracked under an ignored path\n", encoding="utf-8"
        )
        self.git(root, "add", "--force", "dist/pinned.txt")
        self.git(root, "commit", "-qm", "pin a file under an ignored path")
        store = root / "html_publish" / "store.py"
        store.write_text(
            store.read_text(encoding="utf-8") + "# work in progress\n", encoding="utf-8"
        )
        (root / "notes.md").write_text("draft\n", encoding="utf-8")
        (root / "latest").symlink_to("html_publish")

        recorded = self.record(root, runs, "e2e-dirty")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        run = runs / "e2e-dirty" / "artifacts"
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["schema_version"], manifest["dirty"]), (2, True))
        passed = f"ok: 1 e2e tests left verified records in {run}\n"
        dirty = self.audit(root, runs, "e2e-dirty")
        self.assertEqual((dirty.returncode, dirty.stdout), (0, passed), dirty.stderr)

        (root / "dist" / "html_publish-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        (root / "html_publish" / "__pycache__").mkdir(exist_ok=True)
        (root / "html_publish" / "__pycache__" / "extra.cpython-311.pyc").write_bytes(b"cache")
        for path in (store, root / "tests" / "e2e" / "test_cli.py", root / "notes.md"):
            os.utime(path, (1, 1))
        generated = self.audit(root, runs, "e2e-dirty")
        self.assertEqual((generated.returncode, generated.stdout), (0, passed), generated.stderr)

        def copied() -> Path:
            parent = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-copy-")))
            return shutil.copytree(root, parent / "checkout", symlinks=True)

        def audit_copy(copy: Path) -> subprocess.CompletedProcess[str]:
            return self.audit(root, runs, "e2e-dirty", "--root", str(copy))

        moved = audit_copy(copied())
        self.assertEqual((moved.returncode, moved.stdout), (0, passed), moved.stderr)

        def edit_test_body(copy: Path) -> None:
            test_module = copy / "tests" / "e2e" / "test_cli.py"
            body = test_module.read_text(encoding="utf-8")
            test_module.write_text(body.replace('"hi\\n")', '"hi\\n", "edited")'), encoding="utf-8")

        def retarget_symlink(copy: Path) -> None:
            (copy / "latest").unlink()
            (copy / "latest").symlink_to("tests")

        mutations: list[tuple[str, Callable[[Path], object]]] = [
            (
                "edit tracked product code",
                lambda copy: (copy / "html_publish" / "__main__.py").write_text(
                    'print("bye")\n', encoding="utf-8"
                ),
            ),
            ("edit a test body", edit_test_body),
            (
                "edit an untracked file",
                lambda copy: (copy / "notes.md").write_text("final\n", encoding="utf-8"),
            ),
            (
                "edit a tracked file under an ignored path",
                lambda copy: (copy / "dist" / "pinned.txt").write_text(
                    "edited\n", encoding="utf-8"
                ),
            ),
            (
                "add a file",
                lambda copy: (copy / "html_publish" / "extra.py").write_text("", encoding="utf-8"),
            ),
            ("delete a file", lambda copy: (copy / "html_publish" / "cli.py").unlink()),
            (
                "rename a file",
                lambda copy: (copy / "html_publish" / "cli.py").rename(
                    copy / "html_publish" / "command.py"
                ),
            ),
            (
                "make a file executable",
                lambda copy: (copy / "html_publish" / "store.py").chmod(0o755),
            ),
            ("retarget a symlink", retarget_symlink),
        ]
        for label, mutate in mutations:
            with self.subTest(label):
                copy = copied()
                mutate(copy)
                result = audit_copy(copy)
                self.assertEqual(
                    (result.returncode, result.stderr),
                    (
                        1,
                        f"error: {run}: [e2e-artifacts] run tested other source files than the "
                        f"checkout holds; fix: rerun just check --only e2e\n{VERBOSE_HINT}",
                    ),
                )

        committed = copied()
        self.git(committed, "commit", "-q", "--allow-empty", "-m", "next")
        later = audit_copy(committed)
        self.assertEqual(
            (later.returncode, later.stderr),
            (
                1,
                f"error: {run}: [e2e-artifacts] run is from commit {manifest['commit']}, "
                f"checkout is at {self.git(committed, 'rev-parse', 'HEAD')}; "
                f"fix: rerun just check --only e2e\n{VERBOSE_HINT}",
            ),
        )

        unfingerprinted = {
            key: value for key, value in manifest.items() if key != "source_fingerprint"
        }
        for run_id, old_manifest, problem in (
            (
                "e2e-version-1",
                {**unfingerprinted, "schema_version": 1},
                "/manifest.json has schema_version 1, expected 2",
            ),
            ("e2e-no-fingerprint", unfingerprinted, "/manifest.json lacks source_fingerprint"),
            (
                "e2e-null-fingerprint",
                {**manifest, "source_fingerprint": {"start": None, "end": None}},
                ": [e2e-artifacts] records no source fingerprint",
            ),
        ):
            with self.subTest(run_id):
                old_run = shutil.copytree(run.parent, runs / run_id) / "artifacts"
                (old_run / "manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")
                result = self.audit(root, runs, run_id)
                self.assertEqual(
                    (result.returncode, result.stderr),
                    (
                        1,
                        f"error: {old_run}{problem}; "
                        f"fix: rerun just check --only e2e\n{VERBOSE_HINT}",
                    ),
                )

        (root / "tests" / "e2e" / "test_writer.py").write_text(
            textwrap.dedent(
                """\
                import subprocess
                import sys
                import unittest
                from pathlib import Path


                class WriterTest(unittest.TestCase):
                    def test_edits_the_checkout_mid_run(self) -> None:
                        Path("notes.md").write_text("edited while the suite ran\\n")
                        command = [sys.executable, "-m", "html_publish"]
                        result = subprocess.run(command, capture_output=True, text=True)
                        self.assertEqual(result.stdout, "hi\\n")
                """
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.record(root, runs, "e2e-moving").returncode, 0)
        (root / "notes.md").write_text("draft\n", encoding="utf-8")
        moving_run = runs / "e2e-moving" / "artifacts"
        moving = self.audit(root, runs, "e2e-moving")
        self.assertEqual(
            (moving.returncode, moving.stderr),
            (
                1,
                f"error: {moving_run}: [e2e-artifacts] source files changed while the suite ran; "
                "fix: rerun just check --only e2e and leave the checkout unchanged until it ends\n"
                + VERBOSE_HINT,
            ),
        )


if __name__ == "__main__":
    unittest.main()
