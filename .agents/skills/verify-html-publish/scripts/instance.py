#!/usr/bin/env python3
"""Drive a private installed-wheel instance through its owning supervisor."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from html_publish import command_line
from html_publish.discovery import register_command

REPO = Path(__file__).resolve().parents[4]
RUNS = Path("/tmp/html-publish-verify").resolve()
RUN_ID = re.compile(r"[A-Za-z0-9_-]+")
PROG = "instance.sh"
EXAMPLE_RUN = "first-pub-20260921"

log = logging.getLogger("html_publish.instance")


def health(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/_html-publish-health", timeout=1
        ) as response:
            return response.status == 200 and response.read(10) == b"ok\n"
    except (OSError, urllib.error.URLError):
        return False


def communicate(instance: Path, operation: str) -> dict[str, object]:
    identity = json.loads((instance / "server.identity").read_text())
    stopped = instance / "stopped.json"
    if stopped.exists():
        if json.loads(stopped.read_text()) == identity:
            return {"stopped": True}
        raise ValueError("server identity does not match the stopped owner")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(8)
        client.connect(identity["socket"])
        with client.makefile("rwb") as stream:
            request = {"identity": identity, "operation": operation}
            stream.write(json.dumps(request).encode() + b"\n")
            stream.flush()
            response = json.loads(stream.readline(65536))
    if response.get("error"):
        raise ValueError(response["error"])
    return response


def stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)


def supervise(instance: Path, port: int) -> None:
    control = Path(tempfile.mkdtemp(prefix="hp-control-", dir="/tmp"))
    endpoint = control / "control.sock"
    identity = {"pid": os.getpid(), "socket": str(endpoint), "token": secrets.token_hex(32)}
    child = None
    with socket.socket(socket.AF_UNIX) as listener, (instance / "server.log").open("w") as log:
        listener.bind(str(endpoint))
        listener.listen(4)
        listener.settimeout(1)
        (instance / "server.identity").write_text(json.dumps(identity))
        try:
            child = subprocess.Popen(
                [
                    str(instance / "venv/bin/html-publish-server"),
                    "--directory",
                    str(instance / "runtime/public"),
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                stdout=log,
                stderr=log,
            )
            while child.poll() is None:
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(2)
                    with connection.makefile("rwb") as stream:
                        try:
                            request = json.loads(stream.readline(65536))
                            if request.get("identity") != identity:
                                response = {"error": "server identity does not match the owner"}
                            elif request.get("operation") == "stop":
                                stop_child(child)
                                response = {"stopped": True}
                            else:
                                response = {"alive": child.poll() is None, "healthy": health(port)}
                            stream.write(json.dumps(response).encode() + b"\n")
                            stream.flush()
                        except (OSError, ValueError):
                            continue
        finally:
            if child is not None:
                stop_child(child)
            (instance / "stopped.json").write_text(json.dumps(identity))
            endpoint.unlink(missing_ok=True)
            control.rmdir()


def stop(instance: Path) -> None:
    response = communicate(instance, "stop")
    if not response.get("stopped"):
        raise ValueError("owner did not confirm server shutdown")
    identity = json.loads((instance / "server.identity").read_text())
    for _ in range(100):
        if not Path(identity["socket"]).exists():
            break
        time.sleep(0.05)
    else:
        raise ValueError("owner did not finish cleanup")
    if health(int((instance / "port").read_text())):
        raise ValueError("port still answers after the owner stopped its server")


def quiet(command: list[str], cwd: Path | None = None) -> None:
    """Run a setup command and keep its output unless it fails."""
    log.debug("run %s", " ".join(command))
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        raise ValueError(f"{' '.join(command[:2])} exited {result.returncode}: {' '.join(detail)}")


def start(run: Path) -> dict[str, object]:
    instance = run / "instance"
    instance.mkdir(mode=0o700, parents=True)
    artifacts = run / "artifacts"
    artifacts.mkdir(exist_ok=True)
    build = artifacts / "wheel"
    quiet(["uv", "build", "--wheel", "--out-dir", str(build)], cwd=REPO)
    (wheel,) = build.glob("*.whl")
    quiet(["uv", "venv", "--python", sys.executable, str(instance / "venv")])
    quiet(["uv", "pip", "install", "--python", str(instance / "venv/bin/python"), str(wheel)])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    (instance / "port").write_text(str(port))
    config = instance / "publisher.json"
    config.write_text(
        json.dumps(
            {
                "archive": str(instance / "archive.git"),
                "runtime": str(instance / "runtime"),
                "base_url": f"http://127.0.0.1:{port}/",
                "allow_http": True,
                "object_format": "sha1",
            }
        )
    )
    with (artifacts / "supervisor.log").open("w") as log:
        supervisor = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "supervise",
                run.name,
                "--port",
                str(port),
            ],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
    for _ in range(100):
        if supervisor.poll() is not None:
            raise ValueError(f"supervisor exited; inspect {artifacts / 'supervisor.log'}")
        try:
            response = communicate(instance, "doctor")
            if response.get("healthy"):
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    else:
        stop(instance)
        raise ValueError(f"server did not become healthy; inspect {instance / 'server.log'}")
    return {
        "REPO_ROOT": str(REPO),
        "RUN_ID": run.name,
        "INSTANCE": str(instance),
        "CONFIG": str(config),
        "URL": f"http://127.0.0.1:{port}",
        "PORT": port,
        "ARTIFACTS": str(artifacts),
        "CLI": str(instance / "venv/bin/html-publish"),
    }


class Parser(command_line.Parser):
    exit_codes = (0, 1, 2, 130, 143)


def _run_id(value: str) -> str:
    if not RUN_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("run_id must use letters, digits, hyphens and underscores")
    return value


COMMANDS = {
    "start": (
        "build, install, and serve a private instance",
        (f"{PROG} start {EXAMPLE_RUN}", f"{PROG} start {EXAMPLE_RUN} --json"),
        (
            "builds a wheel and installs it in the instance's virtual environment",
            "starts a supervisor that owns a loopback server",
            "writes only under /tmp/html-publish-verify/<run_id>",
            "prints KEY=value lines, or one JSON object with --json",
        ),
    ),
    "sources": (
        "write the two standard page fixtures",
        (f"{PROG} sources {EXAMPLE_RUN}", f"{PROG} sources {EXAMPLE_RUN} --json"),
        (
            "rewrites the same two fixture files with the same bytes",
            "prints PAGE_A= and PAGE_B=, or one JSON object with --json",
        ),
    ),
    "doctor": (
        "check that the owned server answers and the installed executable runs",
        (f"{PROG} doctor {EXAMPLE_RUN}", f"{PROG} doctor {EXAMPLE_RUN} --debug"),
        ("asks the supervisor for health and runs the installed --version", "changes nothing"),
    ),
    "offline": (
        "stop the owned server and keep the instance",
        (f"{PROG} offline {EXAMPLE_RUN}", f"{PROG} offline {EXAMPLE_RUN} --debug"),
        ("stops only the server this run's supervisor owns",),
    ),
    "stop": (
        "stop the owned server, remove the instance, and keep the artifacts",
        (f"{PROG} stop {EXAMPLE_RUN}", f"{PROG} stop {EXAMPLE_RUN} --debug"),
        (
            "stops only the server this run's supervisor owns",
            "prints ARTIFACTS= with the kept evidence directory",
            "a repeated stop changes nothing",
        ),
    ),
}
NEXT = {
    "start": "stop",
    "sources": "start",
    "doctor": "stop",
}


def _debug(parser: argparse.ArgumentParser, *, child: bool) -> None:
    parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS if child else False,
        help="also print setup commands and tracebacks on stderr (or set INSTANCE_DEBUG=1)",
    )


def _parser() -> Parser:
    parser = Parser(
        prog=PROG,
        description=__doc__,
        epilog="Examples:\n  "
        + "\n  ".join(
            (
                f"{PROG} start {EXAMPLE_RUN}",
                f"{PROG} doctor {EXAMPLE_RUN}",
                f"{PROG} stop {EXAMPLE_RUN}",
                f"{PROG} help start",
            )
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    _debug(parser, child=False)
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="{" + ",".join([*COMMANDS, "help"]) + "}"
    )
    for name, (summary, examples, effects) in COMMANDS.items():
        command = register_command(commands, name, summary, examples=examples, effects=effects)
        command.add_argument("run_id", type=_run_id, help="this proof's run ID")
        if name in {"start", "sources"}:
            command.add_argument(
                "--json", action="store_true", help="print one JSON object instead of KEY=value"
            )
        _debug(command, child=True)
    supervise = commands.add_parser("supervise")
    supervise.add_argument("run_id", type=_run_id)
    supervise.add_argument("--port", type=int, required=True)
    command_line.add_help_command(commands, PROG, (f"{PROG} help start", f"{PROG} help stop"))
    return parser


def _emit(values: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(values))
    else:
        for key, value in values.items():
            print(f"{key}={value}")


def _command(args: argparse.Namespace) -> int:
    run = RUNS / args.run_id
    instance = run / "instance"
    if args.command == "supervise":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        supervise(instance, args.port)
    elif args.command == "start":
        _emit(start(run), args.json)
    elif args.command == "sources":
        pages: dict[str, object] = {}
        for label in ("A", "B"):
            path = instance / f"page-{label.lower()}.html"
            path.write_text(f"<!doctype html><title>Page {label}</title><h1>Page {label}</h1>\n")
            pages[f"PAGE_{label}"] = str(path)
        _emit(pages, args.json)
    elif args.command == "doctor":
        response = communicate(instance, "doctor")
        if not response.get("healthy"):
            raise ValueError("owned server is not healthy")
        config = json.loads((instance / "publisher.json").read_text())
        if config["archive"] != str(instance / "archive.git") or config["runtime"] != str(
            instance / "runtime"
        ):
            raise ValueError("config paths do not stay inside this instance")
        quiet([str(instance / "venv/bin/html-publish"), "--version"])
    elif args.command == "offline":
        stop(instance)
    elif instance.exists():
        stop(instance)
        shutil.copy2(instance / "server.log", run / "artifacts/server.log")
        shutil.rmtree(instance)
        print(f"ARTIFACTS={run / 'artifacts'}")
    elif (run / "artifacts").is_dir():
        print(f"ARTIFACTS={run / 'artifacts'}")
    return 0


def _main(arguments: list[str]) -> int:
    parser = _parser()
    try:
        help_parser = command_line.requested_help(parser, arguments)
        if help_parser is not None:
            print(help_parser.format_help(), end="")
            return 0
        args = parser.parse_args(arguments)
    except command_line.UsageError as error:
        sys.stderr.write(command_line.usage_text(error, parser, arguments))
        return 2
    command_line.configure_logging(
        PROG, verbose=False, debug=args.debug or command_line.debug_requested(PROG, arguments)
    )
    try:
        return _command(args)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(
            f"{PROG}: server identity does not match or instance unavailable: {error}",
            file=sys.stderr,
        )
        fix = NEXT.get(args.command)
        print(
            f"next: {PROG} {fix} {args.run_id}" if fix else f"next: {PROG} start <fresh-run-id>",
            file=sys.stderr,
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    return command_line.run(PROG, arguments, lambda: _main(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
