from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]


class RemoteCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="html-publish-remote-test-")
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "transport.jsonl"
        self.snapshot = self.root / "snapshot.json"
        self._write_fake_ssh()
        self._write_fake_scp()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{self.environment['PATH']}",
                "FAKE_TRANSPORT_LOG": str(self.log),
                "FAKE_SNAPSHOT": str(self.snapshot),
                "FAKE_REMOTE_STDOUT": '{"outcome":"observed","active_revision":"abc123"}\n',
                "FAKE_REMOTE_EXIT": "0",
                "FAKE_SCP_EXIT": "0",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_executable(self, name: str, source: str) -> None:
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        path.chmod(0o755)

    def _write_fake_ssh(self) -> None:
        self._write_executable(
            "ssh",
            textwrap.dedent(
                """
                import json
                import os
                import sys
                from pathlib import Path

                log = Path(os.environ["FAKE_TRANSPORT_LOG"])
                with log.open("a", encoding="utf-8") as output:
                    output.write(json.dumps({"program": "ssh", "argv": sys.argv[1:]}) + "\\n")
                command = sys.argv[-1]
                if " --json " in f" {command} ":
                    sys.stdout.write(os.environ.get("FAKE_REMOTE_STDOUT", ""))
                    sys.stderr.write(os.environ.get("FAKE_REMOTE_STDERR", ""))
                    raise SystemExit(int(os.environ.get("FAKE_REMOTE_EXIT", "0")))
                raise SystemExit(int(os.environ.get("FAKE_SETUP_EXIT", "0")))
                """
            ),
        )

    def _write_fake_scp(self) -> None:
        self._write_executable(
            "scp",
            textwrap.dedent(
                """
                import json
                import os
                import sys
                from pathlib import Path

                log = Path(os.environ["FAKE_TRANSPORT_LOG"])
                source = Path(sys.argv[-2])
                snapshot = {
                    "source": str(source),
                    "index": (source / "index.html").read_text(encoding="utf-8"),
                    "mode": source.parent.stat().st_mode & 0o777,
                }
                Path(os.environ["FAKE_SNAPSHOT"]).write_text(
                    json.dumps(snapshot), encoding="utf-8"
                )
                with log.open("a", encoding="utf-8") as output:
                    output.write(json.dumps({"program": "scp", "argv": sys.argv[1:]}) + "\\n")
                sys.stderr.write(os.environ.get("FAKE_SCP_STDERR", ""))
                raise SystemExit(int(os.environ.get("FAKE_SCP_EXIT", "0")))
                """
            ),
        )

    def run_remote(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "html_publish.remote", *arguments],
            cwd=ROOT,
            env=environment or self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def records(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [
            cast(dict[str, Any], json.loads(line))
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]

    def test_status_uses_strict_ssh_and_relays_remote_result_without_transfer(self) -> None:
        result = self.run_remote("status", "--name", "release-notes")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            '{"outcome":"observed","active_revision":"abc123"}\n',
        )
        records = self.records()
        self.assertEqual([record["program"] for record in records], ["ssh"])
        arguments = cast(list[str], records[0]["argv"])
        self.assertEqual(
            arguments[:-2],
            [
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ForwardAgent=no",
            ],
        )
        self.assertEqual(arguments[-2], "pascal@om1.donkey-arcturus.ts.net")
        command = arguments[-1]
        self.assertIn(
            "/home/pascal/.local/share/html-publish/current/.venv/bin/html-publish",
            command,
        )
        self.assertIn("--config", command)
        self.assertIn("/home/pascal/.config/html-publish/publisher.json", command)
        self.assertIn("--json status --name release-notes", command)

    def test_plan_transfers_a_private_snapshot_and_forwards_expected_revision(self) -> None:
        source = self.root / "notes.html"
        source.write_text("<!doctype html><h1>A</h1>\n", encoding="utf-8")
        remote_output = '{"operation":"plan","outcome":"planned"}\n'
        environment = self.environment | {"FAKE_REMOTE_STDOUT": remote_output}

        result = self.run_remote(
            "plan",
            "--name",
            "release-notes",
            "--source",
            str(source),
            "--expected-revision",
            "old-revision",
            environment=environment,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, remote_output)
        snapshot = cast(dict[str, Any], json.loads(self.snapshot.read_text(encoding="utf-8")))
        self.assertNotEqual(snapshot["source"], str(source))
        self.assertEqual(snapshot["index"], "<!doctype html><h1>A</h1>\n")
        self.assertEqual(snapshot["mode"], 0o700)
        records = self.records()
        self.assertEqual([record["program"] for record in records], ["ssh", "scp", "ssh"])
        transfer = cast(list[str], records[1]["argv"])
        self.assertIn("StrictHostKeyChecking=yes", transfer)
        self.assertRegex(
            transfer[-1],
            r"^pascal@om1\.donkey-arcturus\.ts\.net:"
            r"/home/pascal/\.local/share/html-publish/incoming/[0-9a-f]{32}/$",
        )
        invocation = cast(list[str], records[2]["argv"])[-1]
        self.assertIn("--json plan --name release-notes", invocation)
        self.assertIn("--expected-revision old-revision", invocation)
        self.assertRegex(invocation, r"--source .*/incoming/[0-9a-f]{32}/site")
        self.assertIn("trap 'rm -rf -- ", invocation)

    def test_transfer_failure_reports_pre_invocation_and_preserves_source(self) -> None:
        source = self.root / "report.html"
        original = b"<!doctype html><h1>source survives</h1>\n"
        source.write_bytes(original)
        environment = self.environment | {
            "FAKE_SCP_EXIT": "23",
            "FAKE_SCP_STDERR": "transfer failed\n",
        }

        result = self.run_remote(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            environment=environment,
        )

        self.assertEqual(result.returncode, 23)
        payload = cast(dict[str, Any], json.loads(result.stdout))
        self.assertEqual(payload["error"]["code"], "transport_failure")
        self.assertFalse(payload["publication_may_have_started"])
        self.assertEqual(source.read_bytes(), original)
        records = self.records()
        self.assertEqual([record["program"] for record in records], ["ssh", "scp", "ssh"])
        cleanup = cast(list[str], records[-1]["argv"])[-1]
        self.assertRegex(cleanup, r"^rm -rf -- .*/incoming/[0-9a-f]{32}$")

    def test_publish_relays_remote_failure_and_preserves_source(self) -> None:
        source = self.root / "report.html"
        original = b"<!doctype html><h1>conflict</h1>\n"
        source.write_bytes(original)
        remote_output = (
            '{"operation":"publish","outcome":"error","error":{"code":"revision_conflict"}}\n'
        )
        environment = self.environment | {
            "FAKE_REMOTE_STDOUT": remote_output,
            "FAKE_REMOTE_EXIT": "1",
        }

        result = self.run_remote(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            "--request-id",
            "attempt-001",
            environment=environment,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, remote_output)
        self.assertEqual(source.read_bytes(), original)
        invocation = cast(list[str], self.records()[-1]["argv"])[-1]
        self.assertIn("--request-id attempt-001", invocation)

    def test_lost_publish_result_reports_uncertain_outcome_with_generated_request_id(self) -> None:
        source = self.root / "report.html"
        source.write_text("<!doctype html><h1>B</h1>\n", encoding="utf-8")
        environment = self.environment | {"FAKE_REMOTE_EXIT": "255"}

        result = self.run_remote(
            "publish",
            "--name",
            "report",
            "--source",
            str(source),
            environment=environment,
        )

        self.assertEqual(result.returncode, 255)
        payload = cast(dict[str, Any], json.loads(result.stdout))
        self.assertEqual(payload["error"]["code"], "publication_outcome_unknown")
        self.assertTrue(payload["publication_may_have_started"])
        self.assertRegex(payload["request_id"], r"^remote-[0-9a-f]{32}$")
        invocation = cast(list[str], self.records()[-1]["argv"])[-1]
        self.assertIn(f"--request-id {payload['request_id']}", invocation)


if __name__ == "__main__":
    unittest.main()
