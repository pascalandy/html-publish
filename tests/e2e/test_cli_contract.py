"""The command-line contract in docs/contract.md, checked across every in-scope executable.

Each rule has one owning test that walks a table of executables. A signal test never uses a
timer: a sitecustomize.py hook blocks the named process right after `parse_args` returns, inside
its guards, and writes its PID to a ready file; the test signals the process only after that.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
READY_SECONDS = 30
BLOCK_HOOK = """\
import argparse
import os
import signal
import sys
from pathlib import Path

_parse_args = argparse.ArgumentParser.parse_args
_blocked = []


def _parse_then_block(self, *args, **kwargs):
    result = _parse_args(self, *args, **kwargs)
    target = os.environ.get("HP_CONTRACT_BLOCK", "")
    if target and not _blocked and sys.argv and sys.argv[0].endswith(target):
        _blocked.append(True)
        ready = Path(os.environ["HP_CONTRACT_READY"])
        ready.with_suffix(".tmp").write_text(str(os.getpid()))
        ready.with_suffix(".tmp").rename(ready)
        while True:
            signal.pause()
    return result


argparse.ArgumentParser.parse_args = _parse_then_block
"""
CHECK_SCRIPTS = (
    "check_test_layout",
    "check_e2e_boundary",
    "check_isolated_failure_modes",
    "check_test_smells",
    "check_test_only_code",
)
RUNNER_SCRIPTS = ("_common.py", "_test_tree.py", "check.py", "check_test_layout.py")
FIXTURE_TREE = {
    "pyproject.toml": (
        '[project]\nname = "fixture"\nversion = "0"\n\n'
        '[project.scripts]\nhtml-publish = "html_publish.cli:main"\n\n'
        '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["E", "F"]\n'
    ),
    "html_publish/__init__.py": "",
    "html_publish/cli.py": "def main() -> None: ...\n",
    "tests/__init__.py": "",
    "tests/e2e/__init__.py": "",
    "tests/isolated/__init__.py": "",
}
ENVIRONMENT = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


@dataclass(frozen=True)
class Script:
    """A repository script that runs through scripts/_common.run_script.

    `success` holds arguments for a cheap successful run, or None when a success needs a
    recorded E2E run, which tests/e2e/test_check_rules.py owns.
    """

    name: str
    success: tuple[str, ...] | None

    @property
    def path(self) -> str:
        return f"scripts/{self.name}.py"

    @property
    def prog(self) -> str:
        return "just check" if self.name == "check" else f"{self.name}.py"


def check_scripts(root: Path) -> tuple[Script, ...]:
    """Each rule script, run against `root`, plus the runner listing its checks."""
    return (
        *(Script(name, ("--root", str(root))) for name in CHECK_SCRIPTS),
        Script("check_e2e_artifacts", None),
        Script("check", ("--list",)),
    )


class ScriptContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-contract-")))
        for relative, content in FIXTURE_TREE.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.hook = self.root / "hook"
        self.hook.mkdir()
        (self.hook / "sitecustomize.py").write_text(BLOCK_HOOK, encoding="utf-8")

    def run_script(
        self,
        script: Script | str,
        *arguments: str,
        env: Mapping[str, str] | None = None,
        cwd: Path = ROOT,
    ) -> subprocess.CompletedProcess[str]:
        path = script.path if isinstance(script, Script) else script
        return subprocess.run(
            [PYTHON, path, *arguments],
            cwd=cwd,
            env={**ENVIRONMENT, **(env or {})},
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_help_wins_and_lists_examples_and_exit_codes(self) -> None:
        for script in check_scripts(self.root):
            with self.subTest(script=script.name):
                reference = self.run_script(script, "--help")
                self.assertEqual((reference.returncode, reference.stderr), (0, ""))
                self.assertTrue(reference.stdout.startswith("usage: "), reference.stdout)
                examples = examples_in(reference.stdout)
                self.assertTrue(2 <= len(examples) <= 5, examples)
                self.assertRegex(
                    reference.stdout,
                    r"exit codes: 0 ok, 1 [^,]+, 2 bad usage, 130 interrupted, 143 terminated\n",
                )
                for arguments in (
                    ("--bogus", "--help"),
                    ("--root", str(self.root / "missing"), "--help"),
                    ("-vh",),
                    ("-h",),
                ):
                    with self.subTest(arguments=arguments):
                        result = self.run_script(script, *arguments)
                        self.assertEqual(
                            (result.returncode, result.stdout, result.stderr),
                            (0, reference.stdout, ""),
                        )

    def test_usage_errors_print_usage_and_the_help_hint(self) -> None:
        for script in check_scripts(self.root):
            with self.subTest(script=script.name):
                prog = script.prog
                for arguments in (("--bogus",), ("--root",), ("--", "--help")):
                    with self.subTest(arguments=arguments):
                        result = self.run_script(script, *arguments)
                        self.assertEqual((result.returncode, result.stdout), (2, ""))
                        lines = result.stderr.splitlines()
                        self.assertTrue(lines[0].startswith(f"usage: {prog} "), result.stderr)
                        self.assertTrue(lines[-2].startswith(f"{prog}: error: "), result.stderr)
                        self.assertEqual(lines[-1], f"run '{prog} --help' for details")

    def test_success_is_silent_and_verbosity_only_adds_stderr(self) -> None:
        for script in check_scripts(self.root):
            if script.success is None:
                continue
            with self.subTest(script=script.name):
                default = self.run_script(script, *script.success)
                verbose = self.run_script(script, *script.success, "-v")
                debug = self.run_script(script, *script.success, "--debug")
                self.assertEqual(default.returncode, 0, default.stderr)
                self.assertEqual(default.stderr, "")
                for level in (verbose, debug):
                    self.assertEqual(
                        (level.returncode, level.stdout), (default.returncode, default.stdout)
                    )
                self.assertNotIn(" finished in ", verbose.stderr)
                self.assertIn(" finished in ", debug.stderr)
                self.assertNotIn("Traceback", verbose.stderr + debug.stderr)
                if script.name != "check":
                    self.assertTrue(verbose.stderr.splitlines()[-1].startswith("ok: "))
                    self.assertEqual(default.stdout, "")
        spaced = self.run_script(Script("check_test_layout", None), "--root", str(self.root), "-v")
        joined = self.run_script(Script("check_test_layout", None), f"--root={self.root}", "-v")
        self.assertEqual(
            (joined.returncode, joined.stdout, joined.stderr),
            (spaced.returncode, spaced.stdout, spaced.stderr),
        )

    def test_a_crash_prints_a_traceback_only_when_debugging(self) -> None:
        (self.root / "tests" / "e2e" / "test_broken.py").write_text("def (:\n", encoding="utf-8")
        script = Script("check_test_layout", None)
        quiet = self.run_script(script, "--root", str(self.root))
        self.assertEqual((quiet.returncode, quiet.stdout), (1, ""))
        self.assertNotIn("Traceback", quiet.stderr)
        self.assertEqual(
            quiet.stderr.splitlines()[-1], "rerun with --debug for a traceback", quiet.stderr
        )
        for label, arguments, env in (
            ("flag", ("--debug",), {}),
            ("variable", (), {"CHECK_TEST_LAYOUT_DEBUG": "1"}),
        ):
            with self.subTest(label):
                debug = self.run_script(script, "--root", str(self.root), *arguments, env=env)
                self.assertEqual((debug.returncode, debug.stdout), (1, ""))
                self.assertIn("Traceback (most recent call last):", debug.stderr)

    def test_signals_exit_130_and_143_without_a_traceback(self) -> None:
        for script in check_scripts(self.root):
            for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
                with self.subTest(script=script.name, signal=signal_number.name):
                    process, _ = self.blocked(script.path, [script.path, *(script.success or ())])
                    process.send_signal(signal_number)
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, code, stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertEqual(stdout, "")

    def test_the_runner_stops_its_child_process_group_on_a_signal(self) -> None:
        fixture = self.runner_fixture()
        for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signal_number.name):
                process, child = self.blocked(
                    "check_test_layout.py",
                    ["scripts/check.py", "--only", "test-layout"],
                    cwd=fixture,
                )
                self.addCleanup(kill_group, child)
                process.send_signal(signal_number)
                _, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, code, stderr)
                self.assertNotIn("Traceback", stderr)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(child, 0)

    def test_the_runner_lists_checks_and_forwards_verbosity_only_to_repository_scripts(
        self,
    ) -> None:
        listed = {
            level: self.run_script("scripts/check.py", "--list", *level)
            for level in ((), ("-v",), ("--debug",))
        }
        for level, result in listed.items():
            with self.subTest(level=level):
                self.assertEqual(
                    (result.returncode, result.stdout), (0, listed[()].stdout), result.stderr
                )
        self.assertEqual(listed[()].stderr, "")
        self.assertIn(f"  {PYTHON} scripts/check_test_layout.py\n", listed[("-v",)].stderr)

        fixture = self.runner_fixture()
        quiet = self.run_script("scripts/check.py", "--only", "test-layout", cwd=fixture)
        self.assertEqual((quiet.returncode, quiet.stdout, quiet.stderr), (0, "", ""))
        verbose = self.run_script(
            "scripts/check.py", "--only", "test-layout", "--only", "lint", "-v", cwd=fixture
        )
        self.assertEqual((verbose.returncode, verbose.stdout), (0, ""), verbose.stderr)
        self.assertIn(
            f"==> test-layout: {PYTHON} scripts/check_test_layout.py -v\n", verbose.stderr
        )
        self.assertIn("ok: 0 e2e and 0 isolated test modules\n", verbose.stderr)
        self.assertIn(f"==> lint: {PYTHON} -m ruff check .\n", verbose.stderr)
        self.assertTrue(verbose.stderr.endswith("ok: 2 passed\n"), verbose.stderr)

    def runner_fixture(self) -> Path:
        """A copy of the rule scripts over the fixture tree, so the runner checks only it."""
        fixture = self.root / "checkout"
        shutil.copytree(self.root, fixture, ignore=shutil.ignore_patterns("hook", "checkout"))
        (fixture / "scripts").mkdir()
        for name in RUNNER_SCRIPTS:
            shutil.copy2(ROOT / "scripts" / name, fixture / "scripts" / name)
        return fixture

    def blocked(
        self, target: str, arguments: Sequence[str], cwd: Path = ROOT
    ) -> tuple[subprocess.Popen[str], int]:
        """Start a process that blocks `target` after parsing; return it and the blocked PID."""
        ready = self.root / f"ready-{time.monotonic_ns()}"
        process = subprocess.Popen(
            [PYTHON, *arguments],
            cwd=cwd,
            env={
                **ENVIRONMENT,
                "PYTHONPATH": str(self.hook),
                "HP_CONTRACT_BLOCK": target,
                "HP_CONTRACT_READY": str(ready),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(stop, process)
        deadline = time.monotonic() + READY_SECONDS
        while not ready.exists():
            if process.poll() is not None or time.monotonic() > deadline:
                stdout, stderr = process.communicate(timeout=30)
                self.fail(f"{target} never blocked: {process.returncode} {stdout} {stderr}")
            time.sleep(0.02)
        return process, int(ready.read_text())


def stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
        process.communicate(timeout=30)


def kill_group(group: int) -> None:
    """Kill a process group a failed runner left behind; a stopped group is already gone."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(group, signal.SIGKILL)


def examples_in(help_text: str) -> list[str]:
    """The indented lines of the help section headed `examples:`."""
    match = re.search(r"^examples:\n((?:  .*\n)+)", help_text, re.MULTILINE)
    return [line.strip() for line in match.group(1).splitlines()] if match else []


if __name__ == "__main__":
    unittest.main()
