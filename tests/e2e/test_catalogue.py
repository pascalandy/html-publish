"""Prove the private catalogue is reconstructed from publication history.

The test uses real CLI processes, temporary Git and runtime directories, and loopback HTTP only.
"""

from __future__ import annotations

import errno
import http.client
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGUE_PROJECTION_TIMEOUT = 22


class PublicationCatalogueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="html-publish-catalogue-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "archive.git"
        self.runtime = self.root / "runtime"
        self.config = self.root / "publisher.json"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}/"
        self._init_publisher(self.config, self.base_url)
        self.server: subprocess.Popen[str] | None = None
        self.last_server_stderr = ""
        self.catalogue_response_headers: dict[str, str] = {}
        self.addCleanup(self._stop_server)

    def _init_publisher(self, config: Path, base_url: str) -> None:
        self._cli(
            "config",
            "init",
            "--role",
            "publisher",
            "--config",
            str(config),
            "--archive",
            str(self.archive),
            "--runtime",
            str(self.runtime),
            "--base-url",
            base_url,
            "--allow-http",
        )

    def _cli(self, *arguments: str) -> dict[str, object]:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "html_publish",
                "--config",
                str(self.config),
                "--json",
                *arguments,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"CLI failed: {result.stderr}\n{result.stdout}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def _publish(self, name: str, source: Path, expected: str | None = None) -> dict[str, object]:
        arguments = [
            "publish",
            "--name",
            name,
            "--source",
            str(source),
            "--target",
            self.base_url,
        ]
        if expected is not None:
            arguments.extend(("--expected-revision", expected))
        return self._cli(*arguments)

    def _page(self, title: str, metadata: Mapping[str, object] | None = None) -> str:
        meta = ""
        if metadata is not None:
            value = json.dumps(metadata, separators=(",", ":"))
            meta = f"<meta name=\"html-publish-catalogue\" content='{value}'>"
        return (
            f"<!doctype html><html><head><title>{title}</title>{meta}</head>"
            f"<body>{title}</body></html>\n"
        )

    def _start_server(self, *, configured: bool = True) -> None:
        environment = os.environ.copy()
        if configured:
            environment["HTML_PUBLISH_PUBLISHER_CONFIG"] = str(self.config)
        else:
            environment.pop("HTML_PUBLISH_PUBLISHER_CONFIG", None)
        self._launch_server(
            [
                sys.executable,
                "-m",
                "html_publish.server",
                "--directory",
                str(self.runtime / "public"),
                "--bind",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            environment,
            "127.0.0.1",
            self.port,
        )

    def _launch_server(
        self, argv: list[str], environment: dict[str, str], host: str, port: int
    ) -> None:
        self.server = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                stdout, stderr = self.server.communicate()
                self.last_server_stderr = stderr
                self.server = None
                self.fail(f"server exited early: {stderr}\n{stdout}")
            try:
                with socket.create_connection((host, port), timeout=0.25):
                    return
            except OSError:
                time.sleep(0.05)
        self.fail("server did not answer the synthetic publication")

    def _stop_server(self) -> None:
        server = self.server
        if server is None:
            return
        if server.poll() is None:
            server.terminate()
        try:
            _, self.last_server_stderr = server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            _, self.last_server_stderr = server.communicate(timeout=5)
        finally:
            self.server = None

    def _catalogue(self, query: dict[str, str] | None = None) -> tuple[int, str]:
        suffix = "?" + urlencode(query) if query else ""
        try:
            with urlopen(
                f"{self.base_url}_catalogue/{suffix}",
                timeout=CATALOGUE_PROJECTION_TIMEOUT,
            ) as response:
                self.catalogue_response_headers = dict(response.headers.items())
                return response.status, response.read().decode("utf-8")
        except HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def test_ipv6_loopback_serves_page_and_catalogue_from_both_commands(self) -> None:
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
                listener.bind(("::1", 0))
                port = int(listener.getsockname()[1])
        except OSError as error:
            if error.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL}:
                self.skipTest(f"IPv6 loopback is unavailable: {error}")
            raise

        fixture = self._page("IPv6 loopback fixture").encode("utf-8")
        source = self.root / "ipv6.html"
        source.write_bytes(fixture)
        self._start_server()
        self._publish("ipv6", source)
        self._stop_server()

        config = self.root / "publisher-ipv6.json"
        self._init_publisher(config, f"http://[::1]:{port}/")
        for module, arguments, environment in (
            (
                "html_publish.server",
                ["--directory", str(self.runtime / "public")],
                {**os.environ, "HTML_PUBLISH_PUBLISHER_CONFIG": str(config)},
            ),
            ("html_publish", ["--config", str(config), "host", "serve"], os.environ.copy()),
        ):
            with self.subTest(module=module):
                try:
                    self._launch_server(
                        [
                            sys.executable,
                            "-m",
                            module,
                            *arguments,
                            "--bind",
                            "::1",
                            "--port",
                            str(port),
                        ],
                        environment,
                        "::1",
                        port,
                    )
                    page_connection = http.client.HTTPConnection("::1", port, timeout=2)
                    try:
                        page_connection.request("GET", "/ipv6/")
                        page = page_connection.getresponse()
                        self.assertEqual((page.status, page.read()), (200, fixture))
                    finally:
                        page_connection.close()

                    catalogue_connection = http.client.HTTPConnection(
                        "::1", port, timeout=CATALOGUE_PROJECTION_TIMEOUT
                    )
                    try:
                        catalogue_connection.request("GET", "/_catalogue/")
                        catalogue = catalogue_connection.getresponse()
                        self.assertEqual(catalogue.status, 200)
                        self.assertEqual(catalogue.read().count(b'data-publication="ipv6"'), 1)
                    finally:
                        catalogue_connection.close()
                finally:
                    self._stop_server()

    def _start_with_git_shim(self, script: str) -> None:
        git = shutil.which("git")
        assert git is not None
        shim = self.root / "bin"
        shim.mkdir()
        wrapper = shim / "git"
        wrapper.write_text("#!/bin/sh\nset -eu\n" + script + f'exec {shlex.quote(git)} "$@"\n')
        wrapper.chmod(0o755)
        self._launch_server(
            [
                sys.executable,
                "-m",
                "html_publish",
                "--config",
                str(self.config),
                "host",
                "serve",
                "--port",
                str(self.port),
            ],
            {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"},
            "127.0.0.1",
            self.port,
        )

    def test_cold_catalogue_shares_history_walks_across_current_pages(self) -> None:
        self._start_server()
        page = self.root / "page.html"
        for index in range(4):
            page.write_text(self._page(f"Current page {index}"), encoding="utf-8")
            self._publish(f"page-{index}", page)
        self._stop_server()

        # Count the costly history walks independently of runner speed.
        calls = self.root / "history-walks"
        calls.touch()
        self._start_with_git_shim(
            'for arg do case "$arg" in log|rev-list) '
            f"echo walk >> {shlex.quote(str(calls))} ;; esac; done\n"
        )
        status, body = self._catalogue()
        self.assertEqual(status, 200)
        self.assertEqual(body.count("data-publication="), 4)
        self.assertLessEqual(len(calls.read_text().splitlines()), 2)

    def test_catalogue_reads_archive_after_concurrent_activation(self) -> None:
        self._start_server()
        page = self.root / "page.html"
        page.write_text(self._page("First selection"), encoding="utf-8")
        initial = self._publish("concurrent", page)
        self._stop_server()

        # After the reader receives the original head, let the real publisher
        # commit and activate its update before the reader observes the public link.
        page.write_text(self._page("Second selection"), encoding="utf-8")
        git = shutil.which("git")
        assert git is not None
        publish = [
            sys.executable,
            "-m",
            "html_publish",
            "--config",
            str(self.config),
            "publish",
            "--name",
            "concurrent",
            "--source",
            str(page),
            "--target",
            self.base_url,
            "--expected-revision",
            str(initial["active_revision"]),
        ]
        triggered = shlex.quote(str(self.root / "triggered"))
        self._start_with_git_shim(
            "for arg do\n"
            f'  if [ "$arg" = "rev-parse" ] && [ ! -e {triggered} ]; then\n'
            f"    touch {triggered}\n"
            f'    {shlex.quote(git)} "$@"\n'
            f"    PATH={shlex.quote(os.environ['PATH'])} {shlex.join(publish)} >/dev/null\n"
            "    exit 0\n"
            "  fi\n"
            "done\n"
        )
        status, body = self._catalogue()
        self.assertEqual(status, 200)
        self.assertIn("Second selection", body)
        self.assertIn(f'href="{self.base_url}concurrent/"', body)

    def test_catalogue_tracks_update_restore_and_archived_inventory(self) -> None:
        self._start_server()
        legacy = self.root / "legacy.html"
        legacy.write_text(
            "<!doctype html><head><meta name><title>Legacy page</title></head>"
            "<body>Legacy fixture.</body>",
            encoding="utf-8",
        )
        self._publish("legacy", legacy)

        implicit = self.root / "implicit.html"
        implicit.write_text(
            """<title>Implicit title</title><meta name="html-publish-catalogue"
content='{"title":"Ignored metadata title","type":"report"}'>
<body>Implicit fixture.</body>""",
            encoding="utf-8",
        )
        self._publish("implicit", implicit)

        safe_values = self.root / "safe-values.html"
        safe_values.write_text(
            "<!doctype html><head><title>Safe metadata fallback</title>"
            '<meta name="html-publish-catalogue" content=\''
            '{"title":"\\ud800","type":"prototype",'
            '"topics":["bad\\u0001topic"],"projects":["Safe"],'
            '"source_url":"https://github.com:99999/owner/repo/blob/main/file.md"}\'>'
            "</head><body>Safe values fixture.</body>",
            encoding="utf-8",
        )
        self._publish("safe-values", safe_values)

        unsafe_urls = {
            "unsafe-auth": "https://user@github.com/owner/repo/blob/main/file.md",
            "unsafe-query": "https://github.com/owner/repo/blob/main/file.md?view=1",
            "unsafe-fragment": "https://github.com/owner/repo/blob/main/file.md#part",
            "unsafe-space": "https://github.com/owner/repo/blob/main/a file.md",
            "unsafe-backslash": "https://github.com/owner/repo/blob/main/a\\file.md",
            "unsafe-trailing": "https://github.com/owner/repo/blob/main/docs/",
            "unsafe-path": "https://github.com/owner/repo/blob/main/",
            "unsafe-host": "https://bad..host/owner/repo/blob/main/file.md",
        }
        for name, source_url in unsafe_urls.items():
            unsafe_source = self.root / f"{name}.html"
            unsafe_source.write_text(
                self._page(
                    name,
                    {
                        "title": name,
                        "type": "report",
                        "source_url": source_url,
                    },
                ),
                encoding="utf-8",
            )
            self._publish(name, unsafe_source)

        gitlab = self.root / "gitlab.html"
        gitlab_pointer = "https://gitlab.com/group/project/-/tree/0123abc/docs"
        gitlab.write_text(
            self._page("GitLab pointer", {"source_url": gitlab_pointer}), encoding="utf-8"
        )
        self._publish("gitlab", gitlab)

        deep_json = self.root / "deep-json.html"
        nested = "[" * 1200 + "0" + "]" * 1200
        deep_json.write_text(
            "<!doctype html><head><title>Deep JSON fallback</title>"
            f"<meta name=\"html-publish-catalogue\" content='{nested}'>"
            "</head><body>Deep JSON fixture.</body>",
            encoding="utf-8",
        )
        self._publish("deep-json", deep_json)

        markdown_source = self.root / "large-markdown"
        asset_dir = markdown_source / ("a" * 180) / ("b" * 180) / ("c" * 180)
        asset_dir.mkdir(parents=True)
        (markdown_source / "index.md").write_text(
            "---\n"
            'html-publish-catalogue: {"title":"Large provenance","type":"report",'
            '"projects":["Synthetic"],'
            '"source_url":"https://github.com/example/project/blob/main/docs/large.md"}\n'
            "---\n# Synthetic Markdown page\n",
            encoding="utf-8",
        )
        for index in range(100):
            (asset_dir / f"asset-{index:04d}.bin").write_bytes(b"")
        markdown_result = self._cli(
            "publish",
            "--name",
            "large-provenance",
            "--source",
            str(markdown_source),
            "--target",
            self.base_url,
            "--format",
            "markdown",
            "--entry",
            "index.md",
        )
        self.assertEqual(markdown_result["outcome"], "published")

        prefix_page = self.root / "prefix.html"
        prefix_head = (
            b"<!doctype html><head><title>Prefix title</title><meta "
            b'name="html-publish-catalogue" content=\''
            b'{"title":"Prefix retained","type":"report",'
            b'"source_url":"https://github.com/example/project/blob/main/docs/prefix.md"}'
            b"'></head><body>"
        )
        prefix_page.write_bytes(
            prefix_head + b"x" * (65535 - len(prefix_head)) + b"\xc3\xa9</body>"
        )
        self._publish("prefix-page", prefix_page)

        source = self.root / "demo.html"
        metadata_a = {
            "title": "Budget A",
            "type": "prototype",
            "topics": ["Budget", "Planning"],
            "projects": ["Atlas", "Orion"],
            "source_url": "https://github.com/example/project/blob/main/docs/demo.md",
        }
        source.write_text(self._page("Budget A", metadata_a), encoding="utf-8")
        published_a = self._publish("demo", source)
        retry_a = self._publish("demo", source)
        self.assertEqual(retry_a["outcome"], "unchanged")

        metadata_b = {
            **metadata_a,
            "title": "Budget B",
            "source_url": "https://github.com/example/project/blob/main/docs/updated.md",
            "topics": ["Budget", "Forecast"],
            "projects": ["Atlas", "Mixtape"],
        }
        source.write_text(self._page("Budget B", metadata_b), encoding="utf-8")
        published_b = self._publish("demo", source, str(published_a["active_revision"]))

        history = self._cli("history", "--name", "demo")
        entries_value = history.get("entries")
        self.assertIsInstance(entries_value, list)
        entries = cast(list[dict[str, object]], entries_value)
        revision_a = published_a.get("active_revision")
        self.assertIsInstance(revision_a, str)
        commit_a: str | None = None
        for history_entry in entries:
            if history_entry.get("archived_revision") == revision_a:
                archive_commit = history_entry.get("archive_commit")
                if isinstance(archive_commit, str):
                    commit_a = archive_commit
                    break
        self.assertIsNotNone(commit_a)
        assert commit_a is not None
        self._cli(
            "restore",
            "--name",
            "demo",
            "--archive-commit",
            commit_a,
            "--target",
            self.base_url,
            "--expected-revision",
            str(published_b["active_revision"]),
        )

        publication_lock = self.runtime / ".publish.lock"
        self.assertEqual(publication_lock.parent, self.runtime)
        publication_lock.unlink()
        status, body = self._catalogue()
        self.assertEqual(status, 200)
        self.assertIn("Budget A", body)
        self.assertIn("Legacy page", body)
        self.assertIn("Implicit title", body)
        self.assertNotIn("Ignored metadata title", body)
        self.assertIn("Safe metadata fallback", body)
        self.assertIn("Deep JSON fallback", body)
        self.assertIn("Large provenance", body)
        self.assertIn("https://github.com/example/project/blob/main/docs/large.md", body)
        self.assertIn("Prefix retained", body)
        self.assertNotIn("https://github.com:99999/owner/repo/blob/main/file.md", body)
        for invalid_url in unsafe_urls.values():
            self.assertNotIn(invalid_url, body)
        self.assertIn(gitlab_pointer, body)
        self.assertIn("Planning", body)
        self.assertIn("Orion", body)
        self.assertIn("https://github.com/example/project/blob/main/docs/demo.md", body)
        self.assertEqual(body.count('data-publication="demo"'), 1)
        self.assertIn("Sujets", body)
        self.assertIn("Projets", body)
        self.assertIn("Ouvrir la page", body)
        self.assertIn("Reprendre la source", body)
        self.assertRegex(body, r'<time datetime="\d{4}-\d{2}-\d{2}T[^\"]+Z">')
        self.assertNotIn("<ul", body)
        self.assertNotIn("<li", body)
        self.assertNotRegex(body, r"<nav>\s*</nav>")
        self.assertIn("overflow-wrap:anywhere", body)
        self.assertIn("width:100%", body)
        self.assertIn("minmax(min(100%,12rem)", body)
        self.assertIn("Rechercher", body)
        headers = self.catalogue_response_headers
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("noindex", headers["X-Robots-Tag"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("script-src", headers["Content-Security-Policy"])

        invalid_query = f"{self.base_url}_catalogue/?unknown=1"
        for method, expected_body in (("GET", b"Requete invalide.\n"), ("HEAD", b"")):
            request = Request(invalid_query, method=method)
            try:
                with urlopen(request, timeout=2) as response:
                    status = response.status
                    response_body = response.read()
            except HTTPError as error:
                status = error.code
                response_body = error.read()
            self.assertEqual(status, 400)
            self.assertEqual(response_body, expected_body)

        filtered_status, filtered = self._catalogue(
            {
                "q": "Atlas",
                "type": "prototype",
                "topic": "Planning",
                "project": "Atlas",
            }
        )
        self.assertEqual(filtered_status, 200)
        self.assertIn("Budget A", filtered)
        for href in re.findall(r'href="([^"]*)"', body):
            self.assertTrue(
                not href
                or href.startswith((self.base_url, "https://github.com/", "https://gitlab.com/")),
                href,
            )
        self.assertNotIn("<script", body.lower())
        self.assertNotIn("<link", body.lower())

        # Activation can lag behind an archive commit. An earlier selection must
        # display its own metadata, even though the archived head restored A.
        demo_link = self.runtime / "public" / "demo"
        demo_link.unlink()
        demo_link.symlink_to(Path("../releases") / str(published_b["active_revision"]))
        selected_status, selected = self._catalogue({"project": "Mixtape"})
        self.assertEqual(selected_status, 200)
        self.assertIn("Budget B", selected)
        self.assertNotIn("Budget A", selected)
        self.assertIn(f'href="{self.base_url}demo/"', selected)
        demo_link.unlink()
        cyclic_selection = self.runtime / "public" / "legacy"
        self.assertTrue(cyclic_selection.is_symlink())
        cyclic_selection.unlink()
        cyclic_selection.symlink_to("legacy")
        archived_status, archived = self._catalogue()
        self.assertEqual(archived_status, 200)
        self.assertIn("Archivée", archived)
        self.assertNotIn(f'href="{self.base_url}demo/"', archived)
        self.assertIn('data-state="unavailable"', archived)
        self.assertIn("Indisponible", archived)
        self.assertNotIn(f'href="{self.base_url}legacy/"', archived)
        state_status, state_filtered = self._catalogue({"state": "archived"})
        self.assertEqual(state_status, 200)
        self.assertIn('data-publication="demo"', state_filtered)
        self.assertFalse(publication_lock.exists())

        self._cli(
            "restore",
            "--name",
            "demo",
            "--archive-commit",
            commit_a,
            "--target",
            self.base_url,
        )
        restored_status, restored = self._catalogue()
        self.assertEqual(restored_status, 200)
        self.assertIn("Budget A", restored)
        self.assertIn(f'href="{self.base_url}demo/"', restored)

    def _assert_managed_config_failure_keeps_stable_page(
        self, name: str, config_contents: str, untouched_paths: tuple[Path, ...] = ()
    ) -> None:
        self._start_server()
        source = self.root / f"{name}.html"
        fixture = f"<!doctype html><title>{name}</title><p>Known stable bytes for {name}.</p>\n"
        source.write_text(fixture, encoding="utf-8")
        published = self._publish(name, source)
        self.assertEqual(published["outcome"], "published")
        self.assertIsInstance(published.get("active_revision"), str)
        with urlopen(f"{self.base_url}{name}/", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), fixture.encode("utf-8"))
        self._stop_server()

        self.config.write_text(config_contents, encoding="utf-8")
        self._start_server()
        try:
            self.assertIsNotNone(self.server)
            assert self.server is not None
            self.assertIsNone(self.server.poll())
            with urlopen(f"{self.base_url}{name}/", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), fixture.encode("utf-8"))
            self.assertEqual(self._catalogue()[0], 503)
            self.assertIsNone(self.server.poll())
            for path in untouched_paths:
                self.assertFalse(path.exists(), f"unexpected managed-config access: {path.name}")
        finally:
            self._stop_server()
        self.assertEqual(
            self.last_server_stderr.count("managed catalogue config unavailable or mismatched"), 1
        )
        self.assertNotIn(str(self.config), self.last_server_stderr)

    def test_invalid_managed_config_keeps_existing_stable_page(self) -> None:
        valid_config = json.loads(self.config.read_text(encoding="utf-8"))
        invalid_configs = (
            ("malformed-managed-config", "{ not valid JSON\n"),
            (
                "unknown-field-managed-config",
                json.dumps({**valid_config, "unknown": "synthetic"}),
            ),
        )
        for name, contents in invalid_configs:
            with self.subTest(config=name):
                self.config.write_text(json.dumps(valid_config), encoding="utf-8")
                self._assert_managed_config_failure_keeps_stable_page(name, contents)

    def test_mismatched_managed_runtime_keeps_existing_stable_page(self) -> None:
        other_archive = self.root / "other-archive.git"
        other_runtime = self.root / "other-runtime"
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["archive"] = str(other_archive)
        config["runtime"] = str(other_runtime)
        self._assert_managed_config_failure_keeps_stable_page(
            "mismatched-managed-runtime",
            json.dumps(config),
            (other_archive, other_runtime),
        )

    def test_unconfigured_standalone_server_keeps_file_only_behavior(self) -> None:
        self._start_server(configured=False)
        self.assertEqual(self._catalogue()[0], 404)


if __name__ == "__main__":
    unittest.main()
