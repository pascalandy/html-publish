from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from html_publish.configuration import ClientConfig, Role, read_document
from html_publish.model import Config, PublishError

Check = dict[str, str]


def _check(identifier: str, scope: str, status: str, detail: str, next_step: str) -> Check:
    return {
        "id": identifier,
        "scope": scope,
        "status": status,
        "detail": detail[:1000],
        "next_step": next_step,
    }


def _run(argv: list[str], seconds: float) -> tuple[int, str]:
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        stdout, stderr = process.communicate(timeout=max(0.001, seconds))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return 124, "command timed out"
    return process.returncode, (stdout or stderr).strip()[:65536]


def _path_checks(label: str, path: Path) -> list[Check]:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            return [
                _check(
                    label,
                    "local",
                    "fail",
                    f"{path} has the wrong path type",
                    "Correct the configured path outside doctor",
                )
            ]
        return [_check(label, "local", "pass", f"{path} exists", "none")]
    parent = next((ancestor for ancestor in path.parents if ancestor.exists()), None)
    if parent is None or not parent.is_dir():
        return [
            _check(
                label,
                "local",
                "fail",
                f"No usable parent for {path}",
                "Choose a directory under an accessible parent",
            )
        ]
    if not os.access(parent, os.W_OK | os.X_OK):
        return [
            _check(
                label,
                "local",
                "fail",
                f"Existing parent {parent} lacks write or search permission",
                "Change the configured path or its parent permissions",
            )
        ]
    return [
        _check(
            label,
            "local",
            "warning",
            f"{path} is not initialized; parent metadata permits creation",
            "Initialize it through an explicit publication or setup command",
        )
    ]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _http_check(url: str, remaining: float) -> Check:
    if remaining <= 0:
        return _check(
            "http_reachability",
            "client_http",
            "fail",
            "Command budget expired",
            "Increase --command-seconds",
        )
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, method="GET")
    try:
        with opener.open(request, timeout=remaining) as response:
            response.read(1)
            return _check(
                "http_reachability",
                "client_http",
                "pass",
                f"HTTP {response.status} at {url}",
                "none",
            )
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            location = error.headers.get("Location", "")
            destination = urlsplit(location)
            origin = urlsplit(url)
            if destination.netloc and destination.netloc != origin.netloc:
                detail = f"Redirect leaves configured origin: {location}"
            else:
                detail = f"Redirect is not followed: {location}"
        elif error.code == 404:
            return _check(
                "http_reachability",
                "client_http",
                "warning",
                f"HTTP 404 at configured path {url}",
                "Check the configured mount if a page should exist there",
            )
        else:
            detail = f"HTTP {error.code} at {url}"
        return _check(
            "http_reachability",
            "client_http",
            "fail",
            detail,
            "Inspect the configured URL and read-only host service",
        )
    except (OSError, ValueError) as error:
        return _check(
            "http_reachability",
            "client_http",
            "fail",
            str(error),
            "Inspect URL, listener, and network access",
        )


def run_doctor(
    role: Role,
    config: Config | ClientConfig,
    *,
    network: bool,
    command_seconds: float | None,
) -> list[Check]:
    saved_budget = config.limits.command_seconds
    budget = command_seconds or saved_budget
    deadline = time.monotonic() + budget
    checks: list[Check] = []
    git = shutil.which("git")
    if git:
        try:
            code, detail = _run([git, "--version"], min(5, deadline - time.monotonic()))
            checks.append(
                _check(
                    "git",
                    "local",
                    "pass" if code == 0 else "fail",
                    detail,
                    "none" if code == 0 else "Install a working Git executable",
                )
            )
        except OSError as error:
            checks.append(
                _check("git", "local", "fail", str(error), "Install a working Git executable")
            )
    else:
        checks.append(_check("git", "local", "fail", "git is unavailable on PATH", "Install Git"))

    try:
        code, detail = _run(
            [sys.executable, "-m", "html_publish", "--version"],
            min(5, deadline - time.monotonic()),
        )
        checks.append(
            _check(
                "html_publish",
                "local",
                "pass" if code == 0 else "fail",
                detail or f"Installed tool exited {code}",
                "none" if code == 0 else "Install a working html-publish wheel",
            )
        )
    except OSError as error:
        checks.append(
            _check(
                "html_publish", "local", "fail", str(error), "Install a working html-publish wheel"
            )
        )

    if role == "publisher" and isinstance(config, Config):
        checks.extend(_path_checks("archive", config.archive))
        checks.extend(_path_checks("runtime", config.runtime))
        lock = config.runtime / ".publish.lock"
        if lock.is_symlink() or (lock.exists() and not lock.is_file()):
            checks.append(
                _check(
                    "lock_path",
                    "local",
                    "fail",
                    f"{lock} has the wrong path type",
                    "Inspect the lock path outside doctor",
                )
            )
        else:
            checks.append(
                _check(
                    "lock_path",
                    "local",
                    "pass" if lock.exists() else "warning",
                    f"{lock} {'exists' if lock.exists() else 'is absent'}",
                    "none",
                )
            )
        url = config.base_url
    elif isinstance(config, ClientConfig):
        if config.executor.kind == "local":
            assert config.executor.publisher_config is not None
            client_command = config.executor.command[0]
            available = shutil.which(client_command)
            checks.append(
                _check(
                    "client_command",
                    "local",
                    "pass" if available else "fail",
                    available or f"{client_command} is unavailable on PATH",
                    "none" if available else "Install the configured local client command",
                )
            )
            publisher_path = Path(config.executor.publisher_config)
            if not publisher_path.is_absolute():
                checks.append(
                    _check(
                        "publisher_target",
                        "local",
                        "fail",
                        "Legacy publisher_config is not an absolute path",
                        "Select a client config with an explicit publisher path",
                    )
                )
            else:
                try:
                    _, publisher = read_document("publisher", publisher_path)
                    assert isinstance(publisher, Config)
                    status = "pass" if publisher.base_url == config.target.base_url else "fail"
                    checks.append(
                        _check(
                            "publisher_target",
                            "local",
                            status,
                            f"Publisher base URL is {publisher.base_url}",
                            "Align the explicit client target and publisher config"
                            if status == "fail"
                            else "none",
                        )
                    )
                except PublishError as error:
                    checks.append(
                        _check(
                            "publisher_target",
                            "local",
                            "fail",
                            error.failure.message,
                            "Correct the referenced publisher config",
                        )
                    )
        else:
            missing = [
                name
                for name, value in (
                    ("remote_executable", config.executor.remote_executable),
                    ("remote_config", config.executor.remote_config),
                    ("incoming_root", config.executor.incoming_root),
                )
                if value is None
            ]
            if missing:
                checks.append(
                    _check(
                        "remote_destination",
                        "local",
                        "fail",
                        "Legacy client config lacks " + ", ".join(missing),
                        "Complete the remote destination explicitly",
                    )
                )
            for executable in ("ssh", "scp"):
                found = shutil.which(executable)
                checks.append(
                    _check(
                        executable,
                        "local",
                        "pass" if found else "fail",
                        found or f"{executable} is unavailable on PATH",
                        "none" if found else f"Install {executable}",
                    )
                )
        url = config.target.base_url
    else:
        raise ValueError("role and config disagree")

    if not network:
        checks.append(
            _check(
                "network",
                "network",
                "skipped",
                "Network checks require --network",
                "Run doctor --network to authorize bounded reads",
            )
        )
        return checks

    if isinstance(config, ClientConfig) and config.executor.kind == "remote":
        if config.executor.remote_executable is None or config.executor.remote_config is None:
            checks.append(
                _check(
                    "ssh_host",
                    "ssh_host",
                    "fail",
                    "Remote executable or config is missing",
                    "Complete the client destination explicitly",
                )
            )
            checks.append(_http_check(url, deadline - time.monotonic()))
            return checks
        if shutil.which("ssh") is None:
            checks.append(
                _check("ssh_host", "ssh_host", "skipped", "SSH is unavailable", "Install SSH")
            )
        else:
            connect_seconds = min(
                config.executor.connect_timeout or 10,
                max(1, int(deadline - time.monotonic())),
            )
            options = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UpdateHostKeys=no",
                "-o",
                "PermitLocalCommand=no",
                "-o",
                "ForwardAgent=no",
                "-o",
                "ClearAllForwardings=yes",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPath=none",
                "-o",
                f"ConnectTimeout={connect_seconds}",
                config.executor.host,
            ]
            for identifier, command in (
                ("host_version", [config.executor.remote_executable, "--version"]),
                (
                    "host_doctor",
                    [
                        config.executor.remote_executable,
                        "doctor",
                        "--role",
                        "publisher",
                        "--config",
                        config.executor.remote_config,
                        "--json",
                    ],
                ),
            ):
                if deadline - time.monotonic() <= 0:
                    checks.append(
                        _check(
                            identifier,
                            "ssh_host",
                            "fail",
                            "Command budget expired",
                            "Increase --command-seconds",
                        )
                    )
                    continue
                try:
                    code, detail = _run(
                        [*options, shlex.join(command)], deadline - time.monotonic()
                    )
                    if code == 0 and identifier == "host_doctor":
                        try:
                            report: object = json.loads(detail)
                            typed = (
                                cast(dict[str, object], report)
                                if isinstance(report, dict)
                                else None
                            )
                            if (
                                typed is None
                                or typed.get("operation") != "doctor"
                                or typed.get("role") != "publisher"
                                or typed.get("outcome") != "diagnosed"
                            ):
                                code = 1
                                detail = "Host returned an invalid publisher doctor report"
                        except json.JSONDecodeError:
                            code = 1
                            detail = "Host returned invalid doctor JSON"
                    checks.append(
                        _check(
                            identifier,
                            "ssh_host",
                            "pass" if code == 0 else "fail",
                            detail or f"SSH exited {code}",
                            "none"
                            if code == 0
                            else "Inspect SSH trust, authentication, and host config",
                        )
                    )
                except OSError as error:
                    checks.append(
                        _check(identifier, "ssh_host", "fail", str(error), "Inspect SSH transport")
                    )
    checks.append(_http_check(url, deadline - time.monotonic()))
    return checks
