#!/usr/bin/env python3
"""Drive a private installed-wheel instance through its owning supervisor."""

from __future__ import annotations

import argparse
import json
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

REPO = Path(__file__).resolve().parents[4]
RUNS = Path("/tmp/html-publish-verify").resolve()


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


def start(run: Path) -> None:
    instance = run / "instance"
    instance.mkdir(mode=0o700, parents=True)
    artifacts = run / "artifacts"
    artifacts.mkdir(exist_ok=True)
    build = artifacts / "wheel"
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(build)], cwd=REPO, check=True)
    (wheel,) = build.glob("*.whl")
    subprocess.run(["uv", "venv", "--python", sys.executable, str(instance / "venv")], check=True)
    subprocess.run(
        ["uv", "pip", "install", "--python", str(instance / "venv/bin/python"), str(wheel)],
        check=True,
    )
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
    values = {
        "REPO_ROOT": REPO,
        "RUN_ID": run.name,
        "INSTANCE": instance,
        "CONFIG": config,
        "URL": f"http://127.0.0.1:{port}",
        "PORT": port,
        "ARTIFACTS": artifacts,
        "CLI": instance / "venv/bin/html-publish",
    }
    for key, value in values.items():
        print(f"{key}={value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["start", "sources", "doctor", "offline", "stop", "supervise"]
    )
    parser.add_argument("run_id")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run_id must use letters, digits, hyphens and underscores")
    run = RUNS / args.run_id
    instance = run / "instance"
    try:
        if args.command == "supervise":
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            supervise(instance, args.port)
        elif args.command == "start":
            start(run)
        elif args.command == "sources":
            for label in ("A", "B"):
                path = instance / f"page-{label.lower()}.html"
                path.write_text(
                    f"<!doctype html><title>Page {label}</title><h1>Page {label}</h1>\n"
                )
                print(f"PAGE_{label}={path}")
        elif args.command == "doctor":
            response = communicate(instance, "doctor")
            if not response.get("healthy"):
                raise ValueError("owned server is not healthy")
            config = json.loads((instance / "publisher.json").read_text())
            if config["archive"] != str(instance / "archive.git") or config["runtime"] != str(
                instance / "runtime"
            ):
                raise ValueError("config paths do not stay inside this instance")
            subprocess.run([str(instance / "venv/bin/html-publish"), "--version"], check=True)
            print("OK owner and installed executable match; health endpoint answers")
        elif not instance.exists() and args.command == "stop":
            print("OK no instance; nothing to stop")
        else:
            stop(instance)
            if args.command == "stop":
                shutil.copy2(instance / "server.log", run / "artifacts/server.log")
                shutil.rmtree(instance)
                print(f"OK instance removed; artifacts kept at {run / 'artifacts'}")
            else:
                print(f"OK server stopped; instance kept at {instance}")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"FAIL server identity does not match or instance unavailable: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
