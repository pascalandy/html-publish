from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

from html_publish.configuration import load_client_config

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT / ".agents/skills/verify-html-publish/scripts/instance.sh"


class InstalledArtifactTest(unittest.TestCase):
    def test_receipt_lifecycle_through_wheel_and_http(self) -> None:
        run_id = f"artifact-{os.getpid()}-{time.time_ns()}"
        run = Path("/tmp/html-publish-verify") / run_id
        artifacts = run / "artifacts"

        def lifecycle(action: str) -> str:
            result = subprocess.run(
                ["bash", str(INSTANCE), action, run_id],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

        try:
            values = dict(line.split("=", 1) for line in lifecycle("start").splitlines())
            (artifacts / "launch-artifact.txt").write_text(str(values))
            lifecycle("doctor")
            cli = values["CLI"]
            target = values["URL"] + "/"
            publisher_config = values["CONFIG"]
            client_config = run / "instance/client.json"
            wrapper = run / "instance/publisher-wrapper.py"
            drop = run / "instance/drop-next-result"
            wrapper.write_text(
                "import pathlib, subprocess, sys\n"
                f"flag = pathlib.Path({str(drop)!r})\n"
                f"result = subprocess.run([{cli!r}, *sys.argv[1:]], capture_output=True)\n"
                "if flag.exists() and any(x in sys.argv for x in ('publish', 'restore')):\n"
                "    flag.unlink()\n"
                "    print('lost publisher response')\n"
                "    raise SystemExit(1)\n"
                "sys.stdout.buffer.write(result.stdout)\n"
                "sys.stderr.buffer.write(result.stderr)\n"
                "raise SystemExit(result.returncode)\n"
            )
            client_config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "target": {"id": "loopback", "base_url": target},
                        "execution": {
                            "kind": "local",
                            "command": [sys.executable, str(wrapper)],
                            "publisher_config": publisher_config,
                        },
                    }
                )
            )
            receipt = run / "notes.publish"
            source = run / "instance/notes"
            source.mkdir()
            (source / "index.html").write_bytes(b"A exact bytes\n")
            (source / "old.txt").write_bytes(b"old asset\n")
            log = artifacts / "artifact-workflow.jsonl"

            def call(*args: str, code: int = 0) -> dict[str, object]:
                command = [cli, *args]
                result = subprocess.run(
                    command, cwd=artifacts, capture_output=True, text=True, timeout=35
                )
                with log.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "run_id": run_id,
                                "command": command,
                                "exit_code": result.returncode,
                                "stdout": result.stdout,
                                "stderr": result.stderr,
                            }
                        )
                        + "\n"
                    )
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                return cast(dict[str, object], json.loads(result.stdout))

            def artifact(*args: str, code: int = 0) -> dict[str, object]:
                return call("--config", str(client_config), "artifact", *args, "--json", code=code)

            def publisher(*args: str, code: int = 0) -> dict[str, object]:
                return call("--config", publisher_config, "--json", *args, code=code)

            def fetch(path: str) -> tuple[int, bytes]:
                try:
                    with urllib.request.urlopen(values["URL"] + path, timeout=3) as response:
                        result = (response.status, response.read())
                except urllib.error.HTTPError as error:
                    result = (error.code, error.read())
                with log.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "run_id": run_id,
                                "http": path,
                                "status": result[0],
                                "body": result[1].decode("utf-8", "replace"),
                            }
                        )
                        + "\n"
                    )
                return result

            schema_commands = cast(list[dict[str, object]], call("schema")["commands"])
            self.assertEqual(schema_commands[6]["name"], "artifact")
            invalid = call("--json", "artifact", "publish", "--bad", code=2)
            self.assertEqual(invalid["operation"], "publish")
            self.assertEqual(cast(dict[str, object], invalid["error"])["code"], "invalid_usage")
            self.assertIn("receipt_persisted", invalid)
            self.assertIn(
                "--archive-commit",
                subprocess.run(
                    [cli, "artifact", "restore", "--help"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout,
            )
            first = artifact("publish", str(source), "--new", "notes", "--receipt", str(receipt))
            self.assertEqual(first["outcome"], "completed")
            self.assertEqual(first["publisher_calls"], 1)
            self.assertEqual(first["effects"], {"archive_advanced": True, "activated": True})
            self.assertEqual(fetch("/notes/")[1], b"A exact bytes\n")
            self.assertEqual(fetch("/notes/old.txt"), (200, b"old asset\n"))
            rev_a = first["accepted_revision"]
            history_entries = cast(
                list[dict[str, object]], publisher("history", "--name", "notes")["entries"]
            )
            first_commit = history_entries[0]["archive_commit"]
            self.assertEqual(json.loads((receipt / "receipt.json").read_text())["version"], 1)

            (source / "index.html").write_bytes(b"B exact bytes\n")
            (source / "old.txt").unlink()
            second = artifact("publish", str(source), "--receipt", str(receipt))
            rev_b = second["accepted_revision"]
            self.assertNotEqual(rev_a, rev_b)
            self.assertEqual(second["original_expectation"], rev_a)
            self.assertEqual(second["url"], first["url"])
            self.assertEqual(fetch("/notes/")[1], b"B exact bytes\n")
            self.assertEqual(fetch("/notes/old.txt")[0], 404)

            (source / "index.html").write_bytes(b"D reviewed bytes\n")
            competing = run / "instance/competing.html"
            competing.write_bytes(b"C competing bytes\n")
            external = publisher(
                "publish",
                "--name",
                "notes",
                "--source",
                str(competing),
                "--target",
                target,
                "--expected-revision",
                str(rev_b),
            )
            rev_c = external["active_revision"]
            observed = artifact("status", "--receipt", str(receipt))
            self.assertEqual(observed["accepted_revision"], rev_b)
            self.assertEqual(observed["active_revision"], rev_c)
            conflict = artifact("publish", str(source), "--receipt", str(receipt), code=1)
            self.assertEqual(conflict["pending_state"], "conflict")
            self.assertEqual(conflict["accepted_revision"], rev_b)
            attempt = conflict["attempt_id"]
            self.assertEqual(fetch("/notes/")[1], b"C competing bytes\n")
            bad_review = artifact(
                "publish",
                str(source),
                "--receipt",
                str(receipt),
                "--reviewed-revision",
                str(rev_b),
                "--replaces-attempt",
                str(attempt),
                code=1,
            )
            self.assertEqual(cast(dict[str, object], bad_review["error"])["code"], "stale_review")
            reviewed = artifact(
                "publish",
                str(source),
                "--receipt",
                str(receipt),
                "--reviewed-revision",
                str(rev_c),
                "--replaces-attempt",
                str(attempt),
            )
            self.assertEqual(reviewed["original_expectation"], rev_c)
            self.assertEqual(fetch("/notes/")[1], b"D reviewed bytes\n")

            restore = artifact(
                "restore", "--receipt", str(receipt), "--archive-commit", str(first_commit)
            )
            self.assertEqual(restore["outcome"], "completed")
            self.assertEqual(restore["archive_commit"], str(first_commit))
            self.assertEqual(restore["accepted_revision"], rev_a)
            self.assertEqual(fetch("/notes/")[1], b"A exact bytes\n")
            restored_state = json.loads((receipt / "receipt.json").read_text())
            self.assertEqual(restored_state["version"], 2)
            self.assertIsNone(restored_state["pending"])
            self.assertEqual(
                len(cast(list[object], publisher("history", "--name", "notes")["entries"])), 5
            )

            (source / "index.html").write_bytes(b"E frozen bytes\n")
            drop.touch()
            lost = artifact("publish", str(source), "--receipt", str(receipt), code=1)
            self.assertEqual(lost["pending_state"], "uncertain")
            frozen_id = lost["attempt_id"]
            (source / "index.html").write_bytes(b"F later edit\n")
            before_retry = len(
                cast(list[object], publisher("history", "--name", "notes")["entries"])
            )
            recovered = artifact("retry", "--receipt", str(receipt))
            self.assertEqual(recovered["attempt_id"], frozen_id)
            self.assertEqual(recovered["accepted_revision"], recovered["active_revision"])
            self.assertEqual(fetch("/notes/")[1], b"E frozen bytes\n")
            self.assertEqual(
                len(cast(list[object], publisher("history", "--name", "notes")["entries"])),
                before_retry,
            )
            local = call("artifact", "status", "--receipt", str(receipt), "--local-only", "--json")
            self.assertEqual(local["accepted_revision"], recovered["accepted_revision"])
            self.assertEqual(local["publisher_calls"], 0)

            legacy_first = publisher(
                "publish", "--name", "legacy", "--source", str(competing), "--target", target
            )
            legacy_receipt = run / "legacy.publish"
            legacy_receipt.mkdir(mode=0o700)
            (legacy_receipt / "receipt.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "binding": {
                            "association_id": "existing-v1-association",
                            "name": "legacy",
                            "target": {"kind": "local", "host": "loopback", "base_url": target},
                            "config_fingerprint": load_client_config(client_config).fingerprint,
                        },
                        "accepted_revision": legacy_first["active_revision"],
                        "pending": None,
                        "last_observation": None,
                        "completion": None,
                    }
                )
                + "\n"
            )
            legacy_update = artifact("publish", str(source), "--receipt", str(legacy_receipt))
            self.assertEqual(legacy_update["original_expectation"], legacy_first["active_revision"])
            self.assertEqual(fetch("/legacy/")[1], b"F later edit\n")
            self.assertEqual(
                json.loads((legacy_receipt / "receipt.json").read_text())["version"], 1
            )

            latest = cast(
                list[dict[str, object]], publisher("history", "--name", "notes")["entries"]
            )
            commit_e = latest[0]["archive_commit"]
            drop.touch()
            lost_restore = artifact(
                "restore",
                "--receipt",
                str(receipt),
                "--archive-commit",
                str(first_commit),
                code=1,
            )
            self.assertEqual(lost_restore["pending_state"], "uncertain")
            restore_id = lost_restore["attempt_id"]
            history_after_loss = len(
                cast(list[object], publisher("history", "--name", "notes")["entries"])
            )
            retried_restore = artifact("retry", "--receipt", str(receipt))
            self.assertEqual(retried_restore["attempt_id"], restore_id)
            self.assertEqual(retried_restore["accepted_revision"], rev_a)
            self.assertEqual(fetch("/notes/")[1], b"A exact bytes\n")
            self.assertEqual(
                len(cast(list[object], publisher("history", "--name", "notes")["entries"])),
                history_after_loss,
            )

            adopted_receipt = run / "adopted.publish"
            adopted = artifact(
                "publish",
                str(source),
                "--adopt",
                "notes",
                "--receipt",
                str(adopted_receipt),
                "--reviewed-revision",
                str(rev_a),
            )
            self.assertEqual(adopted["original_expectation"], rev_a)
            self.assertEqual(adopted["accepted_revision"], adopted["active_revision"])
            self.assertEqual(fetch("/notes/")[1], b"F later edit\n")

            stale = artifact(
                "restore",
                "--receipt",
                str(receipt),
                "--archive-commit",
                str(commit_e),
                code=1,
            )
            self.assertEqual(stale["pending_state"], "conflict")
            self.assertEqual(stale["accepted_revision"], rev_a)
            stale_attempt = stale["attempt_id"]
            current = publisher("status", "--name", "notes")["active_revision"]
            reviewed_restore = artifact(
                "restore",
                "--receipt",
                str(receipt),
                "--archive-commit",
                str(commit_e),
                "--reviewed-revision",
                str(current),
                "--replaces-attempt",
                str(stale_attempt),
            )
            self.assertEqual(reviewed_restore["original_expectation"], current)
            self.assertEqual(fetch("/notes/")[1], b"E frozen bytes\n")

            injected = artifacts / "fail-receipt-write.py"
            injected.write_text(
                "import pathlib, sys\n"
                "from html_publish import receipt\n"
                "from html_publish.cli import _parser\n"
                "original = receipt._write_receipt\n"
                "failed = False\n"
                "def fail_after_result(directory, state):\n"
                "    global failed\n"
                "    if state.completion is not None and not failed:\n"
                "        failed = True\n"
                "        raise receipt.PersistenceFailure('injected receipt replace failure')\n"
                "    original(directory, state)\n"
                "receipt._write_receipt = fail_after_result\n"
                "args = ['--config', sys.argv[1], 'artifact', 'publish', sys.argv[2], "
                "'--new', 'persist', '--receipt', sys.argv[3], '--json']\n"
                "parsed = _parser().parse_args(args)\n"
                "raise SystemExit(receipt.run(parsed, pathlib.Path(sys.argv[1])))\n"
            )
            persistence_source = run / "instance/persist.html"
            persistence_source.write_bytes(b"persisted host bytes\n")
            persistence_receipt = run / "persist.publish"
            installed_python = str(Path(cli).with_name("python"))
            clean_environment = os.environ.copy()
            clean_environment.pop("PYTHONPATH", None)
            failure = subprocess.run(
                [
                    installed_python,
                    str(injected),
                    str(client_config),
                    str(persistence_source),
                    str(persistence_receipt),
                ],
                cwd=artifacts,
                capture_output=True,
                text=True,
                timeout=35,
                env=clean_environment,
            )
            with log.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "injection": "receipt replace after installed publisher result",
                            "exit_code": failure.returncode,
                            "stdout": failure.stdout,
                            "stderr": failure.stderr,
                        }
                    )
                    + "\n"
                )
            self.assertEqual(failure.returncode, 1, failure.stdout + failure.stderr)
            failed_handoff = cast(dict[str, object], json.loads(failure.stdout))
            self.assertEqual(
                cast(dict[str, object], failed_handoff["error"])["code"],
                "receipt_persistence_failed",
            )
            self.assertIs(failed_handoff["receipt_persisted"], False)
            self.assertEqual(fetch("/persist/")[1], b"persisted host bytes\n")
            before_recovery = len(
                cast(list[object], publisher("history", "--name", "persist")["entries"])
            )
            recovered_persistence = artifact("retry", "--receipt", str(persistence_receipt))
            self.assertEqual(recovered_persistence["publisher_calls"], 0)
            self.assertEqual(
                len(cast(list[object], publisher("history", "--name", "persist")["entries"])),
                before_recovery,
            )
        finally:
            lifecycle("stop")
        self.assertTrue(log.is_file())
