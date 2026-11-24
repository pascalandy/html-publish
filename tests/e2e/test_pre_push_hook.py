from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORMATS = ("sha1", "sha256")


class PrePushHookTest(unittest.TestCase):
    def environment(self, root: Path, check_log: Path, input_log: Path) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        environment.update(
            {
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "home" / "config"),
                "XDG_CACHE_HOME": str(root / "home" / "cache"),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "HTML_PUBLISH_PRE_PUSH_CHECK_LOG": str(check_log),
                "HTML_PUBLISH_PRE_PUSH_INPUT_LOG": str(input_log),
            }
        )
        return environment

    def git(self, cwd: Path, environment: dict[str, str], *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def checked(self, log: Path) -> list[str]:
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def run_script(
        self, cwd: Path, environment: dict[str, str], payload: bytes
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / "pre-push-check.sh")],
            cwd=cwd,
            env=environment,
            input=payload,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def test_deletion_only_push_skips_check_and_other_refs_run_it(self) -> None:
        for object_format in FORMATS:
            with self.subTest(object_format=object_format):
                root = Path(
                    self.enterContext(tempfile.TemporaryDirectory(prefix="html-publish-push-"))
                )
                seed = root / "seed"
                seed.mkdir()
                bare = root / "remote.git"
                client = root / "client"
                home = root / "home"
                home.mkdir()
                check_log = root / "check.log"
                input_log = root / "pre-push-input.log"
                environment = self.environment(root, check_log, input_log)

                (seed / "index.html").write_text("<!doctype html><h1>base</h1>\n", encoding="utf-8")
                self.git(
                    root,
                    environment,
                    "init",
                    "--quiet",
                    "--initial-branch=main",
                    f"--object-format={object_format}",
                    str(seed),
                )
                self.git(seed, environment, "config", "user.name", "Hook fixture")
                self.git(seed, environment, "config", "user.email", "hook@example.invalid")
                self.git(seed, environment, "add", "index.html")
                self.git(seed, environment, "commit", "--quiet", "-m", "base")

                self.git(seed, environment, "switch", "--quiet", "--create", "feat/range-requests")
                (seed / "feature.txt").write_text("feature\n", encoding="utf-8")
                self.git(seed, environment, "add", "feature.txt")
                self.git(seed, environment, "commit", "--quiet", "-m", "feature")
                self.git(seed, environment, "switch", "--quiet", "main")
                self.git(seed, environment, "switch", "--quiet", "--create", "docs/range-requests")
                (seed / "docs.txt").write_text("docs\n", encoding="utf-8")
                self.git(seed, environment, "add", "docs.txt")
                self.git(seed, environment, "commit", "--quiet", "-m", "docs")

                self.git(root, environment, "clone", "--quiet", "--bare", str(seed), str(bare))
                self.git(
                    root,
                    environment,
                    "--git-dir",
                    str(bare),
                    "symbolic-ref",
                    "HEAD",
                    "refs/heads/main",
                )
                self.git(root, environment, "clone", "--quiet", str(bare), str(client))
                self.git(client, environment, "config", "user.name", "Hook fixture")
                self.git(client, environment, "config", "user.email", "hook@example.invalid")
                self.git(
                    client,
                    environment,
                    "branch",
                    "feat/range-requests",
                    "origin/feat/range-requests",
                )
                self.git(
                    client,
                    environment,
                    "branch",
                    "docs/range-requests",
                    "origin/docs/range-requests",
                )
                (client / "local-only.txt").write_text("local\n", encoding="utf-8")
                self.git(client, environment, "add", "local-only.txt")
                self.git(client, environment, "commit", "--quiet", "-m", "local-only")
                self.assertEqual(
                    self.git(client, environment, "rev-list", "--count", "origin/main..HEAD"), "1"
                )

                (client / "justfile").write_text(
                    "check *args:\n"
                    "    @printf '%s\\n' 'check' >> \"$HTML_PUBLISH_PRE_PUSH_CHECK_LOG\"\n"
                    '    @if [ "${HTML_PUBLISH_PRE_PUSH_CHECK_FAIL:-0}" = "1" ]; then '
                    "exit 17; fi\n",
                    encoding="utf-8",
                )
                hook = client / ".git" / "hooks" / "pre-push"
                script = shlex.quote(str(ROOT / "scripts" / "pre-push-check.sh"))
                hook.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -Eeuo pipefail\n"
                    'tee "$HTML_PUBLISH_PRE_PUSH_INPUT_LOG" | bash '
                    f'{script} "$@"\n',
                    encoding="utf-8",
                )
                hook.chmod(0o755)

                deleted = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(client),
                        "push",
                        "origin",
                        "--delete",
                        "feat/range-requests",
                        "docs/range-requests",
                    ],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(deleted.returncode, 0, deleted.stdout + deleted.stderr)
                self.assertEqual(
                    self.checked(check_log),
                    [],
                    "a deletion-only push must skip the full check even when local HEAD is ahead",
                )
                self.assertIn("pre-push: deletion-only, skipping just check", deleted.stdout)
                rows = input_log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(rows), 2)
                oid_length = 40 if object_format == "sha1" else 64
                for row in rows:
                    local_ref, local_oid, remote_ref, remote_oid = row.split()
                    self.assertEqual(local_ref, "(delete)")
                    self.assertEqual(local_oid, "0" * oid_length)
                    self.assertTrue(remote_ref.startswith("refs/heads/"))
                    self.assertEqual(len(remote_oid), oid_length)

                valid_delete_input = "".join(f"{row}\n" for row in rows).encode()
                direct_skip = self.run_script(client, environment, valid_delete_input)
                self.assertEqual(direct_skip.returncode, 0, direct_skip.stderr)
                self.assertEqual(
                    direct_skip.stdout,
                    b"pre-push: deletion-only, skipping just check\n",
                )
                self.assertEqual(direct_skip.stderr, b"")
                self.assertEqual(self.checked(check_log), [])

                for malformed in (
                    b"",
                    b"not a ref update\n",
                    b"(delete) 0000 refs/heads/missing\n",
                ):
                    before = len(self.checked(check_log))
                    result = self.run_script(client, environment, malformed)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(result.stdout, b"")
                    self.assertEqual(result.stderr, b"")
                    self.assertEqual(self.checked(check_log)[before:], ["check"])

                head_oid = self.git(client, environment, "rev-parse", "HEAD")
                update_input = f"refs/heads/main {head_oid} refs/heads/probe {head_oid}\n".encode()
                before = len(self.checked(check_log))
                quiet_success = self.run_script(client, environment, update_input)
                self.assertEqual(quiet_success.returncode, 0, quiet_success.stderr)
                self.assertEqual(quiet_success.stdout, b"")
                self.assertEqual(quiet_success.stderr, b"")
                self.assertEqual(self.checked(check_log)[before:], ["check"])

                before = len(self.checked(check_log))
                self.git(
                    seed,
                    environment,
                    "push",
                    str(bare),
                    "refs/heads/docs/range-requests:refs/heads/mixed-delete",
                )
                mixed = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(client),
                        "push",
                        "origin",
                        ":refs/heads/mixed-delete",
                        "HEAD:refs/heads/mixed-update",
                    ],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(mixed.returncode, 0, mixed.stdout + mixed.stderr)
                self.assertNotIn("deletion-only, skipping", mixed.stdout)
                self.assertEqual(self.checked(check_log)[before:], ["check"])
                mixed_rows = input_log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(mixed_rows), 2)
                self.assertEqual(sum(row.startswith("(delete) ") for row in mixed_rows), 1)

                before = len(self.checked(check_log))
                (client / "next.txt").write_text("normal update\n", encoding="utf-8")
                self.git(client, environment, "add", "next.txt")
                self.git(client, environment, "commit", "--quiet", "-m", "normal update")
                update = subprocess.run(
                    ["git", "-C", str(client), "push", "origin", "HEAD:refs/heads/mixed-update"],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(update.returncode, 0, update.stdout + update.stderr)
                self.assertEqual(self.checked(check_log)[before:], ["check"])

                self.git(client, environment, "tag", "-a", "v1", "-m", "v1")
                before = len(self.checked(check_log))
                tag = subprocess.run(
                    ["git", "-C", str(client), "push", "origin", "refs/tags/v1"],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(tag.returncode, 0, tag.stdout + tag.stderr)
                self.assertEqual(self.checked(check_log)[before:], ["check"])

                failing_environment = dict(environment)
                failing_environment["HTML_PUBLISH_PRE_PUSH_CHECK_FAIL"] = "1"
                commit = self.git(client, environment, "rev-parse", "HEAD")
                failure = self.run_script(
                    client,
                    failing_environment,
                    f"HEAD {commit} refs/heads/failing {('0' * oid_length)}\n".encode(),
                )
                self.assertNotEqual(failure.returncode, 0)
                self.assertEqual(failure.stdout, b"")
                self.assertTrue(failure.stderr)
