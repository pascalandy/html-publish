from __future__ import annotations

import contextlib
import errno
import functools
import gzip
import http.server
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from html_publish import _git, cli
from html_publish.model import Deadline

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class QuietServer(http.server.ThreadingHTTPServer):
    redirect_location: str | None

    def handle_error(self, request: object, client_address: object) -> None:
        pass


class SlowHandler(http.server.BaseHTTPRequestHandler):
    body = b"<!doctype html><h1>slow</h1>\n" + b"." * 256
    request_started = threading.Event()

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.request_started.set()
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


class RedirectHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        location = getattr(self.server, "redirect_location", None)
        if location is not None and self.path.startswith("/report/"):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_error(404)


class GzipHandler(http.server.BaseHTTPRequestHandler):
    body = gzip.compress(b"<!doctype html><h1>gzipped</h1>\n")

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path not in {"/report/", "/report/index.html"}:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


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
        self,
        *arguments: str,
        json_output: bool = True,
        env: dict[str, str] | None = None,
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
            env={**os.environ, **env} if env else None,
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

        guarded = self.run_cli(
            "plan",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            "missing-active-revision",
        )
        self.assertEqual(guarded.returncode, 0, guarded.stderr)
        self.assertEqual(self.payload(guarded)["prediction"], "conflict")
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.runtime.exists())

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

    def test_newline_temporary_parent_preserves_served_bytes(self) -> None:
        temporary_parent = self.root / "temporary\nparent"
        temporary_parent.mkdir()
        source = self.root / "site"
        source.mkdir()
        files = {
            "index.html": b"<!doctype html><h1>exact bytes</h1>\n",
            "café space.txt": b"\x00binary\ncontent\xff",
        }
        for name, contents in files.items():
            (source / name).write_bytes(contents)
        arguments = ("--name", "report", "--source", str(source), "--target", self.base_url)
        environment = {"TMPDIR": str(temporary_parent)}

        planned = self.run_cli("plan", *arguments, env=environment)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(self.payload(planned)["file_count"], len(files))
        published = self.run_cli("publish", *arguments, env=environment)
        self.assertEqual(published.returncode, 0, published.stderr)
        report = self.payload(published)
        self.assertEqual(report["verification"]["result"], "passed")
        self.assertEqual(report["active_revision"], self.payload(planned)["requested_revision"])
        for name, contents in files.items():
            url = self.base_url + "report/" + urllib.parse.quote(name)
            with urllib.request.urlopen(url) as response:
                self.assertEqual(response.read(), contents)

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

    def test_guarded_update_replaces_complete_site_and_preserves_other_page(self) -> None:
        other = self.root / "other.html"
        other.write_bytes(b"<!doctype html><h1>other</h1>\n")
        other_result = self.run_cli(
            "publish",
            "--name",
            "other",
            "--source",
            str(other),
            "--target",
            self.base_url,
        )
        self.assertEqual(other_result.returncode, 0, other_result.stderr)
        other_payload = self.payload(other_result)
        other_link = os.readlink(self.runtime / "public" / "other")

        site = self.root / "site"
        (site / "assets").mkdir(parents=True)
        (site / "index.html").write_bytes(b"<!doctype html><h1>A</h1>\n")
        (site / "assets" / "removed.css").write_bytes(b"body{color:red}\n")
        first = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(site),
            "--target",
            self.base_url,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        first_payload = self.payload(first)
        first_revision = first_payload["active_revision"]
        stable_url = first_payload["url"]
        first_commit = self.git("rev-parse", "refs/heads/published")
        first_link = os.readlink(self.runtime / "public" / "report")

        (site / "index.html").write_bytes(b"<!doctype html><h1>B</h1>\n")
        (site / "assets" / "removed.css").unlink()
        (site / "assets" / "added.js").write_bytes(b"document.body.dataset.ready='yes'\n")

        planned = self.run_cli(
            "plan",
            "--name",
            "report",
            "--source",
            str(site),
            "--target",
            self.base_url,
            "--expected-revision",
            first_revision,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        planned_payload = self.payload(planned)
        self.assertEqual(planned_payload["prediction"], "update")
        self.assertEqual(planned_payload["expected_revision"], first_revision)
        self.assertEqual(planned_payload["differences"]["added"], ["assets/added.js"])
        self.assertEqual(planned_payload["differences"]["changed"], ["index.html"])
        self.assertEqual(planned_payload["differences"]["deleted"], ["assets/removed.css"])
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), first_commit)
        self.assertEqual(os.readlink(self.runtime / "public" / "report"), first_link)

        updated = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(site),
            "--target",
            self.base_url,
            "--expected-revision",
            first_revision,
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        updated_payload = self.payload(updated)
        self.assertEqual(updated_payload["outcome"], "published")
        self.assertEqual(updated_payload["url"], stable_url)
        self.assertNotEqual(updated_payload["active_revision"], first_revision)
        self.assertEqual(updated_payload["active_revision"], updated_payload["requested_revision"])
        self.assertEqual(updated_payload["verification"]["result"], "passed")
        self.assertEqual(
            updated_payload["effects"],
            {"archive_advanced": True, "activated": True},
        )
        tree_paths = self.git("ls-tree", "-r", "--name-only", "refs/heads/published").splitlines()
        self.assertEqual(
            tree_paths,
            ["other/site/index.html", "report/site/assets/added.js", "report/site/index.html"],
        )
        self.assertEqual(os.readlink(self.runtime / "public" / "other"), other_link)
        self.assertEqual(
            other_payload["active_revision"],
            self.payload(self.run_cli("status", "--name", "other"))["active_revision"],
        )
        self.assertEqual(
            (self.runtime / "public" / "other" / "index.html").read_bytes(), other.read_bytes()
        )
        with urllib.request.urlopen(stable_url, timeout=2) as response:
            self.assertEqual(response.read(), b"<!doctype html><h1>B</h1>\n")
        with urllib.request.urlopen(f"{stable_url}assets/added.js", timeout=2) as response:
            self.assertEqual(response.read(), b"document.body.dataset.ready='yes'\n")
        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{stable_url}assets/removed.css", timeout=2)
        self.assertEqual(missing.exception.code, 404)

        updated_commit = self.git("rev-parse", "refs/heads/published")
        retry_plan = self.run_cli(
            "plan",
            "--name",
            "report",
            "--source",
            str(site),
            "--target",
            self.base_url,
            "--expected-revision",
            first_revision,
        )
        self.assertEqual(retry_plan.returncode, 0, retry_plan.stderr)
        self.assertEqual(self.payload(retry_plan)["prediction"], "unchanged")
        retry = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(site),
            "--target",
            self.base_url,
            "--expected-revision",
            first_revision,
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.payload(retry)["outcome"], "unchanged")
        self.assertEqual(
            self.payload(retry)["effects"],
            {"archive_advanced": False, "activated": False},
        )
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), updated_commit)

    def test_stale_expected_revision_conflicts_without_mutation(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
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
        first_revision = self.payload(first)["active_revision"]

        source.write_bytes(b"<!doctype html><h1>B</h1>\n")
        second = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            first_revision,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        second_revision = self.payload(second)["active_revision"]
        second_commit = self.git("rev-parse", "refs/heads/published")

        source.write_bytes(b"<!doctype html><h1>C</h1>\n")
        plan = self.run_cli(
            "plan",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            first_revision,
        )
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(self.payload(plan)["prediction"], "conflict")

        conflict = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            first_revision,
        )
        self.assertEqual(conflict.returncode, 1)
        conflict_payload = self.payload(conflict)
        self.assertEqual(conflict_payload["error"]["code"], "revision_conflict")
        self.assertEqual(
            conflict_payload["error"]["next_action"]["required_inputs"],
            ["expected_revision", "active_revision", "requested_revision"],
        )
        self.assertEqual(conflict_payload["active_revision"], second_revision)
        self.assertEqual(
            conflict_payload["effects"],
            {"archive_advanced": False, "activated": False},
        )
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), second_commit)
        self.assertEqual(
            (self.runtime / "public" / "report" / "index.html").read_bytes(),
            b"<!doctype html><h1>B</h1>\n",
        )

    def test_two_updates_from_one_revision_yield_one_publish_and_one_conflict(self) -> None:
        original = self.root / "original.html"
        original.write_bytes(b"<!doctype html><h1>A</h1>\n")
        first = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(original),
            "--target",
            self.base_url,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        first_revision = self.payload(first)["active_revision"]

        candidate_b = self.root / "candidate-b.html"
        candidate_c = self.root / "candidate-c.html"
        candidate_b.write_bytes(b"<!doctype html><h1>B</h1>\n")
        candidate_c.write_bytes(b"<!doctype html><h1>C</h1>\n")

        def command(source: Path) -> list[str]:
            return [
                sys.executable,
                "-m",
                "html_publish",
                "--config",
                str(self.config),
                "--json",
                "publish",
                "--name",
                "report",
                "--source",
                str(source),
                "--target",
                self.base_url,
                "--expected-revision",
                first_revision,
            ]

        processes = [
            subprocess.Popen(
                command(source),
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for source in (candidate_b, candidate_c)
        ]
        results = [process.communicate(timeout=10) for process in processes]
        payloads = [json.loads(stdout) for stdout, _stderr in results]

        self.assertEqual(sorted(process.returncode for process in processes), [0, 1])
        self.assertEqual(sorted(payload["outcome"] for payload in payloads), ["error", "published"])
        conflict_payload = next(payload for payload in payloads if payload["outcome"] == "error")
        winner_payload = next(payload for payload in payloads if payload["outcome"] == "published")
        self.assertEqual(conflict_payload["error"]["code"], "revision_conflict")
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "2")
        self.assertEqual(
            self.payload(self.run_cli("status", "--name", "report"))["active_revision"],
            winner_payload["requested_revision"],
        )
        winner_body = (
            b"<!doctype html><h1>B</h1>\n"
            if winner_payload["requested_revision"] == payloads[0]["requested_revision"]
            else b"<!doctype html><h1>C</h1>\n"
        )
        self.assertEqual(
            (self.runtime / "public" / "report" / "index.html").read_bytes(),
            winner_body,
        )

    def test_saved_update_is_reused_when_activation_has_not_happened(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
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
        first_revision = self.payload(first)["active_revision"]

        source.write_bytes(b"<!doctype html><h1>B</h1>\n")
        plan = self.run_cli(
            "plan",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            first_revision,
        )
        self.assertEqual(plan.returncode, 0, plan.stderr)
        requested_revision = self.payload(plan)["requested_revision"]
        invalid_release = self.runtime / "releases" / requested_revision
        invalid_release.write_text("not a release", encoding="utf-8")

        failed = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            first_revision,
        )
        self.assertEqual(failed.returncode, 1)
        failed_payload = self.payload(failed)
        self.assertEqual(failed_payload["error"]["code"], "export_corruption")
        self.assertEqual(
            failed_payload["effects"],
            {"archive_advanced": True, "activated": False},
        )
        self.assertEqual(failed_payload["archived_revision"], requested_revision)
        self.assertEqual(failed_payload["active_revision"], first_revision)
        saved_commit = self.git("rev-parse", "refs/heads/published")
        invalid_release.unlink()

        recovered = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            first_revision,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        recovered_payload = self.payload(recovered)
        self.assertEqual(recovered_payload["outcome"], "published")
        self.assertEqual(
            recovered_payload["effects"],
            {"archive_advanced": False, "activated": True},
        )
        self.assertEqual(recovered_payload["active_revision"], requested_revision)
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), saved_commit)

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

    def test_plan_accepts_existing_empty_bare_archive_without_writing(self) -> None:
        subprocess.run(
            ["git", "init", "--bare", str(self.archive)],
            capture_output=True,
            text=True,
            check=True,
        )
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
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)"), "")
        self.assertIn("count: 0", self.git("count-objects", "-v").splitlines())
        self.assertFalse(self.runtime.exists())

    def test_total_deadline_interrupts_a_trickling_http_body(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(SlowHandler.body)
        initial_config = self.config_payload()
        initial_config["limits"]["command_seconds"] = 120
        self.config.write_text(json.dumps(initial_config), encoding="utf-8")
        published = self.run_cli(
            "publish", "--name", "report", "--source", str(source), "--target", self.base_url
        )
        self.assertEqual(published.returncode, 0, published.stdout)
        active_revision = self.payload(published)["active_revision"]

        self._stop_server()
        SlowHandler.request_started.clear()
        slow_server = QuietServer(self.server.server_address, SlowHandler)
        slow_thread = threading.Thread(target=slow_server.serve_forever, daemon=True)
        slow_thread.start()
        initial_config["limits"]["command_seconds"] = 10
        initial_config["limits"]["verification_seconds"] = 10
        self.config.write_text(json.dumps(initial_config), encoding="utf-8")
        started = time.monotonic()
        try:
            result = self.run_cli("verify", "--name", "report")
        finally:
            slow_server.shutdown()
            slow_server.server_close()
            slow_thread.join(timeout=2)
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 1)
        self.assertTrue(SlowHandler.request_started.is_set())
        response = self.payload(result)
        self.assertEqual(response["error"]["code"], "command_timeout")
        self.assertEqual(response["verification"]["result"], "failed")
        self.assertEqual(response["active_revision"], active_revision)
        self.assertEqual(response["effects"], {"archive_advanced": False, "activated": False})
        self.assertLess(elapsed, 20)

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
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["outcome"], "error")
        self.assertEqual(payload["error"]["code"], "invalid_usage")
        self.assertEqual(result.stderr, "")

    def test_usage_error_route_comes_from_command_not_argument_values(self) -> None:
        for name in ("doctor", "config", "status"):
            with self.subTest(name=name):
                result = self.run_cli("publish", "--name", name)
                self.assertEqual(result.returncode, 2)
                payload = self.payload(result)
                self.assertEqual(payload["operation"], "publish")
                self.assertEqual(payload["error"]["code"], "invalid_usage")
                self.assertIn("verification", payload)
                self.assertEqual(result.stderr, "")
        for config in ("doctor", "config", "status"):
            with self.subTest(config=config):
                result = self.run_cli("--config", config, "publish", "--name", "report")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.payload(result)["operation"], "publish")
        for operation in ("doctor", "config"):
            with self.subTest(operation=operation):
                result = self.run_cli(operation)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.payload(result)["operation"], operation)

    def test_empty_status_has_explicit_entries_and_common_envelope(self) -> None:
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.payload(result),
            {
                "schema_version": 1,
                "operation": "status",
                "request_id": None,
                "outcome": "observed",
                "target": self.base_url,
                "name": None,
                "url": None,
                "expected_revision": None,
                "requested_revision": None,
                "archived_revision": None,
                "archive_commit": None,
                "active_revision": None,
                "effects": {"archive_advanced": False, "activated": False},
                "verification": {
                    "result": "not_checked",
                    "revision": None,
                    "checked_at": None,
                    "probe_location": None,
                    "files_checked": 0,
                    "bytes_checked": 0,
                    "scope": [],
                    "detail": None,
                },
                "warnings": [],
                "error": None,
                "observation": None,
                "entries": [],
                "total": 0,
                "truncated": False,
                "continuation": None,
                "staging": None,
            },
        )

    def test_config_failure_preserves_mutation_identity(self) -> None:
        self.config.write_text("{}")
        result = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            "unused.html",
            "--target",
            self.base_url,
            "--request-id",
            "attempt-1",
            "--expected-revision",
            "rev-a",
        )
        self.assertEqual(result.returncode, 1)
        payload = self.payload(result)
        self.assertEqual(payload["request_id"], "attempt-1")
        self.assertEqual(payload["expected_revision"], "rev-a")
        self.assertEqual(payload["url"], self.base_url + "report/")
        self.assertEqual(payload["effects"], {"archive_advanced": False, "activated": False})

    def test_archive_modes_release_modes_and_parentless_first_commit(self) -> None:
        source = self.root / "site"
        source.mkdir()
        for filename, mode in (("index.html", 0o755), ("private.css", 0o600), ("script.js", 0o744)):
            path = source / filename
            path.write_text(
                "<!doctype html><p>normalized</p>" if filename == "index.html" else "body {}"
            )
            path.chmod(mode)
        result = self.run_cli(
            "publish", "--name", "report", "--source", str(source), "--target", self.base_url
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        commit = payload["archive_commit"]
        self.assertEqual(self.git("rev-list", "--parents", "-n1", commit).split(), [commit])
        tree = self.git("ls-tree", "-r", commit).splitlines()
        self.assertEqual([line.split()[0] for line in tree], ["100644"] * 3)
        self.assertEqual(
            [line.split("\t")[1] for line in tree],
            ["report/site/index.html", "report/site/private.css", "report/site/script.js"],
        )
        release = self.runtime / "releases" / payload["active_revision"]
        self.assertEqual(
            {path.name: path.stat().st_mode & 0o777 for path in release.iterdir()},
            {"index.html": 0o644, "private.css": 0o644, "script.js": 0o644},
        )

    def test_plan_keeps_all_paths_at_default_capture_limit(self) -> None:
        config = self.config_payload()
        config["limits"]["command_seconds"] = 120
        self.config.write_text(json.dumps(config), encoding="utf-8")
        source = self.root / "large-site"
        source.mkdir()
        (source / "index.html").write_text("<!doctype html><p>large</p>")
        for index in range(1999):
            (source / f"asset-{index:04}.txt").write_text("asset")
        result = self.run_cli(
            "plan", "--name", "report", "--source", str(source), "--target", self.base_url
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = self.payload(result)
        self.assertEqual(payload["file_count"], 2000)
        self.assertEqual(
            payload["differences"]["added"],
            [*(f"asset-{index:04}.txt" for index in range(1999)), "index.html"],
        )
        self.assertEqual(payload["differences"]["changed"], [])
        self.assertEqual(payload["differences"]["deleted"], [])

    def test_plan_reports_configured_capture_limits(self) -> None:
        payload = self.config_payload()
        payload["limits"]["max_bytes"] = 2_000
        payload["limits"]["max_files"] = 7
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>limits</h1>\n")

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
        self.assertEqual(
            self.payload(result)["limits"],
            {"max_bytes": 2_000, "max_files": 7},
        )

    def test_git_older_than_the_supported_minimum_is_rejected(self) -> None:
        stub = self.root / "stub-bin"
        stub.mkdir()
        (stub / "git").write_text("#!/bin/sh\necho 'git version 2.35.0'\n", encoding="utf-8")
        (stub / "git").chmod(0o755)

        result = self.run_cli("status", env={"PATH": f"{stub}:{os.environ['PATH']}"})

        self.assertEqual(result.returncode, 1)
        payload = self.payload(result)
        self.assertEqual(payload["error"]["code"], "git_unsupported")
        self.assertEqual(payload["error"]["phase"], "version")
        self.assertIn("2.36", payload["error"]["message"])

    def test_git_at_the_supported_minimum_can_publish(self) -> None:
        real_git = shutil.which("git")
        assert real_git is not None
        stub = self.root / "stub-bin"
        stub.mkdir()
        (stub / "git").write_text(
            "#!/bin/sh\n"
            'for argument in "$@"; do\n'
            '  if [ "$argument" = "--version" ]; then\n'
            "    echo 'git version 2.36.0'\n"
            "    exit 0\n"
            "  fi\n"
            "done\n"
            f'exec {shlex.quote(real_git)} "$@"\n',
            encoding="utf-8",
        )
        (stub / "git").chmod(0o755)
        source = self.root / "report.html"
        body = b"<!doctype html><h1>minimum git</h1>\n"
        source.write_bytes(body)

        result = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            env={"PATH": f"{stub}:{os.environ['PATH']}"},
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.payload(result)["outcome"], "published")
        with urllib.request.urlopen(self.payload(result)["url"], timeout=2) as response:
            self.assertEqual(response.read(), body)

    def test_git_preflight_uses_the_total_command_deadline(self) -> None:
        real_git = shutil.which("git")
        assert real_git is not None
        stub = self.root / "stub-bin"
        stub.mkdir()
        (stub / "git").write_text(
            "#!/bin/sh\n"
            'for argument in "$@"; do\n'
            '  if [ "$argument" = "--version" ]; then\n'
            "    sleep 0.45\n"
            "  else\n"
            "    continue\n"
            "  fi\n"
            f'  exec {shlex.quote(real_git)} "$@"\n'
            "done\n"
            "sleep 0.10\n"
            f'exec {shlex.quote(real_git)} "$@"\n',
            encoding="utf-8",
        )
        (stub / "git").chmod(0o755)
        payload = self.config_payload()
        payload["limits"]["command_seconds"] = 0.6
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>deadline</h1>\n")

        started = time.monotonic()
        result = self.run_cli(
            "plan",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            env={"PATH": f"{stub}:{os.environ['PATH']}"},
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            self.payload(result)["error"]["code"],
            {"command_timeout", "git_timeout"},
        )
        self.assertLess(elapsed, 1.2)

    def test_a_missing_git_executable_is_reported_as_unavailable(self) -> None:
        result = self.run_cli("status", env={"PATH": "/nonexistent-html-publish-bin"})

        self.assertEqual(result.returncode, 1)
        payload = self.payload(result)
        self.assertEqual(payload["error"]["code"], "git_unavailable")
        self.assertEqual(payload["error"]["phase"], "version")

    def test_capture_rejects_unsafe_inputs_without_persistent_state(self) -> None:
        index = b"<!doctype html><h1>unsafe</h1>\n"
        cases: tuple[tuple[str, Path], ...] = ()

        dotfile = self.root / "case-dotfile"
        dotfile.mkdir()
        (dotfile / "index.html").write_bytes(index)
        (dotfile / ".hidden.css").write_bytes(b"body{}\n")
        cases += (("dot-prefixed component", dotfile),)

        backslash = self.root / "case-backslash"
        backslash.mkdir()
        (backslash / "index.html").write_bytes(index)
        (backslash / "bad\\name.html").write_bytes(index)
        cases += (("backslash component", backslash),)

        control = self.root / "case-control"
        control.mkdir()
        (control / "index.html").write_bytes(index)
        (control / "bad\nline.html").write_bytes(index)
        cases += (("control character", control),)

        delete = self.root / "case-delete"
        delete.mkdir()
        (delete / "index.html").write_bytes(index)
        (delete / "bad\x7fname.html").write_bytes(index)
        cases += (("delete character", delete),)

        surrogates = self.root / "case-encoding"
        surrogates.mkdir()
        (surrogates / "index.html").write_bytes(index)
        try:
            descriptor = os.open(
                surrogates / os.fsdecode(b"bad\xff.html"),
                os.O_CREAT | os.O_WRONLY,
                0o644,
            )
        except OSError as error:
            if error.errno != errno.EILSEQ:
                raise
        else:
            os.close(descriptor)
            cases += (("invalid path encoding", surrogates),)

        special = self.root / "case-special"
        special.mkdir()
        (special / "index.html").write_bytes(index)
        os.mkfifo(special / "pipe")
        cases += (("special file", special),)

        no_index = self.root / "case-no-index"
        no_index.mkdir()
        (no_index / "style.css").write_bytes(b"body{}\n")
        cases += (("missing index", no_index),)

        for label, source in cases:
            with self.subTest(case=label):
                result = self.run_cli(
                    "plan",
                    "--name",
                    "unsafe",
                    "--source",
                    str(source),
                    "--target",
                    self.base_url,
                )
                self.assertEqual(result.returncode, 1)
                expected_code = "missing_index" if label == "missing index" else "unsafe_input"
                self.assertEqual(self.payload(result)["error"]["code"], expected_code)
                self.assertFalse(self.archive.exists())
                self.assertFalse(self.runtime.exists())

    def test_capture_limit_violations_report_which_limit_was_hit(self) -> None:
        site = self.root / "site"
        site.mkdir()
        (site / "index.html").write_bytes(b"<!doctype html><h1>big</h1>\n")
        (site / "style.css").write_bytes(b"body{}\n")

        payload = self.config_payload()
        payload["limits"]["max_files"] = 1
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        files = self.run_cli(
            "plan",
            "--name",
            "limited",
            "--source",
            str(site),
            "--target",
            self.base_url,
        )
        self.assertEqual(files.returncode, 1)
        self.assertEqual(self.payload(files)["error"]["code"], "input_limit")
        self.assertIn("file limit", self.payload(files)["error"]["message"])
        self.assertFalse(self.archive.exists())

        payload = self.config_payload()
        payload["limits"]["max_bytes"] = 10
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        bytes_limit = self.run_cli(
            "plan",
            "--name",
            "limited",
            "--source",
            str(site / "index.html"),
            "--target",
            self.base_url,
        )
        self.assertEqual(bytes_limit.returncode, 1)
        self.assertEqual(self.payload(bytes_limit)["error"]["code"], "input_limit")
        self.assertIn("byte limit", self.payload(bytes_limit)["error"]["message"])
        self.assertFalse(self.archive.exists())

    def test_plan_reports_capture_warnings(self) -> None:
        site = self.root / "site"
        site.mkdir()
        (site / "index.html").write_bytes(
            b'<!doctype html><link href="/root.css"><script src="https://cdn.example/x.js">'
            b"</script><script>navigator.serviceWorker.register('/sw.js')</script>\n"
        )

        result = self.run_cli(
            "plan",
            "--name",
            "warnings",
            "--source",
            str(site),
            "--target",
            self.base_url,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.payload(result)["warnings"],
            ["root_relative_reference", "external_dependency", "service_worker"],
        )

    def test_status_of_an_absent_name_observes_nulls(self) -> None:
        result = self.run_cli("status", "--name", "nothing")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.payload(result)
        self.assertEqual(payload["outcome"], "observed")
        self.assertEqual(payload["archived_revision"], None)
        self.assertEqual(payload["active_revision"], None)
        self.assertEqual(payload["observation"]["saved"], None)
        self.assertEqual(payload["observation"]["selection"]["state"], "absent")
        self.assertEqual(payload["url"], f"{self.base_url}nothing/")

    def test_status_pagination_uses_after_and_limit(self) -> None:
        for name in ("alpha", "beta", "gamma"):
            source = self.root / f"{name}.html"
            source.write_bytes(b"<!doctype html><h1>page</h1>\n")
            published = self.run_cli(
                "publish",
                "--name",
                name,
                "--source",
                str(source),
                "--target",
                self.base_url,
            )
            self.assertEqual(published.returncode, 0, published.stderr)

        first_page = self.run_cli("status", "--limit", "2")
        self.assertEqual(first_page.returncode, 0, first_page.stderr)
        first_payload = self.payload(first_page)
        self.assertEqual(first_payload["total"], 3)
        self.assertEqual([entry["name"] for entry in first_payload["entries"]], ["alpha", "beta"])
        self.assertTrue(first_payload["truncated"])
        self.assertEqual(first_payload["continuation"], "beta")

        second_page = self.run_cli("status", "--after", "beta", "--limit", "2")
        self.assertEqual(second_page.returncode, 0, second_page.stderr)
        second_payload = self.payload(second_page)
        self.assertEqual([entry["name"] for entry in second_payload["entries"]], ["gamma"])
        self.assertFalse(second_payload["truncated"])
        self.assertEqual(second_payload["continuation"], None)

    def test_lock_timeout_conflicts_without_mutation(self) -> None:
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
        first_commit = self.git("rev-parse", "refs/heads/published")
        source.write_bytes(b"<!doctype html><h1>B</h1>\n")
        active_revision = self.payload(published)["active_revision"]

        payload = self.config_payload()
        payload["limits"]["lock_seconds"] = 0.2
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        descriptor = os.open(self.runtime / ".publish.lock", os.O_RDWR)
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            blocked = self.run_cli(
                "publish",
                "--name",
                "report",
                "--source",
                str(source),
                "--target",
                self.base_url,
                "--expected-revision",
                active_revision,
            )
        finally:
            os.close(descriptor)

        self.assertEqual(blocked.returncode, 1)
        blocked_payload = self.payload(blocked)
        self.assertEqual(blocked_payload["error"]["code"], "lock_timeout")
        self.assertEqual(blocked_payload["error"]["next_action"]["kind"], "retry")
        self.assertEqual(
            blocked_payload["effects"], {"archive_advanced": False, "activated": False}
        )
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), first_commit)
        self.assertEqual(
            (self.runtime / "public" / "report" / "index.html").read_bytes(),
            b"<!doctype html><h1>A</h1>\n",
        )

    def test_a_broken_archive_path_reports_archive_failure(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>broken archive</h1>\n")
        self.archive.write_text("not a git repository", encoding="utf-8")

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
        self.assertEqual(payload["error"]["code"], "archive_failure")
        self.assertEqual(payload["error"]["phase"], "archive")
        self.assertEqual(payload["effects"], {"archive_advanced": False, "activated": False})
        self.assertNotIn("Traceback", result.stderr)

    def test_byte_level_release_corruption_is_refused_then_recoverable(self) -> None:
        source = self.root / "report.html"
        body = b"<!doctype html><h1>corrupt</h1>\n"
        source.write_bytes(body)
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
        revision = self.payload(published)["active_revision"]
        release_file = self.runtime / "releases" / revision / "index.html"
        corrupted = body.replace(b"<h1>c", b"<h1>C")
        self.assertEqual(len(corrupted), len(body))
        release_file.write_bytes(corrupted)

        refused = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(refused.returncode, 1)
        refused_payload = self.payload(refused)
        self.assertEqual(refused_payload["error"]["code"], "export_corruption")
        self.assertEqual(
            refused_payload["effects"], {"archive_advanced": False, "activated": False}
        )

        release_file.write_bytes(body)
        recovered = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(self.payload(recovered)["outcome"], "unchanged")

    def test_degraded_selection_blocks_publication_until_repair(self) -> None:
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
        revision = self.payload(published)["active_revision"]
        public_link = self.runtime / "public" / "report"
        public_link.unlink()
        public_link.mkdir()

        blocked = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(blocked.returncode, 1)
        blocked_payload = self.payload(blocked)
        self.assertEqual(blocked_payload["error"]["code"], "state_degraded")
        self.assertEqual(
            blocked_payload["effects"], {"archive_advanced": False, "activated": False}
        )

        observed = self.run_cli("status", "--name", "report")
        self.assertEqual(observed.returncode, 1)
        observed_payload = self.payload(observed)
        self.assertEqual(observed_payload["outcome"], "observed")
        self.assertEqual(observed_payload["error"]["code"], "state_degraded")
        self.assertEqual(observed_payload["observation"]["selection"]["state"], "degraded")
        self.assertEqual(observed_payload["archived_revision"], revision)

        listing = self.run_cli("status")
        self.assertEqual(listing.returncode, 1)
        listing_payload = self.payload(listing)
        self.assertEqual(listing_payload["error"]["code"], "state_degraded")
        self.assertEqual(listing_payload["entries"][0]["name"], "report")
        self.assertEqual(
            listing_payload["entries"][0]["observation"]["selection"]["state"],
            "degraded",
        )

        source.write_bytes(b"<!doctype html><h1>B</h1>\n")
        still_blocked = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            revision,
        )
        self.assertEqual(still_blocked.returncode, 1)
        self.assertEqual(self.payload(still_blocked)["error"]["code"], "state_degraded")

        public_link.rmdir()
        os.symlink(f"../releases/{revision}", public_link)
        repaired = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            revision,
        )
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(self.payload(repaired)["outcome"], "published")

    def test_redirect_outside_the_publication_boundary_is_rejected(self) -> None:
        self._stop_server()
        redirect_server = QuietServer(("127.0.0.1", 0), RedirectHandler)
        address = redirect_server.server_address
        redirect_server.redirect_location = f"http://{address[0]}:{address[1]}/elsewhere/"
        redirect_thread = threading.Thread(target=redirect_server.serve_forever, daemon=True)
        redirect_thread.start()
        payload = self.config_payload()
        payload["base_url"] = f"http://{address[0]}:{address[1]}/"
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>redirected</h1>\n")
        try:
            result = self.run_cli(
                "publish",
                "--name",
                "report",
                "--source",
                str(source),
                "--target",
                payload["base_url"],
            )
        finally:
            redirect_server.shutdown()
            redirect_server.server_close()
            redirect_thread.join(timeout=2)

        self.assertEqual(result.returncode, 1)
        error = self.payload(result)["error"]
        self.assertEqual(error["code"], "delivery_failure")
        self.assertEqual(error["phase"], "verify")
        self.assertEqual(error["next_action"]["kind"], "fix_route")

    def test_a_redirect_loop_exceeds_the_hop_limit(self) -> None:
        self._stop_server()
        redirect_server = QuietServer(("127.0.0.1", 0), RedirectHandler)
        redirect_server.redirect_location = "/report/"
        redirect_thread = threading.Thread(target=redirect_server.serve_forever, daemon=True)
        redirect_thread.start()
        payload = self.config_payload()
        payload["base_url"] = (
            f"http://{redirect_server.server_address[0]}:{redirect_server.server_address[1]}/"
        )
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>looped</h1>\n")
        try:
            result = self.run_cli(
                "publish",
                "--name",
                "report",
                "--source",
                str(source),
                "--target",
                payload["base_url"],
            )
        finally:
            redirect_server.shutdown()
            redirect_server.server_close()
            redirect_thread.join(timeout=2)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.payload(result)["error"]["code"], "delivery_failure")

    def test_a_compressed_body_fails_byte_comparison(self) -> None:
        self._stop_server()
        gzip_server = QuietServer(("127.0.0.1", 0), GzipHandler)
        gzip_thread = threading.Thread(target=gzip_server.serve_forever, daemon=True)
        gzip_thread.start()
        payload = self.config_payload()
        payload["base_url"] = (
            f"http://{gzip_server.server_address[0]}:{gzip_server.server_address[1]}/"
        )
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>gzipped</h1>\n")
        try:
            result = self.run_cli(
                "publish",
                "--name",
                "report",
                "--source",
                str(source),
                "--target",
                payload["base_url"],
            )
        finally:
            gzip_server.shutdown()
            gzip_server.server_close()
            gzip_thread.join(timeout=2)

        self.assertEqual(result.returncode, 1)
        result_payload = self.payload(result)
        self.assertEqual(result_payload["error"]["code"], "delivery_failure")
        self.assertEqual(result_payload["verification"]["result"], "failed")
        self.assertEqual(result_payload["effects"], {"archive_advanced": True, "activated": True})

    def test_same_size_update_changes_the_served_bytes(self) -> None:
        first = self.root / "report.html"
        first.write_bytes(b"<!doctype html><h1>aaaa</h1>\n")
        published = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(first),
            "--target",
            self.base_url,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        active_revision = self.payload(published)["active_revision"]

        second = self.root / "report-b.html"
        second.write_bytes(b"<!doctype html><h1>bbbb</h1>\n")
        updated = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(second),
            "--target",
            self.base_url,
            "--expected-revision",
            active_revision,
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertEqual(self.payload(updated)["outcome"], "published")
        with urllib.request.urlopen(self.payload(updated)["url"], timeout=2) as response:
            self.assertEqual(response.read(), b"<!doctype html><h1>bbbb</h1>\n")

    def test_git_filters_and_excludes_do_not_change_artifact_identity(self) -> None:
        global_attributes = self.root / "global-attributes"
        global_attributes.write_text("*.html filter=hostile-global\n", encoding="utf-8")
        global_excludes = self.root / "global-excludes"
        global_excludes.write_text("global.css\n", encoding="utf-8")
        hostile = self.root / "hostile.gitconfig"
        hostile.write_text(
            f"[core]\n"
            f"\tautocrlf = true\n"
            f"\tattributesFile = {global_attributes}\n"
            f"\texcludesFile = {global_excludes}\n"
            f'[filter "hostile-global"]\n'
            f"\tclean = sed s/original/global-filtered/g\n"
            f"\trequired = true\n"
            f"[commit]\n"
            f"\tgpgSign = true\n",
            encoding="utf-8",
        )
        source = self.root / "site"
        source.mkdir()
        bodies = {
            "index.html": b"<!doctype html><h1>original one</h1>\r\n",
            "global.css": b"global original one\n",
            "repo.css": b"repo original one\n",
        }
        for path, body in bodies.items():
            (source / path).write_bytes(body)

        published = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            env={"GIT_CONFIG_GLOBAL": str(hostile)},
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        first_revision = self.payload(published)["active_revision"]

        repository_attributes = self.archive / "info" / "attributes"
        repository_attributes.write_text("*.css filter=hostile-repository\n", encoding="utf-8")
        repository_excludes = self.archive / "info" / "exclude"
        repository_excludes.write_text("repo.css\n", encoding="utf-8")
        config = self.archive / "config"
        config.write_text(
            config.read_text(encoding="utf-8")
            + '[filter "hostile-repository"]\n'
            + "\tclean = sed s/original/repository-filtered/g\n"
            + "\trequired = true\n",
            encoding="utf-8",
        )
        bodies = {
            "index.html": b"<!doctype html><h1>original two</h1>\r\n",
            "global.css": b"global original two\n",
            "repo.css": b"repo original two\n",
        }
        for path, body in bodies.items():
            (source / path).write_bytes(body)
        republished = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            first_revision,
            env={"GIT_CONFIG_GLOBAL": str(hostile)},
        )
        self.assertEqual(republished.returncode, 0, republished.stderr)
        response = self.payload(republished)
        self.assertEqual(response["outcome"], "published")
        revision = response["active_revision"]
        self.assertEqual(
            self.git("ls-tree", "-r", "--name-only", revision).splitlines(),
            ["global.css", "index.html", "repo.css"],
        )
        for path, body in bodies.items():
            archived = subprocess.run(
                ["git", f"--git-dir={self.archive}", "cat-file", "blob", f"{revision}:{path}"],
                capture_output=True,
                check=True,
            ).stdout
            self.assertEqual(archived, body)
            self.assertEqual((self.runtime / "public" / "report" / path).read_bytes(), body)
            with urllib.request.urlopen(f"{response['url']}{path}", timeout=2) as served:
                self.assertEqual(served.read(), body)

    def test_identical_retry_preserves_known_removed_path_verification(self) -> None:
        stale_enabled = True

        class StaleHandler(QuietHandler):
            def do_GET(self) -> None:
                if stale_enabled and self.path in {"/report/old.txt", "/report/foreign.txt"}:
                    body = b"old asset\n"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                super().do_GET()

        self.server.RequestHandlerClass = functools.partial(
            StaleHandler, directory=str(self.runtime / "public")
        )
        source = self.root / "site"
        source.mkdir()
        (source / "index.html").write_bytes(b"<!doctype html><h1>A</h1>\n")
        (source / "old.txt").write_bytes(b"old asset\n")
        arguments = [
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        ]
        published = self.run_cli(*arguments)
        self.assertEqual(published.returncode, 0, published.stdout)
        revision_a = self.payload(published)["active_revision"]
        (source / "index.html").write_bytes(b"<!doctype html><h1>B</h1>\n")
        (source / "old.txt").unlink()
        retry_arguments = [*arguments, "--expected-revision", revision_a, "--request-id", "retry-b"]
        failed = self.run_cli(*retry_arguments)
        self.assertEqual(failed.returncode, 1, failed.stdout)
        failed_payload = self.payload(failed)
        commit_b = failed_payload["archive_commit"]
        revision_b = failed_payload["requested_revision"]
        self.assertEqual(failed_payload["active_revision"], revision_b)
        self.assertEqual(failed_payload["error"]["code"], "delivery_failure")
        self.assertEqual(failed_payload["error"]["phase"], "verify")
        self.assertEqual(failed_payload["effects"], {"archive_advanced": True, "activated": True})

        retry = self.run_cli(*retry_arguments)
        self.assertEqual(retry.returncode, 1, retry.stdout)
        retry_payload = self.payload(retry)
        self.assertEqual(retry_payload["error"]["code"], "delivery_failure")
        self.assertEqual(retry_payload["error"]["phase"], "verify")
        self.assertEqual(retry_payload["verification"]["result"], "failed")
        self.assertEqual(retry_payload["effects"], {"archive_advanced": False, "activated": False})
        self.assertEqual(retry_payload["archive_commit"], commit_b)
        self.assertEqual(retry_payload["active_revision"], revision_b)
        with urllib.request.urlopen(f"{self.base_url}report/old.txt", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"old asset\n")
        verified = self.run_cli("verify", "--name", "report")
        self.assertEqual(verified.returncode, 0, verified.stdout)
        self.assertNotIn("removed_paths", self.payload(verified)["verification"]["scope"])

        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "index.html").write_bytes(b"<!doctype html><h1>other</h1>\n")
        (foreign / "foreign.txt").write_bytes(b"old asset\n")
        other = self.run_cli(
            "publish", "--name", "other", "--source", str(foreign), "--target", self.base_url
        )
        self.assertEqual(other.returncode, 0, other.stdout)
        foreign_revision = self.payload(other)["active_revision"]
        for expectation in (None, revision_b, "f" * 40, foreign_revision):
            with self.subTest(expectation=expectation):
                guard = [] if expectation is None else ["--expected-revision", expectation]
                unchanged = self.run_cli(*arguments, *guard)
                self.assertEqual(unchanged.returncode, 0, unchanged.stdout)
                payload = self.payload(unchanged)
                self.assertEqual(payload["outcome"], "unchanged")
                self.assertEqual(payload["archive_commit"], commit_b)
                self.assertNotIn("removed_paths", payload["verification"]["scope"])

        stale_enabled = False
        repaired = self.run_cli(*retry_arguments)
        self.assertEqual(repaired.returncode, 0, repaired.stdout)
        repaired_payload = self.payload(repaired)
        self.assertEqual(repaired_payload["outcome"], "unchanged")
        self.assertEqual(repaired_payload["archive_commit"], commit_b)
        self.assertEqual(
            repaired_payload["effects"], {"archive_advanced": False, "activated": False}
        )
        self.assertEqual(repaired_payload["verification"]["result"], "passed")
        self.assertIn("removed_paths", repaired_payload["verification"]["scope"])
        with self.assertRaises(urllib.error.HTTPError) as removed:
            urllib.request.urlopen(f"{self.base_url}report/old.txt", timeout=2)
        self.assertEqual(removed.exception.code, 404)
        history = self.run_cli("history", "--name", "report")
        self.assertEqual(history.returncode, 0, history.stdout)
        self.assertEqual(len(self.payload(history)["entries"]), 2)

    def test_file_and_directory_transitions_appear_in_plan_differences(self) -> None:
        file_site = self.root / "file-site"
        file_site.mkdir()
        (file_site / "index.html").write_bytes(b"<!doctype html><h1>transitions</h1>\n")
        (file_site / "assets").write_bytes(b"asset file\n")
        published = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(file_site),
            "--target",
            self.base_url,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        file_revision = self.payload(published)["active_revision"]

        directory_site = self.root / "directory-site"
        directory_site.mkdir()
        (directory_site / "index.html").write_bytes(b"<!doctype html><h1>transitions</h1>\n")
        (directory_site / "assets").mkdir()
        (directory_site / "assets" / "style.css").write_bytes(b"body{}\n")
        planned = self.run_cli(
            "plan",
            "--name",
            "report",
            "--source",
            str(directory_site),
            "--target",
            self.base_url,
            "--expected-revision",
            file_revision,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(
            self.payload(planned)["differences"],
            {"added": ["assets/style.css"], "changed": [], "deleted": ["assets"]},
        )
        updated = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(directory_site),
            "--target",
            self.base_url,
            "--expected-revision",
            file_revision,
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        directory_revision = self.payload(updated)["active_revision"]
        retry = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(directory_site),
            "--target",
            self.base_url,
            "--expected-revision",
            file_revision,
        )
        self.assertEqual(retry.returncode, 0, retry.stdout)
        self.assertEqual(self.payload(retry)["outcome"], "unchanged")
        self.assertNotIn("removed_paths", self.payload(retry)["verification"]["scope"])
        self.assertEqual(
            self.git("ls-tree", "-r", "--name-only", directory_revision).splitlines(),
            ["assets/style.css", "index.html"],
        )
        with urllib.request.urlopen(
            f"{self.payload(updated)['url']}assets/style.css", timeout=2
        ) as response:
            self.assertEqual(response.read(), b"body{}\n")

        back_to_file = self.run_cli(
            "plan",
            "--name",
            "report",
            "--source",
            str(file_site),
            "--target",
            self.base_url,
            "--expected-revision",
            directory_revision,
        )
        self.assertEqual(back_to_file.returncode, 0, back_to_file.stderr)
        self.assertEqual(
            self.payload(back_to_file)["differences"],
            {"added": ["assets"], "changed": [], "deleted": ["assets/style.css"]},
        )
        restored = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(file_site),
            "--target",
            self.base_url,
            "--expected-revision",
            directory_revision,
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        restored_payload = self.payload(restored)
        self.assertEqual(
            self.git(
                "ls-tree", "-r", "--name-only", restored_payload["active_revision"]
            ).splitlines(),
            ["assets", "index.html"],
        )
        with urllib.request.urlopen(f"{restored_payload['url']}assets", timeout=2) as response:
            self.assertEqual(response.read(), b"asset file\n")
        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{restored_payload['url']}assets/style.css", timeout=2)
        self.assertEqual(missing.exception.code, 404)

    def test_publishing_leaves_the_source_untouched(self) -> None:
        source = self.root / "report.html"
        body = b"<!doctype html><h1>immutable</h1>\n"
        source.write_bytes(body)
        before_mtime = source.stat().st_mtime_ns

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
        self.assertEqual(source.read_bytes(), body)
        self.assertEqual(source.stat().st_mtime_ns, before_mtime)
        self.assertEqual(list(self.root.glob("report*")), [source])

    def test_help_documents_the_commands(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "html_publish", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        for command in ("plan", "publish", "status"):
            self.assertIn(command, result.stdout)
        self.assertIn("--config", result.stdout)

    def test_absent_active_content_follows_the_saved_and_expectation_rules(self) -> None:
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
        head_commit = self.git("rev-parse", "refs/heads/published")
        public_link = self.runtime / "public" / "report"

        source_b = self.root / "report-b.html"
        source_b.write_bytes(b"<!doctype html><h1>B</h1>\n")
        public_link.unlink()

        resumed = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        resumed_payload = self.payload(resumed)
        self.assertEqual(resumed_payload["outcome"], "published")
        self.assertEqual(resumed_payload["effects"], {"archive_advanced": False, "activated": True})
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), head_commit)
        self.assertEqual(
            (public_link / "index.html").read_bytes(),
            b"<!doctype html><h1>A</h1>\n",
        )
        self.assertEqual(len(list((self.runtime / "releases").iterdir())), 1)

        public_link.unlink()
        saved_conflict = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source_b),
            "--target",
            self.base_url,
        )
        self.assertEqual(saved_conflict.returncode, 1)
        saved_payload = self.payload(saved_conflict)
        self.assertEqual(saved_payload["error"]["code"], "revision_conflict")
        self.assertEqual(
            saved_payload["error"]["next_action"]["required_inputs"],
            ["archived_revision", "requested_revision"],
        )
        self.assertEqual(saved_payload["effects"], {"archive_advanced": False, "activated": False})

        expectation_conflict = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source_b),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        )
        self.assertEqual(expectation_conflict.returncode, 1)
        expectation_payload = self.payload(expectation_conflict)
        self.assertEqual(expectation_payload["error"]["code"], "revision_conflict")
        self.assertEqual(
            expectation_payload["error"]["next_action"]["required_inputs"],
            ["active_revision"],
        )
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "1")

    def test_noop_with_a_stale_expectation_reports_the_pending_archive(self) -> None:
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
        planned = self.run_cli(
            "plan",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        revision_b = self.payload(planned)["requested_revision"]
        (self.runtime / "releases" / revision_b).write_text("not a release", encoding="utf-8")
        failed = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        )
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(self.payload(failed)["error"]["code"], "export_corruption")
        pending_commit = self.git("rev-parse", "refs/heads/published")

        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
        noop = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_b,
        )
        self.assertEqual(noop.returncode, 0, noop.stderr)
        noop_payload = self.payload(noop)
        self.assertEqual(noop_payload["outcome"], "unchanged")
        self.assertEqual(noop_payload["effects"], {"archive_advanced": False, "activated": False})
        self.assertEqual(noop_payload["archived_revision"], revision_b)
        self.assertEqual(noop_payload["active_revision"], revision_a)
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), pending_commit)

    def test_content_identity_permits_returning_to_an_earlier_revision(self) -> None:
        stable_url = f"{self.base_url}report/"
        source_a = self.root / "report-a.html"
        source_b = self.root / "report-b.html"
        source_a.write_bytes(b"<!doctype html><h1>A</h1>\n")
        source_b.write_bytes(b"<!doctype html><h1>B</h1>\n")

        published_a = self.run_cli(
            "publish", "--name", "report", "--source", str(source_a), "--target", self.base_url
        )
        self.assertEqual(published_a.returncode, 0, published_a.stderr)
        revision_a = self.payload(published_a)["active_revision"]

        published_b = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source_b),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        )
        self.assertEqual(published_b.returncode, 0, published_b.stderr)
        revision_b = self.payload(published_b)["active_revision"]
        self.assertEqual(self.payload(published_b)["url"], stable_url)

        restored_a = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source_a),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_b,
        )
        self.assertEqual(restored_a.returncode, 0, restored_a.stderr)
        self.assertEqual(self.payload(restored_a)["active_revision"], revision_a)

        republished_b = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source_b),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        )
        self.assertEqual(republished_b.returncode, 0, republished_b.stderr)
        self.assertEqual(self.payload(republished_b)["active_revision"], revision_b)
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "4")
        self.assertEqual(
            (self.runtime / "public" / "report" / "index.html").read_bytes(),
            b"<!doctype html><h1>B</h1>\n",
        )

    def test_conditional_ref_failure_leaves_the_proposed_commit_unreferenced(self) -> None:
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
        original_command = _git.command
        proposed: list[str] = []
        rival: list[str] = []

        def racing_command(
            git_dir: Path | None,
            args: Sequence[str],
            deadline: Deadline,
            **kwargs: Any,
        ) -> bytes:
            if args and args[0] == "update-ref":
                head = (
                    original_command(git_dir, ["rev-parse", "refs/heads/published"], deadline)
                    .decode()
                    .strip()
                )
                tree = (
                    original_command(git_dir, ["rev-parse", f"{head}^{{tree}}"], deadline)
                    .decode()
                    .strip()
                )
                rival_commit = (
                    original_command(
                        git_dir, ["commit-tree", tree, "-p", head, "-m", "rival"], deadline
                    )
                    .decode()
                    .strip()
                )
                original_command(
                    git_dir, ["update-ref", "refs/heads/published", rival_commit, head], deadline
                )
                rival.append(rival_commit)
                proposed.append(str(args[2]))
            return original_command(git_dir, args, deadline, **kwargs)

        report = io.StringIO()
        arguments = [
            "--config",
            str(self.config),
            "--json",
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        ]
        with (
            mock.patch.object(_git, "command", racing_command),
            contextlib.redirect_stdout(report),
        ):
            exit_code = cli.main(arguments)

        self.assertEqual(exit_code, 1)
        payload = json.loads(report.getvalue())
        self.assertEqual(payload["error"]["code"], "archive_failure")
        self.assertEqual(payload["error"]["phase"], "archive")
        self.assertEqual(payload["effects"], {"archive_advanced": False, "activated": False})
        self.assertEqual(payload["archived_revision"], revision_a)
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), rival[0])
        proposed_commit = proposed[0]
        self.assertNotEqual(proposed_commit, rival[0])
        subprocess.run(
            ["git", f"--git-dir={self.archive}", "cat-file", "-e", f"{proposed_commit}^{{commit}}"],
            capture_output=True,
            check=True,
        )
        reachable = self.git("rev-list", "refs/heads/published")
        self.assertNotIn(proposed_commit, reachable.splitlines())
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "2")

    def test_concurrent_identical_publications_converge(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>same</h1>\n")

        def command() -> list[str]:
            return [
                sys.executable,
                "-m",
                "html_publish",
                "--config",
                str(self.config),
                "--json",
                "publish",
                "--name",
                "report",
                "--source",
                str(source),
                "--target",
                self.base_url,
            ]

        processes = [
            subprocess.Popen(
                command(),
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=10) for process in processes]
        payloads = [json.loads(stdout) for stdout, _stderr in results]

        for process in processes:
            self.assertEqual(process.returncode, 0)
        self.assertEqual(
            sorted(payload["outcome"] for payload in payloads),
            ["published", "unchanged"],
        )
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "1")
        self.assertEqual(
            (self.runtime / "public" / "report" / "index.html").read_bytes(),
            b"<!doctype html><h1>same</h1>\n",
        )

    def test_concurrent_publications_of_different_names_both_succeed(self) -> None:
        first = self.root / "alpha.html"
        second = self.root / "beta.html"
        first.write_bytes(b"<!doctype html><h1>alpha</h1>\n")
        second.write_bytes(b"<!doctype html><h1>beta</h1>\n")

        def command(name: str, source: Path) -> list[str]:
            return [
                sys.executable,
                "-m",
                "html_publish",
                "--config",
                str(self.config),
                "--json",
                "publish",
                "--name",
                name,
                "--source",
                str(source),
                "--target",
                self.base_url,
            ]

        processes = [
            subprocess.Popen(
                command(name, source),
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for name, source in (("alpha", first), ("beta", second))
        ]
        results = [process.communicate(timeout=10) for process in processes]
        payloads = [json.loads(stdout) for stdout, _stderr in results]

        for process in processes:
            self.assertEqual(process.returncode, 0)
        self.assertEqual([payload["outcome"] for payload in payloads], ["published", "published"])
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "2")
        self.assertEqual(
            (self.runtime / "public" / "alpha" / "index.html").read_bytes(),
            b"<!doctype html><h1>alpha</h1>\n",
        )
        self.assertEqual(
            (self.runtime / "public" / "beta" / "index.html").read_bytes(),
            b"<!doctype html><h1>beta</h1>\n",
        )

    def test_verify_checks_the_selected_export_without_activating(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>verified</h1>\n")
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
        link_before = os.readlink(self.runtime / "public" / "report")

        verified = self.run_cli("verify", "--name", "report")

        self.assertEqual(verified.returncode, 0, verified.stderr)
        payload = self.payload(verified)
        self.assertEqual(payload["outcome"], "verified")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["verification"]["result"], "passed")
        self.assertEqual(
            payload["verification"]["revision"], self.payload(published)["active_revision"]
        )
        self.assertIn("all_files", payload["verification"]["scope"])
        self.assertNotIn("removed_paths", payload["verification"]["scope"])
        self.assertEqual(payload["effects"], {"archive_advanced": False, "activated": False})
        self.assertEqual(os.readlink(self.runtime / "public" / "report"), link_before)

        empty = self.run_cli("verify", "--name", "nothing")
        self.assertEqual(empty.returncode, 1)
        self.assertEqual(self.payload(empty)["error"]["code"], "nothing_selected")
        self.assertEqual(self.payload(empty)["error"]["next_action"]["kind"], "publish")

    def test_verify_refuses_degraded_and_corrupt_selections(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>degraded</h1>\n")
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
        revision = self.payload(published)["active_revision"]
        public_link = self.runtime / "public" / "report"
        public_link.unlink()
        public_link.mkdir()

        degraded = self.run_cli("verify", "--name", "report")
        self.assertEqual(degraded.returncode, 1)
        self.assertEqual(self.payload(degraded)["error"]["code"], "state_degraded")

        public_link.rmdir()
        os.symlink(f"../releases/{revision}", public_link)
        release_file = self.runtime / "releases" / revision / "index.html"
        release_file.write_bytes(b"<!doctype html><h1>CORRUPTED</h1>\n")
        corrupt = self.run_cli("verify", "--name", "report")
        self.assertEqual(corrupt.returncode, 1)
        self.assertEqual(self.payload(corrupt)["error"]["code"], "export_corruption")

    def test_verify_failures_preserve_observed_state_and_verification_facts(self) -> None:
        source = self.root / "report.html"
        expected = b"<!doctype html><h1>verified</h1>\n"
        source.write_bytes(expected)
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
        published_payload = self.payload(published)
        revision = published_payload["active_revision"]

        self._stop_server()
        failed_delivery = self.run_cli("verify", "--name", "report")

        self.assertEqual(failed_delivery.returncode, 1)
        delivery_payload = self.payload(failed_delivery)
        self.assertEqual(delivery_payload["error"]["code"], "delivery_failure")
        self.assertEqual(delivery_payload["archived_revision"], revision)
        self.assertEqual(delivery_payload["archive_commit"], published_payload["archive_commit"])
        self.assertEqual(delivery_payload["active_revision"], revision)
        self.assertTrue(delivery_payload["observation"]["selection"]["integrity_checked"])
        self.assertEqual(delivery_payload["verification"]["result"], "failed")
        self.assertEqual(delivery_payload["verification"]["revision"], revision)
        self.assertEqual(delivery_payload["verification"]["probe_location"], "host")

        release_file = self.runtime / "releases" / revision / "index.html"
        release_file.write_bytes(b"<!doctype html><h1>tampered</h1>\n")
        corrupt = self.run_cli("verify", "--name", "report")

        self.assertEqual(corrupt.returncode, 1)
        corrupt_payload = self.payload(corrupt)
        self.assertEqual(corrupt_payload["error"]["code"], "export_corruption")
        self.assertEqual(corrupt_payload["archived_revision"], revision)
        self.assertEqual(corrupt_payload["archive_commit"], published_payload["archive_commit"])
        self.assertEqual(corrupt_payload["active_revision"], revision)
        self.assertFalse(corrupt_payload["observation"]["selection"]["integrity_checked"])
        self.assertEqual(corrupt_payload["verification"]["result"], "not_checked")
        self.assertIsNone(corrupt_payload["verification"]["revision"])

    def test_history_lists_changes_and_restore_identifiers(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
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
        revision_a = self.payload(first)["active_revision"]

        site = self.root / "site"
        site.mkdir()
        (site / "index.html").write_bytes(b"<!doctype html><h1>B</h1>\n")
        (site / "assets").mkdir()
        (site / "assets" / "style.css").write_bytes(b"body{}\n")
        second = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(site),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        revision_b = self.payload(second)["active_revision"]

        history = self.run_cli("history", "--name", "report")
        self.assertEqual(history.returncode, 0, history.stderr)
        payload = self.payload(history)
        self.assertEqual(payload["outcome"], "observed")
        self.assertEqual(payload["total"], 2)
        self.assertFalse(payload["truncated"])
        entries = payload["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["archived_revision"], revision_b)
        self.assertEqual(
            entries[0]["changes"],
            {"added": ["assets/style.css"], "changed": ["index.html"], "deleted": []},
        )
        self.assertEqual(entries[1]["archived_revision"], revision_a)
        self.assertEqual(
            entries[1]["changes"], {"added": ["index.html"], "changed": [], "deleted": []}
        )

        diff = self.run_cli("history", "--name", "report", "--diff", revision_a)
        self.assertEqual(diff.returncode, 0, diff.stderr)
        diff_payload = self.payload(diff)
        self.assertEqual(diff_payload["diff"]["from"], revision_a)
        self.assertEqual(diff_payload["diff"]["to"], revision_b)
        self.assertEqual(diff_payload["diff"]["truncated"], False)
        self.assertIn("+<!doctype html><h1>B</h1>", diff_payload["diff"]["text"])

        unknown = self.run_cli("history", "--name", "report", "--diff", "f" * 40)
        self.assertEqual(unknown.returncode, 1)
        self.assertEqual(self.payload(unknown)["error"]["code"], "unreachable_revision")

    def test_history_pagination_binds_the_default_window(self) -> None:
        source = self.root / "report.html"
        active_revision: str | None = None
        for number in range(3):
            source.write_bytes(f"<!doctype html><h1>{number}</h1>\n".encode())
            published = self.run_cli(
                "publish",
                "--name",
                "report",
                "--source",
                str(source),
                "--target",
                self.base_url,
                *(("--expected-revision", active_revision) if active_revision else ()),
            )
            self.assertEqual(published.returncode, 0, published.stderr)
            active_revision = self.payload(published)["active_revision"]

        full = self.run_cli("history", "--name", "report")
        self.assertEqual(full.returncode, 0, full.stderr)
        full_payload = self.payload(full)
        self.assertEqual(full_payload["total"], 3)
        self.assertEqual(len(full_payload["entries"]), 3)

        window = self.run_cli("history", "--name", "report", "--limit", "2")
        self.assertEqual(window.returncode, 0, window.stderr)
        window_payload = self.payload(window)
        self.assertEqual(len(window_payload["entries"]), 2)
        self.assertTrue(window_payload["truncated"])
        self.assertEqual(
            window_payload["continuation"],
            window_payload["entries"][-1]["archive_commit"],
        )

        deeper = self.run_cli(
            "history",
            "--name",
            "report",
            "--limit",
            "2",
            "--after",
            window_payload["continuation"],
        )
        self.assertEqual(deeper.returncode, 0, deeper.stderr)
        deeper_payload = self.payload(deeper)
        self.assertEqual(len(deeper_payload["entries"]), 1)
        self.assertEqual(deeper_payload["total"], 1)
        self.assertEqual(deeper_payload["entries"], full_payload["entries"][2:])
        self.assertFalse(deeper_payload["truncated"])

    def test_history_diff_text_is_utf8_capped_after_replacement(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
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
        revision_a = self.payload(first)["active_revision"]
        source.write_bytes(b'<meta charset="iso-8859-1"><h1>\n' + b"\xe9" * 70_000 + b"\n</h1>")
        second = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        )
        self.assertEqual(second.returncode, 0, second.stderr)

        diff = self.run_cli("history", "--name", "report", "--diff", revision_a)
        self.assertEqual(diff.returncode, 0, diff.stderr)
        diff_payload = self.payload(diff)
        self.assertTrue(diff_payload["diff"]["truncated"])
        self.assertLessEqual(len(diff_payload["diff"]["text"].encode("utf-8")), 64 * 1024)
        self.assertIn("\ufffd" * 50, diff_payload["diff"]["text"])

    def test_restore_selects_an_earlier_revision_and_appends_history(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
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
        revision_a = self.payload(first)["active_revision"]
        first_commit = self.payload(first)["archive_commit"]

        source_b = self.root / "report-b.html"
        source_b.write_bytes(b"<!doctype html><h1>B</h1>\n")
        second = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source_b),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        revision_b = self.payload(second)["active_revision"]

        restored = self.run_cli(
            "restore",
            "--name",
            "report",
            "--archive-commit",
            first_commit,
            "--target",
            self.base_url,
            "--expected-revision",
            revision_b,
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        restored_payload = self.payload(restored)
        self.assertEqual(restored_payload["outcome"], "published")
        self.assertEqual(restored_payload["operation"], "restore")
        self.assertEqual(restored_payload["active_revision"], revision_a)
        self.assertEqual(restored_payload["url"], self.payload(first)["url"])
        self.assertEqual(restored_payload["verification"]["result"], "passed")
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "3")
        self.assertEqual(
            (self.runtime / "public" / "report" / "index.html").read_bytes(),
            b"<!doctype html><h1>A</h1>\n",
        )
        history = self.run_cli("history", "--name", "report")
        entries = self.payload(history)["entries"]
        self.assertEqual(entries[0]["archived_revision"], revision_a)
        self.assertEqual(
            entries[0]["changes"],
            {"added": [], "changed": ["index.html"], "deleted": []},
        )

        again = self.run_cli(
            "restore",
            "--name",
            "report",
            "--archive-commit",
            first_commit,
            "--target",
            self.base_url,
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.payload(again)["outcome"], "unchanged")

        plain = self.run_cli(
            "restore",
            "--name",
            "report",
            "--archive-commit",
            first_commit,
            "--target",
            self.base_url,
            json_output=False,
        )
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stdout, f"{self.base_url}report/\n")

    def test_restore_rejects_unreachable_and_wrong_page_selections(self) -> None:
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
        report_commit = self.payload(published)["archive_commit"]

        other = self.root / "other.html"
        other.write_bytes(b"<!doctype html><h1>other</h1>\n")
        other_published = self.run_cli(
            "publish",
            "--name",
            "other",
            "--source",
            str(other),
            "--target",
            self.base_url,
        )
        self.assertEqual(other_published.returncode, 0, other_published.stderr)
        other_commit = self.payload(other_published)["archive_commit"]

        wrong_page = self.run_cli(
            "restore",
            "--name",
            "report",
            "--archive-commit",
            other_commit,
            "--target",
            self.base_url,
        )
        self.assertEqual(wrong_page.returncode, 1)
        self.assertEqual(self.payload(wrong_page)["error"]["code"], "unreachable_revision")
        self.assertEqual(
            self.payload(wrong_page)["error"]["next_action"]["kind"],
            "inspect",
        )

        unknown = self.run_cli(
            "restore",
            "--name",
            "report",
            "--archive-commit",
            "0" * 40,
            "--target",
            self.base_url,
        )
        self.assertEqual(unknown.returncode, 1)
        self.assertEqual(self.payload(unknown)["error"]["code"], "unreachable_revision")
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "2")
        del report_commit

    def test_restore_completes_a_pending_saved_archive_without_a_new_commit(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
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
        revision_a = self.payload(first)["active_revision"]

        source_b = self.root / "report-b.html"
        source_b.write_bytes(b"<!doctype html><h1>B</h1>\n")
        planned = self.run_cli(
            "plan",
            "--name",
            "report",
            "--source",
            str(source_b),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        revision_b = self.payload(planned)["requested_revision"]
        (self.runtime / "releases" / revision_b).write_text("not a release", encoding="utf-8")
        failed = self.run_cli(
            "publish",
            "--name",
            "report",
            "--source",
            str(source_b),
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        )
        self.assertEqual(failed.returncode, 1)
        saved_commit = self.git("rev-parse", "refs/heads/published")
        (self.runtime / "releases" / revision_b).unlink()

        restored = self.run_cli(
            "restore",
            "--name",
            "report",
            "--archive-commit",
            saved_commit,
            "--target",
            self.base_url,
            "--expected-revision",
            revision_a,
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        restored_payload = self.payload(restored)
        self.assertEqual(restored_payload["outcome"], "published")
        self.assertEqual(
            restored_payload["effects"], {"archive_advanced": False, "activated": True}
        )
        self.assertEqual(restored_payload["active_revision"], revision_b)
        self.assertEqual(self.git("rev-parse", "refs/heads/published"), saved_commit)

    def test_host_check_reports_dns_route_and_drift_without_repair(self) -> None:
        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>host</h1>\n")
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
        active_revision = self.payload(published)["active_revision"]

        checked = self.run_cli("status", "--name", "report", "--host-check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        payload = self.payload(checked)
        self.assertEqual(payload["outcome"], "observed")
        self.assertEqual(payload["verification"]["result"], "passed")
        self.assertEqual(payload["verification"]["revision"], active_revision)
        self.assertEqual(payload["host_checks"]["route"], "ok")
        self.assertTrue(payload["host_checks"]["dns_resolved"])

        release_file = self.runtime / "releases" / active_revision / "index.html"
        corrupted_bytes = b"<!doctype html><h1>evil</h1>\n"
        release_file.write_bytes(corrupted_bytes)
        with urllib.request.urlopen(payload["url"], timeout=2) as response:
            self.assertEqual(response.read(), corrupted_bytes)

        corrupted = self.run_cli("status", "--name", "report", "--host-check")

        self.assertEqual(corrupted.returncode, 1, corrupted.stderr)
        corrupted_payload = self.payload(corrupted)
        self.assertEqual(corrupted_payload["error"]["code"], "export_corruption")
        self.assertEqual(corrupted_payload["verification"]["result"], "failed")
        self.assertEqual(corrupted_payload["verification"]["revision"], active_revision)
        self.assertEqual(corrupted_payload["verification"]["probe_location"], "local")
        self.assertEqual(corrupted_payload["host_checks"]["route"], "not_checked")
        self.assertFalse(corrupted_payload["observation"]["selection"]["integrity_checked"])
        release_file.write_bytes(source.read_bytes())

        unnamed = self.run_cli("status", "--host-check")
        self.assertEqual(unnamed.returncode, 0, unnamed.stderr)
        unnamed_payload = self.payload(unnamed)
        self.assertIn("http_status", unnamed_payload["host_checks"])
        self.assertEqual(unnamed_payload["host_checks"]["route"], "ok")

        self._stop_server()
        drifted = self.run_cli("status", "--name", "report", "--host-check")
        self.assertEqual(drifted.returncode, 0, drifted.stderr)
        drifted_payload = self.payload(drifted)
        self.assertEqual(drifted_payload["outcome"], "observed")
        self.assertEqual(drifted_payload["verification"]["result"], "failed")
        self.assertEqual(drifted_payload["host_checks"]["route"], "drift")
        self.assertEqual(
            (self.runtime / "public" / "report" / "index.html").read_bytes(),
            b"<!doctype html><h1>host</h1>\n",
        )

        absent = self.run_cli("status", "--name", "nothing", "--host-check")
        self.assertEqual(absent.returncode, 0, absent.stderr)
        absent_payload = self.payload(absent)
        self.assertEqual(absent_payload["host_checks"]["route"], "not_checked")
        self.assertEqual(absent_payload["verification"]["result"], "not_checked")


if __name__ == "__main__":
    unittest.main()
