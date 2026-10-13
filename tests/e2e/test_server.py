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

ROOT = Path(__file__).resolve().parents[2]
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
        self, path: str, headers: dict[str, str] | None = None, *, method: str = "GET"
    ) -> tuple[int, dict[str, str], bytes]:
        return self.request_at(self.port, path, headers, method=method)

    def request_at(
        self,
        port: int,
        path: str,
        headers: dict[str, str] | None = None,
        *,
        method: str = "GET",
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def assert_head_ignores_range(self, path: str, value: str, size: int) -> None:
        status, headers, body = self.request(path, {"Range": value}, method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], str(size))
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertNotIn("Content-Range", headers)
        self.assertEqual(body, b"")

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

    def test_future_public_directory_can_appear_without_reverse_dns(self) -> None:
        future_runtime = Path(self.temporary.name) / "future-runtime"
        future_public = future_runtime / "public"
        future_port = self._unused_port()
        no_reverse_dns = Path(self.temporary.name) / "no-reverse-dns"
        no_reverse_dns.mkdir()
        (no_reverse_dns / "sitecustomize.py").write_text(
            "import socket\n"
            "def reject(_host):\n"
            "    raise RuntimeError('reverse DNS attempted')\n"
            "socket.getfqdn = reject\n",
            encoding="utf-8",
        )
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
            env={**os.environ, "PYTHONPATH": str(no_reverse_dns)},
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

    def test_single_byte_range_returns_partial_content_to_get_only(self) -> None:
        audio = bytes(range(256)) * 4
        release = self.releases / "revision-a"
        release.mkdir()
        (release / "index.html").write_bytes(b"published")
        (release / "audio.mp3").write_bytes(audio)
        self.select("revision-a")

        status, headers, body = self.request("/report/audio.mp3")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(body, audio)

        for value, first, last in (
            ("bytes=0-1", 0, 1),
            ("bytes=100-199", 100, 199),
            ("bytes=1000-", 1000, 1023),
            ("bytes=-24", 1000, 1023),
            ("bytes=1000-99999", 1000, 1023),
            ("bytes=-5000", 0, 1023),
            ("Bytes=5-5", 5, 5),
        ):
            with self.subTest(range=value):
                status, headers, body = self.request("/report/audio.mp3", {"Range": value})
                self.assertEqual(status, 206)
                self.assertEqual(headers["Content-Range"], f"bytes {first}-{last}/1024")
                self.assertEqual(headers["Content-Length"], str(last - first + 1))
                self.assertEqual(headers["Content-type"], "audio/mpeg")
                self.assertEqual(headers["Accept-Ranges"], "bytes")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(body, audio[first : last + 1])
                self.assert_head_ignores_range("/report/audio.mp3", value, len(audio))

        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as connection:
            connection.sendall(b"GET /report/audio.mp3 HTTP/1.0\r\nRange: bytes=100-199\r\n\r\n")
            response = b""
            while chunk := connection.recv(65536):
                response += chunk
        response_head, _, raw_body = response.partition(b"\r\n\r\n")
        self.assertTrue(response_head.startswith(b"HTTP/1.0 206 "))
        self.assertEqual(raw_body, audio[100:200])

    def test_unsatisfiable_byte_range_returns_416_with_the_complete_length(self) -> None:
        release = self.releases / "revision-a"
        release.mkdir()
        (release / "index.html").write_bytes(b"published")
        (release / "audio.mp3").write_bytes(bytes(1024))
        (release / "empty.txt").write_bytes(b"")
        self.select("revision-a")

        for path, value, size in (
            ("/report/audio.mp3", "bytes=1024-", 1024),
            ("/report/audio.mp3", "bytes=5000-6000", 1024),
            ("/report/audio.mp3", "bytes=-0", 1024),
            ("/report/empty.txt", "bytes=0-", 0),
        ):
            with self.subTest(path=path, range=value):
                status, headers, body = self.request(path, {"Range": value})
                self.assertEqual(status, 416)
                self.assertEqual(headers["Content-Range"], f"bytes */{size}")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(body, b"")
                self.assert_head_ignores_range(path, value, size)

    def test_ignored_byte_ranges_return_the_whole_current_file(self) -> None:
        timestamp = 1_700_000_000
        audio_a = bytes(range(256))
        audio_b = bytes(reversed(range(256)))
        for release_name, audio in (("revision-a", audio_a), ("revision-b", audio_b)):
            release = self.releases / release_name
            release.mkdir()
            (release / "index.html").write_bytes(b"published")
            (release / "audio.mp3").write_bytes(audio)
            (release / "empty.txt").write_bytes(b"")
            os.utime(release / "audio.mp3", (timestamp, timestamp))
        self.select("revision-a")
        _, first_headers, _ = self.request("/report/audio.mp3", {"Range": "bytes=0-1"})
        self.select("revision-b")

        for case, path, headers, expected in (
            ("several ranges", "/report/audio.mp3", {"Range": "bytes=0-1,5-6"}, audio_b),
            ("last before first", "/report/audio.mp3", {"Range": "bytes=5-2"}, audio_b),
            ("no positions", "/report/audio.mp3", {"Range": "bytes=-"}, audio_b),
            ("not digits", "/report/audio.mp3", {"Range": "bytes=a-b"}, audio_b),
            ("unknown unit", "/report/audio.mp3", {"Range": "items=0-1"}, audio_b),
            (
                "numeral beyond int parsing",
                "/report/audio.mp3",
                {"Range": f"bytes=0-{'9' * 5000}"},
                audio_b,
            ),
            (
                "If-Range from the previous revision",
                "/report/audio.mp3",
                {"Range": "bytes=0-1", "If-Range": first_headers["Last-Modified"]},
                audio_b,
            ),
            ("suffix of an empty file", "/report/empty.txt", {"Range": "bytes=-5"}, b""),
        ):
            with self.subTest(case=case):
                status, response_headers, body = self.request(path, headers)
                self.assertEqual(status, 200)
                self.assertNotIn("Content-Range", response_headers)
                self.assertEqual(response_headers["Content-Length"], str(len(expected)))
                self.assertEqual(response_headers["Accept-Ranges"], "bytes")
                self.assertEqual(body, expected)

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
