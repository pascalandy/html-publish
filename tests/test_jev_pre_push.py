"""Exercise the installed advisory hook through pushes to a disposable local remote."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AdvisoryHookTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("lefthook"), "requires lefthook")
    def test_advisory_hook(self) -> None:
        lefthook = shutil.which("lefthook")
        self.assertIsNotNone(lefthook, "Install lefthook to prove the pre-push hook")
        with tempfile.TemporaryDirectory(prefix="jev-hook-proof-") as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            remote = root / "remote.git"
            commands = root / "commands"
            commands.mkdir()
            log = root / "gate-args"
            fake = commands / "just"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import os, pathlib, sys\n"
                "pathlib.Path(os.environ['JEV_PROOF_LOG']).write_text(' '.join(sys.argv[1:]))\n"
                "code = int(os.environ['JEV_PROOF_EXIT'])\n"
                "print('controlled gate result', code)\n"
                "sys.exit(code)\n"
            )
            fake.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
                "JEV_PROOF_LOG": str(log),
                "JEV_PROOF_EXIT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "Jev proof",
                "GIT_COMMITTER_NAME": "Jev proof",
                "GIT_AUTHOR_EMAIL": "proof@example.invalid",
                "GIT_COMMITTER_EMAIL": "proof@example.invalid",
            }

            def run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    args,
                    cwd=work,
                    env=environment,
                    input=input_text,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=120,
                )

            def git(*args: str) -> str:
                return run("git", *args).stdout.strip()

            git("init", "--bare", str(remote))
            git("init", "-b", "main")
            git("commit", "--allow-empty", "-m", "Initial")
            git("remote", "add", "proof", str(remote))
            git("push", "proof", "main")
            (work / "scripts").mkdir()
            shutil.copy2(ROOT / "scripts/jev_pre_push.py", work / "scripts/jev_pre_push.py")
            shutil.copy2(ROOT / "lefthook.yml", work / "lefthook.yml")
            unrelated = work / ".git/hooks/post-commit"
            unrelated.write_text("#!/bin/sh\nexit 0\n")
            unrelated.chmod(0o755)
            run(str(lefthook), "install")
            self.assertEqual(unrelated.read_text(), "#!/bin/sh\nexit 0\n")
            git("add", "scripts", "lefthook.yml")
            git("commit", "-m", "Install advisory hook")
            for code, verdict in (
                (0, "pass"),
                (10, "escalate"),
                (11, "block"),
                (12, "insufficient"),
                (1, "engine error"),
            ):
                with self.subTest(exit=code):
                    base = git("ls-remote", "proof", "refs/heads/main").split()[0]
                    git("commit", "--allow-empty", "-m", f"Proof exit {code}")
                    environment["JEV_PROOF_EXIT"] = str(code)
                    result = run("git", "push", "proof", "main")
                    self.assertEqual(result.returncode, 0)
                    self.assertIn(
                        f"{verdict} (exit {code}); push allowed", result.stdout + result.stderr
                    )
                    self.assertEqual(log.read_text(), f"jev-merge --base {base}")
                    self.assertEqual(
                        git("ls-remote", "proof", "refs/heads/main").split()[0],
                        git("rev-parse", "HEAD"),
                    )
                    print(f"A32: gate exit {code}, git push exit 0, outgoing base {base}")
            log.unlink()
            for destination in ("refs/heads/first", "refs/tags/proof"):
                result = run("git", "push", "proof", f"HEAD:{destination}")
                self.assertIn("insufficient coverage", result.stdout + result.stderr)
                self.assertFalse(log.exists())
            git("branch", "other", "HEAD~1")
            result = run("git", "push", "proof", "other")
            self.assertIn("not checked-out HEAD", result.stdout + result.stderr)
            head = git("rev-parse", "HEAD")
            result = run(
                sys.executable,
                "scripts/jev_pre_push.py",
                input_text=f"refs/heads/main {head} refs/heads/main {'1' * 40}\n",
            )
            self.assertIn("fetch its remote history", result.stdout)
            self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
