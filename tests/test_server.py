from __future__ import annotations

import email.utils
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STARTUP_SECONDS = 15


class PublicationServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="html-publish-server-test-")
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name) / "runtime"
        self.public = self.runtime / "public"
        self.releases = self.runtime / "releases"
        self.public.mkdir(parents=True)
        self.releases.mkdir()
        self.port = self._unused_port()
        self.server_log_path = Path(self.temporary.name) / "server.log"
        self.server_log = self.server_log_path.open("w", encoding="utf-8")
        self.addCleanup(self.server_log.close)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "html_publish.server",
                "--directory",
                str(self.public),
                "--bind",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=ROOT,
            stdout=self.server_log,
            stderr=subprocess.STDOUT,
        )
        self.addCleanup(self._stop_server)
        self._wait_until_ready()

    def _stop_server(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)

    def _unused_port(self) -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail(
                    f"server exited during startup with {self.process.returncode}: "
                    f"{self.server_log_path.read_text(encoding='utf-8')[-2000:]}"
                )
            try:
                status, _, _ = self.request("/")
            except OSError:
                time.sleep(0.02)
                continue
            self.assertEqual(status, 404)
            return
        self.fail(
            f"server did not start within {STARTUP_SECONDS} seconds; "
            f"server log: {self.server_log_path.read_text(encoding='utf-8')[-2000:]}"
        )

    def request(
        self, path: str, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        return self.request_at(self.port, path, headers)

    def request_at(
        self, port: int, path: str, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def select(self, release: str) -> None:
        staged = self.runtime / f"selection-{release}"
        os.symlink(f"../releases/{release}", staged)
        os.replace(staged, self.public / "report")

    def test_health_endpoint_is_available_without_a_publication(self) -> None:
        status, headers, body = self.request("/_html-publish-health")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body, b"ok\n")

    def test_future_public_directory_can_appear_after_server_start(self) -> None:
        future_runtime = Path(self.temporary.name) / "future-runtime"
        future_public = future_runtime / "public"
        future_port = self._unused_port()
        future_log_path = Path(self.temporary.name) / "future-server.log"
        future_log = future_log_path.open("w", encoding="utf-8")
        self.addCleanup(future_log.close)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "html_publish.server",
                "--directory",
                str(future_public),
                "--port",
                str(future_port),
            ],
            cwd=ROOT,
            stdout=future_log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + STARTUP_SECONDS
            while True:
                if process.poll() is not None:
                    self.fail(
                        f"future-root server exited with {process.returncode}: "
                        f"{future_log_path.read_text(encoding='utf-8')[-2000:]}"
                    )
                try:
                    health_status, _, _ = self.request_at(future_port, "/_html-publish-health")
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        self.fail(
                            f"future-root server did not start within {STARTUP_SECONDS} seconds; "
                            f"server log: {future_log_path.read_text(encoding='utf-8')[-2000:]}"
                        )
                    time.sleep(0.02)
            self.assertEqual(health_status, 200)
            self.assertEqual(self.request_at(future_port, "/report/")[0], 404)

            release = future_runtime / "releases" / "revision-a"
            release.mkdir(parents=True)
            future_public.mkdir()
            (release / "index.html").write_bytes(b"available later")
            os.symlink("../releases/revision-a", future_public / "report")

            status, headers, body = self.request_at(future_port, "/report/")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(body, b"available later")
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def test_same_size_same_mtime_update_ignores_old_conditional_request(self) -> None:
        timestamp = 1_700_000_000
        release_a = self.releases / "revision-a"
        release_b = self.releases / "revision-b"
        release_a.mkdir()
        release_b.mkdir()
        page_a = release_a / "index.html"
        page_b = release_b / "index.html"
        page_a.write_bytes(b"page A")
        page_b.write_bytes(b"page B")
        os.utime(page_a, (timestamp, timestamp))
        os.utime(page_b, (timestamp, timestamp))
        self.select("revision-a")

        first_status, first_headers, first_body = self.request("/report/")
        self.assertEqual(first_status, 200)
        self.assertEqual(first_headers["Cache-Control"], "no-store")
        self.assertEqual(first_body, b"page A")

        self.select("revision-b")
        second_status, second_headers, second_body = self.request(
            "/report/",
            {"If-Modified-Since": email.utils.formatdate(timestamp, usegmt=True)},
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_headers["Cache-Control"], "no-store")
        self.assertEqual(second_body, b"page B")

    def test_redirect_and_errors_disable_caching_and_directory_listing(self) -> None:
        release = self.releases / "revision-a"
        release.mkdir()
        (release / "index.html").write_bytes(b"published")
        (release / "assets").mkdir()
        self.select("revision-a")

        redirect_status, redirect_headers, _ = self.request("/report?view=full")
        self.assertEqual(redirect_status, 301)
        self.assertEqual(redirect_headers["Location"], "report/?view=full")
        self.assertEqual(redirect_headers["Cache-Control"], "no-store")
        external_url = "https://om1.example.ts.net/html-publish/report?view=full"
        self.assertEqual(
            urllib.parse.urljoin(external_url, redirect_headers["Location"]),
            "https://om1.example.ts.net/html-publish/report/?view=full",
        )

        nested_status, nested_headers, _ = self.request("/report/assets")
        self.assertEqual(nested_status, 301)
        self.assertEqual(nested_headers["Location"], "assets/")
        self.assertEqual(
            urllib.parse.urljoin(
                "https://om1.example.ts.net/html-publish/report/assets",
                nested_headers["Location"],
            ),
            "https://om1.example.ts.net/html-publish/report/assets/",
        )

        directory_status, directory_headers, _ = self.request("/report/assets/")
        self.assertEqual(directory_status, 404)
        self.assertEqual(directory_headers["Cache-Control"], "no-store")

        missing_status, missing_headers, _ = self.request("/missing.html")
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing_headers["Cache-Control"], "no-store")

    def test_mime_types_are_preserved_and_uncontrolled_symlinks_are_hidden(self) -> None:
        release = self.releases / "revision-a"
        release.mkdir()
        (release / "index.html").write_bytes(b"published")
        (release / "site.css").write_bytes(b"body{}")
        self.select("revision-a")
        outside = Path(self.temporary.name) / "secret.txt"
        outside.write_bytes(b"secret")
        os.symlink(outside, self.public / "secret.txt")

        css_status, css_headers, css_body = self.request("/report/site.css")
        self.assertEqual(css_status, 200)
        self.assertEqual(css_headers["Content-type"], "text/css")
        self.assertEqual(css_body, b"body{}")

        secret_status, secret_headers, _ = self.request("/secret.txt")
        self.assertEqual(secret_status, 404)
        self.assertEqual(secret_headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
