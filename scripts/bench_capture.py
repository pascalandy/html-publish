"""Time `html-publish plan` over 1, 100, and 2,000-file fixtures in both object formats."""

import argparse
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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import Parser, ScriptError, run_script, write_output

EPILOG = """\
Each fixture runs one warmup, then --reps timed plans, then one plan with a git
wrapper on PATH that counts git calls. The JSON report goes to --output.

examples:
  uv run python scripts/bench_capture.py --cli .venv/bin/html-publish
  uv run python scripts/bench_capture.py --cli "$WHEEL_VENV/bin/html-publish" -o capture.json -v
  uv run python scripts/bench_capture.py --cli .venv/bin/html-publish --reps 3 --workdir /var/tmp"""

log = logging.getLogger("bench-capture")


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
                    log.debug("run %s", shlex.join(command))
                    start = time.perf_counter()
                    completed = subprocess.run(
                        command, capture_output=True, text=True, env=env, check=False
                    )
                    elapsed = time.perf_counter() - start
                    if completed.returncode != 0:
                        raise ScriptError(
                            f"plan exited {completed.returncode}: "
                            f"{completed.stdout.strip()} {completed.stderr.strip()}"
                        )
                    payload = json.loads(completed.stdout)
                    if payload["file_count"] != count or payload["prediction"] != "create":
                        raise ScriptError(f"plan returned an unexpected result: {payload}")
                    if revision is None:
                        revision = payload["requested_revision"]
                    elif revision != payload["requested_revision"]:
                        raise ScriptError("the revision changed across repetitions")
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
                log.info(json.dumps(result))
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
    write_output(args.output, json.dumps(report, indent=2) + "\n")
    return f"ok: timed {len(results)} fixtures"


def main(argv=None):
    parser = Parser(
        prog="bench_capture.py",
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
        "--reps", type=positive_reps, default=7, help="timed runs per fixture (default: 7)"
    )
    parser.add_argument(
        "--workdir", type=Path, help="parent directory for fixtures (default: the temp dir)"
    )
    return run_script(parser, bench, argv, failure="a timed plan failed")


if __name__ == "__main__":
    raise SystemExit(main())
