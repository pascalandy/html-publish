"""Time `html-publish publish` of a 100-file fixture with loopback HTTP verification."""

import argparse
import functools
import http.server
import json
import logging
import os
import platform
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import Parser, ScriptError, run_script, write_output

EPILOG = """\
Each run publishes the same fixture into a fresh archive: one warmup, --reps timed
publishes, then one publish with a git wrapper on PATH that counts git calls. The
JSON report goes to --output.

examples:
  uv run python scripts/bench_publish.py --cli .venv/bin/html-publish
  uv run python scripts/bench_publish.py --cli "$WHEEL_VENV/bin/html-publish" -o publish.json -v
  uv run python scripts/bench_publish.py --cli .venv/bin/html-publish --reps 3 --workdir /var/tmp"""

log = logging.getLogger("bench-publish")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def positive_reps(value):
    try:
        reps = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("reps must be a positive integer") from error
    if reps < 1:
        raise argparse.ArgumentTypeError("reps must be at least 1")
    return reps


def executable(value):
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise argparse.ArgumentTypeError(f"{value} is not an executable file")
    return path


def bench(args):
    actual_git = shutil.which("git")
    if actual_git is None:
        raise ScriptError("git is not on PATH; fix: install git or add it to PATH")
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
                log.debug("run %s", shlex.join(command))
                start = time.perf_counter()
                completed = subprocess.run(
                    command, text=True, capture_output=True, env=env, check=False
                )
                elapsed = time.perf_counter() - start
                if completed.returncode:
                    raise ScriptError(
                        f"publish exited {completed.returncode}: "
                        f"{completed.stdout.strip()} {completed.stderr.strip()}"
                    )
                payload = json.loads(completed.stdout)
                if payload["outcome"] != "published":
                    raise ScriptError(f"publish returned an unexpected result: {payload}")
                if payload["verification"]["result"] != "passed":
                    raise ScriptError(f"publish verification failed: {payload}")
                if revision is None:
                    revision = payload["active_revision"]
                elif revision != payload["active_revision"]:
                    raise ScriptError("the revision changed across repetitions")
                log.info("run %d published in %.3f s", attempt, elapsed)
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
                        raise ScriptError("the served index bytes differ from the fixture")
                with urllib.request.urlopen(
                    base_url + "fixture/file-0099.txt", timeout=5
                ) as response:
                    if response.read() != files["file-0099.txt"]:
                        raise ScriptError("the served asset bytes differ from the fixture")
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
    write_output(args.output, json.dumps(report, indent=2) + "\n")
    return f"ok: timed {args.reps} publishes"


def main(argv=None):
    parser = Parser(
        prog="bench_publish.py",
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--cli", required=True, type=executable, help="html-publish executable to time"
    )
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="file for the JSON report; - writes it to stdout (default: -)",
    )
    parser.add_argument(
        "--reps", type=positive_reps, default=5, help="timed publishes (default: 5)"
    )
    parser.add_argument(
        "--workdir", type=Path, help="parent directory for fixtures (default: the temp dir)"
    )
    return run_script(parser, bench, argv, failure="a timed publish failed")


if __name__ == "__main__":
    raise SystemExit(main())
