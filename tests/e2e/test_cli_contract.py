"""The command-line contract in docs/contract.md, checked across every in-scope executable.

Each rule has one owning test that walks a table of executables. A signal test never uses a
timer: a sitecustomize.py hook blocks the named process right after `parse_args` returns, inside
its guards, and writes its PID to a ready file; the test signals the process only after that.
"""

from __future__ import annotations

import contextlib
import json
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
from typing import cast

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


@dataclass(frozen=True)
class Installed:
    """An installed executable, run from the checkout as `python -m <module>`."""

    prog: str
    module: str
    exit_codes: tuple[int, ...]
    schema: bool
    commands: tuple[tuple[str, ...], ...] = ()

    @property
    def argv(self) -> tuple[str, ...]:
        return (PYTHON, "-m", self.module)


INSTALLED = (
    Installed("html-publish", "html_publish", (0, 1, 2, 130, 143), schema=True),
    Installed("html-publish-remote", "html_publish.remote", (0, 1, 2, 130, 143), schema=True),
    Installed("html-publish-server", "html_publish.server", (0, 1, 2, 130, 143), schema=False),
    Installed(
        "html-publish-deploy",
        "html_publish.deploy",
        (0, 1, 2, 130, 143),
        schema=False,
        commands=(("install",), ("health",), ("rollback",), ("help",)),
    ),
)


class InstalledContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-installed-")))
        self.env = {
            **ENVIRONMENT,
            **{
                variable: str(self.root / variable.lower())
                for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")
            },
        }
        self.config = self.root / "publisher.json"
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.root / "archive.git"),
                    "runtime": str(self.root / "runtime"),
                    "base_url": "http://127.0.0.1:9/",
                    "allow_http": True,
                }
            ),
            encoding="utf-8",
        )

    def run_installed(
        self, executable: Installed, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*executable.argv, *arguments],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def command_paths(self, executable: Installed) -> list[tuple[str, ...]]:
        """Every command path, from `schema` where the executable has one."""
        if not executable.schema:
            return list(executable.commands)
        schema = json_object(self.run_installed(executable, "schema").stdout)
        paths: list[tuple[str, ...]] = []

        def walk(commands: object, prefix: tuple[str, ...]) -> None:
            for command in cast(list[dict[str, object]], commands):
                path = (*prefix, str(command["name"]))
                paths.append(path)
                examples = cast(list[str], command["examples"])
                self.assertTrue(2 <= len(examples) <= 5, (executable.prog, path, examples))
                walk(command.get("commands", []), path)

        walk(schema["commands"], ())
        return paths

    def test_help_is_the_same_text_however_it_is_asked_for(self) -> None:
        for executable in INSTALLED:
            root_help = self.run_installed(executable, "--help")
            with self.subTest(prog=executable.prog):
                self.assertEqual((root_help.returncode, root_help.stderr), (0, ""))
                self.assertTrue(root_help.stdout.startswith(f"usage: {executable.prog} "))
                examples = examples_in(root_help.stdout)
                self.assertTrue(2 <= len(examples) <= 5, examples)
                self.assert_exit_codes(executable, root_help.stdout)
            for path in self.command_paths(executable):
                with self.subTest(prog=executable.prog, path=path):
                    reference = self.run_installed(executable, *path, "--help")
                    self.assertEqual((reference.returncode, reference.stderr), (0, ""))
                    self.assertTrue(2 <= len(examples_in(reference.stdout)) <= 5)
                    self.assert_exit_codes(executable, reference.stdout)
                    for arguments in (
                        ("help", *path),
                        (*path, "-h"),
                        (*path, "--bogus", "--help"),
                        ("--version", *path, "--help"),
                    ):
                        result = self.run_installed(executable, *arguments)
                        self.assertEqual(
                            (result.returncode, result.stdout, result.stderr),
                            (0, reference.stdout, ""),
                            arguments,
                        )

    def test_help_wins_over_a_bad_command_or_value(self) -> None:
        cases = {
            "html-publish": (("publsh", "--help"), ("status", "--limit", "0", "--help")),
            "html-publish-remote": (("publsh", "--help"), ("status", "--limit", "0", "--help")),
            "html-publish-server": (("--port", "99999", "--help"),),
            "html-publish-deploy": (("instal", "--help"), ("rollback", "--release", "--help")),
        }
        for executable in INSTALLED:
            for arguments in cases[executable.prog]:
                with self.subTest(prog=executable.prog, arguments=arguments):
                    result = self.run_installed(executable, *arguments)
                    self.assertEqual((result.returncode, result.stderr), (0, ""))
                    self.assertTrue(result.stdout.startswith("usage: "), result.stdout)

    def test_usage_errors_name_the_fix_and_the_help_command(self) -> None:
        cases = {
            "html-publish": (
                ("verify",),
                "the following arguments are required: --name",
                "next: html-publish verify --name '<name>'",
                "run 'html-publish verify --help' for details",
            ),
            "html-publish-remote": (
                ("verify",),
                "the following arguments are required: --name",
                "next: html-publish-remote verify --name '<name>'",
                "run 'html-publish-remote verify --help' for details",
            ),
            "html-publish-server": (
                (),
                "the following arguments are required: --directory",
                "next: html-publish-server --directory '<directory>'",
                "run 'html-publish-server --help' for details",
            ),
            "html-publish-deploy": (
                (),
                "the following arguments are required: operation",
                "next: html-publish-deploy '<command>'",
                "run 'html-publish-deploy --help' for details",
            ),
        }
        for executable in INSTALLED:
            arguments, failure, fix, hint = cases[executable.prog]
            with self.subTest(prog=executable.prog):
                result = self.run_installed(executable, *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                lines = result.stderr.splitlines()
                self.assertTrue(lines[0].startswith(f"usage: {executable.prog}"), result.stderr)
                self.assertIn(f"{executable.prog}: {failure}", lines)
                self.assertEqual(lines[-2:], [fix, hint])
                if executable.prog == "html-publish-remote":
                    error = cast(dict[str, object], json_object(result.stdout)["error"])
                    self.assertEqual(error["code"], "invalid_usage")
                else:
                    self.assertEqual(result.stdout, "")
        for executable, mistyped, meant in (
            (INSTALLED[0], ("--json", "publsh", "--name", "notes"), "publish"),
            (INSTALLED[1], ("statsu", "--name", "notes"), "status"),
            (INSTALLED[3], ("rollbak",), "rollback"),
        ):
            with self.subTest(prog=executable.prog, mistyped=mistyped):
                result = self.run_installed(executable, *mistyped)
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"did you mean {meant!r}?\n", result.stderr)
                next_line = result.stderr.splitlines()[-2]
                self.assertTrue(next_line.startswith(f"next: {executable.prog} "), next_line)
                self.assertIn(meant, next_line.split())
        mistyped_flag = self.run_installed(INSTALLED[0], "status", "--nam", "notes")
        self.assertEqual(
            mistyped_flag.stderr.splitlines()[-3:],
            [
                "did you mean '--name'?",
                "next: html-publish status --name notes",
                "run 'html-publish status --help' for details",
            ],
        )

    def test_option_spellings_and_positions_parse_the_same(self) -> None:
        config = str(self.config)
        receipt = str(self.root / "missing.publish")
        equivalent: list[tuple[Installed, tuple[str, ...], tuple[str, ...]]] = [
            (INSTALLED[0], ("--config", config, "status"), (f"--config={config}", "status")),
            (INSTALLED[0], ("-v", "-c", config, "status"), ("-vc", config, "status")),
            (INSTALLED[0], ("--json", "-c", config, "status"), ("status", "--json", "-c", config)),
            (
                INSTALLED[0],
                ("--json", "artifact", "status", "--receipt", receipt, "--local-only"),
                ("artifact", "status", "--local-only", "--receipt", receipt, "--json"),
            ),
            (
                INSTALLED[1],
                ("--connect-timeout", "0", "status"),
                ("status", "--connect-timeout=0"),
            ),
            (INSTALLED[2], ("--port", "99999"), ("--port=99999",)),
            (
                INSTALLED[3],
                ("--state-root", str(self.root / "state"), "rollback"),
                ("rollback", f"--state-root={self.root / 'state'}"),
            ),
        ]
        for executable, first, second in equivalent:
            with self.subTest(prog=executable.prog, first=first, second=second):
                one = self.run_installed(executable, *first)
                other = self.run_installed(executable, *second)
                self.assertEqual(
                    (one.returncode, one.stdout), (other.returncode, other.stdout), one.stderr
                )
        for executable in INSTALLED:
            with self.subTest(prog=executable.prog, check="-- ends options"):
                ended = self.run_installed(executable, "--", "--help")
                self.assertEqual(ended.returncode, 2, ended.stdout)
                self.assertNotIn("usage: ", ended.stdout)

    def assert_exit_codes(self, executable: Installed, help_text: str) -> None:
        listed = re.search(r"^Exit codes:\n((?:  .*\n)+)", help_text, re.MULTILINE)
        self.assertIsNotNone(listed, help_text)
        assert listed is not None
        codes = tuple(int(line.split()[0]) for line in listed.group(1).splitlines())
        self.assertEqual(codes, executable.exit_codes)


def stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
        process.communicate(timeout=30)


def kill_group(group: int) -> None:
    """Kill a process group a failed runner left behind; a stopped group is already gone."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(group, signal.SIGKILL)


def examples_in(help_text: str) -> list[str]:
    """The indented lines of the help section headed `examples:` or `Examples:`."""
    match = re.search(r"^[Ee]xamples:\n((?:  .*\n)+)", help_text, re.MULTILINE)
    return [line.strip() for line in match.group(1).splitlines()] if match else []


def json_object(text: str) -> dict[str, object]:
    value: object = json.loads(text)
    if not isinstance(value, dict):
        raise AssertionError(f"not one JSON object: {text!r}")
    return cast(dict[str, object], value)


if __name__ == "__main__":
    unittest.main()
