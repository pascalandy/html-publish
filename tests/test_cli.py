from __future__ import annotations

import functools
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        pass


class SlowHandler(http.server.BaseHTTPRequestHandler):
    body = b"<!doctype html><h1>slow</h1>\n"

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
        try:
            for byte in self.body:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError):
            pass


class PublisherCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="html-publish-test-")
        self.root = Path(self.temporary.name)
        self.archive = self.root / "archive.git"
        self.runtime = self.root / "runtime"
        handler = functools.partial(QuietHandler, directory=str(self.runtime / "public"))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        address = self.server.server_address
        host, port = str(address[0]), int(address[1])
        self.base_url = f"http://{host}:{port}/"
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
        self._stop_server()
        self.temporary.cleanup()

    def _stop_server(self) -> None:
        if self.server_thread.is_alive():
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=2)

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
        return subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def payload(self, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        return json.loads(result.stdout)

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", f"--git-dir={self.archive}", *arguments],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def config_payload(self) -> dict[str, Any]:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def test_plan_is_advisory_and_does_not_create_persistent_state(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>planned</h1>\n")

        result = self.run_cli(
            "plan",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.payload(result)
        self.assertEqual(payload["outcome"], "planned")
        self.assertEqual(payload["prediction"], "create")
        self.assertEqual(payload["file_count"], 1)
        self.assertEqual(payload["differences"]["added"], ["index.html"])
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.runtime.exists())

    def test_publish_file_and_identical_retry_use_one_commit(self) -> None:
        source = self.root / "report.html"
        body = b"<!doctype html><h1>first</h1>\n"
        source.write_bytes(body)

        first = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--request-id",
            "attempt-1",
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        first_payload = self.payload(first)
        self.assertEqual(first_payload["outcome"], "published")
        self.assertEqual(first_payload["request_id"], "attempt-1")
        self.assertEqual(first_payload["verification"]["result"], "passed")
        self.assertEqual(first_payload["effects"], {"archive_advanced": True, "activated": True})
        active = self.runtime / "public" / "report"
        self.assertTrue(active.is_symlink())
        self.assertEqual((active / "index.html").read_bytes(), body)
        with urllib.request.urlopen(first_payload["url"], timeout=2) as response:
            self.assertEqual(response.read(), body)

        first_commit = self.git("rev-parse", "refs/heads/published")
        second = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--request-id",
            "attempt-2",
        )

        self.assertEqual(second.returncode, 0, second.stderr)
        second_payload = self.payload(second)
        self.assertEqual(second_payload["outcome"], "unchanged")
        self.assertEqual(second_payload["effects"], {"archive_advanced": False, "activated": False})
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), first_commit)
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "1")

        plain = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            json_output=False,
        )
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stdout, f"{self.base_url}report/\n")
        self.assertEqual(plain.stderr, "")

    def test_directory_assets_and_second_name_preserve_both_pages(self) -> None:
        first = self.root / "one.html"
        first.write_bytes(b"<!doctype html><h1>one</h1>\n")
        first_result = self.run_cli(
            "publish",
            "--name",
            "one",
            "--source",
            str(first),
            "--target",
            self.base_url,
        )
        self.assertEqual(first_result.returncode, 0, first_result.stderr)

        site = self.root / "site"
        (site / "assets").mkdir(parents=True)
        (site / "index.html").write_bytes(
            b'<!doctype html><link rel="stylesheet" href="assets/site.css"><h1>two</h1>\n'
        )
        (site / "assets" / "site.css").write_bytes(b"body{color:white;background:black}\n")
        second_result = self.run_cli(
            "publish",
            "--name",
            "two",
            "--source",
            str(site),
            "--target",
            self.base_url,
        )

        self.assertEqual(second_result.returncode, 0, second_result.stderr)
        tree_paths = self.git("ls-tree", "-r", "--name-only", "refs/heads/published").splitlines()
        self.assertEqual(
            tree_paths,
            ["one/site/index.html", "two/site/assets/site.css", "two/site/index.html"],
        )
        self.assertEqual(
            (self.runtime / "public" / "one" / "index.html").read_bytes(), first.read_bytes()
        )
        self.assertEqual(
            (self.runtime / "public" / "two" / "assets" / "site.css").read_bytes(),
            b"body{color:white;background:black}\n",
        )

    def test_changed_active_content_conflicts_without_mutation(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>old</h1>\n")
        first = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        first_commit = self.git("rev-parse", "refs/heads/published")

        source.write_bytes(b"<!doctype html><h1>new</h1>\n")
        conflict = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )

        self.assertEqual(conflict.returncode, 1)
        payload = self.payload(conflict)
        self.assertEqual(payload["error"]["code"], "revision_conflict")
        self.assertEqual(payload["effects"], {"archive_advanced": False, "activated": False})
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), first_commit)
        self.assertEqual(
            (self.runtime / "public" / "report" / "index.html").read_bytes(),
            b"<!doctype html><h1>old</h1>\n",
        )

    def test_delivery_failure_reports_selected_unverified_state(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>offline</h1>\n")
        self._stop_server()

        result = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )

        self.assertEqual(result.returncode, 1)
        payload = self.payload(result)
        self.assertEqual(payload["error"]["code"], "delivery_failure")
        self.assertEqual(payload["verification"]["result"], "failed")
        self.assertEqual(payload["effects"], {"archive_advanced": True, "activated": True})
        self.assertEqual(payload["active_revision"], payload["requested_revision"])
        self.assertTrue((self.runtime / "public" / "report").is_symlink())

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are required")
    def test_symlink_input_is_rejected_without_persistent_state(self) -> None:
        site = self.root / "site"
        site.mkdir()
        (site / "index.html").write_bytes(b"<!doctype html><h1>unsafe</h1>\n")
        os.symlink("index.html", site / "copy.html")

        result = self.run_cli(
            "plan",
            "--name",
            "unsafe",
            "--source",
            str(site),
            "--target",
            self.base_url,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.payload(result)["error"]["code"], "unsafe_input")
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.runtime.exists())

    def test_target_mismatch_is_rejected_before_persistent_state(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>wrong target</h1>\n")

        result = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            f"{self.base_url}other/",
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.payload(result)["error"]["code"], "target_mismatch")
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.runtime.exists())

    def test_invalid_config_values_return_json_without_a_traceback(self) -> None:
        invalid_values: tuple[tuple[str, dict[str, object]], ...] = (
            ("https://[/", {}),
            ("http://127.0.0.1:nope/", {"allow_http": True}),
        )
        for base_url, overrides in invalid_values:
            with self.subTest(base_url=base_url):
                payload = self.config_payload()
                payload.update(overrides)
                payload["base_url"] = base_url
                self.config.write_text(json.dumps(payload), encoding="utf-8")
                result = self.run_cli("status")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.payload(result)["error"]["code"], "invalid_config")
                self.assertNotIn("Traceback", result.stderr)

        payload = self.config_payload()
        payload["base_url"] = self.base_url
        payload["limits"]["max_bytes"] = float("nan")
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.payload(result)["error"]["code"], "invalid_config")
        self.assertNotIn("Traceback", result.stderr)

    def test_runtime_file_failure_still_returns_structured_json(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>runtime failure</h1>\n")
        self.runtime.write_text("not a directory", encoding="utf-8")

        result = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )

        self.assertEqual(result.returncode, 1)
        payload = self.payload(result)
        self.assertEqual(payload["error"]["code"], "runtime_failure")
        self.assertEqual(payload["effects"], {"archive_advanced": False, "activated": False})
        self.assertNotIn("Traceback", result.stderr)

    def test_existing_empty_bare_archive_accepts_first_publication(self) -> None:
        subprocess.run(
            ["git", "init", "--bare", str(self.archive)],
            capture_output=True,
            text=True,
            check=True,
        )
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>initialized</h1>\n")

        result = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.payload(result)["outcome"], "published")
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "1")

    def test_total_deadline_interrupts_a_trickling_http_body(self) -> None:
        self._stop_server()
        slow_server = QuietServer(("127.0.0.1", 0), SlowHandler)
        slow_thread = threading.Thread(target=slow_server.serve_forever, daemon=True)
        slow_thread.start()
        address = slow_server.server_address
        slow_url = f"http://{address[0]}:{address[1]}/"
        payload = self.config_payload()
        payload["base_url"] = slow_url
        payload["limits"]["command_seconds"] = 0.5
        payload["limits"]["verification_seconds"] = 0.2
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        source = self.root / "report.html"
        source.write_bytes(SlowHandler.body)
        started = time.monotonic()
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
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 1)
        response = self.payload(result)
        self.assertEqual(response["error"]["code"], "command_timeout")
        self.assertEqual(response["verification"]["result"], "failed")
        self.assertEqual(response["effects"], {"archive_advanced": True, "activated": True})
        self.assertLess(elapsed, 1.5)

    def test_status_reports_saved_and_selected_facts_without_http(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>status</h1>\n")
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
        expected_revision = self.payload(published)["requested_revision"]
        self._stop_server()

        result = self.run_cli("status", "--name", "report")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.payload(result)
        self.assertEqual(payload["outcome"], "observed")
        self.assertEqual(payload["archived_revision"], expected_revision)
        self.assertEqual(payload["active_revision"], expected_revision)
        self.assertEqual(payload["verification"]["result"], "not_checked")
        self.assertFalse(payload["observation"]["selection"]["integrity_checked"])

        listing = self.run_cli("status")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        listing_payload = self.payload(listing)
        self.assertEqual(len(listing_payload["entries"]), 1)
        self.assertEqual(listing_payload["entries"][0]["name"], "report")
        self.assertEqual(
            listing_payload["entries"][0]["observation"]["saved"]["revision"],
            expected_revision,
        )

    def test_json_usage_error_uses_exit_two_and_one_object(self) -> None:
        result = self.run_cli("publish", "--name", "missing-fields")

        self.assertEqual(result.returncode, 2)
        payload = self.payload(result)
        self.assertEqual(payload["outcome"], "error")
        self.assertEqual(payload["error"]["code"], "invalid_usage")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
