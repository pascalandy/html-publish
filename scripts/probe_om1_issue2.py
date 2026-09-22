#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import os
import pwd
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import NoReturn, cast
from urllib.parse import quote, urlsplit

EXPECTED_HOST = "om1"
EXPECTED_USER = "pascal"
ROUTE_PATH = "/html-publish-issue2-probe"
RESERVED_PORTS = frozenset({443, 5173, 8443, 8444})
PROBE_PARENT = Path("/home/pascal/.local/state/html-publish-probes")
EVIDENCE_PARENT = Path("/home/pascal/.local/state/html-publish-probe-evidence")
CLI_PYTHON = Path("/home/pascal/.local/share/html-publish/current/.venv/bin/python")
MAX_OUTPUT = 65_536
COMMAND_TIMEOUT = 180
FIXTURE_MTIME_NS = 1_700_000_000_000_000_000
PHASES = frozenset({"preflight", "prepared", "b-active", "a-restored", "cleaned"})


class ProbeFailure(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ProbeFailure(message)


@dataclass(frozen=True)
class ProbeState:
    schema_version: int
    owner_token: str
    run_id: str
    expected_host: str
    observed_host: str
    dns_name: str
    name: str
    listen_port: int
    https_port: int
    route_path: str
    route_host_port: str
    route_target: str
    base_url: str
    root: str
    evidence_root: str
    state_path: str
    cli_python: str
    phase: str
    baseline_serve: dict[str, object]
    baseline_serve_fingerprint: str
    baseline_observations: dict[str, object]
    fixtures: dict[str, object]
    pid: int | None = None
    process_start_ticks: str | None = None
    process_cmd: tuple[str, ...] = ()
    route_fingerprint: str | None = None
    revision_a: str | None = None
    archive_commit_a: str | None = None
    revision_b: str | None = None
    archive_commit_b: str | None = None
    restored_revision: str | None = None
    validators: dict[str, object] | None = None

    @property
    def root_path(self) -> Path:
        return Path(self.root)

    @property
    def evidence_path(self) -> Path:
        return Path(self.evidence_root)

    @property
    def config_path(self) -> Path:
        return self.root_path / "publisher.json"


def _parser() -> Parser:
    parser = Parser(description="Run the isolated om1 issue 2 host probe")
    commands = parser.add_subparsers(dest="operation", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--output", required=True, type=Path)
    for operation in ("prepare", "activate-b", "restore-a", "cleanup"):
        command = commands.add_parser(operation)
        command.add_argument("--state", required=True, type=Path)
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--state", required=True, type=Path)
    checkpoint.add_argument("--expect", required=True, choices=("A", "B", "restored-A"))
    return parser


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(_json_bytes(value) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_state(state: ProbeState) -> None:
    path = Path(state.state_path)
    if path.parent.resolve() != state.evidence_path.resolve():
        raise ProbeFailure("state path no longer belongs to the recorded evidence directory")
    _atomic_json(path, asdict(state))


def _write_evidence(state: ProbeState, label: str, payload: object) -> Path:
    path = state.evidence_path / f"{label}-{time.time_ns()}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(_json_bytes(payload) + b"\n")
    return path


def _load_state(path: Path) -> ProbeState:
    resolved = path.expanduser().resolve()
    try:
        raw: object = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProbeFailure(f"could not read probe state: {error}") from error
    if not isinstance(raw, dict):
        raise ProbeFailure("probe state is not a JSON object")
    values = cast(dict[str, object], raw)
    values["process_cmd"] = tuple(cast(list[str], values.get("process_cmd", [])))
    try:
        state = ProbeState(**values)
    except (TypeError, ValueError) as error:
        raise ProbeFailure(f"probe state has the wrong shape: {error}") from error
    if state.schema_version != 1 or state.phase not in PHASES:
        raise ProbeFailure("probe state has an unsupported version or phase")
    if resolved != Path(state.state_path).resolve():
        raise ProbeFailure("probe state path does not match its recorded identity")
    if state.expected_host != EXPECTED_HOST or state.route_path != ROUTE_PATH:
        raise ProbeFailure("probe state does not belong to this script")
    if state.root_path.parent != PROBE_PARENT or not state.evidence_path.is_relative_to(
        EVIDENCE_PARENT
    ):
        raise ProbeFailure("probe state contains an unowned root")
    if state.evidence_path == EVIDENCE_PARENT or state.evidence_path == state.root_path:
        raise ProbeFailure("probe evidence must have its own persistent directory")
    return state


def _host_guard(expected: str = EXPECTED_HOST) -> str:
    observed = socket.gethostname().split(".", 1)[0]
    user = pwd.getpwuid(os.getuid()).pw_name
    if observed != expected or user != EXPECTED_USER:
        raise ProbeFailure(
            "host identity mismatch: "
            f"expected {EXPECTED_USER}@{expected}, observed {user}@{observed}"
        )
    return observed


def _run(
    argv: tuple[str, ...], *, timeout: int = COMMAND_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeFailure(f"command exceeded {timeout} seconds: {argv[0]}") from error
    except OSError as error:
        raise ProbeFailure(f"could not run {argv[0]}: {error}") from error
    if len(result.stdout.encode()) > MAX_OUTPUT or len(result.stderr.encode()) > MAX_OUTPUT:
        raise ProbeFailure(f"command output exceeded {MAX_OUTPUT} bytes: {argv[0]}")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise ProbeFailure(f"command failed with exit {result.returncode}: {argv[0]}: {detail}")
    return result


def _json_command(argv: tuple[str, ...], *, timeout: int = COMMAND_TIMEOUT) -> dict[str, object]:
    result = _run(argv, timeout=timeout)
    try:
        value: object = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProbeFailure(f"command returned invalid JSON: {argv[0]}: {error.msg}") from error
    if not isinstance(value, dict):
        raise ProbeFailure(f"command returned JSON that is not an object: {argv[0]}")
    return cast(dict[str, object], value)


def _serve_status() -> dict[str, object]:
    return _json_command(("tailscale", "serve", "status", "--json"), timeout=15)


def _dns_name() -> str:
    payload = _json_command(("tailscale", "status", "--json"), timeout=15)
    self_state = payload.get("Self")
    if not isinstance(self_state, dict):
        raise ProbeFailure("tailscale status returned no Self object")
    dns_name = cast(dict[str, object], self_state).get("DNSName")
    if not isinstance(dns_name, str) or not dns_name:
        raise ProbeFailure("tailscale status returned no DNS name")
    return dns_name.rstrip(".")


def _route_handler(payload: dict[str, object], host_port: str, path: str) -> object | None:
    web = payload.get("Web", {})
    if not isinstance(web, dict):
        raise ProbeFailure("Tailscale Serve Web state is invalid")
    host = cast(dict[str, object], web).get(host_port)
    if host is None:
        return None
    if not isinstance(host, dict):
        raise ProbeFailure("Tailscale Serve host state is invalid")
    handlers = cast(dict[str, object], host).get("Handlers")
    if not isinstance(handlers, dict):
        raise ProbeFailure("Tailscale Serve handler state is invalid")
    return cast(dict[str, object], handlers).get(path)


def _route_state(state: ProbeState, payload: dict[str, object]) -> tuple[str, str | None]:
    handler = _route_handler(payload, state.route_host_port, state.route_path)
    if handler is None:
        return "absent", None
    fingerprint = _fingerprint(handler)
    if (
        isinstance(handler, dict)
        and cast(dict[str, object], handler).get("Proxy") == state.route_target
    ):
        return "exact", fingerprint
    return "collision", fingerprint


def _strip_owned_route(state: ProbeState, payload: dict[str, object]) -> dict[str, object]:
    stripped = copy.deepcopy(payload)
    web = stripped.get("Web")
    if not isinstance(web, dict):
        return stripped
    host = cast(dict[str, object], web).get(state.route_host_port)
    if not isinstance(host, dict):
        return stripped
    handlers = cast(dict[str, object], host).get("Handlers")
    if not isinstance(handlers, dict):
        return stripped
    cast(dict[str, object], handlers).pop(state.route_path, None)
    if handlers:
        return stripped
    cast(dict[str, object], web).pop(state.route_host_port, None)
    baseline_tcp = state.baseline_serve.get("TCP", {})
    tcp = stripped.get("TCP")
    if isinstance(tcp, dict) and isinstance(baseline_tcp, dict):
        port = str(state.https_port)
        if port not in cast(dict[str, object], baseline_tcp):
            cast(dict[str, object], tcp).pop(port, None)
    return stripped


def _listener_snapshot() -> list[str]:
    output = _run(("ss", "-H", "-lntp"), timeout=10).stdout.splitlines()
    selected = []
    for line in output:
        local = line.split()[3] if len(line.split()) >= 4 else ""
        if any(local.endswith(f":{port}") for port in (*RESERVED_PORTS, 4177)):
            selected.append(" ".join(line.split()))
    return sorted(selected)


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


def _endpoint(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=4) as response:
            body = response.read(MAX_OUTPUT + 1)
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read(MAX_OUTPUT + 1)
        status = error.code
    except (OSError, urllib.error.URLError) as error:
        return {
            "error": type(
                error.reason if isinstance(error, urllib.error.URLError) else error
            ).__name__
        }
    if len(body) > MAX_OUTPUT:
        raise ProbeFailure(f"endpoint response exceeded {MAX_OUTPUT} bytes: {url}")
    return {"status": status, "sha256": hashlib.sha256(body).hexdigest()}


def _observations(dns_name: str) -> dict[str, object]:
    endpoints = {
        "https-443": _endpoint(f"https://{dns_name}/"),
        "https-8443": _endpoint(f"https://{dns_name}:8443/"),
        "installed-https": _endpoint(f"https://{dns_name}:8444/html-publish/_html-publish-health"),
        "installed-loopback": _endpoint("http://127.0.0.1:4177/_html-publish-health"),
        "loopback-5173": _endpoint("http://127.0.0.1:5173/"),
    }
    active = _run(("systemctl", "--user", "is-active", "html-publish.service"), timeout=10)
    linger = _run(("loginctl", "show-user", EXPECTED_USER, "-p", "Linger"), timeout=10)
    return {
        "endpoints": endpoints,
        "listeners": _listener_snapshot(),
        "service": active.stdout.strip(),
        "linger": linger.stdout.strip(),
    }


def _free_loopback_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = cast(tuple[str, int], candidate.getsockname())[1]
    if port in RESERVED_PORTS or port == 4177:
        return _free_loopback_port()
    return port


def _https_port(payload: dict[str, object], listeners: list[str]) -> int:
    web = payload.get("Web", {})
    tcp = payload.get("TCP", {})
    if not isinstance(web, dict) or not isinstance(tcp, dict):
        raise ProbeFailure("Tailscale Serve Web or TCP state is invalid")
    for port in range(9443, 9544):
        if port in RESERVED_PORTS or str(port) in cast(dict[str, object], tcp):
            continue
        if any(key.endswith(f":{port}") for key in cast(dict[str, object], web)):
            continue
        if any(f":{port}" in line for line in listeners):
            continue
        return port
    raise ProbeFailure("no unused HTTPS probe port is available")


def _private_directory(path: Path, *, create: bool) -> None:
    existed = path.exists()
    if create and not existed:
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)
    details = path.stat()
    if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o700:
        raise ProbeFailure(f"directory is not private and user owned: {path}")


def _preflight(output: Path) -> dict[str, object]:
    observed_host = _host_guard()
    output = output.expanduser().resolve()
    if output.name != "probe.json" or output.parent == EVIDENCE_PARENT:
        raise ProbeFailure("--output must be a dedicated evidence directory ending in probe.json")
    if not output.parent.is_relative_to(EVIDENCE_PARENT):
        raise ProbeFailure(f"--output must be below {EVIDENCE_PARENT}")
    if output.exists() or output.parent.exists():
        raise ProbeFailure("probe output or its dedicated evidence directory already exists")
    if not CLI_PYTHON.is_file():
        raise ProbeFailure(f"installed publisher interpreter is missing: {CLI_PYTHON}")
    help_text = _run((str(CLI_PYTHON), "-m", "html_publish", "--help"), timeout=15).stdout
    if "restore" not in help_text:
        raise ProbeFailure("the installed publisher does not include the required restore command")
    serve = _serve_status()
    dns_name = _dns_name()
    listeners = _listener_snapshot()
    https_port = _https_port(serve, listeners)
    listen_port = _free_loopback_port()
    run_id = uuid.uuid4().hex
    root = PROBE_PARENT / run_id
    if root.exists():
        raise ProbeFailure(f"probe root already exists: {root}")
    host_port = f"{dns_name}:{https_port}"
    if _route_handler(serve, host_port, ROUTE_PATH) is not None:
        raise ProbeFailure("the proposed probe route is already occupied")
    observations = _observations(dns_name)
    state = ProbeState(
        schema_version=1,
        owner_token=uuid.uuid4().hex,
        run_id=run_id,
        expected_host=EXPECTED_HOST,
        observed_host=observed_host,
        dns_name=dns_name,
        name=f"issue2-probe-{run_id[:12]}",
        listen_port=listen_port,
        https_port=https_port,
        route_path=ROUTE_PATH,
        route_host_port=host_port,
        route_target=f"http://127.0.0.1:{listen_port}",
        base_url=f"https://{host_port}{ROUTE_PATH}/",
        root=str(root),
        evidence_root=str(output.parent),
        state_path=str(output),
        cli_python=str(CLI_PYTHON),
        phase="preflight",
        baseline_serve=serve,
        baseline_serve_fingerprint=_fingerprint(serve),
        baseline_observations=observations,
        fixtures={},
        validators={},
    )
    _private_directory(EVIDENCE_PARENT, create=True)
    _private_directory(output.parent, create=True)
    _write_state(state)
    evidence = _write_evidence(
        state,
        "preflight",
        {
            "state": asdict(state),
            "versions": {
                "tailscale": _run(("tailscale", "version"), timeout=10).stdout.splitlines()[0],
                "git": _run(("git", "--version"), timeout=10).stdout.strip(),
                "python": _run((str(CLI_PYTHON), "--version"), timeout=10).stdout.strip(),
            },
        },
    )
    return {
        "operation": "preflight",
        "state": str(output),
        "evidence": str(evidence),
        "base_url": state.base_url,
        "route_target": state.route_target,
        "cleanup_arguments": ["cleanup", "--state", str(output)],
        "owned_paths": [str(root), str(output.parent), str(output)],
    }


def _owner_file(state: ProbeState) -> Path:
    return state.root_path / ".probe-owner"


def _assert_owner(state: ProbeState) -> None:
    _host_guard(state.expected_host)
    try:
        owner = _owner_file(state).read_text(encoding="ascii").strip()
    except OSError as error:
        raise ProbeFailure(f"probe owner marker is unavailable: {error}") from error
    if owner != state.owner_token:
        raise ProbeFailure("probe owner token does not match")
    _private_directory(state.root_path, create=False)
    _private_directory(state.evidence_path, create=False)


def _write_fixture_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, ns=(FIXTURE_MTIME_NS, FIXTURE_MTIME_NS))


def _fixture_tree(root: Path, label: str) -> dict[str, object]:
    marker = f"ISSUE2-{label}-{root.parent.name[:12]}"
    html = (
        "<!doctype html><meta charset=utf-8>"
        f"<link rel=stylesheet href=assets/state.css><h1>{marker}</h1>\n"
    ).encode()
    css = f"body{{--issue2-state:'{label}';color:#fff;background:#000}}\n".encode()
    if label == "A":
        html = html.replace(b"ISSUE2-A", b"ISSUE2-A")
        css = css.replace(b"'A'", b"'A'")
    _write_fixture_file(root / "index.html", html)
    _write_fixture_file(root / "assets/state.css", css)
    _write_fixture_file(root / "paths/space name.txt", f"space-{label}\n".encode())
    _write_fixture_file(root / "paths/unicodé.txt", f"unicode-{label}\n".encode())
    _write_fixture_file(root / "paths/percent%25.txt", f"percent-{label}\n".encode())
    if label == "A":
        _write_fixture_file(root / "assets/removed.txt", b"removed-after-a\n")
        _write_fixture_file(root / "flip", b"flip-file-A\n")
        _write_fixture_file(root / "swap/index.html", b"swap-directory-A\n")
    else:
        _write_fixture_file(root / "flip/index.html", b"flip-directory-B\n")
        _write_fixture_file(root / "swap", b"swap-file-B\n")
    files = {
        str(path.relative_to(root)): {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    return {"marker": marker, "files": files}


def _assert_equal_freshness_shape(fixtures: dict[str, object]) -> None:
    a = cast(dict[str, object], fixtures["A"])
    b = cast(dict[str, object], fixtures["B"])
    files_a = cast(dict[str, dict[str, object]], a["files"])
    files_b = cast(dict[str, dict[str, object]], b["files"])
    for path in ("index.html", "assets/state.css"):
        if files_a[path]["size"] != files_b[path]["size"]:
            raise ProbeFailure(f"fixture {path} does not preserve byte length")
        if files_a[path]["mtime_ns"] != files_b[path]["mtime_ns"]:
            raise ProbeFailure(f"fixture {path} does not preserve nanosecond mtime")
        if files_a[path]["sha256"] == files_b[path]["sha256"]:
            raise ProbeFailure(f"fixture {path} does not change bytes")


def _publisher_config(state: ProbeState) -> dict[str, object]:
    return {
        "archive": str(state.root_path / "archive.git"),
        "runtime": str(state.root_path / "runtime"),
        "base_url": state.base_url,
        "allow_http": False,
        "object_format": "sha1",
        "limits": {"command_seconds": 120, "lock_seconds": 30, "verification_seconds": 60},
    }


def _process_identity(pid: int) -> tuple[str, tuple[str, ...], str]:
    proc = Path("/proc") / str(pid)
    try:
        status = proc.stat()
        stat_fields = (proc / "stat").read_text(encoding="ascii").split()
        cmdline = tuple(
            part.decode(errors="strict")
            for part in (proc / "cmdline").read_bytes().split(b"\0")
            if part
        )
        environ = (proc / "environ").read_bytes().split(b"\0")
    except (OSError, UnicodeError) as error:
        raise ProbeFailure(f"recorded server process is unavailable: {error}") from error
    if status.st_uid != os.getuid() or len(stat_fields) < 22:
        raise ProbeFailure("recorded server process has the wrong owner or identity")
    owner = next(
        (
            item.split(b"=", 1)[1].decode()
            for item in environ
            if item.startswith(b"HTML_PUBLISH_PROBE_OWNER=")
        ),
        "",
    )
    return stat_fields[21], cmdline, owner


def _assert_process(state: ProbeState) -> None:
    if state.pid is None or state.process_start_ticks is None or not state.process_cmd:
        raise ProbeFailure("probe state has no server process identity")
    start_ticks, command, owner = _process_identity(state.pid)
    if (
        start_ticks != state.process_start_ticks
        or command != state.process_cmd
        or owner != state.owner_token
    ):
        raise ProbeFailure("recorded PID no longer belongs to the probe server")


def _wait_health(state: ProbeState) -> None:
    deadline = time.monotonic() + 6
    url = f"{state.route_target}/_html-publish-health"
    while time.monotonic() < deadline:
        if _endpoint(url) == {"status": 200, "sha256": hashlib.sha256(b"ok\n").hexdigest()}:
            return
        time.sleep(0.1)
    raise ProbeFailure("probe server did not become healthy")


def _cli(state: ProbeState, *arguments: str) -> dict[str, object]:
    payload = _json_command(
        (
            state.cli_python,
            "-m",
            "html_publish",
            "--config",
            str(state.config_path),
            "--json",
            *arguments,
        )
    )
    if payload.get("outcome") == "error":
        error = payload.get("error")
        code = cast(dict[str, object], error).get("code") if isinstance(error, dict) else None
        raise ProbeFailure(f"html-publish {arguments[0]} failed: {code or 'unknown_error'}")
    return payload


def _required_string(payload: dict[str, object], key: str, operation: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProbeFailure(f"html-publish {operation} returned no usable {key}")
    return value


def _assert_baseline_unchanged(state: ProbeState) -> None:
    current = _serve_status()
    if _fingerprint(current) != state.baseline_serve_fingerprint:
        raise ProbeFailure("Tailscale Serve changed after preflight; refusing mutation")
    if _route_state(state, current)[0] != "absent":
        raise ProbeFailure("probe route collided after preflight")
    with socket.socket() as candidate:
        try:
            candidate.bind(("127.0.0.1", state.listen_port))
        except OSError as error:
            raise ProbeFailure("selected loopback port is no longer free") from error


def _assert_owned_live(state: ProbeState) -> None:
    _assert_owner(state)
    _assert_process(state)
    route, fingerprint = _route_state(state, _serve_status())
    if route != "exact" or fingerprint != state.route_fingerprint:
        raise ProbeFailure("probe route no longer has its recorded target and fingerprint")


def _prepare(state: ProbeState) -> tuple[ProbeState, dict[str, object]]:
    if state.phase != "preflight":
        raise ProbeFailure("prepare requires preflight state")
    _host_guard(state.expected_host)
    _private_directory(state.evidence_path, create=False)
    _assert_baseline_unchanged(state)
    state.root_path.mkdir(parents=True, mode=0o700)
    os.chmod(state.root_path, 0o700)
    _owner_file(state).write_text(state.owner_token + "\n", encoding="ascii")
    os.chmod(_owner_file(state), 0o600)
    fixtures_root = state.root_path / "fixtures"
    fixtures = {
        "A": _fixture_tree(fixtures_root / "a", "A"),
        "B": _fixture_tree(fixtures_root / "b", "B"),
    }
    sentinel = f"private-{state.owner_token}\n".encode()
    _write_fixture_file(state.root_path / "private-sentinel.txt", sentinel)
    fixtures["private_sentinel_sha256"] = hashlib.sha256(sentinel).hexdigest()
    _assert_equal_freshness_shape(fixtures)
    _atomic_json(state.config_path, _publisher_config(state))
    public = state.root_path / "runtime/public"
    public.mkdir(parents=True)
    (public / "uncontrolled").symlink_to("/etc/passwd")
    stdout_path = state.evidence_path / "server.stdout.log"
    stderr_path = state.evidence_path / "server.stderr.log"
    server_cmd = (
        state.cli_python,
        "-m",
        "html_publish.server",
        "--bind",
        "127.0.0.1",
        "--port",
        str(state.listen_port),
        "--directory",
        str(public),
    )
    environment = dict(os.environ)
    environment["HTML_PUBLISH_PROBE_OWNER"] = state.owner_token
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        process = subprocess.Popen(
            server_cmd,
            stdout=stdout,
            stderr=stderr,
            env=environment,
            start_new_session=True,
        )
    start_ticks, process_cmd, owner = _process_identity(process.pid)
    if process_cmd != server_cmd or owner != state.owner_token:
        process.terminate()
        raise ProbeFailure("started server process did not preserve its identity")
    state = replace(
        state,
        fixtures=fixtures,
        pid=process.pid,
        process_start_ticks=start_ticks,
        process_cmd=process_cmd,
    )
    _write_state(state)
    _wait_health(state)
    _run(
        (
            "tailscale",
            "serve",
            "--bg",
            "--yes",
            f"--https={state.https_port}",
            f"--set-path={state.route_path}",
            state.route_target,
        ),
        timeout=20,
    )
    current = _serve_status()
    route, fingerprint = _route_state(state, current)
    if route != "exact" or fingerprint is None:
        raise ProbeFailure("Tailscale did not install the exact probe route")
    if _fingerprint(_strip_owned_route(state, current)) != state.baseline_serve_fingerprint:
        raise ProbeFailure("route installation changed unrelated Tailscale Serve state")
    state = replace(state, route_fingerprint=fingerprint)
    _write_state(state)
    initial = _cli(state, "status", "--name", state.name)
    if initial.get("active_revision") is not None:
        raise ProbeFailure("random probe name already has an active revision")
    plan = _cli(
        state,
        "plan",
        "--name",
        state.name,
        "--source",
        str(fixtures_root / "a"),
        "--target",
        state.base_url,
    )
    if plan.get("prediction") != "create":
        raise ProbeFailure("plan A did not predict create")
    published = _cli(
        state,
        "publish",
        "--name",
        state.name,
        "--source",
        str(fixtures_root / "a"),
        "--target",
        state.base_url,
        "--request-id",
        f"{state.run_id}-a",
    )
    revision_a = _required_string(published, "active_revision", "publish")
    archive_commit_a = _required_string(published, "archive_commit", "publish")
    status = _cli(state, "status", "--name", state.name)
    if status.get("active_revision") != revision_a:
        raise ProbeFailure("status did not observe revision A")
    state = replace(
        state,
        phase="prepared",
        revision_a=revision_a,
        archive_commit_a=archive_commit_a,
    )
    _write_state(state)
    payload = {
        "operation": "prepare",
        "phase": state.phase,
        "base_url": state.base_url,
        "stable_url": f"{state.base_url}{state.name}/",
        "revision_a": revision_a,
        "route_fingerprint": fingerprint,
        "cleanup_arguments": ["cleanup", "--state", state.state_path],
        "owned_paths": [state.root, state.evidence_root, state.state_path],
    }
    payload["evidence"] = str(_write_evidence(state, "prepare", payload))
    return state, payload


def _raw_get(url: str, headers: dict[str, str] | None = None) -> dict[str, object]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None or parsed.port is None:
        raise ProbeFailure(f"checkpoint URL is not explicit HTTPS: {url}")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    connection = http.client.HTTPSConnection(
        parsed.hostname,
        parsed.port,
        timeout=10,
        context=ssl.create_default_context(),
    )
    try:
        connection.request("GET", path, headers={"Accept-Encoding": "identity", **(headers or {})})
        response = connection.getresponse()
        body = response.read(MAX_OUTPUT + 1)
        response_headers = {name.lower(): value for name, value in response.getheaders()}
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        raise ProbeFailure(f"HTTPS request failed for {path}: {error}") from error
    finally:
        connection.close()
    if len(body) > MAX_OUTPUT:
        raise ProbeFailure(f"response exceeded {MAX_OUTPUT} bytes for {path}")
    return {
        "status": response.status,
        "headers": response_headers,
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "body_prefix": body[:120].decode("utf-8", errors="replace"),
    }


def _fixture_file(state: ProbeState, label: str, path: str) -> dict[str, object]:
    fixture = cast(dict[str, object], state.fixtures[label])
    return cast(dict[str, dict[str, object]], fixture["files"])[path]


def _expect_response(
    rows: list[dict[str, object]],
    row: str,
    url: str,
    statuses: set[int],
    expected: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    response = _raw_get(url, headers)
    ok = response["status"] in statuses
    if expected is not None:
        ok = ok and response["sha256"] == expected["sha256"]
    cache = cast(dict[str, str], response["headers"]).get("cache-control", "")
    ok = ok and "no-store" in {part.strip().lower() for part in cache.split(",")}
    rows.append({"id": row, "url": url, "ok": ok, "response": response})
    if not ok:
        raise ProbeFailure(f"checkpoint row {row} failed")
    return response


def _checkpoint(state: ProbeState, expected_label: str) -> tuple[ProbeState, dict[str, object]]:
    required_phase = {"A": "prepared", "B": "b-active", "restored-A": "a-restored"}[expected_label]
    if state.phase != required_phase:
        raise ProbeFailure(f"checkpoint {expected_label} requires phase {required_phase}")
    _assert_owned_live(state)
    source_label = "B" if expected_label == "B" else "A"
    stable = f"{state.base_url}{state.name}"
    rows: list[dict[str, object]] = []
    index = _fixture_file(state, source_label, "index.html")
    css = _fixture_file(state, source_label, "assets/state.css")
    validators = dict(state.validators or {})
    prior_label = "A" if expected_label == "B" else "B" if expected_label == "restored-A" else None
    replay: dict[str, str] = {}
    if prior_label is not None:
        previous = validators.get(prior_label)
        if not isinstance(previous, dict):
            raise ProbeFailure(f"checkpoint {expected_label} has no {prior_label} validators")
        previous_values = cast(dict[str, object], previous)
        modified = previous_values.get("last-modified")
        etag = previous_values.get("etag") or f'"{previous_values["sha256"]}"'
        if isinstance(modified, str) and modified:
            replay["If-Modified-Since"] = modified
        replay["If-None-Match"] = cast(str, etag)
    index_response = _expect_response(rows, "F2/F3/F4-index", f"{stable}/", {200}, index, replay)
    _expect_response(rows, "F2/F3/F4-css", f"{stable}/assets/state.css", {200}, css, replay)
    validators[source_label] = {
        "last-modified": cast(dict[str, str], index_response["headers"]).get("last-modified"),
        "etag": cast(dict[str, str], index_response["headers"]).get("etag"),
        "sha256": index_response["sha256"],
    }
    state = replace(state, validators=validators)
    _write_state(state)
    for suffix, fixture_path, content_type in (
        ("paths/space%20name.txt", "paths/space name.txt", "text/plain"),
        (f"paths/{quote('unicodé.txt')}", "paths/unicodé.txt", "text/plain"),
        ("paths/percent%2525.txt", "paths/percent%25.txt", "text/plain"),
    ):
        response = _expect_response(
            rows,
            "P2-path",
            f"{stable}/{suffix}",
            {200},
            _fixture_file(state, source_label, fixture_path),
        )
        if (
            not cast(dict[str, str], response["headers"])
            .get("content-type", "")
            .startswith(content_type)
        ):
            raise ProbeFailure(f"checkpoint P2 returned the wrong content type for {suffix}")
    for url, location in (
        (f"{stable}?view=full", f"{state.name}/?view=full"),
        (f"{stable}/assets?view=full", "assets/?view=full"),
    ):
        response = _expect_response(rows, "P1", url, {301})
        if cast(dict[str, str], response["headers"]).get("location") != location:
            raise ProbeFailure("checkpoint P1 returned the wrong route-relative redirect")
    removed_status = {200} if source_label == "A" else {404}
    removed_expected = (
        _fixture_file(state, "A", "assets/removed.txt") if source_label == "A" else None
    )
    _expect_response(rows, "P3", f"{stable}/assets/removed.txt", removed_status, removed_expected)
    if source_label == "A":
        _expect_response(
            rows, "P4-file-to-directory", f"{stable}/flip", {200}, _fixture_file(state, "A", "flip")
        )
        _expect_response(
            rows,
            "P4-directory-to-file",
            f"{stable}/swap/",
            {200},
            _fixture_file(state, "A", "swap/index.html"),
        )
    else:
        _expect_response(
            rows,
            "P4-file-to-directory",
            f"{stable}/flip/",
            {200},
            _fixture_file(state, "B", "flip/index.html"),
        )
        _expect_response(
            rows, "P4-directory-to-file", f"{stable}/swap", {200}, _fixture_file(state, "B", "swap")
        )
        _expect_response(rows, "P4-obsolete-child", f"{stable}/swap/index.html", {404})
    for suffix in ("missing.txt",):
        _expect_response(rows, "P5", f"{stable}/{suffix}", {404})
    _expect_response(rows, "P5-mount", state.base_url, {404})
    for suffix in (
        ".git/HEAD",
        "archive.git/HEAD",
        "staging/index.html",
        "receipts/receipt.json",
        "probe.json",
        "temporary-link",
    ):
        _expect_response(rows, "A1", f"{state.base_url}{suffix}", {404})
    normalized = _raw_get(f"{state.base_url}../etc/passwd")
    sentinel_sha = state.fixtures.get("private_sentinel_sha256")
    if normalized["sha256"] == sentinel_sha:
        raise ProbeFailure("checkpoint A2 served the private sentinel")
    rows.append(
        {
            "id": "A2-url-normalized-outside-mount",
            "url": f"{state.base_url}../etc/passwd",
            "ok": True,
            "interpretation": "only the private sentinel exclusion applies outside the owned mount",
            "response": normalized,
        }
    )
    for suffix in (
        "%2e%2e/etc/passwd",
        "%252e%252e/etc/passwd",
        "%2fetc%2fpasswd",
        "etc/passwd",
    ):
        response = _expect_response(
            rows, "A2-route-bounded-rejection", f"{state.base_url}{suffix}", {400, 403, 404}
        )
        if response["sha256"] in {sentinel_sha, index["sha256"]}:
            raise ProbeFailure("checkpoint A2 served private or application bytes inside the mount")
    _expect_response(rows, "A3", f"{state.base_url}uncontrolled", {404})
    revision = state.revision_b if source_label == "B" else state.revision_a
    if revision is None:
        raise ProbeFailure("checkpoint has no selected revision")
    mode_rows = []
    for path in (
        state.root_path,
        state.root_path / "runtime",
        state.root_path / "runtime/public",
        state.root_path / "runtime/releases" / revision,
    ):
        details = path.stat()
        mode_rows.append(
            {"path": str(path), "uid": details.st_uid, "mode": oct(stat.S_IMODE(details.st_mode))}
        )
        if details.st_uid != os.getuid() or not details.st_mode & stat.S_IWUSR:
            raise ProbeFailure(f"checkpoint A4 found an unowned path: {path}")
    if state.root_path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ProbeFailure("checkpoint A4 found a non-private probe root")
    fixture = cast(dict[str, object], state.fixtures[source_label])
    payload = {
        "operation": "checkpoint",
        "expect": expected_label,
        "marker": fixture["marker"],
        "stable_url": f"{stable}/",
        "rows": rows,
        "mode_walk": mode_rows,
        "same_size_mtime": True,
    }
    payload["evidence"] = str(_write_evidence(state, f"checkpoint-{expected_label}", payload))
    return state, payload


def _activate_b(state: ProbeState) -> tuple[ProbeState, dict[str, object]]:
    if state.phase != "prepared" or state.revision_a is None:
        raise ProbeFailure("activate-b requires prepared revision A")
    _assert_owned_live(state)
    status = _cli(state, "status", "--name", state.name)
    if status.get("active_revision") != state.revision_a:
        raise ProbeFailure("activate-b observed a revision other than A")
    arguments = (
        "--name",
        state.name,
        "--source",
        str(state.root_path / "fixtures/b"),
        "--target",
        state.base_url,
        "--expected-revision",
        state.revision_a,
    )
    plan = _cli(state, "plan", *arguments)
    if plan.get("prediction") != "update":
        raise ProbeFailure("plan B did not predict update")
    published = _cli(state, "publish", *arguments, "--request-id", f"{state.run_id}-b")
    revision_b = _required_string(published, "active_revision", "publish")
    archive_commit_b = _required_string(published, "archive_commit", "publish")
    if revision_b == state.revision_a:
        raise ProbeFailure("publication B did not advance the active revision")
    status = _cli(state, "status", "--name", state.name)
    if status.get("active_revision") != revision_b:
        raise ProbeFailure("status did not observe revision B")
    state = replace(
        state,
        phase="b-active",
        revision_b=revision_b,
        archive_commit_b=archive_commit_b,
    )
    _write_state(state)
    payload = {"operation": "activate-b", "phase": state.phase, "revision_b": revision_b}
    payload["evidence"] = str(_write_evidence(state, "activate-b", payload))
    return state, payload


def _restore_a(state: ProbeState) -> tuple[ProbeState, dict[str, object]]:
    if (
        state.phase != "b-active"
        or state.revision_a is None
        or state.revision_b is None
        or state.archive_commit_a is None
    ):
        raise ProbeFailure("restore-a requires recorded A and B revisions")
    _assert_owned_live(state)
    status = _cli(state, "status", "--name", state.name)
    if status.get("active_revision") != state.revision_b:
        raise ProbeFailure("restore-a observed a revision other than B")
    restored = _cli(
        state,
        "restore",
        "--name",
        state.name,
        "--archive-commit",
        state.archive_commit_a,
        "--target",
        state.base_url,
        "--expected-revision",
        state.revision_b,
        "--request-id",
        f"{state.run_id}-restore-a",
    )
    restored_revision = _required_string(restored, "active_revision", "restore")
    if restored_revision != state.revision_a:
        raise ProbeFailure("restore-a did not select the original archived A revision")
    status = _cli(state, "status", "--name", state.name)
    if status.get("active_revision") != state.revision_a:
        raise ProbeFailure("status did not observe restored revision A")
    state = replace(state, phase="a-restored", restored_revision=restored_revision)
    _write_state(state)
    payload = {
        "operation": "restore-a",
        "phase": state.phase,
        "restored_revision": restored_revision,
    }
    payload["evidence"] = str(_write_evidence(state, "restore-a", payload))
    return state, payload


def _cleanup(state: ProbeState) -> tuple[ProbeState, dict[str, object]]:
    if state.phase == "cleaned":
        return state, {"operation": "cleanup", "phase": "cleaned", "unchanged": True}
    _host_guard(state.expected_host)
    _private_directory(state.evidence_path, create=False)
    if not state.root_path.exists():
        if state.phase != "preflight":
            raise ProbeFailure("the disposable root disappeared before cleanup")
        current = _serve_status()
        if (
            _fingerprint(current) != state.baseline_serve_fingerprint
            or _route_state(state, current)[0] != "absent"
        ):
            raise ProbeFailure("Serve state changed after preflight; no owned route was removed")
        state = replace(state, phase="cleaned")
        _write_state(state)
        payload = {
            "operation": "cleanup",
            "phase": "cleaned",
            "route_removed": False,
            "process_stopped": False,
            "root_retained": False,
            "state_retained": True,
            "evidence_retained": True,
        }
        payload["evidence"] = str(_write_evidence(state, "cleanup", payload))
        return state, payload
    _assert_owner(state)
    current = _serve_status()
    route, fingerprint = _route_state(state, current)
    if route == "collision" or (route == "exact" and fingerprint != state.route_fingerprint):
        raise ProbeFailure("probe route target or fingerprint changed; leaving it for inspection")
    unrelated_drift = (
        _fingerprint(_strip_owned_route(state, current)) != state.baseline_serve_fingerprint
    )
    if route == "exact":
        _run(
            (
                "tailscale",
                "serve",
                "--yes",
                f"--https={state.https_port}",
                f"--set-path={state.route_path}",
                "off",
            ),
            timeout=20,
        )
    after_route = _serve_status()
    route_after, _ = _route_state(state, after_route)
    if route_after != "absent":
        raise ProbeFailure("scoped route removal did not remove the probe handler")
    process_stopped = False
    if state.pid is not None:
        _assert_process(state)
        os.kill(state.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while Path(f"/proc/{state.pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if Path(f"/proc/{state.pid}").exists():
            raise ProbeFailure("probe server did not stop after SIGTERM")
        process_stopped = True
    observations = _observations(state.dns_name)
    serve_restored = _fingerprint(after_route) == state.baseline_serve_fingerprint
    observations_restored = observations == state.baseline_observations
    c1 = serve_restored and observations_restored and not unrelated_drift
    if c1:
        shutil.rmtree(state.root_path)
        state = replace(state, phase="cleaned")
        _write_state(state)
    payload = {
        "operation": "cleanup",
        "phase": state.phase,
        "route_removed": route == "exact",
        "process_stopped": process_stopped,
        "unrelated_route_drift": unrelated_drift,
        "serve_restored": serve_restored,
        "observations_restored": observations_restored,
        "root_retained": state.root_path.exists(),
        "state_retained": Path(state.state_path).exists(),
        "evidence_retained": state.evidence_path.exists(),
    }
    payload["evidence"] = str(_write_evidence(state, "cleanup", payload))
    if not c1:
        raise ProbeFailure(
            "cleanup removed owned route and process but C1 failed; "
            f"root retained at {state.root_path}"
        )
    return state, payload


def main(argv: list[str] | None = None) -> int:
    try:
        parsed = _parser().parse_args(argv)
        operation = cast(str, parsed.operation)
        if operation == "preflight":
            payload = _preflight(cast(Path, parsed.output))
        else:
            state = _load_state(cast(Path, parsed.state))
            if operation == "prepare":
                _, payload = _prepare(state)
            elif operation == "checkpoint":
                _, payload = _checkpoint(state, cast(str, parsed.expect))
            elif operation == "activate-b":
                _, payload = _activate_b(state)
            elif operation == "restore-a":
                _, payload = _restore_a(state)
            else:
                try:
                    _, payload = _cleanup(state)
                except ProbeFailure as error:
                    evidence = _write_evidence(
                        state,
                        "cleanup-refused",
                        {"operation": "cleanup", "outcome": "error", "message": str(error)},
                    )
                    raise ProbeFailure(f"{error}; evidence: {evidence}") from error
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    except ProbeFailure as error:
        print(
            json.dumps({"outcome": "error", "message": str(error)}, separators=(",", ":")),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
