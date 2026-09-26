"""Report Jev's advice for each pushed checked-out HEAD; never reject a push."""

from __future__ import annotations

import subprocess
import sys


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()


def main() -> int:
    try:
        head = git("rev-parse", "HEAD")
        branch = git("symbolic-ref", "-q", "HEAD")
        updates = list(sys.stdin)
        if not updates:
            print("jevgate advisory: insufficient coverage; no outgoing refs")
        for line in updates:
            fields = line.split()
            if len(fields) != 4:
                print("jevgate advisory: insufficient coverage; malformed outgoing ref")
                continue
            local_ref, local_sha, remote_ref, remote_sha = fields
            if (
                local_sha != head
                or local_ref not in (branch, "HEAD")
                or not remote_ref.startswith("refs/heads/")
            ):
                print(
                    f"jevgate advisory: insufficient coverage for {remote_ref}; "
                    "not checked-out HEAD"
                )
                continue
            if set(remote_sha) == {"0"}:
                print(
                    f"jevgate advisory: insufficient coverage for {remote_ref}; "
                    "first push has no base"
                )
                continue
            try:
                git("cat-file", "-e", f"{remote_sha}^{{commit}}")
            except subprocess.CalledProcessError:
                print(
                    f"jevgate advisory: insufficient coverage for {remote_ref}; "
                    "fetch its remote history"
                )
                continue
            print(
                f"jevgate advisory: judging {local_sha} against outgoing base {remote_sha}",
                flush=True,
            )
            result = subprocess.run(
                ["just", "jev-merge", "--base", remote_sha],
                stdin=subprocess.DEVNULL,
                check=False,
                timeout=3900,
            )
            verdict = {0: "pass", 10: "escalate", 11: "block", 12: "insufficient"}.get(
                result.returncode, "engine error"
            )
            print(f"jevgate advisory: {verdict} (exit {result.returncode}); push allowed")
    except (OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"jevgate advisory: engine error ({error}); push allowed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
