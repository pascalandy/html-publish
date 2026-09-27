"""Publisher behavior when a process dies, a durable write fails, or Git misbehaves.

Faults come from tests/isolated/_fault.py, Git shims on PATH, and patched store calls.

Failure modes:
F1: store: a publisher killed before the archive ref advances leaves state its retry cannot recover
F2: store: a first publication killed before the ref advances leaves a half-created page
F3: store: a retry after a kill past the ref advance creates a second archive commit
F4: store: a retry after a crash refuses to reuse a validated release already on disk
F5: store: a kill after selection hides that the page is active but unverified
F6: store: a directory fsync failure after selection is reported as activation_failure
F7: store: a failed public symlink replacement is reported as export_failure
F8: store: an export write failure deletes the partial stage before reporting its byte count
F9: store: a failed release rename deletes the complete stage needed for inspection and retry
F10: artifact: capture publishes a source whose metadata changed while it was copied
F11: git: a Git child that outlives the command deadline is not reaped
F12: git: a missing executable is reported as an archive failure instead of git_unavailable
F13: store: an external ref move is overwritten and the losing commit becomes selected
F14: git: a version older than 2.36 is accepted and the command proceeds
F15: cli: Git version preflight time is refunded, so the command exceeds its total budget
"""

from __future__ import annotations

import contextlib
import errno
import functools
import http.server
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from html_publish import _git, cli, store
from html_publish.model import Deadline

ROOT = Path(__file__).resolve().parents[2]
FAULT_SCRIPT = ROOT / "tests" / "isolated" / "_fault.py"


class RecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="html-publish-recovery-")
        self.root = Path(self.temporary.name).resolve()
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
        self, *arguments: str, json_output: bool = True, env: dict[str, str] | None = None
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

    def config_payload(self) -> dict[str, Any]:
        return json.loads(self.config.read_text(encoding="utf-8"))

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
        """Proves F1."""

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
        """Proves F2."""

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
        """Proves F3."""

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
        """Proves F4."""

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
        """Proves F5."""

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
        """Proves F6."""

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
        """Proves F7."""

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

    def test_export_write_failure_retains_private_bytes_and_reuses_saved_commit(self) -> None:
        """Proves F8."""

        source = self.root / "report.html"
        body_a = b"<!doctype html><h1>A</h1>\n"
        body_b = b"<!doctype html><h1>B</h1>\n"
        source.write_bytes(body_a)
        source.chmod(0o755)
        published = self.run_cli(*self.publish_arguments(source))
        self.assertEqual(published.returncode, 0, published.stdout)
        revision_a = self.payload(published)["active_revision"]
        selected_a = (self.runtime / "public" / "report").readlink()
        source.write_bytes(body_b)
        arguments = [*self.publish_arguments(source, revision_a), "--request-id", "write-b"]
        partial = body_b[:12]

        def failing_export(
            git_dir: Path, object_id: str, destination: Path, deadline: Deadline
        ) -> None:
            self.assertTrue(destination.is_relative_to(self.runtime / "staging"))
            with destination.open("xb") as output:
                output.write(partial)
                output.flush()
                raise OSError(errno.ENOSPC, "No space left on device", str(destination))

        report = io.StringIO()
        with (
            mock.patch.object(_git, "export_blob", failing_export),
            contextlib.redirect_stdout(report),
        ):
            exit_code = cli.main(["--config", str(self.config), "--json", *arguments])
        self.assertEqual(exit_code, 1)
        failed = json.loads(report.getvalue())
        revision_b = failed["requested_revision"]
        saved_commit = failed["archive_commit"]
        self.assertEqual(failed["error"]["code"], "export_failure")
        self.assertEqual(failed["error"]["phase"], "export")
        self.assertIn("No space left on device", failed["error"]["message"])
        self.assertIn(f"({len(partial)} bytes)", failed["error"]["message"])
        self.assertEqual(failed["effects"], {"archive_advanced": True, "activated": False})
        self.assertEqual(failed["archived_revision"], revision_b)
        self.assertEqual(failed["active_revision"], revision_a)
        self.assertEqual(failed["verification"]["result"], "not_checked")
        self.assertEqual((self.runtime / "public" / "report").readlink(), selected_a)
        self.assertEqual([path.name for path in (self.runtime / "public").iterdir()], ["report"])
        stages = list((self.runtime / "staging").iterdir())
        self.assertEqual(len(stages), 1)
        self.assertEqual(stat.S_IMODE(stages[0].stat().st_mode), 0o700)
        self.assertEqual((stages[0] / "index.html").read_bytes(), partial)
        self.assertFalse((self.runtime / "releases" / revision_b).exists())
        with urllib.request.urlopen(f"{self.base_url}report/", timeout=2) as response:
            self.assertEqual(response.read(), body_a)
        status = self.run_cli("status", "--name", "report")
        self.assertEqual(status.returncode, 0, status.stdout)
        self.assertEqual(
            self.payload(status)["observation"]["saved"]["archive_commit"], saved_commit
        )
        self.assertEqual(self.payload(status)["observation"]["selection"]["revision"], revision_a)

        retry = self.run_cli(*arguments)
        self.assertEqual(retry.returncode, 0, retry.stdout)
        recovered = self.payload(retry)
        self.assertEqual(recovered["outcome"], "published")
        self.assertEqual(recovered["archive_commit"], saved_commit)
        self.assertEqual(recovered["active_revision"], revision_b)
        self.assertEqual(recovered["effects"], {"archive_advanced": False, "activated": True})
        self.assertEqual(recovered["verification"]["result"], "passed")
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/published"), "2")
        self.assertEqual((stages[0] / "index.html").read_bytes(), partial)
        release_file = self.runtime / "releases" / revision_b / "index.html"
        self.assertEqual(stat.S_IMODE(release_file.stat().st_mode), 0o644)
        self.assertEqual(release_file.stat().st_nlink, 1)
        self.assertNotEqual(
            (source.stat().st_dev, source.stat().st_ino),
            (release_file.stat().st_dev, release_file.stat().st_ino),
        )
        self.assertTrue(self.git("ls-tree", revision_b).startswith("100644 blob "))
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o755)
        with urllib.request.urlopen(f"{self.base_url}report/", timeout=2) as response:
            self.assertEqual(response.read(), body_b)

    def test_rename_failure_reports_export_failure_with_stage_usage(self) -> None:
        """Proves F9."""

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

    def test_source_change_during_capture_fails_without_persistent_state(self) -> None:
        """Proves F10."""

        source = self.root / "report.html"
        source.write_bytes(b"<!doctype html><h1>A</h1>\n")
        original_fstat = os.fstat
        calls = {"count": 0}

        def changing_fstat(fd: int) -> Any:
            result = original_fstat(fd)
            calls["count"] += 1
            if calls["count"] > 1:
                return types.SimpleNamespace(
                    st_mode=result.st_mode,
                    st_dev=result.st_dev,
                    st_ino=result.st_ino,
                    st_size=result.st_size + 1,
                    st_mtime_ns=result.st_mtime_ns + 1_000_000,
                )
            return result

        report = io.StringIO()
        with (
            mock.patch("os.fstat", changing_fstat),
            contextlib.redirect_stdout(report),
        ):
            exit_code = cli.main(
                ["--config", str(self.config), "--json", *self.publish_arguments(source)]
            )

        self.assertEqual(exit_code, 1)
        payload = json.loads(report.getvalue())
        self.assertEqual(payload["error"]["code"], "source_changed")
        self.assertEqual(payload["error"]["phase"], "capture")
        self.assertEqual(payload["error"]["next_action"]["kind"], "retry")
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.runtime.exists())

    def test_git_child_timeout_is_reaped(self) -> None:
        """Proves F11."""

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

    def test_a_missing_git_executable_is_reported_as_unavailable(self) -> None:
        """Proves F12."""

        result = self.run_cli("status", env={"PATH": "/nonexistent-html-publish-bin"})

        self.assertEqual(result.returncode, 1)
        payload = self.payload(result)
        self.assertEqual(payload["error"]["code"], "git_unavailable")
        self.assertEqual(payload["error"]["phase"], "version")

    def test_conditional_ref_failure_leaves_the_proposed_commit_unreferenced(self) -> None:
        """Proves F13."""

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

    def test_git_older_than_the_supported_minimum_is_rejected(self) -> None:
        """Proves F14."""

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

    def test_git_preflight_uses_the_total_command_deadline(self) -> None:
        """Proves F15."""

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


if __name__ == "__main__":
    unittest.main()
