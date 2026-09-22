from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_om1_mvp.py"


class PublicationHandler(BaseHTTPRequestHandler):
    state_path: Path
    requests: list[dict[str, str | None]]

    def do_GET(self) -> None:
        state = cast(dict[str, Any], json.loads(self.state_path.read_text(encoding="utf-8")))
        body = cast(str, state["content"]).encode()
        self.requests.append(
            {
                "path": self.path,
                "if_modified_since": self.headers.get("If-Modified-Since"),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if not os.environ.get("OMIT_TEST_NO_STORE"):
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class VerifyOm1MvpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="verify-om1-mvp-test-")
        self.root = Path(self.temporary.name)
        self.state = self.root / "state.json"
        self.log = self.root / "remote.jsonl"
        self.fake_remote = self.root / "fake-html-publish-remote"
        self.state.write_text(
            json.dumps({"active_revision": "previous-revision", "content": "previous"}),
            encoding="utf-8",
        )
        self._write_fake_remote()
        PublicationHandler.state_path = self.state
        PublicationHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PublicationHandler)
        host, port = cast(tuple[str, int], self.server.server_address)
        self.base_url = f"http://{host}:{port}/html-publish/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def _write_fake_remote(self) -> None:
        source = textwrap.dedent(
            """
            import argparse
            import hashlib
            import json
            import os
            import sys
            from pathlib import Path

            parser = argparse.ArgumentParser()
            commands = parser.add_subparsers(dest="operation", required=True)
            status = commands.add_parser("status")
            status.add_argument("--name", required=True)
            for operation in ("plan", "publish"):
                command = commands.add_parser(operation)
                command.add_argument("--name", required=True)
                command.add_argument("--source", required=True, type=Path)
                command.add_argument("--expected-revision")
            args = parser.parse_args()
            state_path = Path(os.environ["FAKE_REMOTE_STATE"])
            log_path = Path(os.environ["FAKE_REMOTE_LOG"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            url = os.environ["FAKE_BASE_URL"] + args.name + "/"
            record = {"operation": args.operation, "name": args.name}
            if args.operation == "status":
                payload = {
                    "operation": "status",
                    "outcome": "observed",
                    "url": url,
                    "active_revision": state["active_revision"],
                }
                exit_code = 0
            else:
                body = args.source.read_bytes()
                requested = hashlib.sha1(body).hexdigest()
                record.update({
                    "expected_revision": args.expected_revision,
                    "requested_revision": requested,
                    "content": body.decode(),
                    "source_mode": args.source.stat().st_mode & 0o777,
                    "directory_mode": args.source.parent.stat().st_mode & 0o777,
                    "source_sha256": hashlib.sha256(body).hexdigest(),
                })
                active = state["active_revision"]
                if active is not None and args.expected_revision != active and requested != active:
                    payload = {
                        "operation": args.operation,
                        "outcome": "error",
                        "url": url,
                        "active_revision": active,
                        "requested_revision": requested,
                        "error": {"code": "revision_conflict"},
                    }
                    exit_code = 1
                elif args.operation == "plan":
                    payload = {
                        "operation": "plan",
                        "outcome": "planned",
                        "prediction": "unchanged" if requested == active else (
                            "create" if active is None else "update"
                        ),
                        "url": url,
                        "active_revision": active,
                        "requested_revision": requested,
                    }
                    exit_code = 0
                else:
                    state = {"active_revision": requested, "content": body.decode()}
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    payload = {
                        "operation": "publish",
                        "outcome": "unchanged" if requested == active else "published",
                        "url": url,
                        "active_revision": requested,
                        "requested_revision": requested,
                    }
                    exit_code = 0
            with log_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record) + "\\n")
            print(json.dumps(payload, separators=(",", ":")))
            raise SystemExit(exit_code)
            """
        )
        self.fake_remote.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        self.fake_remote.chmod(0o755)

    def run_verifier(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "FAKE_REMOTE_STATE": str(self.state),
                "FAKE_REMOTE_LOG": str(self.log),
                "FAKE_BASE_URL": self.base_url,
            }
        )
        return subprocess.run(
            [
                sys.executable,
                str(VERIFIER),
                "--remote-command",
                str(self.fake_remote),
                "--timeout",
                "5",
                *arguments,
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def records(self) -> list[dict[str, Any]]:
        return [
            cast(dict[str, Any], json.loads(line))
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]

    def test_real_process_verifies_update_conflict_http_and_source_preservation(self) -> None:
        result = self.run_verifier()

        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = cast(dict[str, Any], json.loads(result.stdout))
        self.assertEqual(
            [operation["step"] for operation in evidence["operations"]],
            [
                "status-initial",
                "plan-a",
                "publish-a",
                "status-a",
                "plan-b",
                "publish-b",
                "status-b",
                "publish-c-stale-a",
            ],
        )
        self.assertEqual(evidence["revisions"]["initial"], "previous-revision")
        self.assertNotEqual(evidence["revisions"]["a"], evidence["revisions"]["b"])
        self.assertEqual(evidence["active_revision"], evidence["revisions"]["b"])
        self.assertEqual(evidence["conflict_code"], "revision_conflict")
        self.assertEqual(evidence["http"]["status"], 200)
        self.assertEqual(evidence["http"]["cache_control"], "no-store")
        self.assertTrue(evidence["source_hash_preservation"]["verified"])
        self.assertEqual(set(evidence["source_hash_preservation"]["sha256"]), {"A", "B", "C"})

        records = self.records()
        self.assertEqual([record["operation"] for record in records].count("status"), 3)
        artifacts = [record for record in records if "content" in record]
        self.assertEqual(artifacts[0]["expected_revision"], "previous-revision")
        self.assertEqual(artifacts[1]["expected_revision"], "previous-revision")
        self.assertEqual(artifacts[-1]["expected_revision"], evidence["revisions"]["a"])
        self.assertEqual({record["source_mode"] for record in artifacts}, {0o600})
        self.assertEqual({record["directory_mode"] for record in artifacts}, {0o700})
        self.assertEqual(len({record["content"] for record in artifacts}), 3)
        for record in artifacts:
            label = cast(str, record["content"]).split("MVP ", 1)[1][0]
            self.assertEqual(
                record["source_sha256"],
                evidence["source_hash_preservation"]["sha256"][label],
            )

        self.assertEqual(len(PublicationHandler.requests), 1)
        request = PublicationHandler.requests[0]
        self.assertEqual(request["path"], "/html-publish/om1-deployment-mvp/")
        self.assertEqual(request["if_modified_since"], "Thu, 01 Jan 1970 00:00:00 GMT")
        final = cast(dict[str, Any], json.loads(self.state.read_text(encoding="utf-8")))
        self.assertIn("om1 deployment MVP B", final["content"])

    def test_timeout_argument_is_bounded_before_remote_execution(self) -> None:
        result = self.run_verifier("--timeout", "301")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("timeout must be between 1 and 300 seconds", result.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
