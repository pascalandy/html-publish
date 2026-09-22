from __future__ import annotations

import json
import tempfile
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
            self.serve = {
                "TCP": {"8444": {"HTTPS": True}},
                "Web": {
                    "om1.donkey-arcturus.ts.net:8444": {
                        "Handlers": {"/html-publish": {"Proxy": "http://127.0.0.1:4177"}}
                    }
                },
            }
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
        self.assertTrue(self.layout.current.exists())

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


if __name__ == "__main__":
    unittest.main()
