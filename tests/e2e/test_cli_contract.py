"""The command-line contract in docs/contract.md, checked across every in-scope executable.

Each rule has one owning test that walks a table of executables. A signal test never uses a
timer: a sitecustomize.py hook blocks the named process right after `parse_args` returns, inside
its guards, and writes its PID to a ready file; the test signals the process only after that.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import http.server
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
READY_SECONDS = 30
PARKING = """\
import os
import signal
import sys
from pathlib import Path


def pause_when_ready(variable):
    ready = Path(os.environ[variable])
    ready.with_suffix(".tmp").write_text(str(os.getpid()))
    ready.with_suffix(".tmp").rename(ready)
    while True:
        signal.pause()


"""
BLOCK_HOOK = (
    PARKING
    + """\
import argparse

_parse_args = argparse.ArgumentParser.parse_args
_blocked = []


def _parse_then_block(self, *args, **kwargs):
    result = _parse_args(self, *args, **kwargs)
    target = os.environ.get("HP_CONTRACT_BLOCK", "")
    if target and not _blocked and sys.argv and sys.argv[0].endswith(target):
        _blocked.append(True)
        pause_when_ready("HP_CONTRACT_READY")
    return result


argparse.ArgumentParser.parse_args = _parse_then_block
"""
)
SLEEPER = PARKING + 'pause_when_ready("HP_CONTRACT_READY")\n'
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
# A Git hook exports GIT_DIR and friends, and a caller may export CHECK_DEBUG or another
# <NAME>_DEBUG switch; neither may reach a command whose output a test compares
ENVIRONMENT = {
    key: value
    for key, value in os.environ.items()
    if not key.startswith("GIT_") and not key.endswith("_DEBUG")
}


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
                    process, _ = start_blocked(
                        self, [PYTHON, script.path, *(script.success or ())], script.path
                    )
                    process.send_signal(signal_number)
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual((process.returncode, stdout), (code, b""), stderr)
                    self.assertNotIn(b"Traceback", stderr)

    def test_the_runner_stops_its_child_process_group_on_a_signal(self) -> None:
        fixture = self.runner_fixture()
        for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signal_number.name):
                process, child = start_blocked(
                    self,
                    [PYTHON, "scripts/check.py", "--only", "test-layout"],
                    "check_test_layout.py",
                    cwd=fixture,
                )
                self.addCleanup(kill_group, child)
                process.send_signal(signal_number)
                _, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, code, stderr)
                self.assertNotIn(b"Traceback", stderr)
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
        shutil.copytree(self.root, fixture, ignore=shutil.ignore_patterns("checkout"))
        (fixture / "scripts").mkdir()
        for name in RUNNER_SCRIPTS:
            shutil.copy2(ROOT / "scripts" / name, fixture / "scripts" / name)
        return fixture


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
    Installed("html-publish", "html_publish", (0, 1, 2, 75, 130, 143), schema=True),
    Installed("html-publish-remote", "html_publish.remote", (0, 1, 2, 75, 130, 143), schema=True),
    Installed("html-publish-server", "html_publish.server", (0, 1, 2, 130, 143), schema=False),
    Installed(
        "html-publish-deploy",
        "html_publish.deploy",
        (0, 1, 2, 75, 130, 143),
        schema=False,
        commands=(("install",), ("health",), ("rollback",), ("help",)),
    ),
)


class InstalledContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-installed-")))
        self.env = isolated_environment(self.root)
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
            "html-publish": (
                ("publsh", "--help"),
                ("status", "--limit", "0", "--help"),
                ("help", "publsih", "--help"),
            ),
            "html-publish-remote": (("publsh", "--help"), ("status", "--limit", "0", "--help")),
            "html-publish-server": (("--port", "99999", "--help"),),
            "html-publish-deploy": (
                ("instal", "--help"),
                ("rollback", "--release", "--help"),
                ("help", "instal", "-h"),
            ),
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
        plain = self.run_installed(INSTALLED[0], "--config", config, "status", "--", "--json")
        self.assertEqual((plain.returncode, plain.stdout), (2, ""), plain.stderr)

    def assert_exit_codes(self, executable: Installed, help_text: str) -> None:
        listed = re.search(r"^Exit codes:\n((?:  .*\n)+)", help_text, re.MULTILINE)
        self.assertIsNotNone(listed, help_text)
        assert listed is not None
        codes = tuple(int(line.split()[0]) for line in listed.group(1).splitlines())
        self.assertEqual(codes, executable.exit_codes)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


NOISY_PUBLISHER = """\
import os
import sys

if any(key.startswith("HTML_PUBLISH") and key.endswith("_DEBUG") for key in os.environ):
    sys.stderr.write("debug output from the publisher\\n" * 400)
    sys.stderr.flush()
os.execv(sys.executable, [sys.executable, "-m", "html_publish", *sys.argv[1:]])
"""
SSH_SHIM = (
    PARKING
    + """\
command = sys.argv[-1]
invoke = " --json " in command
if os.environ.get("HP_SSH_LOG"):
    with open(os.environ["HP_SSH_LOG"], "a") as log:
        log.write(command + "\\n")
failing = os.environ.get("HP_SSH_FAIL")
if (failing == "setup" and "mkdir" in command) or (failing == "invoke" and invoke):
    sys.exit(255)
if os.environ.get("HP_SSH_BLOCK") and invoke:
    pause_when_ready("HP_SSH_BLOCK")
if os.environ.get("HP_SSH_BLOCK_CLEANUP") and command.startswith("rm "):
    pause_when_ready("HP_SSH_BLOCK_CLEANUP")
os.execvp("sh", ["sh", "-c", command])
"""
)
SCP_SHIM = """\
import os
import shutil
import sys
from pathlib import Path

if os.environ.get("HP_SCP_FAIL"):
    sys.exit(1)
source = Path(sys.argv[-2])
destination = Path(sys.argv[-1].split(":", 1)[1]) / source.name
if source.is_dir():
    shutil.copytree(source, destination)
else:
    shutil.copy2(source, destination)
"""


class PublisherFixture(unittest.TestCase):
    """A publisher config and archive whose pages a loopback HTTP server delivers."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-publisher-")))
        self.env = isolated_environment(self.root)
        self.runtime = self.root / "runtime"
        handler = functools.partial(QuietHandler, directory=str(self.runtime / "public"))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base_url = f"http://127.0.0.1:{server.server_address[1]}/"
        self.config = self.root / "publisher.json"
        self.write_config()
        self.page = self.root / "page.html"
        self.page.write_text("<!doctype html><h1>page</h1>\n", encoding="utf-8")

    def write_config(self, lock_seconds: float = 2) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.root / "archive.git"),
                    "runtime": str(self.runtime),
                    "base_url": self.base_url,
                    "allow_http": True,
                    "limits": {
                        "command_seconds": 120,
                        "lock_seconds": lock_seconds,
                        "verification_seconds": 5,
                    },
                }
            ),
            encoding="utf-8",
        )

    def write_client(
        self,
        command: Sequence[str] = (PYTHON, "-m", "html_publish"),
        *,
        lock_seconds: float = 2,
        output_bytes: int = 1024 * 1024,
        name: str = "client.json",
    ) -> Path:
        client = self.root / name
        client.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "target": {"id": "contract", "base_url": self.base_url},
                    "execution": {
                        "kind": "local",
                        "command": list(command),
                        "publisher_config": str(self.config),
                    },
                    "limits": {"lock_seconds": lock_seconds, "output_bytes": output_bytes},
                }
            ),
            encoding="utf-8",
        )
        return client

    def cli(
        self, *arguments: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, "-m", "html_publish", "--config", str(self.config), *arguments],
            cwd=self.root,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            timeout=120,
        )

    def artifact(
        self, client: Path, *arguments: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, "-m", "html_publish", "--config", str(client), "artifact", *arguments],
            cwd=self.root,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            timeout=120,
        )

    def popen(
        self, *arguments: str, env: Mapping[str, str] | None = None
    ) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [PYTHON, "-m", "html_publish", *arguments],
            cwd=self.root,
            env={**self.env, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(stop_process, process)
        return process

    def publish_arguments(self, name: str = "page", source: Path | None = None) -> list[str]:
        return [
            "publish",
            "--name",
            name,
            "--source",
            str(source or self.page),
            "--target",
            self.base_url,
        ]


class PublisherContractTest(PublisherFixture):
    """html-publish root operations, artifact commands, and host serve."""

    def test_publisher_exit_codes(self) -> None:
        published = self.cli(*self.publish_arguments())
        self.assertEqual(
            (published.returncode, published.stdout, published.stderr),
            (0, f"{self.base_url}page/\n", ""),
        )
        missing = self.cli("verify", "--name", "missing")
        self.assertEqual((missing.returncode, missing.stdout), (1, ""))
        failure, next_line = missing.stderr.splitlines()
        self.assertTrue(failure.startswith("html-publish: "), missing.stderr)
        self.assertTrue(next_line.startswith(f"next: html-publish --config {self.config} "))
        self.write_config(lock_seconds=0.2)
        update = self.root / "update.html"
        update.write_text("<!doctype html><h1>update</h1>\n", encoding="utf-8")
        with held(self.runtime / ".publish.lock"):
            waited = self.cli("--json", *self.publish_arguments(source=update))
            plain = self.cli(*self.publish_arguments(source=update))
        self.assertEqual(waited.returncode, 75, waited.stderr)
        payload = json_object(waited.stdout)
        self.assertEqual(cast(dict[str, object], payload["error"])["code"], "lock_timeout")
        self.assertEqual(payload["effects"], {"archive_advanced": False, "activated": False})
        self.assertEqual((plain.returncode, plain.stdout), (75, ""))
        self.assertEqual(
            plain.stderr.splitlines(),
            [
                "html-publish: The publication lock did not become available in time",
                "next: html-publish --config "
                + f"{self.config} publish --name page --source {update} --target {self.base_url}",
            ],
        )

    def test_signals_while_waiting_for_the_publication_lock(self) -> None:
        self.assertEqual(self.cli(*self.publish_arguments()).returncode, 0)
        self.write_config(lock_seconds=60)
        for operation, effects in (
            (self.publish_arguments(), {"archive_advanced": None, "activated": None}),
            (["status", "--name", "page"], {"archive_advanced": False, "activated": False}),
        ):
            for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
                with self.subTest(operation=operation[0], signal=signal_number.name):
                    with held(self.runtime / ".publish.lock"):
                        process = self.popen(
                            "--config", str(self.config), "-v", "--json", *operation
                        )
                        read_until(process, b"waiting for the publication lock")
                        process.send_signal(signal_number)
                        stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, code, stderr)
                    self.assertNotIn(b"Traceback", stderr)
                    payload = json_object(stdout.decode())
                    error = cast(dict[str, object], payload["error"])
                    self.assertEqual((error["code"], payload["effects"]), ("interrupted", effects))

    def test_plain_publish_prints_the_url_and_warnings_on_stderr(self) -> None:
        self.page.write_text('<!doctype html><img src="missing.png">\n', encoding="utf-8")
        result = self.cli(*self.publish_arguments())
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (
                0,
                f"{self.base_url}page/\n",
                "html-publish: warning: missing_relative_asset: index.html references "
                "missing.png\n",
            ),
        )

    def test_verbosity_changes_only_stderr(self) -> None:
        self.assertEqual(self.cli(*self.publish_arguments()).returncode, 0)
        plan = ["plan", "--name", "page", "--source", str(self.page), "--target", self.base_url]
        for arguments in (plan, ["history", "--name", "page"], ["schema"]):
            with self.subTest(command=arguments[0]):
                default = self.cli("--json", *arguments)
                verbose = self.cli("--json", "-v", *arguments)
                debug = self.cli("--json", "--debug", *arguments)
                self.assertEqual((default.returncode, default.stderr), (0, ""))
                for level in (verbose, debug):
                    self.assertEqual(
                        (level.returncode, level.stdout), (default.returncode, default.stdout)
                    )
                for line in verbose.stderr.splitlines():
                    self.assertRegex(line, r"^html-publish: (?!\+\d)")
                for line in debug.stderr.splitlines():
                    self.assertTrue(line.startswith("html-publish: "), line)
                if arguments[0] != "schema":
                    self.assertRegex(debug.stderr, r"html-publish: \+\d+\.\d{3}s git ")
        self.assertIn("html-publish: capture ", self.cli("-v", *plan).stderr)

    def test_config_init_dry_run_writes_nothing(self) -> None:
        target = self.root / "new" / "publisher.json"
        init = [
            "--json",
            "config",
            "init",
            "--role",
            "publisher",
            "--config",
            str(target),
            "--base-url",
            "https://review.example/pages/",
        ]
        planned = json_object(self.cli(*init, "-n").stdout)
        self.assertEqual(
            (planned["outcome"], planned["effects"]), ("planned", {"config_written": False})
        )
        self.assertFalse(target.parent.exists())
        written = self.cli(*init)
        self.assertEqual(json_object(written.stdout)["outcome"], "config_written")
        repeated = json_object(self.cli(*init, "--dry-run").stdout)
        self.assertEqual(repeated["outcome"], "unchanged")
        different = self.cli(*init[:-1], "https://other.example/pages/", "-n")
        self.assertEqual(different.returncode, 1)
        error = cast(dict[str, object], json_object(different.stdout)["error"])
        self.assertEqual(error["code"], "config_exists")

    def test_host_serve_dies_from_the_signal(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signal_number.name):
                port = unused_port()
                process = self.popen(
                    "--config", str(self.config), "host", "serve", "--port", str(port)
                )
                wait_for_health(process, port)
                process.send_signal(signal_number)
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, -signal_number, stderr)
                self.assertEqual((stdout, stderr), (b"", b""))

    def test_artifact_exit_codes(self) -> None:
        client = self.write_client(lock_seconds=0.2)
        receipt = f"{self.page}.publish"
        created = self.artifact(client, "publish", str(self.page), "--new", "page")
        self.assertEqual((created.returncode, created.stderr), (0, ""), created.stdout)
        with held(Path(receipt) / "lock"):
            busy = self.artifact(client, "publish", str(self.page))
        self.assertEqual(busy.returncode, 75, busy.stdout)
        self.assertEqual(handoff_error(busy)["code"], "receipt_busy")
        self.page.write_text("<!doctype html><h1>changed</h1>\n", encoding="utf-8")
        self.write_config(lock_seconds=0.2)
        with held(self.runtime / ".publish.lock"):
            waited = self.artifact(client, "publish", str(self.page))
            retried = self.artifact(client, "retry", "--receipt", receipt)
        self.assertEqual(waited.returncode, 1, waited.stdout)
        self.assertEqual(
            (handoff_error(waited)["code"], handoff_error(waited)["next_action"]),
            ("lock_timeout", "retry"),
        )
        self.assertEqual(json_object(waited.stdout)["pending_state"], "retryable")
        self.assertEqual(retried.returncode, 75, retried.stdout)
        self.assertEqual(handoff_error(retried)["code"], "lock_timeout")
        finished = self.artifact(client, "retry", "--receipt", receipt)
        self.assertEqual(finished.returncode, 0, finished.stdout)

    def test_artifact_signals_while_waiting_and_while_the_publisher_runs(self) -> None:
        client = self.write_client(lock_seconds=60)
        receipt = Path(f"{self.page}.publish")
        self.assertEqual(
            self.artifact(client, "publish", str(self.page), "--new", "page").returncode, 0
        )
        sleeper = self.root / "sleeping_publisher.py"
        sleeper.write_text(SLEEPER, encoding="utf-8")
        for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(wait="receipt lock", signal=signal_number.name):
                with held(receipt / "lock"):
                    process = self.popen(
                        "--config", str(client), "-v", "artifact", "publish", str(self.page)
                    )
                    read_until(process, b"waiting for the receipt lock")
                    process.send_signal(signal_number)
                    stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, code, stderr)
                self.assertNotIn(b"Traceback", stderr)
                self.assertEqual(handoff_error(stdout)["code"], "interrupted")
            with self.subTest(wait="publisher", signal=signal_number.name):
                sleeping = self.write_client(
                    (PYTHON, str(sleeper)), lock_seconds=60, name="sleeping-client.json"
                )
                source = self.root / f"sleep-{signal_number.name}.html"
                source.write_text("<!doctype html><h1>sleep</h1>\n", encoding="utf-8")
                ready = self.root / f"publisher-{signal_number.name}.ready"
                process = self.popen(
                    "--config",
                    str(sleeping),
                    "artifact",
                    "publish",
                    str(source),
                    "--new",
                    f"sleep-{signal_number.name.lower()}",
                    env={"HP_CONTRACT_READY": str(ready)},
                )
                publisher = wait_for_ready(process, ready)
                process.send_signal(signal_number)
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, code, stderr)
                self.assertNotIn(b"Traceback", stderr)
                result = json_object(stdout.decode())
                self.assertEqual(handoff_error(stdout)["code"], "interrupted")
                self.assertTrue(cast(dict[str, object], result["publisher"])["cancelled"])
                with self.assertRaises(ProcessLookupError):
                    os.kill(publisher, 0)

    def test_the_publisher_never_receives_debug_switches(self) -> None:
        noisy = self.root / "noisy_publisher.py"
        noisy.write_text(NOISY_PUBLISHER, encoding="utf-8")
        client = self.write_client((PYTHON, str(noisy)))
        self.assertEqual(
            self.artifact(client, "publish", str(self.page), "--new", "page").returncode, 0
        )
        client = self.write_client((PYTHON, str(noisy)), output_bytes=4096)
        status = self.artifact(
            client,
            "status",
            "--receipt",
            f"{self.page}.publish",
            env={"HTML_PUBLISH_DEBUG": "1"},
        )
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(json_object(status.stdout)["outcome"], "observed")


class RemoteContractTest(PublisherFixture):
    """html-publish-remote's own transport outcomes, through ssh and scp shims on PATH."""

    def setUp(self) -> None:
        super().setUp()
        shims = self.root / "shims"
        shims.mkdir()
        for name, body in (("ssh", SSH_SHIM), ("scp", SCP_SHIM)):
            (shims / name).write_text(f"#!{PYTHON}\n{body}", encoding="utf-8")
            (shims / name).chmod(0o755)
        host = self.root / "host-publish"
        host.write_text(f'#!/bin/sh\nexec {PYTHON} -m html_publish "$@"\n', encoding="utf-8")
        host.chmod(0o755)
        (self.root / "incoming").mkdir()
        self.env = {
            **self.env,
            "PATH": f"{shims}{os.pathsep}{self.env['PATH']}",
            "HP_SSH_LOG": str(self.root / "ssh.log"),
        }
        self.destination = [
            "--host",
            "fixture",
            "--remote-executable",
            str(host),
            "--remote-config",
            str(self.config),
            "--target",
            self.base_url,
            "--incoming-root",
            str(self.root / "incoming"),
        ]
        published = self.cli(*self.publish_arguments())
        self.assertEqual(published.returncode, 0, published.stderr)

    def remote(
        self, *arguments: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, "-m", "html_publish.remote", *self.destination, *arguments],
            cwd=self.root,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_remote_exit_codes(self) -> None:
        update = self.root / "update.html"
        update.write_text("<!doctype html><h1>update</h1>\n", encoding="utf-8")
        publish = ("publish", "--name", "page", "--source", str(update))
        cases: list[tuple[str, tuple[str, ...], Mapping[str, str], int, str | None]] = [
            ("success", ("status", "--name", "page"), {}, 0, None),
            ("host failure", ("verify", "--name", "missing"), {}, 1, None),
            ("scp failure", publish, {"HP_SCP_FAIL": "1"}, 1, "transport_failure"),
            ("ssh 255 on setup", publish, {"HP_SSH_FAIL": "setup"}, 75, "transport_failure"),
            (
                "ssh 255 on a read-only invocation",
                ("status", "--name", "page"),
                {"HP_SSH_FAIL": "invoke"},
                75,
                "transport_failure",
            ),
            (
                "deadline before the host command",
                ("status", "--name", "page", "--command-seconds", "1e-9"),
                {},
                75,
                "command_timeout",
            ),
            (
                "ssh 255 on a mutation",
                publish,
                {"HP_SSH_FAIL": "invoke"},
                1,
                "publication_outcome_unknown",
            ),
        ]
        for label, arguments, env, code, error_code in cases:
            with self.subTest(label):
                result = self.remote(*arguments, env=env)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                payload = json_object(result.stdout)
                if code == 0:
                    self.assertEqual(result.stderr, "")
                if error_code is not None:
                    self.assertEqual(handoff_error(result)["code"], error_code)
                if code == 75:
                    self.assertEqual(
                        payload["effects"], {"archive_advanced": False, "activated": False}
                    )

    def test_a_cancel_during_parsing_or_cleanup_keeps_the_json_handoff(self) -> None:
        update = self.root / "cleanup.html"
        update.write_text("<!doctype html><h1>cleanup</h1>\n", encoding="utf-8")
        command = [PYTHON, "-m", "html_publish.remote", *self.destination]
        for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(stage="parse", signal=signal_number.name):
                process, remote = start_blocked(
                    self,
                    [*command, "status", "--name", "page"],
                    "html_publish/remote.py",
                    cwd=self.root,
                    env=self.env,
                )
                os.kill(remote, signal_number)
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, code, stderr)
                self.assertNotIn(b"Traceback", stderr)
                self.assertEqual(handoff_error(stdout)["code"], "interrupted")
            with self.subTest(stage="cleanup", signal=signal_number.name):
                ready = self.root / f"cleanup-{signal_number.name}.ready"
                name = f"cleanup-{signal_number.name.lower()}"
                process = subprocess.Popen(
                    [*command, "publish", "--name", name, "--source", str(update)],
                    cwd=self.root,
                    env={**self.env, "HP_SSH_BLOCK_CLEANUP": str(ready)},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.addCleanup(stop_process, process)
                transport = wait_for_ready(process, ready)
                process.send_signal(signal_number)
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, code, stderr)
                payload = json_object(stdout.decode())
                self.assertEqual(handoff_error(stdout)["code"], "interrupted")
                self.assertEqual(
                    (
                        payload["effects"],
                        cast(dict[str, object], payload["verification"])["result"],
                    ),
                    ({"archive_advanced": True, "activated": True}, "passed"),
                )
                self.assertNotIn("superseded_error", payload)
                self.assertEqual(
                    cast(dict[str, object], payload["transport"])["detail"],
                    "The caller cancelled cleanup",
                )
                with self.assertRaises(ProcessLookupError):
                    os.kill(transport, 0)

    def test_the_receipt_inspects_after_its_remote_publisher_is_cancelled(self) -> None:
        client = self.root / "remote-client.json"
        client.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "target": {"id": "contract", "base_url": self.base_url},
                    "execution": {
                        "kind": "remote",
                        "command": [PYTHON, "-m", "html_publish.remote"],
                        "host": "fixture",
                        "remote_executable": self.destination[3],
                        "remote_config": str(self.config),
                        "incoming_root": self.destination[-1],
                    },
                }
            ),
            encoding="utf-8",
        )
        artifact = [PYTHON, "-m", "html_publish", "--config", str(client), "artifact"]
        source = self.root / "receipt-page.html"
        source.write_text("<!doctype html><h1>receipt</h1>\n", encoding="utf-8")
        ready = self.root / "receipt-cleanup.ready"
        process = subprocess.Popen(
            [*artifact, "publish", str(source), "--new", "receipt-page"],
            cwd=self.root,
            env={**self.env, "HP_SSH_BLOCK_CLEANUP": str(ready)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(stop_process, process)
        transport = wait_for_ready(process, ready)
        remote = int(
            subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(transport)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        os.kill(remote, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 1, stderr)
        handoff = json_object(stdout.decode())
        self.assertEqual((handoff["outcome"], handoff["pending_state"]), ("uncertain", "uncertain"))
        publisher = cast(dict[str, object], handoff["publisher"])
        self.assertEqual((publisher["exit_code"], publisher["error_code"]), (143, "interrupted"))
        retried = subprocess.run(
            [*artifact, "retry", "--receipt", f"{source}.publish"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        completed = json_object(retried.stdout)
        self.assertEqual((completed["outcome"], completed["publisher_calls"]), ("completed", 2))

    def test_a_local_cancel_keeps_the_json_handoff(self) -> None:
        for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signal_number.name):
                ready = self.root / f"ssh-{signal_number.name}.ready"
                process = subprocess.Popen(
                    [
                        PYTHON,
                        "-m",
                        "html_publish.remote",
                        *self.destination,
                        "status",
                        "--name",
                        "page",
                    ],
                    cwd=self.root,
                    env={**self.env, "HP_SSH_BLOCK": str(ready)},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.addCleanup(stop_process, process)
                transport = wait_for_ready(process, ready)
                process.send_signal(signal_number)
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, code, stderr)
                self.assertNotIn(b"Traceback", stderr)
                payload = json_object(stdout.decode())
                self.assertEqual(handoff_error(stdout)["code"], "interrupted")
                superseded = cast(dict[str, object], payload["superseded_error"])
                self.assertEqual(superseded["code"], "transport_failure")
                self.assertEqual(
                    cast(dict[str, object], payload["transport"])["detail"],
                    "The caller cancelled the invocation",
                )
                with self.assertRaises(ProcessLookupError):
                    os.kill(transport, 0)

    def test_host_lock_timeouts_pass_through_and_host_interruptions_need_inspection(
        self,
    ) -> None:
        self.write_config(lock_seconds=0.2)
        with held(self.runtime / ".publish.lock"):
            waited = self.remote("status", "--name", "page")
        self.assertEqual(waited.returncode, 75, waited.stdout + waited.stderr)
        self.assertEqual(handoff_error(waited)["code"], "lock_timeout")
        update = self.root / "update.html"
        update.write_text("<!doctype html><h1>update</h1>\n", encoding="utf-8")
        publish = ["publish", "--name", "page", "--source", str(update)]
        process, host = start_blocked(
            self,
            [PYTHON, "-m", "html_publish.remote", *self.destination, *publish],
            "html_publish/__main__.py",
            cwd=self.root,
            env=self.env,
        )
        os.kill(host, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 1, stderr)
        payload = json_object(stdout.decode())
        error = cast(dict[str, object], payload["error"])
        self.assertEqual(error["code"], "interrupted")
        self.assertEqual(cast(dict[str, object], error["next_action"])["kind"], "inspect")
        self.assertEqual(payload["effects"], {"archive_advanced": None, "activated": None})

    def test_verify_om1_mvp_passes_through_the_real_remote_publisher_and_server(self) -> None:
        port = unused_port()
        public = self.root / "mvp-runtime" / "public"
        server = subprocess.Popen(
            [PYTHON, "-m", "html_publish.server", "--directory", str(public), "--port", str(port)],
            cwd=self.root,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(stop_process, server)
        wait_for_health(server, port)
        base = f"http://127.0.0.1:{port}/"
        config = self.root / "mvp-publisher.json"
        config.write_text(
            json.dumps(
                {
                    "archive": str(self.root / "mvp-archive.git"),
                    "runtime": str(public.parent),
                    "base_url": base,
                    "allow_http": True,
                }
            ),
            encoding="utf-8",
        )
        remote = shlex.join(
            [
                PYTHON,
                "-m",
                "html_publish.remote",
                *self.destination[:4],
                "--remote-config",
                str(config),
                "--target",
                base,
                *self.destination[-2:],
            ]
        )
        result = subprocess.run(
            [PYTHON, "scripts/verify_om1_mvp.py", "--remote-command", remote, "--name", "mvp"],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""), result.stdout)
        evidence = json_object(result.stdout)
        self.assertEqual(evidence["url"], f"{base}mvp/")
        self.assertEqual(evidence["conflict_code"], "revision_conflict")
        http = cast(dict[str, object], evidence["http"])
        self.assertEqual((http["status"], http["cache_control"]), (200, "no-store"))

    def test_verbosity_changes_only_stderr_and_never_reaches_the_host(self) -> None:
        levels = {
            level: self.remote("status", "--name", "page", *level)
            for level in ((), ("-v",), ("--debug",))
        }
        default = levels[()]
        self.assertEqual((default.returncode, default.stderr), (0, ""))
        for level, result in levels.items():
            with self.subTest(level=level):
                self.assertEqual((result.returncode, result.stdout), (0, default.stdout))
        self.assertIn("html-publish-remote: run status on fixture\n", levels[("-v",)].stderr)
        self.assertNotRegex(levels[("-v",)].stderr, r"\+\d+\.\d{3}s")
        self.assertRegex(levels[("--debug",)].stderr, r"\+\d+\.\d{3}s run ssh -o BatchMode=yes")
        forwarded = (self.root / "ssh.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(forwarded), 3)
        for command in forwarded:
            self.assertNotIn(" -v", command)
            self.assertNotIn("--debug", command)


SYSTEMCTL_SHIM = (
    PARKING
    + """\
arguments = sys.argv[1:]
if "is-active" in arguments:
    if os.environ.get("HP_SYSTEMCTL_BLOCK"):
        pause_when_ready("HP_SYSTEMCTL_BLOCK")
    print(os.environ.get("HP_SERVICE_STATE", "active"))
elif "--property=UnitFileState" in arguments:
    print("enabled")
"""
)
TAILSCALE_SHIM = """\
import json

print(json.dumps({
    "TCP": {"8444": {"HTTPS": True}},
    "Web": {
        "om1.donkey-arcturus.ts.net:8444": {
            "Handlers": {"/html-publish": {"Proxy": "http://127.0.0.1:4177"}}
        }
    },
}))
"""


class ServerContractTest(PublisherFixture):
    """html-publish-server, and the bind error it shares with html-publish host serve."""

    def server(self, port: int, *arguments: str) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "html_publish.server",
                "--directory",
                str(self.runtime / "public"),
                "--port",
                str(port),
                *arguments,
            ],
            cwd=self.root,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(stop_process, process)
        return process

    def test_the_access_log_needs_verbose_and_a_signal_ends_the_server(self) -> None:
        (self.runtime / "public").mkdir(parents=True)
        for level, signal_number in (((), signal.SIGINT), (("-v",), signal.SIGTERM)):
            with self.subTest(level=level, signal=signal_number.name):
                port = unused_port()
                process = self.server(port, *level)
                wait_for_health(process, port)
                process.send_signal(signal_number)
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, -signal_number, stderr)
                self.assertEqual(stdout, b"")
                if level:
                    self.assertIn(b"html-publish-server: serving ", stderr)
                    self.assertIn(b'"GET /_html-publish-health HTTP/1.1" 200', stderr)
                else:
                    self.assertEqual(stderr, b"")

    def test_a_busy_port_is_one_line_and_a_next_command(self) -> None:
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            server = self.server(port)
            stdout, stderr = server.communicate(timeout=30)
            host = self.cli("host", "serve", "--port", str(port))
        busy_error = rf"cannot listen on 127\.0\.0\.1:{port}: \[Errno \d+\] Address already in use"
        self.assertEqual((server.returncode, stdout), (1, b""))
        failure, next_line = stderr.decode().splitlines()
        self.assertRegex(failure, rf"^html-publish-server: {busy_error}$")
        self.assertEqual(
            next_line,
            f"next: html-publish-server --directory {self.runtime / 'public'} "
            "--bind 127.0.0.1 --port '<port>'",
        )
        self.assertEqual((host.returncode, host.stdout), (1, ""))
        failure, next_line = host.stderr.splitlines()
        self.assertRegex(failure, rf"^html-publish: {busy_error}$")
        self.assertEqual(
            next_line, f"next: html-publish --config {self.config} host serve --port '<port>'"
        )


class DeployContractTest(unittest.TestCase):
    """html-publish-deploy against systemctl and tailscale shims and a closed proxy port."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-deploy-")))
        shims = self.root / "shims"
        shims.mkdir()
        for name, body in (("systemctl", SYSTEMCTL_SHIM), ("tailscale", TAILSCALE_SHIM)):
            (shims / name).write_text(f"#!{PYTHON}\n{body}", encoding="utf-8")
            (shims / name).chmod(0o755)
        self.state = self.root / "state"
        for release in ("sha256-a", "sha256-b"):
            (self.state / "app-releases" / release).mkdir(parents=True)
            (self.state / "app-releases" / release / ".ready").write_text(release)
        (self.state / "current").symlink_to(self.state / "app-releases" / "sha256-a")
        (self.state / "previous").symlink_to(self.state / "app-releases" / "sha256-b")
        closed = f"http://127.0.0.1:{unused_port()}"
        self.env = {
            **isolated_environment(self.root),
            "PATH": f"{shims}{os.pathsep}{ENVIRONMENT['PATH']}",
            "http_proxy": closed,
            "https_proxy": closed,
            "HTTP_PROXY": closed,
            "HTTPS_PROXY": closed,
            "no_proxy": "",
            "NO_PROXY": "",
        }
        self.layout = [
            "--state-root",
            str(self.state),
            "--config",
            str(self.root / "publisher.json"),
            "--unit",
            str(self.root / "html-publish.service"),
        ]

    def deploy(
        self, *arguments: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, "-m", "html_publish.deploy", *self.layout, *arguments],
            cwd=self.root,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_deploy_exit_codes(self) -> None:
        planned = self.deploy("install", "--source", str(ROOT), "-n")
        self.assertEqual((planned.returncode, planned.stderr), (0, ""), planned.stdout)
        install = json_object(planned.stdout)
        self.assertEqual(
            (install["outcome"], install["release"], install["previous"]),
            ("planned", None, "sha256-a"),
        )
        self.assertEqual(
            install["preflight"],
            {"config": "write", "route": "keep", "unit_file_state": "enabled"},
        )
        self.assertFalse((self.root / "publisher.json").exists())
        rollback = self.deploy("rollback", "--dry-run")
        self.assertEqual((rollback.returncode, rollback.stderr), (0, ""))
        self.assertEqual(
            {key: json_object(rollback.stdout)[key] for key in ("outcome", "release", "previous")},
            {"outcome": "planned", "release": "sha256-b", "previous": "sha256-a"},
        )
        self.assertEqual(
            os.readlink(self.state / "current"), str(self.state / "app-releases" / "sha256-a")
        )
        missing = self.deploy("rollback", "--release", "sha256-missing")
        self.assertEqual((missing.returncode, missing.stdout), (1, ""))
        self.assertEqual(json_object(missing.stderr)["outcome"], "error")
        unreachable = self.deploy("health")
        self.assertEqual(unreachable.returncode, 75, unreachable.stdout + unreachable.stderr)
        failed = {
            str(check["name"]): str(check["detail"])
            for check in cast(list[dict[str, object]], json_object(unreachable.stdout)["checks"])
            if check["ok"] is not True
        }
        self.assertEqual(set(failed), {"loopback", "https"})
        self.assertTrue(all(detail.startswith("cannot connect: ") for detail in failed.values()))
        stopped = self.deploy("health", env={"HP_SERVICE_STATE": "failed"})
        self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)

    def test_deploy_signals_while_a_command_runs(self) -> None:
        for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signal_number.name):
                ready = self.root / f"systemctl-{signal_number.name}.ready"
                process = subprocess.Popen(
                    [PYTHON, "-m", "html_publish.deploy", *self.layout, "health"],
                    cwd=self.root,
                    env={**self.env, "HP_SYSTEMCTL_BLOCK": str(ready)},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.addCleanup(stop_process, process)
                command = wait_for_ready(process, ready)
                process.send_signal(signal_number)
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual((process.returncode, stdout), (code, b""), stderr)
                self.assertEqual(
                    json_object(stderr.decode())["error"],
                    f"Deployment cancelled by {signal_number.name}",
                )
                with self.assertRaises(ProcessLookupError):
                    os.kill(command, 0)

    def test_verbosity_changes_only_stderr(self) -> None:
        arguments = ("install", "--source", str(ROOT), "--dry-run")
        default = self.deploy(*arguments)
        verbose = self.deploy(*arguments, "-v")
        debug = self.deploy("--debug", *arguments)
        self.assertEqual((default.returncode, default.stderr), (0, ""))
        for level in (verbose, debug):
            self.assertEqual((level.returncode, level.stdout), (0, default.stdout))
        self.assertEqual(
            verbose.stderr,
            "html-publish-deploy: check the existing publisher config and Tailscale route\n",
        )
        self.assertRegex(debug.stderr, r"\+\d+\.\d{3}s run tailscale serve status --json\n")


RUNNER_TEST = """\
import subprocess
import sys
import unittest


class CliTest(unittest.TestCase):
    def test_child_prints(self) -> None:
        command = [sys.executable, "-m", "html_publish"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.stdout, "hi\\n")
"""
FAILING_REMOTE = """\
import json
import sys

print(json.dumps({"outcome": "error", "error": {"code": "%s"}}))
sys.exit(%d)
"""
INSTANCE = ROOT / ".agents/skills/verify-html-publish/scripts/instance.sh"
SYSTEMD = "scripts/verify-host-systemd.sh"


class OtherScriptsContractTest(unittest.TestCase):
    """The bench scripts, verify_om1_mvp, instance.sh, the E2E runner, and the systemd proof."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hp-scripts-")))
        self.cli = str(Path(PYTHON).with_name("html-publish"))

    def run_command(
        self, *command: str, env: Mapping[str, str] | None = None, cwd: Path = ROOT
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            cwd=cwd,
            env={**ENVIRONMENT, **(env or {})},
            capture_output=True,
            text=True,
            timeout=300,
        )

    def interrupt(
        self,
        command: Sequence[str],
        target: str,
        signal_number: signal.Signals,
        *,
        cwd: Path = ROOT,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes, bytes, int]:
        """Block `target` after it parses, signal it, and return
        (exit code, stdout, stderr, blocked PID)."""
        process, blocked = start_blocked(self, command, target, cwd=cwd, env=env)
        os.kill(blocked, signal_number)
        stdout, stderr = process.communicate(timeout=60)
        return process.returncode, stdout, stderr, blocked

    def assert_help(
        self,
        command: Sequence[str],
        exit_codes: str,
        *,
        verbose: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> str:
        reference = self.run_command(*command, "--help", env=env)
        self.assertEqual((reference.returncode, reference.stderr), (0, ""))
        self.assertTrue(reference.stdout.startswith("usage: "), reference.stdout)
        self.assertTrue(2 <= len(examples_in(reference.stdout)) <= 5, reference.stdout)
        self.assertIn(exit_codes, " ".join(reference.stdout.split()))
        for arguments in (("-h",), ("--bogus", "--help"), *((("-vh",),) if verbose else ())):
            with self.subTest(command=command[-1], arguments=arguments):
                result = self.run_command(*command, *arguments, env=env)
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr), (0, reference.stdout, "")
                )
        return reference.stdout

    def assert_usage(
        self, command: Sequence[str], *arguments: str, env: Mapping[str, str] | None = None
    ) -> str:
        result = self.run_command(*command, *arguments, env=env)
        self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
        lines = result.stderr.splitlines()
        self.assertTrue(lines[0].startswith("usage: "), result.stderr)
        self.assertRegex(lines[-1], r"^run '.+ --help' for details$")
        return result.stderr

    def test_bench_scripts(self) -> None:
        codes = "exit codes: 0 ok, 1 {}, 2 bad usage, 130 interrupted, 143 terminated"
        for name, failure in (
            ("bench_capture", "a timed plan failed"),
            ("bench_publish", "a timed publish failed"),
        ):
            command = (PYTHON, f"scripts/{name}.py")
            with self.subTest(script=name):
                self.assert_help(command, codes.format(failure))
                self.assert_usage(command, "--cli", self.cli, "--reps", "0")
                self.assert_usage(command, "--reps", "1")
                levels: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
                for index, level in enumerate(((), ("-v",), ("--debug",))):
                    report = self.root / f"{name}-{index}.json"
                    result = self.run_command(
                        *command, "--cli", self.cli, "--reps", "1", "-o", str(report), *level
                    )
                    levels[level] = result
                    self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
                    self.assertIn(
                        "results" if name == "bench_capture" else "seconds",
                        json_object(report.read_text()),
                    )
                    self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(levels[()].stderr, "")
                self.assertTrue(levels[("-v",)].stderr)
                self.assertIn(" finished in ", levels[("--debug",)].stderr)
                failing = self.root / "failing-cli"
                failing.write_text("#!/bin/sh\necho 'refused' >&2\nexit 1\n")
                failing.chmod(0o755)
                failed = self.run_command(*command, "--cli", str(failing), "--reps", "1")
                self.assertEqual((failed.returncode, failed.stdout), (1, ""))
                self.assertIn("refused", failed.stderr)
                for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
                    returncode, stdout, stderr, _ = self.interrupt(
                        [*command, "--cli", self.cli], f"scripts/{name}.py", signal_number
                    )
                    self.assertEqual((returncode, stdout), (code, b""), stderr)
                    self.assertNotIn(b"Traceback", stderr)
        stdout_report = self.run_command(
            PYTHON, "scripts/bench_capture.py", "--cli", self.cli, "--reps", "1"
        )
        self.assertEqual((stdout_report.returncode, stdout_report.stderr), (0, ""))
        self.assertIn("results", json_object(stdout_report.stdout))

    def test_verify_om1_mvp(self) -> None:
        command = (PYTHON, "scripts/verify_om1_mvp.py")
        self.assert_help(
            command,
            "exit codes: 0 ok, 1 the verification failed, 2 bad usage, "
            "75 a remote step reported a temporary failure, 130 interrupted, 143 terminated",
        )
        self.assert_usage(command, "--timeout", "0")
        self.assert_usage(command, "--remote-command", "")
        remotes: dict[str, str] = {}
        for label, code, exit_code in (
            ("failing", "status_failed", 1),
            ("busy", "lock_timeout", 75),
        ):
            remote = self.root / f"{label}-remote.py"
            remote.write_text(FAILING_REMOTE % (code, exit_code), encoding="utf-8")
            remotes[label] = f"{PYTHON} {remote}"
        levels = {
            level: self.run_command(*command, "--remote-command", remotes["failing"], *level)
            for level in ((), ("-v",), ("--debug",))
        }
        for result in levels.values():
            self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertEqual(
            levels[()].stderr,
            "error: status-initial failed with status_failed (exit 1)\n",
        )
        self.assertIn("status-initial: status --name om1-deployment-mvp\n", levels[("-v",)].stderr)
        self.assertRegex(levels[("--debug",)].stderr, r"status-initial exit 1 after \d+\.\d{3} s\n")
        busy = self.run_command(*command, "--remote-command", remotes["busy"])
        self.assertEqual((busy.returncode, busy.stdout), (75, ""), busy.stderr)
        self.assertIn("status-initial reported lock_timeout (exit 75)", busy.stderr)
        sleeping = self.root / "sleeping-remote.py"
        sleeping.write_text(SLEEPER, encoding="utf-8")
        for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signal_number.name):
                ready = self.root / f"remote-{signal_number.name}.ready"
                process = subprocess.Popen(
                    [*command, "--remote-command", f"{PYTHON} {sleeping}"],
                    cwd=ROOT,
                    env={**ENVIRONMENT, "HP_CONTRACT_READY": str(ready)},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.addCleanup(stop_process, process)
                remote = wait_for_ready(process, ready)
                process.send_signal(signal_number)
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual((process.returncode, stdout), (code, b""), stderr)
                self.assertNotIn(b"Traceback", stderr)
                with self.assertRaises(ProcessLookupError):
                    os.kill(remote, 0)

    def test_instance_helper(self) -> None:
        command = ("bash", str(INSTANCE))
        codes = "Exit codes: 0 success, including no change 1 runtime failure 2 usage error"
        self.assert_help(command, codes, verbose=False)
        for path in ("start", "sources", "doctor", "offline", "stop", "help"):
            with self.subTest(path=path):
                reference = self.run_command(*command, path, "--help")
                self.assertEqual((reference.returncode, reference.stderr), (0, ""))
                self.assertTrue(2 <= len(examples_in(reference.stdout)) <= 5)
                for arguments in (("help", path), (path, "-h")):
                    result = self.run_command(*command, *arguments)
                    self.assertEqual((result.returncode, result.stdout), (0, reference.stdout))
        self.assertNotIn("supervise", self.run_command(*command, "--help").stdout)
        self.assert_usage(command, "doctor")
        mistyped = self.assert_usage(command, "stpo", "run")
        self.assertIn("did you mean 'stop'?\n", mistyped)
        run_id = f"contract-{os.getpid()}-{time.monotonic_ns()}"
        run = Path("/tmp/html-publish-verify") / run_id
        self.addCleanup(shutil.rmtree, run, True)
        nothing = self.run_command(*command, "stop", run_id)
        self.assertEqual((nothing.returncode, nothing.stdout, nothing.stderr), (0, "", ""))
        (run / "instance").mkdir(parents=True)
        sources = self.run_command(*command, "sources", run_id, "--json")
        self.assertEqual((sources.returncode, sources.stderr), (0, ""))
        pages = json_object(sources.stdout)
        plain = self.run_command(*command, "sources", run_id)
        self.assertEqual(plain.stdout, "".join(f"{key}={value}\n" for key, value in pages.items()))
        debug = self.run_command(*command, "sources", run_id, "--debug")
        self.assertEqual((debug.returncode, debug.stdout), (0, plain.stdout))
        unavailable = self.run_command(*command, "sources", f"{run_id}-missing", "--json")
        self.assertEqual(unavailable.returncode, 1, unavailable.stderr)
        self.assertEqual(handoff_error(unavailable)["code"], "failed")
        for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(json=True, signal=signal_number.name):
                returncode, stdout, stderr, _ = self.interrupt(
                    [*command, "sources", run_id, "--json"], "scripts/instance.py", signal_number
                )
                self.assertEqual(returncode, code, stderr)
                self.assertEqual(handoff_error(stdout)["code"], "interrupted")
        missing = self.run_command(*command, "doctor", run_id)
        self.assertEqual((missing.returncode, missing.stdout), (1, ""))
        self.assertEqual(
            missing.stderr.splitlines()[-1], f"next: instance.sh stop {run_id}", missing.stderr
        )
        for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signal_number.name):
                returncode, stdout, stderr, _ = self.interrupt(
                    [*command, "stop", run_id], "scripts/instance.py", signal_number
                )
                self.assertEqual((returncode, stdout), (code, b""), stderr)
                self.assertNotIn(b"Traceback", stderr)

    def test_e2e_runner(self) -> None:
        checkout = self.root / "checkout"
        for relative, content in {
            "html_publish/__init__.py": "",
            "html_publish/__main__.py": 'print("hi")\n',
            "tests/__init__.py": "",
            "tests/e2e/__init__.py": "",
            "tests/e2e/test_cli.py": RUNNER_TEST,
        }.items():
            (checkout / relative).parent.mkdir(parents=True, exist_ok=True)
            (checkout / relative).write_text(content, encoding="utf-8")
        for name in ("__main__.py", "_fingerprint.py"):
            shutil.copy2(ROOT / "tests/e2e" / name, checkout / "tests/e2e" / name)
        subprocess.run(["git", "init", "-q"], cwd=checkout, env=ENVIRONMENT, check=True)
        runs = self.root / "runs"
        command = (PYTHON, "-m", "tests.e2e")

        def recorded(run_id: str, *arguments: str) -> subprocess.CompletedProcess[str]:
            return self.run_command(
                *command,
                *arguments,
                cwd=checkout,
                env={"HTML_PUBLISH_E2E_ROOT": str(runs), "HTML_PUBLISH_E2E_RUN_ID": run_id},
            )

        help_text = self.run_command(*command, "--help", cwd=checkout)
        self.assertEqual((help_text.returncode, help_text.stderr), (0, ""))
        self.assertTrue(2 <= len(examples_in(help_text.stdout)) <= 5)
        self.assertIn("130 interrupted", help_text.stdout)
        levels = {
            level: recorded(f"e2e-{index}", *level) for index, level in enumerate(((), ("-v",)))
        }
        for index, result in enumerate(levels.values()):
            self.assertEqual(
                (result.returncode, result.stdout),
                (0, f"E2E_ARTIFACTS={runs / f'e2e-{index}' / 'artifacts'}\n"),
                result.stderr,
            )
        self.assertEqual(levels[()].stderr, "")
        self.assertIn("test_child_prints", levels[("-v",)].stderr)
        reused = recorded("e2e-0")
        self.assertEqual((reused.returncode, reused.stdout), (2, ""))
        self.assertIn("fix: choose a fresh HTML_PUBLISH_E2E_RUN_ID", reused.stderr)
        self.assertEqual(recorded("e2e-usage", "--bogus").returncode, 2)
        for level, tracebacks in (((), 0), (("--debug",), 1)):
            with self.subTest(unwritable_root=level):
                crashed = self.run_command(
                    *command,
                    *level,
                    cwd=checkout,
                    env={"HTML_PUBLISH_E2E_ROOT": "/dev/null", "HTML_PUBLISH_E2E_RUN_ID": "e2e-x"},
                )
                self.assertEqual((crashed.returncode, crashed.stdout), (1, ""), crashed.stderr)
                self.assertEqual(crashed.stderr.count("Traceback"), tracebacks, crashed.stderr)
                self.assertIn("python -m tests.e2e: unexpected ", crashed.stderr)
        (checkout / "html_publish/__main__.py").write_text('print("bye")\n', encoding="utf-8")
        failed = recorded("e2e-failed")
        self.assertEqual(
            (failed.returncode, failed.stdout),
            (1, f"E2E_ARTIFACTS={runs / 'e2e-failed' / 'artifacts'}\n"),
        )
        self.assertIn("FAIL: test_child_prints", failed.stderr)
        for signal_number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signal_number.name):
                returncode, stdout, stderr, _ = self.interrupt(
                    command,
                    "tests/e2e/__main__.py",
                    signal_number,
                    cwd=checkout,
                    env={
                        "HTML_PUBLISH_E2E_ROOT": str(runs),
                        "HTML_PUBLISH_E2E_RUN_ID": f"e2e-{signal_number.name}",
                    },
                )
                self.assertEqual((returncode, stdout), (code, b""), stderr)
                self.assertNotIn(b"Traceback", stderr)

    def test_systemd_proof_help_and_usage(self) -> None:
        # A CI runner sets RUNNER_TEMP; an empty one keeps every call here out of the real
        # stages, which create an account with sudo
        command, runner = ("bash", SYSTEMD), {"RUNNER_TEMP": ""}
        self.assert_help(
            command,
            "exit codes: 0 ok, 1 a stage failed or cleanup refused (result.txt keeps the raw "
            "code), 2 bad usage, 130 interrupted, 143 terminated",
            env=runner,
        )
        self.assert_usage(command, env=runner)
        self.assert_usage(command, "--bogus", "wheel", "uv", env=runner)
        self.assert_usage(command, "--", "--help", env=runner)
        missing = self.assert_usage(command, "wheel.whl", "uv", env=runner)
        self.assertIn("RUNNER_TEMP must name the directory for evidence", missing)


@contextlib.contextmanager
def held(path: Path) -> Generator[None]:
    """Hold an exclusive flock on `path`, as a concurrent writer would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_until(process: subprocess.Popen[bytes], marker: bytes) -> bytes:
    """Read the process's stderr until `marker` appears; the words before it stay readable."""
    assert process.stderr is not None
    collected = b""
    deadline = time.monotonic() + READY_SECONDS
    with selectors.DefaultSelector() as selector:
        selector.register(process.stderr, selectors.EVENT_READ)
        while marker not in collected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"never saw {marker!r} on stderr: {collected!r}")
            if selector.select(min(remaining, 0.2)):
                chunk = os.read(process.stderr.fileno(), 65536)
                if not chunk:
                    raise AssertionError(f"stderr ended before {marker!r}: {collected!r}")
                collected += chunk
    return collected


def wait_for_ready(process: subprocess.Popen[bytes], ready: Path) -> int:
    """Wait until a blocked process names itself in `ready`; return its PID."""
    deadline = time.monotonic() + READY_SECONDS
    while not ready.exists():
        if process.poll() is not None or time.monotonic() > deadline:
            stdout, stderr = process.communicate(timeout=30)
            raise AssertionError(f"never became ready: {process.returncode} {stdout!r} {stderr!r}")
        time.sleep(0.02)
    return int(ready.read_text())


def wait_for_health(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + READY_SECONDS
    while True:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/_html-publish-health", timeout=1
            ) as response:
                if response.read() == b"ok\n":
                    return
        except OSError:
            pass
        if process.poll() is not None or time.monotonic() > deadline:
            stdout, stderr = process.communicate(timeout=30)
            raise AssertionError(
                f"server never healthy: {process.returncode} {stdout!r} {stderr!r}"
            )
        time.sleep(0.05)


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def handoff_error(result: subprocess.CompletedProcess[str] | bytes) -> dict[str, object]:
    text = result.decode() if isinstance(result, bytes) else result.stdout
    return cast(dict[str, object], json_object(text)["error"])


def stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        process.kill()
        process.communicate(timeout=30)


def isolated_environment(root: Path) -> dict[str, str]:
    """ENVIRONMENT with XDG directories under `root`, so no user config or state leaks into a
    command under test."""
    environment = dict(ENVIRONMENT)
    for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        environment[variable] = str(root / variable.lower())
    return environment


def start_blocked(
    test: unittest.TestCase,
    command: Sequence[str],
    target: str,
    *,
    cwd: Path = ROOT,
    env: Mapping[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], int]:
    """Start `command` with the BLOCK_HOOK parking the process whose argv[0] ends with
    `target` right after it parses; return the started process and the parked PID."""
    scratch = Path(test.enterContext(tempfile.TemporaryDirectory(prefix="hp-blocked-")))
    (scratch / "sitecustomize.py").write_text(BLOCK_HOOK, encoding="utf-8")
    ready = scratch / "ready"
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env={
            **ENVIRONMENT,
            **(env or {}),
            "PYTHONPATH": str(scratch),
            "HP_CONTRACT_BLOCK": target,
            "HP_CONTRACT_READY": str(ready),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    test.addCleanup(stop_process, process)
    return process, wait_for_ready(process, ready)


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
