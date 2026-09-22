from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import tempfile
import time
import unittest
import urllib.error
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast
from unittest import mock

import html_publish.deploy as deploy_module
from html_publish.deploy import (
    CommandResult,
    DeployError,
    Layout,
    health,
    install,
    rollback,
)


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.wheel_bytes = b"release-a"
        self.serve: dict[str, object] = {}
        self.service_active = True
        self.restart_failures = 0
        self.unit_file_state = "not-found"

    def __call__(self, argv: Sequence[str]) -> CommandResult:
        command = tuple(argv)
        self.calls.append(command)
        if command[:2] == ("uv", "build"):
            output = Path(command[command.index("--out-dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / "html_publish-0.1.0-py3-none-any.whl").write_bytes(self.wheel_bytes)
        elif command[:2] == ("uv", "venv"):
            (Path(command[-1]) / "bin").mkdir(parents=True)
            (Path(command[-1]) / "bin" / "python").write_text("fake", encoding="utf-8")
        elif command[:2] == ("git", "init"):
            Path(command[-1]).mkdir(parents=True)
        elif command == ("tailscale", "serve", "status", "--json"):
            return CommandResult(json.dumps(self.serve))
        elif command[:2] == ("tailscale", "serve"):
            tcp = cast(dict[str, object], self.serve.setdefault("TCP", {}))
            web = cast(dict[str, object], self.serve.setdefault("Web", {}))
            host = cast(dict[str, object], web.setdefault("om1.donkey-arcturus.ts.net:8444", {}))
            handlers = cast(dict[str, object], host.setdefault("Handlers", {}))
            if command[-1] == "off":
                handlers.pop("/html-publish", None)
            else:
                tcp["8444"] = {"HTTPS": True}
                handlers["/html-publish"] = {"Proxy": "http://127.0.0.1:4177"}
        elif command[:3] == ("systemctl", "--user", "show"):
            return CommandResult(self.unit_file_state)
        elif command[:3] == ("systemctl", "--user", "enable"):
            self.unit_file_state = "enabled"
        elif command[:3] == ("systemctl", "--user", "disable"):
            self.unit_file_state = "disabled"
        elif command == ("systemctl", "--user", "is-active", "html-publish.service"):
            if not self.service_active:
                raise DeployError("service is inactive")
            return CommandResult("active\n")
        elif command == ("systemctl", "--user", "restart", "html-publish.service"):
            if self.restart_failures:
                self.restart_failures -= 1
                raise DeployError("systemctl restart failed")
        return CommandResult()


def successful_probe(url: str) -> tuple[bool, str]:
    return True, f"HTTP 200 from {url}"


class DeploymentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="html-publish-deploy-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.layout = Layout(
            self.root / "state",
            self.root / "config" / "publisher.json",
            self.root / "systemd" / "html-publish.service",
        )
        self.runner = FakeRunner()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reinstall_is_idempotent(self) -> None:
        probed: list[str] = []

        def recording_probe(url: str) -> tuple[bool, str]:
            probed.append(url)
            return True, "HTTP 200"

        first = install(self.layout, self.source, self.runner, recording_probe)
        current = self.layout.current.resolve()
        previous = self.layout.previous.resolve()
        first_config = self.layout.config.read_bytes()
        second = install(self.layout, self.source, self.runner, recording_probe)

        self.assertEqual(first["outcome"], "updated")
        self.assertEqual(second["outcome"], "unchanged")
        self.assertEqual(self.layout.current.resolve(), current)
        self.assertEqual(self.layout.previous.resolve(), previous)
        self.assertEqual(self.layout.config.read_bytes(), first_config)
        self.assertFalse(self.layout.archive.exists())
        self.assertFalse(self.layout.runtime.exists())
        self.assertFalse(any(call[:2] == ("git", "init") for call in self.runner.calls))
        self.assertEqual(sum(call[:2] == ("uv", "venv") for call in self.runner.calls), 1)
        serve_writes = [
            call
            for call in self.runner.calls
            if call[:2] == ("tailscale", "serve") and "status" not in call
        ]
        self.assertEqual(len(serve_writes), 1)
        self.assertEqual(
            probed,
            [
                "http://127.0.0.1:4177/_html-publish-health",
                "https://om1.donkey-arcturus.ts.net:8444/html-publish/_html-publish-health",
            ]
            * 2,
        )
        unit = self.layout.unit.read_text(encoding="utf-8")
        self.assertIn("-m html_publish.server --port 4177 --bind 127.0.0.1", unit)
        self.assertIn(f"--directory {self.layout.runtime / 'public'}", unit)
        self.assertNotIn("-m http.server", unit)
        first_build = next(
            index for index, call in enumerate(self.runner.calls) if call[:2] == ("uv", "build")
        )
        first_restart = next(
            index
            for index, call in enumerate(self.runner.calls)
            if call == ("systemctl", "--user", "restart", "html-publish.service")
        )
        first_serve_write = self.runner.calls.index(serve_writes[0])
        self.assertLess(first_build, first_restart)
        self.assertLess(first_restart, first_serve_write)

    def test_upgrade_preserves_publication_state_and_existing_config(self) -> None:
        install(self.layout, self.source, self.runner, successful_probe)
        self.layout.archive.mkdir()
        self.layout.runtime.mkdir()
        (self.layout.archive / "archive-sentinel").write_text("history", encoding="utf-8")
        (self.layout.runtime / "runtime-sentinel").write_text("active", encoding="utf-8")
        config = cast(dict[str, object], json.loads(self.layout.config.read_text(encoding="utf-8")))
        config["limits"] = {"max_files": 27}
        self.layout.config.write_text(json.dumps(config), encoding="utf-8")
        preserved_config = self.layout.config.read_bytes()
        first_release = self.layout.current.resolve()

        self.runner.wheel_bytes = b"release-b"
        result = install(self.layout, self.source, self.runner, successful_probe)

        self.assertEqual(result["outcome"], "updated")
        self.assertNotEqual(self.layout.current.resolve(), first_release)
        self.assertEqual(self.layout.previous.resolve(), first_release)
        self.assertEqual((self.layout.archive / "archive-sentinel").read_text(), "history")
        self.assertEqual((self.layout.runtime / "runtime-sentinel").read_text(), "active")
        self.assertEqual(self.layout.config.read_bytes(), preserved_config)

    def test_route_collision_refuses_to_change_tailscale(self) -> None:
        self.runner.serve = {
            "TCP": {"8444": {"HTTPS": True}},
            "Web": {
                "om1.donkey-arcturus.ts.net:8444": {
                    "Handlers": {"/html-publish": {"Proxy": "http://127.0.0.1:9999"}}
                }
            },
        }

        with self.assertRaisesRegex(DeployError, "owned by another target"):
            install(self.layout, self.source, self.runner, successful_probe)

        serve_writes = [
            call
            for call in self.runner.calls
            if call[:2] == ("tailscale", "serve") and "status" not in call
        ]
        self.assertEqual(serve_writes, [])
        self.assertFalse(self.layout.state_root.exists())
        self.assertFalse(self.layout.unit.exists())
        self.assertFalse(self.layout.config.exists())
        self.assertEqual(self.runner.calls, [("tailscale", "serve", "status", "--json")])

    def test_upgrade_then_default_rollback_swaps_releases(self) -> None:
        first = install(self.layout, self.source, self.runner, successful_probe)
        first_release = cast(str, first["release"])
        self.runner.wheel_bytes = b"release-b"
        second = install(self.layout, self.source, self.runner, successful_probe)
        second_release = cast(str, second["release"])

        result = rollback(self.layout, None, self.runner, successful_probe)

        self.assertEqual(result["release"], first_release)
        self.assertEqual(self.layout.current.resolve().name, first_release)
        self.assertEqual(self.layout.previous.resolve().name, second_release)
        report = health(self.layout, self.runner, successful_probe)
        self.assertTrue(report["healthy"])
        self.assertEqual(report["release"], first_release)

    def test_failed_upgrade_health_restores_prior_release(self) -> None:
        first = install(self.layout, self.source, self.runner, successful_probe)
        first_release = cast(str, first["release"])
        self.runner.wheel_bytes = b"release-b"

        def failed_https(url: str) -> tuple[bool, str]:
            if url.startswith("https://"):
                return False, "HTTP 503"
            return True, "HTTP 200"

        with self.assertRaisesRegex(DeployError, "Post-install health check failed"):
            install(self.layout, self.source, self.runner, failed_https)

        self.assertEqual(self.layout.current.resolve().name, first_release)
        self.assertEqual(self.layout.previous.resolve().name, first_release)

    def test_real_probe_retries_transient_failures_until_healthy(self) -> None:
        outcomes: list[Exception | FakeResponse] = [
            urllib.error.URLError("connection refused"),
            FakeResponse(503),
            FakeResponse(200),
        ]
        timeouts: list[float] = []

        def open_url(request: object, *, timeout: float) -> FakeResponse:
            del request
            timeouts.append(timeout)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            mock.patch("html_publish.deploy.urllib.request.urlopen", side_effect=open_url),
            mock.patch("html_publish.deploy.time.sleep") as sleep,
        ):
            probe = cast(
                Callable[[str], tuple[bool, str]],
                deploy_module.__dict__["_probe"],
            )
            result = probe("http://127.0.0.1:4177/_html-publish-health")

        self.assertEqual(result, (True, "HTTP 200"))
        self.assertEqual(len(timeouts), 3)
        self.assertTrue(all(0 < timeout <= 0.75 for timeout in timeouts))
        self.assertEqual(sleep.call_count, 2)

    def test_install_restart_failure_restores_both_pointers(self) -> None:
        install(self.layout, self.source, self.runner, successful_probe)
        original_current = self.layout.current.resolve()
        original_previous = self.layout.previous.resolve()
        self.runner.wheel_bytes = b"release-b"
        self.runner.restart_failures = 2

        with self.assertRaisesRegex(DeployError, "systemctl restart failed"):
            install(self.layout, self.source, self.runner, successful_probe)

        self.assertEqual(self.layout.current.resolve(), original_current)
        self.assertEqual(self.layout.previous.resolve(), original_previous)
        self.assertEqual(self.runner.restart_failures, 0)

    def test_rollback_restart_failure_restores_both_pointers(self) -> None:
        install(self.layout, self.source, self.runner, successful_probe)
        self.runner.wheel_bytes = b"release-b"
        install(self.layout, self.source, self.runner, successful_probe)
        original_current = self.layout.current.resolve()
        original_previous = self.layout.previous.resolve()
        self.runner.restart_failures = 2

        with self.assertRaisesRegex(DeployError, "systemctl restart failed"):
            rollback(self.layout, None, self.runner, successful_probe)

        self.assertEqual(self.layout.current.resolve(), original_current)
        self.assertEqual(self.layout.previous.resolve(), original_previous)
        self.assertEqual(self.runner.restart_failures, 0)

    def test_config_collision_and_invalid_files_precede_all_mutations(self) -> None:
        self.layout.config.parent.mkdir()
        for content in ('{"archive":"other"}', "[]"):
            with self.subTest(content=content):
                self.layout.config.write_text(content)
                with self.assertRaisesRegex(DeployError, "config"):
                    install(self.layout, self.source, self.runner, successful_probe)
                self.assertEqual(self.layout.config.read_text(), content)
                self.assertFalse(self.layout.state_root.exists())
                self.assertEqual(self.runner.calls, [])
        self.layout.config.unlink()
        self.layout.unit.parent.mkdir()
        self.layout.unit.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(DeployError, "regular file"):
            install(self.layout, self.source, self.runner, successful_probe)
        self.assertFalse(self.layout.state_root.exists())
        self.assertTrue(self.layout.unit.is_symlink())

    def test_same_wheel_failure_restores_exact_unit_config_and_relative_pointers(self) -> None:
        install(self.layout, self.source, self.runner, successful_probe)
        release = self.layout.current.resolve()
        for pointer in (self.layout.current, self.layout.previous):
            pointer.unlink()
            pointer.symlink_to(f"./app-releases/{release.name}")
        self.layout.unit.write_bytes(b"[Service]\nExecStart=/original\n")
        self.layout.unit.chmod(0o640)
        self.layout.config.chmod(0o640)
        before = self.layout.config.read_bytes()
        self.runner.restart_failures = 1

        with self.assertRaisesRegex(DeployError, "restart failed"):
            install(self.layout, self.source, self.runner, successful_probe)

        self.assertEqual(self.layout.unit.read_bytes(), b"[Service]\nExecStart=/original\n")
        self.assertEqual(self.layout.unit.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.layout.config.read_bytes(), before)
        self.assertEqual(self.layout.config.stat().st_mode & 0o777, 0o640)
        for pointer in (self.layout.current, self.layout.previous):
            self.assertEqual(os.readlink(pointer), f"./app-releases/{release.name}")
        self.assertEqual(self.runner.unit_file_state, "enabled")

    def test_failed_first_install_removes_only_owned_state_and_retains_release(self) -> None:
        self.runner.serve = {
            "TCP": {"443": {"HTTPS": True}, "8444": {"HTTPS": True}},
            "Web": {
                "om1.donkey-arcturus.ts.net:8444": {
                    "Handlers": {"/other": {"Proxy": "http://127.0.0.1:9999"}}
                }
            },
        }
        original_routes = json.dumps(self.runner.serve, sort_keys=True)
        with self.assertRaisesRegex(DeployError, "Post-install health check failed"):
            install(self.layout, self.source, self.runner, lambda _: (False, "fixture failure"))
        self.assertFalse(self.layout.current.is_symlink())
        self.assertFalse(self.layout.previous.is_symlink())
        self.assertFalse(self.layout.config.exists())
        self.assertFalse(self.layout.unit.exists())
        self.assertEqual(self.runner.unit_file_state, "disabled")
        self.assertEqual(json.dumps(self.runner.serve, sort_keys=True), original_routes)
        self.assertEqual(len(list(self.layout.releases.glob("*/.ready"))), 1)

    def test_failed_upgrade_preserves_disabled_state_and_publication_data(self) -> None:
        install(self.layout, self.source, self.runner, successful_probe)
        self.runner.unit_file_state = "disabled"
        self.layout.archive.mkdir()
        self.layout.runtime.mkdir()
        (self.layout.archive / "sentinel").write_bytes(b"archive")
        (self.layout.runtime / "sentinel").write_bytes(b"runtime")
        original = self.layout.current.resolve()
        self.runner.wheel_bytes = b"release-b"
        with self.assertRaisesRegex(DeployError, "Post-install health check failed"):
            install(self.layout, self.source, self.runner, lambda _: (False, "HTTP 503"))
        self.assertEqual(self.runner.unit_file_state, "disabled")
        self.assertEqual(self.layout.current.resolve(), original)
        self.assertEqual((self.layout.archive / "sentinel").read_bytes(), b"archive")
        self.assertEqual((self.layout.runtime / "sentinel").read_bytes(), b"runtime")
        self.assertEqual(len(list(self.layout.releases.glob("*/.ready"))), 2)

    def test_partial_pointer_activation_recovers_exact_original_pair(self) -> None:
        install(self.layout, self.source, self.runner, successful_probe)
        self.runner.wheel_bytes = b"release-b"
        install(self.layout, self.source, self.runner, successful_probe)
        original = (os.readlink(self.layout.current), os.readlink(self.layout.previous))
        replace = os.replace

        def fail_current(source: str | Path, destination: str | Path) -> None:
            if Path(destination) == self.layout.current:
                raise OSError("fixture pointer rename failed")
            replace(source, destination)

        self.runner.wheel_bytes = b"release-c"
        with (
            mock.patch("html_publish.deploy.os.replace", side_effect=fail_current),
            self.assertRaisesRegex(DeployError, "fixture pointer rename failed"),
        ):
            install(self.layout, self.source, self.runner, successful_probe)
        self.assertEqual(
            (os.readlink(self.layout.current), os.readlink(self.layout.previous)), original
        )

    def test_changed_unit_is_preserved_and_original_and_recovery_errors_are_reported(self) -> None:
        install(self.layout, self.source, self.runner, successful_probe)

        def concurrent_edit(_: str) -> tuple[bool, str]:
            self.layout.unit.write_bytes(b"someone else's unit")
            return False, "original health failure"

        with self.assertRaises(DeployError) as raised:
            install(self.layout, self.source, self.runner, concurrent_edit)
        self.assertIn("original health failure", str(raised.exception))
        self.assertIn("recovery errors", str(raised.exception))
        self.assertIn("Refusing to restore changed file", str(raised.exception))
        self.assertEqual(self.layout.unit.read_bytes(), b"someone else's unit")

    def test_recovery_restart_error_is_not_suppressed(self) -> None:
        install(self.layout, self.source, self.runner, successful_probe)
        self.runner.restart_failures = 2
        with self.assertRaisesRegex(
            DeployError, "restart failed; recovery errors: service state: systemctl restart failed"
        ):
            install(self.layout, self.source, self.runner, successful_probe)

    def test_route_removed_during_build_stops_before_activation(self) -> None:
        install(self.layout, self.source, self.runner, successful_probe)
        original = os.readlink(self.layout.current)
        self.runner.wheel_bytes = b"release-b"

        def changed_route(argv: Sequence[str]) -> CommandResult:
            result = self.runner(argv)
            if tuple(argv[:2]) == ("uv", "build"):
                self.runner.serve = {}
            return result

        with self.assertRaisesRegex(DeployError, "route changed during release preparation"):
            install(self.layout, self.source, changed_route, successful_probe)
        self.assertEqual(os.readlink(self.layout.current), original)
        self.assertEqual(self.runner.serve, {})

    def test_incomplete_release_is_retained_and_not_overwritten(self) -> None:
        def failed_pip(argv: Sequence[str]) -> CommandResult:
            if tuple(argv[:2]) == ("uv", "pip"):
                raise DeployError("fixture install failure")
            return self.runner(argv)

        with self.assertRaisesRegex(DeployError, "Release preparation failed; retained"):
            install(self.layout, self.source, failed_pip, successful_probe)
        release = next(self.layout.releases.iterdir())
        executable = release / ".venv/bin/python"
        self.assertEqual(executable.read_text(), "fake")
        with self.assertRaisesRegex(DeployError, "Incomplete release already exists"):
            install(self.layout, self.source, self.runner, successful_probe)
        self.assertEqual(executable.read_text(), "fake")
        self.assertFalse(self.layout.current.is_symlink())

    def test_route_changed_after_creation_is_not_removed_by_recovery(self) -> None:
        def concurrent_route(_: str) -> tuple[bool, str]:
            self.runner.serve = {
                "Web": {
                    "om1.donkey-arcturus.ts.net:8444": {
                        "Handlers": {"/html-publish": {"Proxy": "http://127.0.0.1:9999"}}
                    }
                }
            }
            return False, "route replaced"

        with self.assertRaisesRegex(DeployError, "route changed before recovery"):
            install(self.layout, self.source, self.runner, concurrent_route)
        self.assertIn("9999", json.dumps(self.runner.serve))
        self.assertFalse(any(call[-1] == "off" for call in self.runner.calls))

    def test_command_deadline_and_normal_exit_kill_descendants(self) -> None:
        run = cast(Callable[..., CommandResult], deploy_module.__dict__["_run"])
        for wait in (True, False):
            with self.subTest(wait=wait):
                identity = self.root / f"child-{wait}"
                child_code = (
                    "import os,signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                    f"Path({str(identity)!r}).write_text(str(os.getpid())); time.sleep(60)"
                )
                parent_code = (
                    "import subprocess,sys,time; from pathlib import Path; "
                    f"subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
                    f"p=Path({str(identity)!r}); "
                    "\nwhile not p.exists(): time.sleep(.01)\n"
                    + ("time.sleep(60)" if wait else "print('parent exited')")
                )
                started = time.monotonic()
                try:
                    if wait:
                        with self.assertRaisesRegex(DeployError, "timed out"):
                            run((sys.executable, "-c", parent_code), timeout_seconds=0.5)
                    else:
                        result = run((sys.executable, "-c", parent_code), timeout_seconds=2)
                        self.assertEqual(result.stdout, "parent exited\n")
                    self.assertLess(time.monotonic() - started, 5)
                    pid = int(identity.read_text())
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        status = Path(f"/proc/{pid}/stat")
                        if not status.exists() or status.read_text().split()[2] == "Z":
                            break
                        time.sleep(0.01)
                    else:
                        self.fail(f"descendant {pid} still running")
                finally:
                    if identity.exists():
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(int(identity.read_text()), signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
