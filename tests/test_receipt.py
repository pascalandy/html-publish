from __future__ import annotations

import contextlib
import fcntl
import functools
import http.server
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast
from unittest import mock

from html_publish import receipt

# pyright: reportPrivateUsage=false

SCRIPT = Path(receipt.__file__).resolve()

FAKE_PUBLISHER = r"""#!/usr/bin/env python3
import hashlib
import json
import sys
import time
from pathlib import Path

root = Path(__file__).parent
scenario_path = root / "scenario.json"
state_path = root / "state.json"
calls_path = root / "calls.jsonl"
scenario = json.loads(scenario_path.read_text())
state = json.loads(state_path.read_text()) if state_path.exists() else {"active_revision": None}
arguments = sys.argv[1:]
operation = next(value for value in arguments if value in {"publish", "status"})

def failure(code):
    return {"code": code, "phase": "publish", "message": "fixture failure",
            "next_action": {"kind": "inspect", "required_inputs": []}}

def value(flag):
    return arguments[arguments.index(flag) + 1] if flag in arguments else None

def source_digest(path):
    source = Path(path)
    digest = hashlib.sha256()
    if source.is_file():
        digest.update(b"F")
        digest.update(source.read_bytes())
    else:
        for item in sorted(source.rglob("*"),
                           key=lambda entry: entry.relative_to(source).as_posix()):
            relative = item.relative_to(source).as_posix().encode()
            if item.is_dir():
                digest.update(b"D" + relative)
            else:
                digest.update(b"F" + relative + item.read_bytes())
    return digest.hexdigest()

name = value("--name")
target = value("--target") or scenario["target"]
if operation == "status":
    call = {"operation": "status", "name": name, "active_revision": state["active_revision"]}
    with calls_path.open("a") as stream:
        stream.write(json.dumps(call) + "\n")
    payload = {
        "schema_version": 1,
        "operation": "status",
        "request_id": None,
        "outcome": "observed",
        "target": target,
        "name": name,
        "url": f"{target}{name}/",
        "expected_revision": None,
        "requested_revision": None,
        "active_revision": state["active_revision"],
        "effects": {"archive_advanced": False, "activated": False},
        "verification": {"result": "not_checked", "revision": None},
        "error": None,
    }
    print(json.dumps(payload))
    raise SystemExit(0)

publish_count = sum(
    1 for line in calls_path.read_text().splitlines()
    if json.loads(line)["operation"] == "publish"
) if calls_path.exists() else 0
responses = scenario.get("responses", [])
mode = responses[publish_count] if publish_count < len(responses) else "auto"
source = value("--source")
digest = source_digest(source)
requested = f"rev-{digest[:20]}"
expected = value("--expected-revision")
request_id = value("--request-id")
call = {
    "operation": "publish",
    "mode": mode,
    "name": name,
    "target": target,
    "request_id": request_id,
    "expected_revision": expected,
    "requested_revision": requested,
    "source_digest": digest,
}
with calls_path.open("a") as stream:
    stream.write(json.dumps(call) + "\n")

if mode == "sleep":
    time.sleep(float(scenario.get("sleep_seconds", 0.6)))
    mode = "auto"
if mode == "ambiguous":
    state["active_revision"] = requested
    state_path.write_text(json.dumps(state))
    print("lost response")
    raise SystemExit(1)
if mode == "preflight":
    print(json.dumps({
        "schema_version": 1,
        "operation": "publish",
        "request_id": request_id,
        "outcome": "error",
        "target": target,
        "name": name,
        "url": f"{target}{name}/",
        "expected_revision": expected,
        "requested_revision": requested,
        "active_revision": state["active_revision"],
        "effects": {"archive_advanced": False, "activated": False},
        "verification": {"result": "not_checked", "revision": None},
        "error": failure("transport_failure"),
    }))
    raise SystemExit(1)

active = state["active_revision"]
outcome = "published"
activated = True
verification = "passed"
verification_revision = requested
error = None
exit_code = 0
reported_request = request_id
if mode == "conflict":
    outcome = "error"
    activated = False
    verification = "not_checked"
    error = failure("revision_conflict")
    exit_code = 1
elif mode == "active_unverified":
    state["active_revision"] = requested
    active = requested
    outcome = "error"
    verification = "failed"
    error = failure("delivery_failure")
    exit_code = 1
elif mode == "failed_unchanged":
    state["active_revision"] = requested
    active = requested
    outcome = "unchanged"
    activated = False
    verification = "failed"
    error = failure("delivery_failure")
    exit_code = 1
elif mode == "mismatch":
    reported_request = "wrong-request"
elif mode == "host_error":
    state["active_revision"] = requested
    active = requested
    outcome = "error"
    error = failure("archive_durability_failed")
    exit_code = 1
elif mode == "error_without_code":
    state["active_revision"] = requested
    active = requested
    error = {"message": "publisher reported failure without an error code"}
elif mode == "wrong_verification":
    state["active_revision"] = requested
    active = requested
    verification_revision = "different-revision"
elif mode == "unknown_effects":
    state["active_revision"] = requested
    active = requested
    outcome = "error"
    activated = None
    verification = "not_checked"
    error = failure("publication_outcome_unknown")
    exit_code = 1
elif active == requested:
    outcome = "unchanged"
    activated = False
else:
    state["active_revision"] = requested
    active = requested
state_path.write_text(json.dumps(state))
payload = {
    "schema_version": 1,
    "operation": "publish",
    "request_id": reported_request,
    "outcome": outcome,
    "target": target,
    "name": name,
    "url": f"{target}{name}/",
    "expected_revision": expected,
    "requested_revision": requested,
    "active_revision": active,
    "effects": {"archive_advanced": activated, "activated": activated},
    "verification": {
        "result": verification,
        "revision": verification_revision if verification != "not_checked" else None,
    },
    "error": error,
}
print(json.dumps(payload))
raise SystemExit(exit_code)
"""


class ReceiptFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="html-publish-receipt-test-")
        self.root = Path(self.temporary.name)
        self.fake = self.root / "fake_publisher.py"
        self.fake.write_text(FAKE_PUBLISHER)
        self.fake.chmod(0o700)
        self.scenario = self.root / "scenario.json"
        self.calls_path = self.root / "calls.jsonl"
        self.state_path = self.root / "state.json"
        self.config = self.root / "client.json"
        self.target = "https://publisher.test/html-publish/"
        self.write_scenario()
        self.write_config()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_scenario(self, *responses: str, **extra: object) -> None:
        self.scenario.write_text(
            json.dumps({"target": self.target, "responses": list(responses), **extra})
        )

    def write_config(
        self,
        *,
        target: str | None = None,
        lock_seconds: float = 1.0,
        command_seconds: float = 5,
        output_bytes: int = 64 * 1024,
    ) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "target": {"id": "fixture", "base_url": target or self.target},
                    "execution": {
                        "kind": "local",
                        "command": [sys.executable, str(self.fake)],
                        "publisher_config": str(self.root / "publisher.json"),
                    },
                    "limits": {
                        "max_bytes": 1024 * 1024,
                        "max_files": 100,
                        "copy_seconds": 5,
                        "command_seconds": command_seconds,
                        "lock_seconds": lock_seconds,
                        "output_bytes": output_bytes,
                    },
                }
            )
        )

    def run_helper(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.config), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )

    def run_main(self, *arguments: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = receipt.main(["--config", str(self.config), *arguments])
        return code, json.loads(output.getvalue())

    def payload(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertTrue(result.stdout, result.stderr)
        return json.loads(result.stdout)

    def calls(self) -> list[dict[str, object]]:
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def receipt(self, path: Path) -> dict[str, object]:
        return json.loads((path / "receipt.json").read_text())


class HelperCliTest(ReceiptFixture):
    def test_canonical_v1_config_fingerprint_remains_compatible(self) -> None:
        config = self.root / "canonical-v1.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "target": {"id": "fixture", "base_url": "https://example.test/pages/"},
                    "execution": {
                        "kind": "local",
                        "command": ["/usr/bin/html-publish"],
                        "publisher_config": "/tmp/publisher.json",
                    },
                }
            )
        )

        self.assertEqual(
            receipt.load_config(config).fingerprint,
            "4fcefde651bcce5451d70013d587a6ad03bd21048573616025e1121e28c38382",
        )

    def test_contradictory_activation_advances_acceptance_without_completion(
        self,
    ) -> None:
        for mode, name, expected_error in (
            ("host_error", "host-error", "archive_durability_failed"),
            ("wrong_verification", "wrong-verification", "delivery_failed"),
        ):
            with self.subTest(mode=mode):
                self.calls_path.unlink(missing_ok=True)
                self.state_path.unlink(missing_ok=True)
                self.write_scenario(mode)
                source = self.root / f"{name}.html"
                source.write_text(name)

                result = self.run_helper("publish", str(source), "--new", name)

                self.assertEqual(result.returncode, 1)
                payload = self.payload(result)
                self.assertEqual(payload["outcome"], "delivery_failed")
                error = payload["error"]
                assert isinstance(error, dict)
                self.assertEqual(error["code"], expected_error)
                state = self.receipt(Path(str(source) + ".publish"))
                self.assertIsNotNone(state["accepted_revision"])
                pending = state["pending"]
                assert isinstance(pending, dict)
                self.assertEqual(pending["state"], "retryable")

    def test_malformed_host_error_preserves_accepted_baseline(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.calls_path.unlink(missing_ok=True)
                self.state_path.unlink(missing_ok=True)
                source = self.root / f"malformed-{existing}.html"
                source.write_text("first")
                bundle = Path(str(source) + ".publish")
                accepted = None
                if existing:
                    self.write_scenario("auto", "error_without_code")
                    first = self.run_helper("publish", str(source), "--new", "malformed")
                    self.assertEqual(first.returncode, 0)
                    accepted = self.receipt(bundle)["accepted_revision"]
                    source.write_text("second")
                    result = self.run_helper("publish", str(source))
                else:
                    self.write_scenario("error_without_code")
                    result = self.run_helper("publish", str(source), "--new", "malformed")

                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.payload(result)["outcome"], "uncertain")
                state = self.receipt(bundle)
                self.assertEqual(state["accepted_revision"], accepted)
                pending = state["pending"]
                assert isinstance(pending, dict)
                self.assertEqual(pending["state"], "uncertain")

    def test_sigterm_stops_publisher_before_receipt_lock_is_available(self) -> None:
        for reported in (False, True):
            with self.subTest(reported=reported):
                self.calls_path.unlink(missing_ok=True)
                self.state_path.unlink(missing_ok=True)
                pid_file = self.root / f"publisher-{reported}.pid"
                marker = self.root / f"late-{reported}"
                ending = (
                    "import os\n"
                    + (
                        "print(json.dumps(payload), flush=True)\nos.close(1)\nos.close(2)\n"
                        if reported
                        else ""
                    )
                    + f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                    + "time.sleep(0.8)\n"
                    + f"Path({str(marker)!r}).write_text('late effect')\n"
                    + "time.sleep(30)\n"
                )
                self.fake.write_text(
                    FAKE_PUBLISHER.replace(
                        "print(json.dumps(payload))\nraise SystemExit(exit_code)",
                        ending,
                    )
                )
                source = self.root / f"cancel-{reported}.html"
                source.write_text("cancel")
                bundle = Path(str(source) + ".publish")
                helper = subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--config",
                        str(self.config),
                        "publish",
                        str(source),
                        "--new",
                        "cancel",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                publisher_pid = None
                try:
                    deadline = time.monotonic() + 3
                    while not pid_file.exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.002)
                    publisher_pid = int(pid_file.read_text())
                    before = self.receipt(bundle)
                    with (bundle / "lock").open("rb") as lock:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        helper.send_signal(signal.SIGTERM)
                        while True:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                self.assertLess(time.monotonic(), deadline)
                                time.sleep(0.002)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(publisher_pid, 0)
                        stdout, stderr = helper.communicate(timeout=2)
                    self.assertEqual(helper.returncode, 1, stderr)
                    payload = json.loads(stdout)
                    self.assertEqual(payload["error"]["code"], "interrupted")
                    self.assertTrue(payload["publisher"]["cancelled"])
                    self.assertFalse(payload["publisher"]["timed_out"])
                    after = self.receipt(bundle)
                    pending_before = before["pending"]
                    pending_after = after["pending"]
                    assert isinstance(pending_before, dict)
                    assert isinstance(pending_after, dict)
                    self.assertEqual(pending_after["intent"], pending_before["intent"])
                    self.assertEqual(pending_after["dispatch_generation"], 1)
                    if reported:
                        self.assertEqual(payload["publisher"]["outcome"], "published")
                        self.assertEqual(after["accepted_revision"], payload["requested_revision"])
                        self.assertIsNotNone(after["accepted_revision"])
                    else:
                        self.assertIsNone(after["accepted_revision"])
                    time.sleep(0.85)
                    self.assertFalse(marker.exists())
                finally:
                    if publisher_pid is not None:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(publisher_pid, signal.SIGKILL)
                    if helper.poll() is None:
                        helper.kill()
                        helper.wait(timeout=2)

    def test_publisher_restores_the_previous_sigterm_handler(self) -> None:
        def previous_handler(_signal: int, _frame: object) -> None:
            return

        previous = signal.signal(signal.SIGTERM, previous_handler)
        try:
            source = self.root / "handler.html"
            source.write_text("handler")
            code, payload = self.run_main("publish", str(source), "--new", "handler")
            self.assertEqual(code, 0)
            self.assertEqual(payload["outcome"], "completed")
            self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_timeout_after_activation_keeps_pending_and_kills_process(self) -> None:
        for close_streams in (False, True):
            with self.subTest(close_streams=close_streams):
                self.calls_path.unlink(missing_ok=True)
                self.state_path.unlink(missing_ok=True)
                self.fake.write_text(
                    FAKE_PUBLISHER.replace(
                        "print(json.dumps(payload))\nraise SystemExit(exit_code)",
                        "print(json.dumps(payload), flush=True)\n"
                        + ("import os\nos.close(1)\nos.close(2)\n" if close_streams else "")
                        + "time.sleep(5)\nraise SystemExit(exit_code)",
                    )
                )
                self.write_config(command_seconds=2)
                source = self.root / f"timeout-{close_streams}.html"
                source.write_text("timeout")

                started = time.monotonic()
                result = self.run_helper("publish", str(source), "--new", "timeout")

                self.assertLess(time.monotonic() - started, 4)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                payload = self.payload(result)
                publish_calls = [call for call in self.calls() if call["operation"] == "publish"]
                self.assertEqual(len(publish_calls), 1, payload)
                self.assertTrue(self.state_path.exists(), payload)
                self.assertEqual(
                    json.loads(self.state_path.read_text())["active_revision"],
                    payload["requested_revision"],
                    payload,
                )
                self.assertEqual(payload["outcome"], "delivery_failed", payload)
                error = payload["error"]
                assert isinstance(error, dict)
                self.assertEqual(error["code"], "publisher_timeout")
                state = self.receipt(Path(str(source) + ".publish"))
                self.assertIsNotNone(state["accepted_revision"])
                self.assertIsNotNone(state["pending"])

    def test_successful_parent_stops_redirected_descendant_before_unlock(self) -> None:
        marker = self.root / "child-finished"
        child_code = (
            "import time; from pathlib import Path; time.sleep(0.5); "
            f"Path({str(marker)!r}).write_text('survived')"
        )
        self.fake.write_text(
            FAKE_PUBLISHER.replace(
                "print(json.dumps(payload))\nraise SystemExit(exit_code)",
                "import subprocess\n"
                f"child_code = {child_code!r}\n"
                "subprocess.Popen([sys.executable, '-c', child_code], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "print(json.dumps(payload))\nraise SystemExit(0)",
            )
        )
        source = self.root / "redirected.html"
        source.write_text("redirected child")

        result = self.run_helper("publish", str(source), "--new", "redirected")
        time.sleep(0.65)

        self.assertEqual(result.returncode, 1)
        payload = self.payload(result)
        self.assertEqual(payload["outcome"], "delivery_failed")
        error = payload["error"]
        publisher = payload["publisher"]
        assert isinstance(error, dict) and isinstance(publisher, dict)
        self.assertEqual(error["code"], "publisher_process_group")
        self.assertEqual(publisher["exit_code"], 0)
        self.assertFalse(publisher["timed_out"])
        self.assertTrue(publisher["group_stopped"])
        state = self.receipt(Path(str(source) + ".publish"))
        self.assertIsNotNone(state["accepted_revision"])
        self.assertIsNotNone(state["pending"])
        self.assertFalse(marker.exists())

    def test_term_ignoring_descendant_is_killed_before_receipt_unlock(self) -> None:
        ready = self.root / "child-ready"
        marker = self.root / "child-survived"
        child_code = (
            "import signal, time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"Path({str(ready)!r}).write_text('ready'); "
            "time.sleep(0.7); "
            f"Path({str(marker)!r}).write_text('survived')"
        )
        self.fake.write_text(
            FAKE_PUBLISHER.replace(
                "print(json.dumps(payload))\nraise SystemExit(exit_code)",
                "import subprocess\n"
                f"child_code = {child_code!r}\n"
                "subprocess.Popen([sys.executable, '-c', child_code], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                f"ready = Path({str(ready)!r})\n"
                "until = time.monotonic() + 2\n"
                "while not ready.exists() and time.monotonic() < until: time.sleep(0.01)\n"
                "assert ready.exists()\n"
                "print(json.dumps(payload))\nraise SystemExit(0)",
            )
        )
        source = self.root / "ignores-term.html"
        source.write_text("ignores term")

        result = self.run_helper("publish", str(source), "--new", "ignores-term")
        time.sleep(0.85)

        self.assertEqual(result.returncode, 1, result.stderr)
        payload = self.payload(result)
        self.assertEqual(payload["outcome"], "delivery_failed")
        self.assertEqual(
            cast(dict[str, object], payload["error"])["code"], "publisher_process_group"
        )
        self.assertTrue(cast(dict[str, object], payload["publisher"])["group_stopped"])
        self.assertFalse(marker.exists())

    def test_uninspectable_process_group_keeps_attempt_unresolved(self) -> None:
        source = self.root / "uninspectable.html"
        source.write_text("uninspectable")

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(self.config),
                "publish",
                str(source),
                "--new",
                "uninspectable",
            ],
            env={**os.environ, "PATH": str(self.root / "no-system-tools")},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        payload = self.payload(result)
        self.assertEqual(payload["outcome"], "delivery_failed")
        self.assertEqual(
            cast(dict[str, object], payload["error"])["code"],
            "publisher_process_group_unknown",
        )
        self.assertIsNone(cast(dict[str, object], payload["publisher"])["group_stopped"])
        self.assertIsNotNone(self.receipt(Path(str(source) + ".publish"))["pending"])

    def test_running_publisher_is_stopped_when_group_inspection_fails(self) -> None:
        marker = self.root / "publisher-survived"
        self.fake.write_text(
            "import signal, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(1.4)\n"
            f"Path({str(marker)!r}).write_text('survived')\n"
        )
        self.write_config(command_seconds=0.1)
        source = self.root / "running.html"
        source.write_text("running")

        started = time.monotonic()
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(self.config),
                "publish",
                str(source),
                "--new",
                "running",
            ],
            env={**os.environ, "PATH": str(self.root / "no-system-tools")},
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        elapsed = time.monotonic() - started
        time.sleep(1.5)

        self.assertLess(elapsed, 1.0)
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = self.payload(result)
        self.assertEqual(payload["outcome"], "uncertain")
        self.assertIsNone(cast(dict[str, object], payload["publisher"])["group_stopped"])
        self.assertIsNotNone(self.receipt(Path(str(source) + ".publish"))["pending"])
        self.assertFalse(marker.exists())

    def test_timeout_terminates_descendant_process_group(self) -> None:
        marker = self.root / "child-finished"
        pid_file = self.root / "child.pid"
        self.fake.write_text(
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"marker=Path({str(marker)!r})\n"
            "child=subprocess.Popen([sys.executable, '-c', "
            'f"import time; from pathlib import Path; time.sleep(0.5); '
            "Path({str(marker)!r}).write_text('survived')\"])\n"
            f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
            "time.sleep(5)\n"
        )
        self.write_config(command_seconds=0.1)
        source = self.root / "child.html"
        source.write_text("child")

        result = self.run_helper("publish", str(source), "--new", "child-timeout")
        time.sleep(0.65)

        self.assertEqual(result.returncode, 1)
        self.assertTrue(pid_file.exists())
        self.assertFalse(marker.exists())

    def test_output_limit_stops_process_without_spooling_unbounded_bytes(self) -> None:
        marker = self.root / "flood-finished"
        self.fake.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.stdout.buffer.write(b'x' * (8 * 1024 * 1024))\n"
            "sys.stdout.buffer.flush()\n"
            f"Path({str(marker)!r}).write_text('finished')\n"
        )
        self.write_config(output_bytes=1024)
        source = self.root / "flood.html"
        source.write_text("flood")

        result = self.run_helper("publish", str(source), "--new", "output-flood")

        self.assertEqual(result.returncode, 1)
        payload = self.payload(result)
        error = payload["error"]
        assert isinstance(error, dict)
        self.assertEqual(error["code"], "publisher_output_limit")
        self.assertFalse(marker.exists())
        result_path = payload["result"]
        assert isinstance(result_path, str)
        saved = json.loads(Path(result_path).read_text())
        self.assertTrue(saved["output_limited"])
        self.assertLessEqual(len(saved["stdout"].encode()), 1024)

    def test_file_create_and_update_use_one_publish_call_each(self) -> None:
        source = self.root / "report.html"
        source.write_text("<h1>A</h1>")

        first = self.run_helper("publish", str(source), "--new", "release-notes")
        self.assertEqual(first.returncode, 0, first.stderr)
        first_payload = self.payload(first)
        self.assertEqual(first_payload["publisher_calls"], 1)
        receipt_dir = Path(str(source) + ".publish")
        first_receipt = self.receipt(receipt_dir)
        revision_a = first_receipt["accepted_revision"]

        source.write_text("<h1>B</h1>")
        second = self.run_helper("publish", str(source))

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.payload(second)["publisher_calls"], 1)
        publish_calls = [call for call in self.calls() if call["operation"] == "publish"]
        self.assertEqual(len(publish_calls), 2)
        self.assertEqual(publish_calls[1]["expected_revision"], revision_a)
        self.assertNotEqual(self.receipt(receipt_dir)["accepted_revision"], revision_a)

    def test_lost_response_retry_uses_saved_bytes_identity_and_expectation(
        self,
    ) -> None:
        self.write_scenario("ambiguous", "auto")
        source = self.root / "site"
        source.mkdir()
        (source / "index.html").write_text("A")
        receipt_dir = self.root / "portable.receipt"

        first = self.run_helper(
            "publish", str(source), "--new", "handbook", "--receipt", str(receipt_dir)
        )
        self.assertEqual(first.returncode, 1)
        pending = self.receipt(receipt_dir)["pending"]
        assert isinstance(pending, dict)
        attempt_id = cast(dict[str, object], pending["intent"])["id"]
        (source / "index.html").write_text("B")
        moved = self.root / "moved" / "receipt"
        moved.parent.mkdir()
        receipt_dir.rename(moved)

        retried = self.run_helper("retry", "--receipt", str(moved))

        self.assertEqual(retried.returncode, 0, retried.stderr)
        calls = self.calls()
        self.assertEqual([call["operation"] for call in calls], ["publish", "status", "publish"])
        self.assertEqual(calls[0]["source_digest"], calls[2]["source_digest"])
        self.assertEqual(calls[0]["request_id"], calls[2]["request_id"])
        self.assertEqual(calls[0]["expected_revision"], calls[2]["expected_revision"])
        self.assertEqual(calls[2]["request_id"], attempt_id)

    def test_conflict_observation_does_not_advance_baseline_and_review_replaces_it(
        self,
    ) -> None:
        source = self.root / "report.html"
        source.write_text("A")
        receipt_dir = Path(str(source) + ".publish")
        self.assertEqual(
            self.run_helper("publish", str(source), "--new", "report").returncode,
            0,
        )
        baseline = self.receipt(receipt_dir)["accepted_revision"]
        competitor = "rev-reviewed-competitor"
        self.state_path.write_text(json.dumps({"active_revision": competitor}))
        self.write_scenario("auto", "conflict", "auto")
        source.write_text("B")

        conflict = self.run_helper("publish", str(source))

        self.assertEqual(conflict.returncode, 1)
        conflicted = self.receipt(receipt_dir)
        pending = conflicted["pending"]
        observation = conflicted["last_observation"]
        assert isinstance(pending, dict) and isinstance(observation, dict)
        self.assertEqual(conflicted["accepted_revision"], baseline)
        self.assertEqual(pending["state"], "conflict")
        intent = cast(dict[str, object], pending["intent"])
        assert isinstance(intent, dict)
        attempt_id = intent["id"]
        assert isinstance(attempt_id, str)
        self.assertEqual(observation["active_revision"], competitor)

        reviewed = self.run_helper(
            "publish",
            str(source),
            "--reviewed-revision",
            competitor,
            "--replaces-attempt",
            attempt_id,
        )

        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        self.assertEqual(
            [call for call in self.calls() if call["operation"] == "publish"][-1][
                "expected_revision"
            ],
            competitor,
        )

    def test_activation_with_failed_verification_advances_and_remains_retryable(
        self,
    ) -> None:
        self.write_scenario("active_unverified", "auto")
        source = self.root / "report.html"
        source.write_text("delivery")
        receipt_dir = Path(str(source) + ".publish")

        failed = self.run_helper("publish", str(source), "--new", "delivery")

        self.assertEqual(failed.returncode, 1)
        state = self.receipt(receipt_dir)
        pending = state["pending"]
        assert isinstance(pending, dict)
        self.assertIsNotNone(state["accepted_revision"])
        self.assertEqual(pending["state"], "retryable")
        intent = cast(dict[str, object], pending["intent"])
        assert isinstance(intent, dict)
        attempt_id = intent["id"]

        retried = self.run_helper("retry", "--receipt", str(receipt_dir))

        self.assertEqual(retried.returncode, 0, retried.stderr)
        calls = self.calls()
        self.assertEqual([call["operation"] for call in calls], ["publish", "publish"])
        self.assertEqual(calls[0]["request_id"], calls[1]["request_id"])
        self.assertEqual(calls[1]["request_id"], attempt_id)

    def test_failed_identical_verification_does_not_advance_acceptance(self) -> None:
        self.write_scenario("failed_unchanged")
        source = self.root / "report.html"
        source.write_text("same")

        result = self.run_helper("publish", str(source), "--new", "same")

        self.assertEqual(result.returncode, 1)
        state = self.receipt(Path(str(source) + ".publish"))
        pending = state["pending"]
        assert isinstance(pending, dict)
        self.assertIsNone(state["accepted_revision"])
        self.assertEqual(pending["state"], "uncertain")

    def test_mismatched_result_cannot_change_receipt(self) -> None:
        self.write_scenario("mismatch")
        source = self.root / "report.html"
        source.write_text("mismatch")

        result = self.run_helper("publish", str(source), "--new", "mismatch")

        self.assertEqual(result.returncode, 1)
        state = self.receipt(Path(str(source) + ".publish"))
        pending = state["pending"]
        assert isinstance(pending, dict)
        self.assertIsNone(state["accepted_revision"])
        self.assertEqual(pending["state"], "uncertain")

    def test_preinvocation_transport_failure_is_retryable_without_status(self) -> None:
        self.write_scenario("preflight", "auto")
        source = self.root / "report.html"
        source.write_text("preflight")
        receipt_dir = Path(str(source) + ".publish")

        failed = self.run_helper("publish", str(source), "--new", "preflight")

        self.assertEqual(failed.returncode, 1)
        state = self.receipt(receipt_dir)
        pending = state["pending"]
        assert isinstance(pending, dict)
        self.assertEqual(pending["state"], "retryable")

        retried = self.run_helper("retry", "--receipt", str(receipt_dir))

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual([call["operation"] for call in self.calls()], ["publish", "publish"])

    def test_correlated_unknown_effects_remain_uncertain(self) -> None:
        self.write_scenario("unknown_effects")
        source = self.root / "report.html"
        source.write_text("unknown")

        result = self.run_helper("publish", str(source), "--new", "unknown")

        self.assertEqual(result.returncode, 1)
        state = self.receipt(Path(str(source) + ".publish"))
        pending = state["pending"]
        assert isinstance(pending, dict)
        self.assertEqual(pending["state"], "uncertain")
        self.assertIsNone(state["accepted_revision"])

    def test_adoption_observes_explicit_name_and_reviewed_revision(self) -> None:
        reviewed = "rev-reviewed"
        self.state_path.write_text(json.dumps({"active_revision": reviewed}))
        source = self.root / "adopt.html"
        source.write_text("adopted")
        receipt_dir = self.root / "adopted.receipt"

        result = self.run_helper(
            "publish",
            str(source),
            "--adopt",
            "known-page",
            "--reviewed-revision",
            reviewed,
            "--receipt",
            str(receipt_dir),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call["operation"] for call in calls], ["status", "publish"])
        self.assertEqual(calls[1]["expected_revision"], reviewed)

    def test_source_and_receipt_overlap_is_rejected_without_mutation(self) -> None:
        source = self.root / "site"
        source.mkdir()
        (source / "index.html").write_text("unsafe")
        receipt_dir = source / "receipt"

        result = self.run_helper(
            "publish", str(source), "--new", "overlap", "--receipt", str(receipt_dir)
        )

        self.assertEqual(result.returncode, 1)
        self.assertFalse(receipt_dir.exists())
        self.assertEqual(self.calls(), [])

    def test_target_drift_fails_before_dispatch(self) -> None:
        source = self.root / "report.html"
        source.write_text("A")
        receipt_dir = Path(str(source) + ".publish")
        self.assertEqual(self.run_helper("publish", str(source), "--new", "drift").returncode, 0)
        call_count = len(self.calls())
        self.write_config(target="https://other.test/html-publish/")

        result = self.run_helper("status", "--receipt", str(receipt_dir))

        self.assertEqual(result.returncode, 1)
        error = self.payload(result)["error"]
        assert isinstance(error, dict)
        self.assertEqual(error["code"], "target_drift")
        self.assertEqual(len(self.calls()), call_count)

    def test_limit_changes_do_not_change_binding_identity(self) -> None:
        source = self.root / "limits.html"
        source.write_text("limits")
        receipt_dir = Path(str(source) + ".publish")
        self.assertEqual(
            self.run_helper("publish", str(source), "--new", "limits").returncode,
            0,
        )
        self.write_config(command_seconds=7, output_bytes=32 * 1024)

        result = self.run_helper("status", "--receipt", str(receipt_dir))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.payload(result)["outcome"], "observed")

    def test_local_only_does_not_create_receipt_or_call_publisher(self) -> None:
        source = self.root / "report.html"
        source.write_text("local")

        result = self.run_helper("publish", str(source), "--new", "local", "--local-only")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.payload(result)["outcome"], "local_only")
        self.assertFalse(Path(str(source) + ".publish").exists())
        self.assertEqual(self.calls(), [])

    def test_concurrent_writer_is_rejected_without_second_publish(self) -> None:
        self.write_scenario("sleep", sleep_seconds=0.8)
        self.write_config(lock_seconds=0.1)
        source = self.root / "report.html"
        source.write_text("lock")
        first = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(self.config),
                "publish",
                str(source),
                "--new",
                "lock",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 2
        while not self.calls_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)

        second = self.run_helper("publish", str(source))
        first_stdout, first_stderr = first.communicate(timeout=5)

        self.assertEqual(first.returncode, 0, first_stderr)
        self.assertTrue(first_stdout)
        self.assertEqual(second.returncode, 1)
        error = self.payload(second)["error"]
        assert isinstance(error, dict)
        self.assertEqual(error["code"], "receipt_busy")
        self.assertEqual(len([call for call in self.calls() if call["operation"] == "publish"]), 1)

    def test_symlink_input_is_rejected_before_dispatch(self) -> None:
        source = self.root / "real.html"
        source.write_text("real")
        link = self.root / "link.html"
        link.symlink_to(source)

        result = self.run_helper("publish", str(link), "--new", "unsafe")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls(), [])

    def test_unsafe_persisted_attempt_ids_fail_before_retry(self) -> None:
        self.write_scenario("ambiguous")
        source = self.root / "attempt.html"
        source.write_text("one")
        receipt_dir = Path(str(source) + ".publish")
        self.assertEqual(self.run_helper("publish", str(source), "--new", "attempt").returncode, 1)
        state = self.receipt(receipt_dir)
        pending = cast(dict[str, object], state["pending"])
        intent = cast(dict[str, object], pending["intent"])
        intent["id"] = "../../escape"
        (receipt_dir / "receipt.json").write_text(json.dumps(state))

        result = self.run_helper("retry", "--receipt", str(receipt_dir))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            cast(dict[str, object], self.payload(result)["error"])["code"], "invalid_state"
        )
        self.assertEqual(len([call for call in self.calls() if call["operation"] == "publish"]), 1)

    def test_remote_config_builds_the_existing_transport_command(self) -> None:
        remote_config = self.root / "remote-client.json"
        remote_config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "target": {"id": "om1", "base_url": self.target},
                    "execution": {
                        "kind": "remote",
                        "command": ["/stable/html-publish-remote"],
                        "host": "pascal@om1.example",
                        "remote_executable": "/stable/html-publish",
                        "remote_config": "/stable/publisher.json",
                        "incoming_root": "/stable/incoming",
                        "connect_timeout": 7,
                    },
                }
            )
        )
        config = receipt.load_config(remote_config)

        command = receipt._executor_command(
            config,
            "publish",
            "page",
            Path("/tmp/snapshot"),
            "revision-a",
            "attempt-a",
        )

        self.assertEqual(command[0], "/stable/html-publish-remote")
        self.assertIn("pascal@om1.example", command)
        self.assertIn("/stable/html-publish", command)
        self.assertIn("/tmp/snapshot", command)
        self.assertNotIn("ssh", command)


class PersistenceRecoveryTest(ReceiptFixture):
    def test_cleanup_failure_keeps_publication_success_and_reports_path(self) -> None:
        source = self.root / "report.html"
        source.write_text("cleanup")
        receipt_dir = Path(str(source) + ".publish")

        with mock.patch.object(
            receipt.shutil, "rmtree", side_effect=OSError("injected cleanup failure")
        ):
            code, payload = self.run_main("publish", str(source), "--new", "cleanup")

        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "completed")
        self.assertIsNotNone(payload["cleanup_pending"])
        state = receipt.load_receipt(receipt_dir)
        classification = receipt.Classification(state, "completed", "cleanup")
        self.assertIsNone(receipt._cleanup_completed_attempt(receipt_dir, classification))

    def test_result_saved_before_receipt_replace_recovers_without_publish(self) -> None:
        source = self.root / "report.html"
        source.write_text("recover")
        receipt_dir = Path(str(source) + ".publish")
        real_replace = receipt.os.replace
        failed = False

        def fail_final_receipt_replace(
            source_path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            destination_path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        ) -> None:
            nonlocal failed
            destination = Path(os.fsdecode(destination_path))
            if (
                not failed
                and destination == receipt_dir / "receipt.json"
                and any(receipt_dir.glob("attempt-*/result.json"))
            ):
                failed = True
                raise OSError("injected receipt replace failure")
            real_replace(source_path, destination_path)

        with mock.patch.object(receipt.os, "replace", side_effect=fail_final_receipt_replace):
            code, payload = self.run_main("publish", str(source), "--new", "recover")

        self.assertEqual(code, 1)
        self.assertFalse(payload["receipt_persisted"])
        self.assertEqual(payload["url"], f"{self.target}recover/")
        self.assertIsNotNone(payload["requested_revision"])
        self.assertEqual(payload["active_revision"], payload["requested_revision"])
        self.assertIsNone(payload["accepted_revision"])
        self.assertEqual(len([call for call in self.calls() if call["operation"] == "publish"]), 1)

        retry_code, retry_payload = self.run_main("retry", "--receipt", str(receipt_dir))

        self.assertEqual(retry_code, 0)
        self.assertEqual(retry_payload["publisher_calls"], 0)
        self.assertEqual(len([call for call in self.calls() if call["operation"] == "publish"]), 1)

    def test_result_save_failure_still_reports_bounded_host_facts(self) -> None:
        source = self.root / "result-save.html"
        source.write_text("result save")

        with mock.patch.object(
            receipt,
            "_save_result",
            side_effect=receipt.PersistenceFailure("injected result save failure"),
        ):
            code, payload = self.run_main("publish", str(source), "--new", "result-save")

        self.assertEqual(code, 1)
        self.assertFalse(payload["receipt_persisted"])
        self.assertEqual(payload["url"], f"{self.target}result-save/")
        self.assertIsNotNone(payload["requested_revision"])
        self.assertEqual(payload["active_revision"], payload["requested_revision"])
        self.assertIsNone(payload["accepted_revision"])
        result_path = payload["result"]
        assert isinstance(result_path, str)
        self.assertFalse(Path(result_path).exists())

    def test_renamed_but_unsynced_completion_recovers_without_publish(self) -> None:
        source = self.root / "report.html"
        source.write_text("recover")
        receipt_dir = Path(str(source) + ".publish")
        real_sync = receipt._sync_directory
        failed = False

        def fail_final_receipt_sync(path: Path) -> None:
            nonlocal failed
            if not failed and path == receipt_dir and (receipt_dir / "receipt.json").exists():
                raw = json.loads((receipt_dir / "receipt.json").read_text())
                if raw.get("completion") and any(receipt_dir.glob("attempt-*/result.json")):
                    failed = True
                    raise OSError("injected directory sync failure")
            real_sync(path)

        with mock.patch.object(receipt, "_sync_directory", side_effect=fail_final_receipt_sync):
            code, payload = self.run_main("publish", str(source), "--new", "recover-sync")

        self.assertEqual(code, 1)
        self.assertFalse(payload["receipt_persisted"])
        visible = self.receipt(receipt_dir)
        self.assertIsNotNone(visible["completion"])

        retry_code, retry_payload = self.run_main("retry", "--receipt", str(receipt_dir))

        self.assertEqual(retry_code, 0)
        self.assertEqual(retry_payload["publisher_calls"], 0)
        self.assertEqual(len([call for call in self.calls() if call["operation"] == "publish"]), 1)

    def test_stale_saved_result_cannot_overwrite_newer_completion(self) -> None:
        source = self.root / "report.html"
        source.write_text("first")
        receipt_dir = Path(str(source) + ".publish")
        self.assertEqual(self.run_helper("publish", str(source), "--new", "stale").returncode, 0)
        current = receipt.load_receipt(receipt_dir)
        stale_dir = receipt_dir / "attempt-stale"
        stale_dir.mkdir()
        stale = receipt.SavedResult("stale", 1, "stale-digest", 0, False, False, "{}", "", {})
        (stale_dir / "result.json").write_text(json.dumps(receipt.saved_result_dict(stale)))
        before = (receipt_dir / "receipt.json").read_bytes()

        recovered = receipt._recover_saved_result(receipt_dir, current)

        self.assertFalse(recovered.local_completion)
        self.assertEqual((receipt_dir / "receipt.json").read_bytes(), before)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


@unittest.skipUnless(
    os.environ.get("HTML_PUBLISH_HOST_CHECKOUT"),
    "set HTML_PUBLISH_HOST_CHECKOUT to run the real publisher proof",
)
class RealLoopbackPublisherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="html-publish-receipt-loopback-")
        self.root = Path(self.temporary.name)
        self.host_checkout = Path(os.environ["HTML_PUBLISH_HOST_CHECKOUT"]).resolve()
        self.archive = self.root / "archive.git"
        self.runtime = self.root / "runtime"
        handler = functools.partial(QuietHandler, directory=str(self.runtime / "public"))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        address = self.server.server_address
        host, port = str(address[0]), int(address[1])
        self.base_url = f"http://{host}:{port}/"
        self.publisher_config = self.root / "publisher.json"
        self.publisher_config.write_text(
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
            )
        )
        self.client_config = self.root / "client.json"
        self.client_config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "target": {"id": "loopback", "base_url": self.base_url},
                    "execution": {
                        "kind": "local",
                        "command": [sys.executable, "-m", "html_publish"],
                        "publisher_config": str(self.publisher_config),
                    },
                    "limits": {"command_seconds": 15, "lock_seconds": 2},
                }
            )
        )
        self.env = os.environ.copy()
        existing = self.env.get("PYTHONPATH")
        self.env["PYTHONPATH"] = (
            str(self.host_checkout)
            if not existing
            else f"{self.host_checkout}{os.pathsep}{existing}"
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def run_helper(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(self.client_config),
                *arguments,
            ],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )

    def test_file_and_directory_revision_through_real_cli_and_http(self) -> None:
        page = self.root / "page.html"
        page.write_text("<!doctype html><h1>file</h1>")
        file_result = self.run_helper("publish", str(page), "--new", "real-file")
        self.assertEqual(file_result.returncode, 0, file_result.stderr)
        with urllib.request.urlopen(f"{self.base_url}real-file/", timeout=2) as response:
            self.assertEqual(response.read(), page.read_bytes())

        site = self.root / "site"
        site.mkdir()
        (site / "index.html").write_text("<!doctype html><h1>A</h1>")
        assets = site / "assets"
        assets.mkdir()
        removed = assets / "removed.css"
        removed.write_text("body{}")
        first = self.run_helper("publish", str(site), "--new", "real-directory")
        self.assertEqual(first.returncode, 0, first.stderr)
        removed.unlink()
        (site / "index.html").write_text("<!doctype html><h1>B</h1>")

        second = self.run_helper("publish", str(site))

        self.assertEqual(second.returncode, 0, second.stderr)
        with urllib.request.urlopen(f"{self.base_url}real-directory/", timeout=2) as response:
            self.assertIn(b"<h1>B</h1>", response.read())
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{self.base_url}real-directory/assets/removed.css", timeout=2)
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
