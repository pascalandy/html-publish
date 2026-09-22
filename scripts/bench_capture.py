import argparse
import json
import os
import platform
import shlex
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--reps", type=int, default=7)
    parser.add_argument("--workdir", type=Path)
    args = parser.parse_args()
    actual_git = shutil.which("git")
    if actual_git is None:
        raise RuntimeError("git unavailable")
    results = []
    with tempfile.TemporaryDirectory(prefix="html-publish-perf-", dir=args.workdir) as temporary:
        root = Path(temporary)
        shim = root / "shim"
        shim.mkdir()
        trace = root / "git-calls.txt"
        wrapper = shim / "git"
        wrapper.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> "
            + shlex.quote(str(trace))
            + "\nexec "
            + shlex.quote(actual_git)
            + ' "$@"\n'
        )
        wrapper.chmod(0o755)
        for count in (1, 100, 2000):
            source = root / f"source-{count}"
            source.mkdir()
            (source / "index.html").write_bytes(b"<!doctype html><h1>performance fixture</h1>\n")
            for number in range(1, count):
                (source / f"file-{number:04d}.txt").write_bytes(
                    f"file-{number:04d}: deterministic fixture bytes\n".encode()
                )
            for object_format in ("sha1", "sha256"):
                times = []
                revision = None
                git_calls = None
                for attempt in range(args.reps + 2):
                    run = root / f"run-{count}-{object_format}-{attempt}"
                    run.mkdir()
                    config = run / "config.json"
                    config.write_text(
                        json.dumps(
                            {
                                "archive": str(run / "archive.git"),
                                "runtime": str(run / "runtime"),
                                "base_url": "https://example.invalid/pages/",
                                "object_format": object_format,
                                "limits": {"command_seconds": 120},
                            }
                        )
                    )
                    env = os.environ.copy()
                    env["TMPDIR"] = str(root)
                    if attempt == args.reps + 1:
                        trace.write_text("")
                        env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
                    command = [
                        str(args.cli),
                        "--config",
                        str(config),
                        "--json",
                        "plan",
                        "--name",
                        "fixture",
                        "--source",
                        str(source),
                        "--target",
                        "https://example.invalid/pages/",
                    ]
                    start = time.perf_counter()
                    completed = subprocess.run(command, capture_output=True, text=True, env=env)
                    elapsed = time.perf_counter() - start
                    if completed.returncode != 0:
                        raise RuntimeError(f"plan failed: {completed.stdout} {completed.stderr}")
                    payload = json.loads(completed.stdout)
                    if payload["file_count"] != count or payload["prediction"] != "create":
                        raise RuntimeError(f"unexpected plan: {payload}")
                    if revision is None:
                        revision = payload["requested_revision"]
                    elif revision != payload["requested_revision"]:
                        raise RuntimeError("revision changed across repetitions")
                    if 1 <= attempt <= args.reps:
                        times.append(elapsed)
                    if attempt == args.reps + 1:
                        calls = trace.read_text().splitlines()
                        git_calls = {
                            "total": len(calls),
                            "hash_object": sum("hash-object" in call for call in calls),
                            "mktree": sum("mktree" in call for call in calls),
                        }
                result = {
                    "files": count,
                    "object_format": object_format,
                    "seconds": times,
                    "median_seconds": statistics.median(times),
                    "revision": revision,
                    "git_calls": git_calls,
                }
                results.append(result)
                print(json.dumps(result), flush=True)
    report = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "fixture_parent": str(args.workdir) if args.workdir else tempfile.gettempdir(),
        "git_version": subprocess.check_output([actual_git, "--version"], text=True).strip(),
        "python_version": subprocess.check_output(
            [str(args.cli.parent / "python"), "--version"], text=True
        ).strip(),
        "warmup_runs": 1,
        "timed_runs": args.reps,
        "source_cache": "warm after fixture generation and warmup",
        "results": results,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
