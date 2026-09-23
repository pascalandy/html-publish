from __future__ import annotations

import contextlib
import copy
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
TARGET = "https://review.example/pages/"
HOST_EXECUTABLE = "/usr/local/bin/html-publish"
HOST_CONFIG = "/etc/html-publish/publisher.json"
INCOMING_ROOT = "/tmp/html-publish/incoming"


def report(
    operation: str,
    *,
    name: str | None = "release-notes",
    request_id: str | None = None,
    expected: str | None = None,
    expected_record: str | None = None,
    record_revision: str | None = None,
    outcome: str = "observed",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "operation": operation,
        "request_id": request_id,
        "outcome": outcome,
        "target": TARGET,
        "name": name,
        "url": TARGET + name + "/" if name else None,
        "expected_revision": expected,
        "requested_revision": None,
        "expected_record_revision": expected_record,
        "requested_record_revision": None,
        "archived_revision": None,
        "archived_record_revision": None,
        "render_profile_id": None,
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
    }
    if operation == "status" and name is None:
        payload["entries"] = []
    if outcome in {"published", "unchanged", "verified"}:
        payload.update(
            {
                "requested_revision": "rev-b" if operation in {"publish", "restore"} else None,
                "requested_record_revision": record_revision,
                "archived_revision": "rev-b",
                "archived_record_revision": record_revision,
                "archive_commit": "commit-b",
                "active_revision": "rev-b",
                "observation": {
                    "saved": {
                        "revision": "rev-b",
                        "record_revision": record_revision,
                        "archive_commit": "commit-b",
                    },
                    "selection": {
                        "state": "selected",
                        "revision": "rev-b",
                        "integrity_checked": True,
                        "detail": None,
                    },
                },
                "verification": {
                    "result": "passed",
                    "revision": "rev-b",
                    "checked_at": "2026-09-22T12:00:00+00:00",
                    "probe_location": "host",
                    "files_checked": 1,
                    "bytes_checked": 8,
                    "scope": [
                        "local_export",
                        "directory_url",
                        "index_html",
                        "all_files",
                        "missing_path",
                    ],
                    "detail": None,
                },
            }
        )
        if outcome == "published":
            payload["effects"] = {"archive_advanced": True, "activated": True}
    return payload


class RemoteCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="html-publish-remote-test-")
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "transport.jsonl"
        self.snapshot = self.root / "snapshot.json"
        self.pids = self.root / "pids.json"
        fixture = textwrap.dedent("""\
            import json
            import os
            import shlex
            import signal
            import subprocess
            import sys
            import time
            from pathlib import Path

            program = Path(sys.argv[0]).name
            command = sys.argv[-1]
            stage = "transfer" if program == "scp" else (
                "invoke" if "--json" in shlex.split(command) else (
                    "cleanup" if command.startswith("rm ") else "setup"))
            with Path(os.environ["FIXTURE_LOG"]).open("a") as output:
                output.write(json.dumps({"program": program, "stage": stage,
                                         "argv": sys.argv[1:]}) + "\\n")
            if stage == "transfer":
                source = Path(sys.argv[-2])
                files = sorted(
                    item.relative_to(source).as_posix()
                    for item in source.rglob("*") if item.is_file()
                )
                index = next((name for name in ("index.html", "index.md") if name in files), None)
                Path(os.environ["FIXTURE_SNAPSHOT"]).write_text(json.dumps({
                    "source": str(source),
                    "index": (source / index).read_text() if index else None,
                    "files": files,
                    "mode": source.parent.stat().st_mode & 0o777}))
            if os.environ.get("FIXTURE_HANG") == stage:
                child = subprocess.Popen([sys.executable, "-c",
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "time.sleep(60)"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                Path(os.environ["FIXTURE_PIDS"]).write_text(json.dumps([os.getpid(), child.pid]))
                signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
                if os.environ.get("FIXTURE_CLOSE_PIPES"):
                    os.close(1)
                    os.close(2)
                time.sleep(60)
            if stage == "invoke":
                sys.stdout.write(os.environ.get("FIXTURE_STDOUT", ""))
            sys.stderr.write(os.environ.get("FIXTURE_STDERR", ""))
            raise SystemExit(int(os.environ.get("FIXTURE_" + stage.upper() + "_EXIT", "0")))
            """)
        for name in ("ssh", "scp"):
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n{fixture}", encoding="utf-8")
            executable.chmod(0o755)
        self.environment = os.environ | {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FIXTURE_LOG": str(self.log),
            "FIXTURE_SNAPSHOT": str(self.snapshot),
            "FIXTURE_PIDS": str(self.pids),
            "FIXTURE_STDOUT": json.dumps(report("status")),
        }
        self.source = self.root / "notes.html"
        self.source.write_text("<!doctype html><h1>A</h1>\n", encoding="utf-8")

    def tearDown(self) -> None:
        if self.pids.exists():
            for pid in json.loads(self.pids.read_text()):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        self.temporary.cleanup()

    def command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "html_publish.remote",
            "--host",
            "operator@example.test",
            "--remote-executable",
            HOST_EXECUTABLE,
            "--remote-config",
            HOST_CONFIG,
            "--target",
            TARGET,
            "--incoming-root",
            INCOMING_ROOT,
            *arguments,
        ]

    def run_remote(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*arguments),
            cwd=ROOT,
            env=environment or self.environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=8,
        )

    def records(self) -> list[dict[str, Any]]:
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def artifact_args(self, operation: str = "publish") -> list[str]:
        args = [
            operation,
            "--name",
            "release-notes",
            "--source",
            str(self.source),
            "--expected-revision",
            "rev-a",
        ]
        return args + (["--request-id", "attempt-1"] if operation == "publish" else [])

    def test_global_options_after_command_preserve_remote_json_result(self) -> None:
        options = [
            "--host",
            "operator@example.test",
            "--remote-executable",
            "/usr/local/bin/html-publish",
            "--remote-config",
            "/etc/html-publish/publisher.json",
            "--target",
            TARGET,
            "--incoming-root",
            "/tmp/html-publish-incoming",
            "--connect-timeout",
            "3",
            "--command-seconds",
            "10",
            "--json",
        ]
        before = self.run_remote(*options, "status", "--name", "release-notes")
        after = self.run_remote("status", "--name", "release-notes", *options)
        for result in (before, after):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout), report("status"))
            self.assertEqual(result.stderr, "")
        invocations = [entry for entry in self.records() if entry["stage"] == "invoke"]
        self.assertEqual(len(invocations), 2)
        for invocation in invocations:
            self.assertIn("operator@example.test", invocation["argv"])
            self.assertIn("/usr/local/bin/html-publish", invocation["argv"][-1])
            self.assertIn("/etc/html-publish/publisher.json", invocation["argv"][-1])

    def test_summary_protocol_validates_mode_counts_and_warning_context(self) -> None:
        payload = report("status")
        payload["warning_details"] = []
        payload["report"] = {
            "mode": "summary",
            "collections": {"/warning_details": {"total": 2, "included": 0, "omitted": 2}},
            "text": {},
        }
        environment = self.environment | {"FIXTURE_STDOUT": json.dumps(payload)}
        result = self.run_remote(
            "status", "--name", "release-notes", "--report", "summary", environment=environment
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["report"], payload["report"])
        invocation = next(item for item in self.records() if item["stage"] == "invoke")
        self.assertIn("--report summary", invocation["argv"][-1])

        for broken in (
            payload | {"report": {**payload["report"], "mode": "detail"}},
            payload
            | {
                "report": {
                    **payload["report"],
                    "collections": {
                        "/warning_details": {"total": 2, "included": 0, "omitted": True}
                    },
                }
            },
            payload
            | {
                "warning_details": [
                    {"code": "missing_relative_asset", "source_path": "index.html", "reference": 2}
                ]
            },
        ):
            invalid = self.run_remote(
                "status",
                "--name",
                "release-notes",
                "--report",
                "summary",
                environment=self.environment | {"FIXTURE_STDOUT": json.dumps(broken)},
            )
            self.assertEqual(invalid.returncode, 1)
            self.assertEqual(json.loads(invalid.stdout)["error"]["code"], "remote_protocol_failure")

    def test_report_mode_survives_usage_errors_and_schema_lists_choices(self) -> None:
        invalid = self.run_remote(
            "status", "--name", "release-notes", "--report", "summary", "--limit", "0"
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(json.loads(invalid.stdout)["report"]["mode"], "summary")
        schema = self.run_remote("schema")
        self.assertEqual(schema.returncode, 0)
        commands = json.loads(schema.stdout)["commands"]
        status = next(item for item in commands if item["name"] == "status")
        option = next(item for item in status["options"] if "--report" in item["flags"])
        self.assertEqual(option["choices"], ["detail", "summary"])
        self.assertIn("summary", option["help"])

    def test_cleanup_warning_keeps_text_metadata_in_both_modes(self) -> None:
        for mode in ("detail", "summary"):
            with self.subTest(mode=mode):
                payload = report(
                    "publish", request_id="attempt-1", expected="rev-a", outcome="published"
                )
                payload["warning_details"] = []
                payload["report"] = {
                    "mode": mode,
                    "collections": {"/warning_details": {"total": 0, "included": 0, "omitted": 0}},
                    "text": {},
                }
                args = self.artifact_args() + (["--report", mode] if mode == "summary" else [])
                result = self.run_remote(
                    *args,
                    environment=self.environment
                    | {"FIXTURE_STDOUT": json.dumps(payload), "FIXTURE_CLEANUP_EXIT": "17"},
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                actual = json.loads(result.stdout)
                self.assertEqual(actual["outcome"], "published")
                self.assertEqual(actual["transport"]["detail"], "Cleanup exited 17")
                self.assertEqual(
                    actual["report"]["text"]["/transport/detail"],
                    {"total_bytes": 17, "included_bytes": 17, "omitted_bytes": 0},
                )

    def test_explicit_host_overrides_client_file_without_rewriting_it(self) -> None:
        client = self.root / "client.json"
        client.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "target": {"id": "review", "base_url": TARGET},
                    "execution": {
                        "kind": "remote",
                        "command": ["html-publish-remote"],
                        "host": "saved@example.test",
                        "remote_executable": HOST_EXECUTABLE,
                        "remote_config": HOST_CONFIG,
                        "incoming_root": INCOMING_ROOT,
                    },
                    "limits": {},
                }
            )
        )
        before = client.read_bytes()
        result = self.run_remote(
            "--config",
            str(client),
            "--host",
            "override@example.test",
            "status",
            "--name",
            "release-notes",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), report("status"))
        self.assertIn("override@example.test", self.records()[-1]["argv"])
        self.assertEqual(client.read_bytes(), before)

    def failure(
        self, operation: str, code: str, phase: str, message: str, action: str, required: list[str]
    ) -> dict[str, Any]:
        expected = report(operation, request_id="attempt-1", expected="rev-a", outcome="error")
        expected["error"] = {
            "code": code,
            "phase": phase,
            "message": message,
            "next_action": {"kind": action, "required_inputs": required},
        }
        return expected

    def assert_no_live_children(self) -> None:
        self.assertTrue(self.pids.exists(), "fixture did not reach the hanging phase")
        for pid in json.loads(self.pids.read_text()):
            path = Path(f"/proc/{pid}/stat")
            for _ in range(50):
                if not path.exists() or path.read_text().split(")", 1)[1].split()[0] == "Z":
                    break
                time.sleep(0.01)
            else:
                self.fail(f"owned child {pid} survived the command")

    def test_all_non_upload_commands_forward_exact_arguments_and_additive_fields(self) -> None:
        unsafe = "value with ' quotes ; $(touch should-not-exist)"
        cases = [
            (
                ["status", "--after", "earlier", "--limit", "2", "--host-check"],
                report("status", name=None),
                ["status", "--after", "earlier", "--limit", "2", "--host-check"],
            ),
            (
                ["verify", "--name", "release-notes"],
                report("verify", outcome="verified"),
                ["verify", "--name", "release-notes"],
            ),
            (
                [
                    "history",
                    "--name",
                    "release-notes",
                    "--limit",
                    "5",
                    "--after",
                    unsafe,
                    "--diff",
                    unsafe,
                ],
                report("history"),
                [
                    "history",
                    "--name",
                    "release-notes",
                    "--limit",
                    "5",
                    "--after",
                    unsafe,
                    "--diff",
                    unsafe,
                ],
            ),
            (
                [
                    "restore",
                    "--name",
                    "release-notes",
                    "--archive-commit",
                    unsafe,
                    "--expected-revision",
                    "rev-a",
                    "--request-id",
                    unsafe,
                ],
                report("restore", request_id=unsafe, expected="rev-a", outcome="published"),
                [
                    "restore",
                    "--name",
                    "release-notes",
                    "--archive-commit",
                    unsafe,
                    "--target",
                    TARGET,
                    "--expected-revision",
                    "rev-a",
                    "--request-id",
                    unsafe,
                ],
            ),
        ]
        for args, payload, forwarded in cases:
            with self.subTest(operation=args[0]):
                if payload["outcome"] == "published":
                    payload["effects"] = {"archive_advanced": True, "activated": True}
                payload["future_detail"] = {"kept": True}
                result = self.run_remote(
                    *args, environment=self.environment | {"FIXTURE_STDOUT": json.dumps(payload)}
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), payload)
                record = self.records()[-1]
                argv = cast(list[str], record["argv"])
                self.assertEqual(
                    argv[:-2],
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
                self.assertEqual(
                    shlex.split(argv[-1]),
                    [
                        HOST_EXECUTABLE,
                        "--config",
                        HOST_CONFIG,
                        "--json",
                        forwarded[0],
                        "--report",
                        "detail",
                        *forwarded[1:],
                    ],
                )
        self.assertEqual([r["stage"] for r in self.records()], ["invoke"] * 4)

    def test_plan_captures_private_source_and_cleans_after_validated_completion(self) -> None:
        payload = report("plan", expected="rev-a", outcome="planned")
        result = self.run_remote(
            *self.artifact_args("plan"),
            environment=self.environment | {"FIXTURE_STDOUT": json.dumps(payload)},
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout), payload)
        snapshot = json.loads(self.snapshot.read_text())
        self.assertEqual(snapshot["index"], self.source.read_text())
        self.assertEqual(snapshot["mode"], 0o700)
        self.assertFalse(Path(snapshot["source"]).exists())
        self.assertEqual(
            [r["stage"] for r in self.records()], ["setup", "transfer", "invoke", "cleanup"]
        )
        command = self.records()[2]["argv"][-1]
        self.assertNotIn("trap", command)
        self.assertIn("--expected-revision rev-a", command)

    def test_markdown_transfer_preserves_raw_source_and_forwards_both_guards(self) -> None:
        source = self.root / "docs"
        (source / "assets").mkdir(parents=True)
        (source / "index.md").write_text("# Guide\n")
        (source / "assets" / "diagram.svg").write_text("<svg></svg>")
        payload = report(
            "publish",
            request_id="attempt-markdown",
            expected="output-a",
            expected_record="record-a",
            record_revision="record-b",
            outcome="published",
        )
        arguments = [
            "publish",
            "--name",
            "release-notes",
            "--source",
            str(source),
            "--format",
            "markdown",
            "--entry",
            "index.md",
            "--target",
            TARGET,
            "--expected-revision",
            "output-a",
            "--expected-record-revision",
            "record-a",
            "--request-id",
            "attempt-markdown",
        ]
        result = self.run_remote(
            *arguments,
            environment=self.environment | {"FIXTURE_STDOUT": json.dumps(payload)},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), payload)
        snapshot = json.loads(self.snapshot.read_text())
        self.assertEqual(snapshot["files"], ["assets/diagram.svg", "index.md"])
        self.assertEqual(snapshot["index"], "# Guide\n")
        self.assertTrue(snapshot["source"].endswith("/source"))
        command = shlex.split(self.records()[2]["argv"][-1])
        self.assertIn("--format", command)
        self.assertIn("markdown", command)
        self.assertIn("--entry", command)
        self.assertIn("index.md", command)
        self.assertEqual(command[command.index("--expected-revision") + 1], "output-a")
        self.assertEqual(command[command.index("--expected-record-revision") + 1], "record-a")

    def test_markdown_file_transfer_keeps_file_input_shape(self) -> None:
        source = self.root / "article.md"
        source.write_text("# Article\n")
        payload = report("plan", expected="output-a", outcome="planned")
        result = self.run_remote(
            "plan",
            "--name",
            "release-notes",
            "--source",
            str(source),
            "--format",
            "markdown",
            "--target",
            TARGET,
            "--expected-revision",
            "output-a",
            environment=self.environment | {"FIXTURE_STDOUT": json.dumps(payload)},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        snapshot = json.loads(self.snapshot.read_text())
        self.assertEqual(snapshot["files"], ["article.md"])
        command = shlex.split(self.records()[2]["argv"][-1])
        self.assertTrue(command[command.index("--source") + 1].endswith("/source/article.md"))

    def test_transfer_failure_has_complete_common_envelope_and_exit_one(self) -> None:
        result = self.run_remote(
            *self.artifact_args(), environment=self.environment | {"FIXTURE_TRANSFER_EXIT": "23"}
        )
        expected = self.failure(
            "publish",
            "transport_failure",
            "transfer",
            "Transfer failed before invocation",
            "retry",
            ["source", "request_id", "expected_revision"],
        )
        expected["transport"] = {"detail": "scp exited 23"}
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        for key, value in expected.items():
            self.assertEqual(payload[key], value)
        self.assertEqual(payload["report"]["mode"], "detail")
        self.assertEqual([r["stage"] for r in self.records()], ["setup", "transfer", "cleanup"])
        self.assertEqual(self.source.read_text(), "<!doctype html><h1>A</h1>\n")

    def test_lost_mutation_response_retains_staging_and_caller_context(self) -> None:
        for operation in ("publish", "restore"):
            with self.subTest(operation=operation):
                args = (
                    self.artifact_args()
                    if operation == "publish"
                    else [
                        "restore",
                        "--name",
                        "release-notes",
                        "--archive-commit",
                        "commit-a",
                        "--expected-revision",
                        "rev-a",
                        "--request-id",
                        "attempt-1",
                    ]
                )
                result = self.run_remote(
                    *args, environment=self.environment | {"FIXTURE_INVOKE_EXIT": "255"}
                )
                payload = json.loads(result.stdout)
                expected = self.failure(
                    operation,
                    "publication_outcome_unknown",
                    "invoke",
                    "SSH lost the publication result",
                    "inspect",
                    ["name", "request_id", "expected_revision"],
                )
                expected["effects"] = {"archive_advanced": None, "activated": None}
                expected["transport"] = {"cleanup": "skipped", "detail": "SSH exited 255"}
                if operation == "publish":
                    staging = payload["transport"]["staging"]
                    self.assertRegex(staging, r"/incoming/[0-9a-f]{32}$")
                    expected["transport"]["staging"] = staging
                self.assertEqual(result.returncode, 1)
                for key, value in expected.items():
                    self.assertEqual(payload[key], value)
                self.assertEqual(payload["report"]["mode"], "detail")
        self.assertNotIn("cleanup", [r["stage"] for r in self.records()])

    def test_generated_identity_is_reported_after_lost_response(self) -> None:
        result = self.run_remote(
            "restore",
            "--name",
            "release-notes",
            "--archive-commit",
            "commit-a",
            environment=self.environment | {"FIXTURE_INVOKE_EXIT": "255"},
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertRegex(payload["request_id"], r"^remote-[0-9a-f]{32}$")
        self.assertIn(payload["request_id"], shlex.split(self.records()[-1]["argv"][-1]))

    def test_usage_failure_preserves_valid_context_and_exits_two(self) -> None:
        result = self.run_remote("--command-seconds", "0", *self.artifact_args())
        expected = self.failure(
            "publish",
            "invalid_usage",
            "usage",
            "argument --command-seconds: command seconds must be positive",
            "fix_arguments",
            [],
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        for key, value in expected.items():
            self.assertEqual(payload[key], value)
        self.assertEqual(payload["report"]["mode"], "detail")
        self.assertEqual(self.records(), [])

    def test_malformed_and_mismatched_results_are_protocol_failures(self) -> None:
        valid = report("status")
        bad = [
            "",
            "{",
            "{} {}",
            "[]",
            json.dumps(valid | {"schema_version": True}),
            json.dumps(valid | {"operation": "verify"}),
            json.dumps(valid | {"name": "other"}),
            json.dumps(valid | {"target": "other"}),
            json.dumps(valid | {"request_id": "late"}),
            json.dumps(valid | {"effects": {"archive_advanced": [], "activated": False}}),
            json.dumps(valid | {"effects": {"archive_advanced": 0, "activated": False}}),
            json.dumps(valid | {"effects": {}}),
            json.dumps(valid | {"error": {}}),
        ]
        for stdout in bad:
            with self.subTest(stdout=stdout):
                result = self.run_remote(
                    "status",
                    "--name",
                    "release-notes",
                    environment=self.environment | {"FIXTURE_STDOUT": stdout},
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["error"]["code"], "remote_protocol_failure")
                self.assertEqual(
                    payload["effects"], {"archive_advanced": False, "activated": False}
                )

    def test_host_operational_and_usage_errors_keep_structured_actions(self) -> None:
        for exit_code, phase, outcome in ((1, "status", "observed"), (2, "usage", "error")):
            payload = report("status", outcome=outcome)
            payload["error"] = {
                "code": "state_degraded" if exit_code == 1 else "invalid_usage",
                "phase": phase,
                "message": "Inspect input",
                "next_action": {"kind": "inspect", "required_inputs": []},
            }
            result = self.run_remote(
                "status",
                "--name",
                "release-notes",
                environment=self.environment
                | {"FIXTURE_STDOUT": json.dumps(payload), "FIXTURE_INVOKE_EXIT": str(exit_code)},
            )
            self.assertEqual(result.returncode, exit_code)
            self.assertEqual(json.loads(result.stdout), payload)

    def test_untrusted_mutation_results_retain_staging(self) -> None:
        valid = report("publish", request_id="attempt-1", expected="rev-a", outcome="published")
        valid["effects"] = {"archive_advanced": True, "activated": True}
        for changed in (
            {"request_id": "different-attempt"},
            {"effects": {"archive_advanced": None, "activated": None}},
            {"effects": {"archive_advanced": True, "activated": False}},
            {"outcome": "unchanged"},
        ):
            with self.subTest(changed=changed):
                result = self.run_remote(
                    *self.artifact_args(),
                    environment=self.environment | {"FIXTURE_STDOUT": json.dumps(valid | changed)},
                )
                self.assertEqual(result.returncode, 1)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["error"]["code"], "remote_protocol_failure")
                self.assertEqual(payload["effects"], {"archive_advanced": None, "activated": None})
                self.assertRegex(payload["transport"]["staging"], r"/incoming/[0-9a-f]{32}$")
        self.assertNotIn("cleanup", [record["stage"] for record in self.records()])

    def test_nested_mutation_report_contract_rejects_false_success_without_cleanup(self) -> None:
        valid = report("publish", request_id="attempt-1", expected="rev-a", outcome="published")
        cases: list[tuple[str, object]] = [
            ("verification", {"result": "passed"}),
            ("verification.result", "failed"),
            ("verification.result", "not_checked"),
            ("verification.revision", "different-revision"),
            ("verification.checked_at", None),
            ("verification.checked_at", 12),
            ("verification.probe_location", ["host"]),
            ("verification.probe_location", "client"),
            ("verification.files_checked", True),
            ("verification.files_checked", -1),
            ("verification.files_checked", 0),
            ("verification.bytes_checked", 1.5),
            ("verification.bytes_checked", False),
            ("verification.scope", "all_files"),
            ("verification.scope", ["all_files", 12]),
            ("verification.scope", ["local_export"]),
            ("verification.detail", {"unexpected": "object"}),
            ("observation", "not a state object"),
            ("observation", None),
            ("observation", {"saved": None}),
            ("observation.saved", {"revision": "rev-b"}),
            ("observation.saved.archive_commit", 9),
            ("observation.saved.revision", "different-revision"),
            ("observation.selection", {"state": "selected"}),
            ("observation.selection.state", "unknown"),
            ("observation.selection.revision", None),
            ("observation.selection.integrity_checked", "yes"),
            ("observation.selection.integrity_checked", False),
            ("observation.selection.detail", []),
            ("requested_revision", "different-revision"),
            ("active_revision", "different-revision"),
        ]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                payload = copy.deepcopy(valid)
                owner = payload
                parts = path.split(".")
                for part in parts[:-1]:
                    owner = owner[part]
                owner[parts[-1]] = value
                result = self.run_remote(
                    *self.artifact_args(),
                    environment=self.environment | {"FIXTURE_STDOUT": json.dumps(payload)},
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                rejected = json.loads(result.stdout)
                self.assertEqual(rejected["error"]["code"], "remote_protocol_failure")
                self.assertEqual(rejected["effects"], {"archive_advanced": None, "activated": None})
                self.assertEqual(rejected["request_id"], "attempt-1")
                self.assertEqual(rejected["expected_revision"], "rev-a")
                self.assertEqual(rejected["transport"]["cleanup"], "skipped")
                self.assertRegex(rejected["transport"]["staging"], r"/incoming/[0-9a-f]{32}$")
        self.assertNotIn("cleanup", [record["stage"] for record in self.records()])

    def test_restore_and_verify_require_successful_revision_bound_verification(self) -> None:
        for operation in ("restore", "verify"):
            args = [operation, "--name", "release-notes"]
            if operation == "restore":
                args += [
                    "--archive-commit",
                    "commit-b",
                    "--request-id",
                    "attempt-1",
                    "--expected-revision",
                    "rev-a",
                ]
            for path, value in (("result", "failed"), ("revision", "other")):
                with self.subTest(operation=operation, path=path):
                    payload = report(
                        operation,
                        outcome="published" if operation == "restore" else "verified",
                        request_id="attempt-1" if operation == "restore" else None,
                        expected="rev-a" if operation == "restore" else None,
                    )
                    payload["verification"][path] = value
                    result = self.run_remote(
                        *args,
                        environment=self.environment | {"FIXTURE_STDOUT": json.dumps(payload)},
                    )
                    self.assertEqual(result.returncode, 1)
                    rejected = json.loads(result.stdout)
                    self.assertEqual(rejected["error"]["code"], "remote_protocol_failure")
                    effect = None if operation == "restore" else False
                    self.assertEqual(
                        rejected["effects"], {"archive_advanced": effect, "activated": effect}
                    )

    def test_valid_pending_archive_noop_preserves_nested_additive_fields(self) -> None:
        payload = report("publish", request_id="attempt-1", expected="rev-a", outcome="unchanged")
        payload["archived_revision"] = "pending-revision"
        payload["archive_commit"] = "pending-commit"
        payload["observation"]["saved"].update(
            {
                "revision": "pending-revision",
                "archive_commit": "pending-commit",
                "future_saved": True,
            }
        )
        payload["verification"]["future_verification"] = {"kept": True}
        payload["observation"]["future_observation"] = [1, 2]
        payload["observation"]["selection"]["future_selection"] = "kept"
        result = self.run_remote(
            *self.artifact_args(),
            environment=self.environment | {"FIXTURE_STDOUT": json.dumps(payload)},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), payload)
        self.assertEqual(self.records()[-1]["stage"], "cleanup")

    def test_status_preserves_failed_host_checks_and_degraded_observations(self) -> None:
        payload = report("status")
        payload["archived_revision"] = "rev-b"
        payload["archive_commit"] = "commit-b"
        payload["active_revision"] = "rev-b"
        payload["observation"] = {
            "saved": {
                "revision": "rev-b",
                "record_revision": None,
                "archive_commit": "commit-b",
            },
            "selection": {
                "state": "selected",
                "revision": "rev-b",
                "integrity_checked": False,
                "detail": None,
            },
        }
        payload["verification"].update(
            {
                "result": "failed",
                "revision": "rev-b",
                "probe_location": "host",
                "scope": ["local_export"],
                "detail": "Route drift",
            }
        )
        result = self.run_remote(
            "status",
            "--name",
            "release-notes",
            "--host-check",
            environment=self.environment | {"FIXTURE_STDOUT": json.dumps(payload)},
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), payload)
        payload["active_revision"] = None
        payload["observation"] = {
            "saved": {
                "revision": "rev-b",
                "record_revision": None,
                "archive_commit": "commit-b",
            },
            "selection": {
                "state": "degraded",
                "revision": None,
                "integrity_checked": False,
                "detail": "Dangling symlink",
            },
        }
        payload["verification"] = report("status")["verification"]
        payload["error"] = {
            "code": "state_degraded",
            "phase": "status",
            "message": "Dangling symlink",
            "next_action": {"kind": "inspect", "required_inputs": []},
        }
        result = self.run_remote(
            "status",
            "--name",
            "release-notes",
            environment=self.environment
            | {"FIXTURE_STDOUT": json.dumps(payload), "FIXTURE_INVOKE_EXIT": "1"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), payload)
        listing = report("status", name=None)
        listing["error"] = payload["error"]
        listing["entries"] = [
            {
                "name": "release-notes",
                "url": TARGET + "release-notes/",
                "observation": payload["observation"],
            }
        ]
        result = self.run_remote(
            "status",
            environment=self.environment
            | {"FIXTURE_STDOUT": json.dumps(listing), "FIXTURE_INVOKE_EXIT": "1"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), listing)
        listing["entries"][0]["observation"] = "not a state object"
        result = self.run_remote(
            "status",
            environment=self.environment
            | {"FIXTURE_STDOUT": json.dumps(listing), "FIXTURE_INVOKE_EXIT": "1"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "remote_protocol_failure")

    def test_valid_failed_mutation_preserves_activation_and_failed_verification(self) -> None:
        payload = report("publish", request_id="attempt-1", expected="rev-a", outcome="published")
        payload["outcome"] = "error"
        payload["verification"].update(
            {
                "result": "failed",
                "checked_at": None,
                "files_checked": 0,
                "bytes_checked": 0,
                "scope": [],
                "detail": "Bytes did not match",
            }
        )
        payload["error"] = {
            "code": "delivery_failure",
            "phase": "verify",
            "message": "Bytes did not match",
            "next_action": {"kind": "inspect", "required_inputs": []},
        }
        result = self.run_remote(
            *self.artifact_args(),
            environment=self.environment
            | {"FIXTURE_STDOUT": json.dumps(payload), "FIXTURE_INVOKE_EXIT": "1"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), payload)
        self.assertEqual(self.records()[-1]["stage"], "cleanup")

    def test_deadline_reaps_children_in_every_phase_and_preserves_effects(self) -> None:
        success = report("publish", request_id="attempt-1", expected="rev-a", outcome="published")
        success["effects"] = {"archive_advanced": True, "activated": True}
        for stage in ("setup", "transfer", "invoke", "cleanup"):
            with self.subTest(stage=stage):
                self.pids.unlink(missing_ok=True)
                records_before = len(self.records())
                start = time.monotonic()
                result = self.run_remote(
                    "--command-seconds",
                    "4",
                    *self.artifact_args(),
                    environment=self.environment
                    | {
                        "FIXTURE_HANG": stage,
                        "FIXTURE_STDOUT": json.dumps(success),
                        "FIXTURE_CLOSE_PIPES": "1",
                    },
                )
                self.assertLess(time.monotonic() - start, 5.5)
                self.assertIn(
                    stage,
                    [entry["stage"] for entry in self.records()[records_before:]],
                    result.stdout + result.stderr,
                )
                self.assert_no_live_children()
                payload = json.loads(result.stdout)
                self.assertEqual(result.returncode, 0 if stage == "cleanup" else 1, result.stderr)
                self.assertEqual(
                    payload["effects"],
                    {
                        "archive_advanced": True
                        if stage == "cleanup"
                        else None
                        if stage == "invoke"
                        else False,
                        "activated": True
                        if stage == "cleanup"
                        else None
                        if stage == "invoke"
                        else False,
                    },
                )
                if stage == "cleanup":
                    self.assertEqual(payload["outcome"], "published")
                    self.assertEqual(payload["transport"]["cleanup"], "failed")
                    self.assertTrue(payload["warnings"])
                if stage == "invoke":
                    self.assertEqual(payload["transport"]["cleanup"], "skipped")

    def test_cancelled_invocation_reaps_transport_group_and_reports_uncertainty(self) -> None:
        environment = self.environment | {"FIXTURE_HANG": "invoke"}
        with subprocess.Popen(
            self.command(*self.artifact_args()),
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            end = time.monotonic() + 3
            while not self.pids.exists() and time.monotonic() < end:
                time.sleep(0.01)
            self.assertTrue(self.pids.exists())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 1, stderr)
        self.assertEqual(
            json.loads(stdout)["effects"], {"archive_advanced": None, "activated": None}
        )
        self.assert_no_live_children()

    def test_help_explains_continuation(self) -> None:
        root_help = self.run_remote("--help")
        self.assertEqual(root_help.returncode, 0)
        normalized = " ".join(root_help.stdout.split())
        for description in (
            "capture and inspect without publication changes",
            "archive, activate, and verify a finished artifact",
            "observe one publication or a paged list",
            "check selected files and host HTTP delivery",
            "list per-name history and optional text differences",
            "select an archived revision under a revision guard",
            "SSH destination as user@host",
            "total client budget for capture, transport, and cleanup",
        ):
            self.assertIn(description, normalized)
        for operation in ("status", "history"):
            result = self.run_remote(operation, "--help")
            self.assertEqual(result.returncode, 0)
            self.assertIn("opaque continuation", result.stdout)
            self.assertIn("--after '<continuation>'", result.stdout)


if __name__ == "__main__":
    unittest.main()
