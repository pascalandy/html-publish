"""Restore every page from a clone of the publication archive into a throwaway publisher."""

import argparse
import hashlib
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

LIVE_ARCHIVE = Path.home() / ".local/share/html-publish/archive.git"
INSTALLED_CLI = Path.home() / ".local/share/html-publish/current/.venv/bin/html-publish"
GIT_ENVIRONMENT = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(git_dir: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "--git-dir", str(git_dir), *arguments],
        env=GIT_ENVIRONMENT,
        check=True,
        capture_output=True,
    ).stdout


def git_text(git_dir: Path, *arguments: str) -> str:
    return git(git_dir, *arguments).decode().strip()


def source_state(source: str) -> dict[str, str] | None:
    path = Path(source)
    if not path.is_dir():
        return None
    return {
        "refs": git_text(path, "for-each-ref", "--format=%(refname) %(objectname)"),
        "head": (path / "HEAD").read_text(),
        "config_sha256": hashlib.sha256((path / "config").read_bytes()).hexdigest(),
    }


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def site_files(clone: Path, commit: str, name: str) -> list[tuple[str, str]]:
    listing = git(clone, "ls-tree", "-r", "-z", f"{commit}:{name}/site").decode()
    files: list[tuple[str, str]] = []
    for entry in filter(None, listing.split("\0")):
        header, path = entry.split("\t", 1)
        files.append((header.split()[2], path))
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(LIVE_ARCHIVE), help="archive path or Git URL")
    parser.add_argument("--run", type=Path, required=True, help="new directory for this run")
    parser.add_argument("--executable", type=Path, default=INSTALLED_CLI)
    parser.add_argument("--record", type=Path, required=True, help="JSON record to write")
    args = parser.parse_args()

    run: Path = args.run.resolve()
    run.mkdir(parents=True)
    executable = str(args.executable)
    started = time.monotonic()
    before = source_state(args.source)

    clone = run / "archive.git"
    clone_command = ["git", "clone", "--quiet", "--bare", "--no-local", "--branch", "published"]
    subprocess.run([*clone_command, args.source, str(clone)], env=GIT_ENVIRONMENT, check=True)
    tip_before = git_text(clone, "rev-parse", "published")

    port = free_port()
    target = f"http://127.0.0.1:{port}/"
    config = run / "publisher.json"
    subprocess.run(
        [
            *(executable, "config", "init", "--role", "publisher", "--config", str(config)),
            *("--archive", str(clone), "--runtime", str(run / "runtime")),
            *("--base-url", target, "--allow-http"),
        ],
        check=True,
        capture_output=True,
    )

    def command(*arguments: str) -> dict[str, object]:
        result = subprocess.run(
            [executable, "--config", str(config), "--json", *arguments],
            capture_output=True,
            text=True,
            timeout=600,
        )
        report: dict[str, object] = json.loads(result.stdout)
        report["exit_code"] = result.returncode
        return report

    def restore(name: str, commit: str, attempt: str) -> dict[str, object]:
        return command(
            "restore", "--name", name, "--archive-commit", commit, "--target", target
        ) | {"attempt": attempt}

    with (run / "server.log").open("wb") as log:
        server = subprocess.Popen(
            [executable, "--config", str(config), "host", "serve", "--port", str(port)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(f"{target}_html-publish-health", timeout=2) as reply:
                    if reply.status == 200:
                        break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

        names = [
            line.split("\t", 1)[1]
            for line in git_text(clone, "ls-tree", "published").splitlines()
            if line.split()[1] == "tree"
        ]
        pages: list[dict[str, object]] = []
        total_files = 0
        total_bytes = 0
        for index, name in enumerate(names, 1):
            commit = git_text(clone, "log", "-1", "--format=%H", "published", "--", name)
            revision = git_text(clone, "rev-parse", f"{commit}:{name}/site")
            first = restore(name, commit, "first")
            verification = first.get("verification")
            verified = isinstance(verification, dict) and verification.get("result") == "passed"
            served_bytes = 0
            mismatched = 0
            files = site_files(clone, commit, name)
            for blob, path in files:
                expected = hashlib.sha256(git(clone, "cat-file", "blob", blob)).hexdigest()
                address = target + urllib.parse.quote(f"{name}/{path}")
                with urllib.request.urlopen(address, timeout=120) as reply:
                    body = reply.read()
                served_bytes += len(body)
                mismatched += hashlib.sha256(body).hexdigest() != expected
            total_files += len(files)
            total_bytes += served_bytes
            second = restore(name, commit, "second")
            page = {
                "page": f"page-{index:02d}",
                "revision": revision,
                "archive_commit": commit,
                "first_exit_code": first["exit_code"],
                "first_outcome": first.get("outcome"),
                "first_verification_passed": verified,
                "active_revision_matches": first.get("active_revision") == revision,
                "files": len(files),
                "served_bytes": served_bytes,
                "mismatched_files": mismatched,
                "second_exit_code": second["exit_code"],
                "second_outcome": second.get("outcome"),
            }
            pages.append(page)
            print(name, json.dumps(page), flush=True)

        observations: list[dict[str, dict[str, str]]] = []
        status_exit_codes: list[object] = []
        after: list[str] = []
        while True:
            status = command("status", "--limit", "100", *after)
            status_exit_codes.append(status["exit_code"])
            entries = status.get("entries")
            if isinstance(entries, list):
                observations += [entry["observation"] for entry in entries]
            if not status.get("truncated"):
                break
            after = ["--after", str(status["continuation"])]
        all_selected = len(observations) == len(names) and all(
            observation["selection"]["state"] == "selected"
            and observation["selection"]["revision"] == observation["saved"]["revision"]
            for observation in observations
        )
        tip_after = git_text(clone, "rev-parse", "published")
    finally:
        server.send_signal(signal.SIGTERM)
        server.wait(timeout=15)

    after = source_state(args.source)
    checks = {
        "every_first_restore_exit_0": all(page["first_exit_code"] == 0 for page in pages),
        "every_first_restore_verified": all(page["first_verification_passed"] for page in pages),
        "every_active_revision_matches": all(page["active_revision_matches"] for page in pages),
        "every_served_file_matches_its_blob": all(page["mismatched_files"] == 0 for page in pages),
        "every_second_restore_unchanged": all(
            page["second_exit_code"] == 0 and page["second_outcome"] == "unchanged"
            for page in pages
        ),
        "status_shows_every_page_selected": set(status_exit_codes) == {0} and all_selected,
        "clone_branch_tip_unchanged": tip_before == tip_after,
        "source_config_and_refs_unchanged": before is None or before == after,
    }
    version = subprocess.run([executable, "--version"], capture_output=True, text=True)
    record = {
        "schema": "html-publish-restore-drill/1",
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": platform.node(),
        "python": platform.python_version(),
        "git": subprocess.run(
            ["git", "--version"], capture_output=True, text=True, check=True
        ).stdout.strip(),
        "executable": executable,
        "executable_version": version.stdout.strip(),
        "source_is_local_path": before is not None,
        "clone": "git clone --bare --no-local --branch published",
        "archive_tip": tip_before,
        "pages": pages,
        "totals": {"pages": len(pages), "files": total_files, "served_bytes": total_bytes},
        "checks": checks,
        "seconds": round(time.monotonic() - started, 1),
    }
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"checks": checks, "totals": record["totals"]}, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
