import argparse
import functools
import http.server
import json
import os
import platform
import shlex
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--workdir", type=Path)
    args = parser.parse_args()
    actual_git = shutil.which("git")
    if actual_git is None:
        raise RuntimeError("git unavailable")
    with tempfile.TemporaryDirectory(prefix="html-publish-publish-", dir=args.workdir) as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        files = {"index.html": b"<!doctype html><h1>publish benchmark</h1>\n"}
        for number in range(1, 100):
            files[f"file-{number:04d}.txt"] = (
                f"file-{number:04d}: deterministic fixture bytes\n".encode()
            )
        for name, contents in files.items():
            (source / name).write_bytes(contents)
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
        times = []
        git_calls = None
        revision = None
        for attempt in range(args.reps + 2):
            run = root / f"run-{attempt}"
            run.mkdir()
            runtime = run / "runtime"
            handler = functools.partial(QuietHandler, directory=str(runtime / "public"))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server_thread = threading.Thread(target=server.serve_forever)
            server_thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}/"
                config = run / "config.json"
                config.write_text(
                    json.dumps(
                        {
                            "archive": str(run / "archive.git"),
                            "runtime": str(runtime),
                            "base_url": base_url,
                            "allow_http": True,
                            "object_format": "sha1",
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
                    "publish",
                    "--name",
                    "fixture",
                    "--source",
                    str(source),
                    "--target",
                    base_url,
                ]
                start = time.perf_counter()
                completed = subprocess.run(command, text=True, capture_output=True, env=env)
                elapsed = time.perf_counter() - start
                if completed.returncode:
                    raise RuntimeError(f"publish failed: {completed.stdout} {completed.stderr}")
                payload = json.loads(completed.stdout)
                if payload["outcome"] != "published":
                    raise RuntimeError(f"unexpected publish: {payload}")
                if payload["verification"]["result"] != "passed":
                    raise RuntimeError(f"verification failed: {payload}")
                if revision is None:
                    revision = payload["active_revision"]
                elif revision != payload["active_revision"]:
                    raise RuntimeError("revision changed across repetitions")
                if 1 <= attempt <= args.reps:
                    times.append(elapsed)
                if attempt == args.reps + 1:
                    calls = trace.read_text().splitlines()
                    git_calls = {
                        "total": len(calls),
                        "hash_object": sum("hash-object" in call for call in calls),
                        "cat_file": sum("cat-file" in call for call in calls),
                    }
                with urllib.request.urlopen(base_url + "fixture/", timeout=5) as response:
                    if response.read() != files["index.html"]:
                        raise RuntimeError("served index bytes differ")
                with urllib.request.urlopen(
                    base_url + "fixture/file-0099.txt", timeout=5
                ) as response:
                    if response.read() != files["file-0099.txt"]:
                        raise RuntimeError("served asset bytes differ")
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join()
    report = {
        "platform": platform.platform(),
        "git_version": subprocess.check_output([actual_git, "--version"], text=True).strip(),
        "python_version": subprocess.check_output(
            [str(args.cli.parent / "python"), "--version"], text=True
        ).strip(),
        "files": 100,
        "warmup_runs": 1,
        "timed_runs": args.reps,
        "seconds": times,
        "median_seconds": statistics.median(times),
        "revision": revision,
        "git_calls": git_calls,
        "http": "full CLI verification plus served index and asset bytes",
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
