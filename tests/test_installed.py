from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".agents/skills/verify-html-publish/scripts/instance.sh"


class InstalledWorkflowTest(unittest.TestCase):
    def test_installed_wheel_publishes_updates_retries_and_restores(self) -> None:
        run_id = f"installed-{os.getpid()}-{time.time_ns()}"
        run = Path("/tmp/html-publish-verify") / run_id
        artifacts = run / "artifacts"

        def lifecycle(command: str) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                ["bash", str(HELPER), command, run_id],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result

        try:
            started = lifecycle("start")
            values = dict(line.split("=", 1) for line in started.stdout.splitlines())
            (artifacts / "launch.txt").write_text(started.stdout + started.stderr)
            doctor = lifecycle("doctor")
            (artifacts / "doctor.txt").write_text(doctor.stdout + doctor.stderr)
            sources = dict(line.split("=", 1) for line in lifecycle("sources").stdout.splitlines())
            evidence = artifacts / "installed-workflow.jsonl"

            def record(value: dict[str, object]) -> None:
                with evidence.open("a") as output:
                    output.write(json.dumps(value) + "\n")

            def cli(*args: str, exit_code: int = 0) -> dict[str, object]:
                command = [values["CLI"], "--config", values["CONFIG"], "--json", *args]
                result = subprocess.run(
                    command, cwd=artifacts, capture_output=True, text=True, timeout=30
                )
                record(
                    {
                        "command": command,
                        "exit_code": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                )
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                return cast(dict[str, object], json.loads(result.stdout))

            def fetch(path: str) -> tuple[int, bytes]:
                try:
                    with urllib.request.urlopen(values["URL"] + path, timeout=3) as response:
                        status, body = response.status, response.read()
                except urllib.error.HTTPError as error:
                    status, body = error.code, error.read()
                record({"path": path, "status": status, "body": body.decode("utf-8", "replace")})
                return status, body

            target = values["URL"] + "/"
            plan = cli("plan", "--name", "notes", "--source", sources["PAGE_A"], "--target", target)
            self.assertEqual(plan["prediction"], "create")
            self.assertFalse((run / "instance/archive.git").exists())
            self.assertFalse((run / "instance/runtime").exists())
            a = cli("publish", "--name", "notes", "--source", sources["PAGE_A"], "--target", target)
            self.assertEqual(a["outcome"], "published")
            revision_a = str(a["active_revision"])
            self.assertEqual(fetch("/notes/"), (200, Path(sources["PAGE_A"]).read_bytes()))
            self.assertEqual(cli("status", "--name", "notes")["active_revision"], revision_a)
            b = cli(
                "publish",
                "--name",
                "notes",
                "--source",
                sources["PAGE_B"],
                "--target",
                target,
                "--expected-revision",
                revision_a,
            )
            revision_b = str(b["active_revision"])
            self.assertNotEqual(revision_a, revision_b)
            self.assertEqual(b["url"], a["url"])
            self.assertEqual(fetch("/notes/"), (200, Path(sources["PAGE_B"]).read_bytes()))
            retry = cli(
                "publish",
                "--name",
                "notes",
                "--source",
                sources["PAGE_B"],
                "--target",
                target,
                "--expected-revision",
                revision_a,
            )
            self.assertEqual(retry["outcome"], "unchanged")
            self.assertEqual(retry["archive_commit"], b["archive_commit"])
            self.assertEqual(retry["effects"], {"archive_advanced": False, "activated": False})
            conflict = cli(
                "publish",
                "--name",
                "notes",
                "--source",
                sources["PAGE_A"],
                "--target",
                target,
                "--expected-revision",
                revision_a,
                exit_code=1,
            )
            self.assertEqual(
                cast(dict[str, object], conflict["error"])["code"], "revision_conflict"
            )
            self.assertEqual(cli("status", "--name", "notes")["active_revision"], revision_b)
            self.assertEqual(cli("verify", "--name", "notes")["outcome"], "verified")
            history = cli("history", "--name", "notes")
            self.assertEqual(len(cast(list[object], history["entries"])), 2)
            restored = cli(
                "restore",
                "--name",
                "notes",
                "--archive-commit",
                str(a["archive_commit"]),
                "--target",
                target,
                "--expected-revision",
                revision_b,
            )
            self.assertEqual(restored["active_revision"], revision_a)
            self.assertEqual(fetch("/notes/"), (200, Path(sources["PAGE_A"]).read_bytes()))
            self.assertEqual(
                len(cast(list[object], cli("history", "--name", "notes")["entries"])), 3
            )
            site = run / "instance/site with spaces"
            site.mkdir()
            (site / "index.html").write_bytes(Path(sources["PAGE_A"]).read_bytes())
            asset = site / "café.txt"
            asset.write_bytes(b"asset\n")
            directory = cli("publish", "--name", "site", "--source", str(site), "--target", target)
            self.assertEqual(fetch("/site/caf%C3%A9.txt"), (200, b"asset\n"))
            asset.unlink()
            removed = cli(
                "publish",
                "--name",
                "site",
                "--source",
                str(site),
                "--target",
                target,
                "--expected-revision",
                str(directory["active_revision"]),
            )
            self.assertEqual(removed["outcome"], "published")
            self.assertEqual(fetch("/site/caf%C3%A9.txt")[0], 404)
            self.assertEqual(fetch("/site/missing.txt")[0], 404)
            lifecycle("offline")
            self.assertEqual(cli("status", "--name", "notes")["active_revision"], revision_a)
            failed = cli("verify", "--name", "notes", exit_code=1)
            self.assertEqual(cast(dict[str, object], failed["error"])["code"], "delivery_failure")
        finally:
            lifecycle("stop")
        self.assertTrue(evidence.is_file())
        self.assertTrue((artifacts / "doctor.txt").is_file())
        self.assertTrue((artifacts / "server.log").is_file())
        self.assertFalse((run / "instance").exists())
        lifecycle("stop")
