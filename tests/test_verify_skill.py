from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".agents/skills/verify-html-publish/scripts/instance.sh"


class VerifySkillLifecycleTest(unittest.TestCase):
    def test_stop_refuses_stale_identity_and_preserves_the_instance(self) -> None:
        run_id = f"lifecycle-safety-{os.getpid()}-{time.time_ns()}"
        run_dir = Path("/tmp/html-publish-verify") / run_id
        instance = run_dir / "instance"
        artifacts = run_dir / "artifacts"
        identity = instance / "server.identity"
        original_identity = ""
        unrelated: subprocess.Popen[bytes] | None = None

        try:
            started = self.run_helper("start", run_id)
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            original_identity = identity.read_text(encoding="utf-8")
            server_pid = json.loads(original_identity)["pid"]

            unrelated = subprocess.Popen(["sleep", "30"], start_new_session=True)
            stale = json.loads(original_identity)
            stale["pid"] = unrelated.pid
            stale["token"] = "stale-owner"
            identity.write_text(json.dumps(stale), encoding="utf-8")

            refused = self.run_helper("stop", run_id)

            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("server identity does not match", refused.stdout)
            self.assertTrue(instance.is_dir())
            self.assertTrue(identity.is_file())
            os.kill(server_pid, 0)
            self.assertIsNone(unrelated.poll())

            identity.write_text(original_identity, encoding="utf-8")
            stopped = self.run_helper("stop", run_id)
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
            self.assertFalse(instance.exists())
            self.assertTrue(artifacts.is_dir())
            self.assertIsNone(unrelated.poll())

            stopped_again = self.run_helper("stop", run_id)
            self.assertEqual(
                stopped_again.returncode, 0, stopped_again.stdout + stopped_again.stderr
            )
            self.assertIn("nothing to stop", stopped_again.stdout)
        finally:
            if original_identity and instance.exists():
                identity.write_text(original_identity, encoding="utf-8")
                self.run_helper("stop", run_id)
            if unrelated is not None and unrelated.poll() is None:
                unrelated.terminate()
                unrelated.wait(timeout=2)

    def run_helper(self, command: str, run_id: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(HELPER), command, run_id],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
