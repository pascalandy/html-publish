from __future__ import annotations

import contextlib
import functools
import http.server
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from html_publish import cli, store
from html_publish.model import Deadline
from tests.test_cli import QuietServer


class StallHandler(http.server.BaseHTTPRequestHandler):
    body = b"<!doctype html><h1>stalled</h1>\n"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path not in {"/report/", "/report/index.html"}:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        time.sleep(1.5)
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(self.body)


ROOT = Path(__file__).resolve().parents[1]
FAULT_SCRIPT = ROOT / "tests" / "_fault.py"


class RecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="html-publish-recovery-")
        self.root = Path(self.temporary.name)
        self.archive = self.root / "archive.git"
        self.runtime = self.root / "runtime"
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler,
            directory=str(self.runtime / "public"),
        )
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        address = self.server.server_address
        self.base_url = f"http://{address[0]}:{address[1]}/"
        self.config = self.root / "publisher.json"
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": self.base_url,
                    "allow_http": True,
                    "object_format": "sha1",
                    "limits": {
                        "command_seconds": 10,
                        "lock_seconds": 2,
                        "verification_seconds": 2,
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temporary.cleanup()

    def run_cli(
        self, *arguments: str, json_output: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            "-m",
            "html_publish",
            "--config",
            str(self.config),
        ]
        if json_output:
            command.append("--json")
        command.extend(arguments)
        return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)

    def run_fault(self, fault: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(FAULT_SCRIPT),
            fault,
            "--config",
            str(self.config),
            "--json",
            *arguments,
        ]
        return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)

    def payload(self, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        return json.loads(result.stdout)

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", f"--git-dir={self.archive}", *arguments],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def publish_arguments(self, source: Path, expected_revision: str | None = None) -> list[str]:
        arguments = [
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        ]
        if expected_revision is not None:
            arguments.extend(["--expected-revision", expected_revision])
        return arguments

    def test_kill_before_ref_advancement_recovers_by_retry(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
        published = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        revision_a = self.payload(published)["active_revision"]

        source.write_bytes(b"<!doctype html><h1>B</h1>\n")
        killed = self.run_fault(
            "before_ref",
            *self.publish_arguments(source, revision_a),
        )
        self.assertEqual(killed.returncode, 9)

        observed = self.run_cli("status", "--name", "report")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertEqual(self.payload(observed)["active_revision"], revision_a)
        self.assertEqual(self.payload(observed)["archived_revision"], revision_a)
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "1")

        retry = self.run_cli(*self.publish_arguments(source, revision_a))
        self.assertEqual(retry.returncode, 0, retry.stderr)
        retry_payload = self.payload(retry)
        self.assertEqual(retry_payload["outcome"], "published")
        self.assertEqual(retry_payload["effects"], {"archive_advanced": True, "activated": True})
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "2")
        self.assertEqual(
            (self.runtime / "public" / "report" / "index.html").read_bytes(),
            b"<!doctype html><h1>B</h1>\n",
        )

    def test_kill_on_first_publication_recovers_by_retry(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>first</h1>\n")
        killed = self.run_fault("before_ref", *self.publish_arguments(source))
        self.assertEqual(killed.returncode, 9)

        observed = self.run_cli("status", "--name", "report")
        observed_payload = self.payload(observed)
        self.assertEqual(observed_payload["observation"]["saved"], None)
        self.assertEqual(observed_payload["observation"]["selection"]["state"], "absent")
        self.assertEqual(self.git("for-each-ref"), "")

        retry = self.run_cli(*self.publish_arguments(source))
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.payload(retry)["outcome"], "published")
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "1")

    def test_kill_after_ref_advancement_reuses_the_saved_archive(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
        published = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        revision_a = self.payload(published)["active_revision"]

        source.write_bytes(b"<!doctype html><h1>B</h1>\n")
        killed = self.run_fault(
            "after_ref",
            *self.publish_arguments(source, revision_a),
        )
        self.assertEqual(killed.returncode, 9)

        observed = self.run_cli("status", "--name", "report")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        observed_payload = self.payload(observed)
        saved_revision = observed_payload["archived_revision"]
        self.assertNotEqual(saved_revision, revision_a)
        self.assertEqual(observed_payload["active_revision"], revision_a)
        saved_commit = self.git("rev-parse", "refs/heads/published")

        retry = self.run_cli(*self.publish_arguments(source, revision_a))
        self.assertEqual(retry.returncode, 0, retry.stderr)
        retry_payload = self.payload(retry)
        self.assertEqual(retry_payload["outcome"], "published")
        self.assertEqual(retry_payload["effects"], {"archive_advanced": False, "activated": True})
        self.assertEqual(retry_payload["active_revision"], saved_revision)
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), saved_commit)
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "2")

    def test_kill_after_export_completion_reuses_the_validated_export(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
        published = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        revision_a = self.payload(published)["active_revision"]

        source.write_bytes(b"<!doctype html><h1>B</h1>\n")
        killed = self.run_fault(
            "after_export",
            *self.publish_arguments(source, revision_a),
        )
        self.assertEqual(killed.returncode, 9)
        self.assertTrue((self.runtime / "public" / "report" / "index.html").is_file())

        retry = self.run_cli(*self.publish_arguments(source, revision_a))
        self.assertEqual(retry.returncode, 0, retry.stderr)
        retry_payload = self.payload(retry)
        self.assertEqual(retry_payload["outcome"], "published")
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "2")
        self.assertEqual(len(list((self.runtime / "releases").iterdir())), 2)
        self.assertEqual(
            (self.runtime / "public" / "report" / "index.html").read_bytes(),
            b"<!doctype html><h1>B</h1>\n",
        )

    def test_kill_after_selection_leaves_active_but_unverified_content(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
        published = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        revision_a = self.payload(published)["active_revision"]

        source.write_bytes(b"<!doctype html><h1>B</h1>\n")
        killed = self.run_fault(
            "after_selection",
            *self.publish_arguments(source, revision_a),
        )
        self.assertEqual(killed.returncode, 9)

        observed = self.run_cli("status", "--name", "report")
        observed_payload = self.payload(observed)
        active_revision = observed_payload["active_revision"]
        self.assertNotEqual(active_revision, revision_a)
        self.assertEqual(
            (self.runtime / "public" / "report" / "index.html").read_bytes(),
            b"<!doctype html><h1>B</h1>\n",
        )

        inspection = self.run_cli("verify", "--name", "report")
        self.assertEqual(inspection.returncode, 0, inspection.stderr)
        self.assertEqual(self.payload(inspection)["verification"]["result"], "passed")

        retry = self.run_cli(*self.publish_arguments(source, revision_a))
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.payload(retry)["outcome"], "unchanged")
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "2")

        source.write_bytes(b"<!doctype html><h1>C</h1>\n")
        changed = self.run_cli(*self.publish_arguments(source, revision_a))
        self.assertEqual(changed.returncode, 1)
        self.assertEqual(self.payload(changed)["error"]["code"], "revision_conflict")

    def test_fsync_failure_after_selection_reports_persistence_failure(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
        published = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        revision_a = self.payload(published)["active_revision"]

        source.write_bytes(b"<!doctype html><h1>B</h1>\n")
        original = store._fsync_directory  # pyright: ignore[reportPrivateUsage]

        def failing_fsync(path: Path) -> None:
            if path.name == "public":
                raise OSError("sync failed")
            original(path)

        report = io.StringIO()
        with (
            mock.patch.object(store, "_fsync_directory", failing_fsync),
            contextlib.redirect_stdout(report),
        ):
            exit_code = cli.main(
                [
                    "--config",
                    str(self.config),
                    "--json",
                    *self.publish_arguments(source, revision_a),
                ]
            )

        self.assertEqual(exit_code, 1)
        payload = json.loads(report.getvalue())
        self.assertEqual(payload["error"]["code"], "persistence_failure")
        self.assertEqual(payload["error"]["phase"], "activate")
        self.assertEqual(payload["effects"], {"archive_advanced": True, "activated": True})
        self.assertEqual(payload["active_revision"], payload["requested_revision"])
        self.assertEqual(payload["verification"]["result"], "not_checked")

    def test_replace_failure_reports_activation_failure(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
        published = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        revision_a = self.payload(published)["active_revision"]

        source.write_bytes(b"<!doctype html><h1>B</h1>\n")

        def failing_replace(src: object, dst: object) -> None:
            raise OSError("replace refused")

        report = io.StringIO()
        with (
            mock.patch("os.replace", failing_replace),
            contextlib.redirect_stdout(report),
        ):
            exit_code = cli.main(
                [
                    "--config",
                    str(self.config),
                    "--json",
                    *self.publish_arguments(source, revision_a),
                ]
            )

        self.assertEqual(exit_code, 1)
        payload = json.loads(report.getvalue())
        self.assertEqual(payload["error"]["code"], "activation_failure")
        self.assertEqual(payload["error"]["phase"], "activate")
        self.assertEqual(payload["effects"], {"archive_advanced": True, "activated": False})

    def test_rename_failure_reports_export_failure_with_stage_usage(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
        published = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        revision_a = self.payload(published)["active_revision"]

        source.write_bytes(b"<!doctype html><h1>B</h1>\n")

        def failing_rename(src: object, dst: object) -> None:
            raise OSError("rename refused")

        report = io.StringIO()
        with (
            mock.patch("os.rename", failing_rename),
            contextlib.redirect_stdout(report),
        ):
            exit_code = cli.main(
                [
                    "--config",
                    str(self.config),
                    "--json",
                    *self.publish_arguments(source, revision_a),
                ]
            )

        self.assertEqual(exit_code, 1)
        payload = json.loads(report.getvalue())
        self.assertEqual(payload["error"]["code"], "export_failure")
        self.assertEqual(payload["error"]["phase"], "export")
        self.assertEqual(payload["effects"], {"archive_advanced": True, "activated": False})
        self.assertIn("staging", payload["error"]["message"])
        match = re.search(r"\((\d+) bytes\)", payload["error"]["message"])
        self.assertIsNotNone(match)
        self.assertGreater(int(match.group(1) if match else "0"), 0)
        self.assertEqual(len(list((self.runtime / "staging").iterdir())), 1)

    def test_orphaned_git_lock_is_reported_and_never_removed(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
        published = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        revision_a = self.payload(published)["active_revision"]
        lock_path = self.archive / "refs" / "heads" / "published.lock"
        lock_path.write_text("", encoding="utf-8")

        source.write_bytes(b"<!doctype html><h1>B</h1>\n")
        failed = self.run_cli(*self.publish_arguments(source, revision_a))
        self.assertEqual(failed.returncode, 1)
        failed_payload = self.payload(failed)
        self.assertEqual(failed_payload["error"]["code"], "archive_failure")
        self.assertIn("published.lock", failed_payload["error"]["message"])
        self.assertEqual(failed_payload["effects"], {"archive_advanced": False, "activated": False})
        self.assertTrue(lock_path.exists())

        lock_path.unlink()
        retry = self.run_cli(*self.publish_arguments(source, revision_a))
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.payload(retry)["outcome"], "published")

    def test_git_child_timeout_is_reaped(self) -> None:
        stub = self.root / "stub-bin"
        stub.mkdir()
        (stub / "git").write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
        (stub / "git").chmod(0o755)

        started = time.monotonic()
        with (
            mock.patch.dict(os.environ, {"PATH": f"{stub}:{os.environ['PATH']}"}),
            self.assertRaises(store.PublishError) as raised,
        ):
            store._git.command(  # pyright: ignore[reportPrivateUsage]
                None, ["--version"], Deadline.start(0.3)
            )
        elapsed = time.monotonic() - started

        self.assertEqual(raised.exception.failure.code, "git_timeout")
        self.assertLess(elapsed, 2.0)

    def test_verification_timeout_reports_failed_delivery(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        slow_server = QuietServer(("127.0.0.1", 0), StallHandler)
        slow_thread = threading.Thread(target=slow_server.serve_forever, daemon=True)
        slow_thread.start()
        address = slow_server.server_address
        slow_url = f"http://{address[0]}:{address[1]}/"
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["base_url"] = slow_url
        payload["limits"]["verification_seconds"] = 0.2
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        source = self.root / "report.html"
        source.write_bytes(StallHandler.body)
        try:
            result = self.run_cli(
                "publish",
                "--name",
                "report",
                "--source",
                str(source),
                "--target",
                slow_url,
            )
        finally:
            slow_server.shutdown()
            slow_server.server_close()
            slow_thread.join(timeout=2)

        self.assertEqual(result.returncode, 1)
        result_payload = self.payload(result)
        self.assertEqual(result_payload["error"]["code"], "delivery_failure")
        self.assertEqual(result_payload["verification"]["result"], "failed")
        self.assertIsNotNone(result_payload["verification"]["detail"])
        self.assertEqual(result_payload["effects"], {"archive_advanced": True, "activated": True})
        self.assertEqual(result_payload["active_revision"], result_payload["requested_revision"])


if __name__ == "__main__":
    unittest.main()
