from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from html_publish import __version__
from html_publish.configuration import read_document
from html_publish.model import Config, PublishError

_UNIT_NAME = re.compile(r"html-publish(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?\Z")


class HostError(Exception):
    def __init__(self, code: str, message: str, next_action: str = "inspect") -> None:
        super().__init__(message)
        self.code = code
        self.next_action = next_action


@dataclass(frozen=True)
class HostSpec:
    config_path: Path
    fingerprint: str
    package_hash: str
    public: Path
    unit_name: str
    unit_path: Path
    record_path: Path
    executable: Path | None
    environment: Path | None
    bind: str
    port: int
    unit: str | None


@dataclass(frozen=True)
class Observation:
    record: dict[str, object] | None
    unit_bytes: str | None
    unit_mode: int | None
    manager: dict[str, str]


@dataclass(frozen=True)
class ServiceBlocker:
    code: str
    message: str
    next_action: str


@dataclass(frozen=True)
class HealthyOwnedService:
    installation_id: str
    uid: int
    config_path: Path
    config_fingerprint: str
    executable: Path
    environment: Path
    package_hash: str
    unit_name: str
    unit_path: Path
    unit_digest: str
    listen_port: int


@dataclass(frozen=True)
class ServiceInspection:
    selected: dict[str, object]
    manager: dict[str, str] | None
    health: str
    blockers: tuple[ServiceBlocker, ...]
    healthy: HealthyOwnedService | None


def _run(
    args: list[str], *, seconds: float = 8, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HostError("inspection_failed", f"Cannot run {' '.join(args)}: {error}") from error
    if check and result.returncode:
        raise HostError(
            "command_failed",
            f"{' '.join(args)} exited {result.returncode}: {result.stderr.strip()}",
        )
    return result


def _xdg_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    path = Path(value).expanduser() if value else fallback
    if not path.is_absolute():
        raise HostError("invalid_path", f"{variable} must be absolute")
    return path


def _safe_path(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise HostError("path_collision", f"Symlink in host setup path: {current}")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise HostError("path_collision", f"Non-directory host setup ancestor: {current}")
        if info.st_uid != os.geteuid() and current.is_relative_to(Path.home()):
            raise HostError("path_collision", f"Foreign owner in host setup path: {current}")


def _unit_arg(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise HostError("invalid_path", "A unit argument contains a control character")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def _durable_executable() -> tuple[Path | None, Path | None, str | None]:
    try:
        tool_dir = Path(_run(["uv", "tool", "dir"]).stdout.strip()).resolve()
    except HostError as error:
        return None, None, str(error)
    environment = tool_dir / "html-publish"
    executable = environment / "bin" / "html-publish"
    if Path(sys.prefix).resolve() != environment.resolve() or not executable.is_file():
        return (
            None,
            None,
            (
                "This process is not the durable uv tool environment. Install with "
                "uv tool install --from <wheel-or-pinned-source> html-publish "
                "and run its installed executable"
            ),
        )
    return executable, environment, None


def _package_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _config_fingerprint(config: Config) -> str:
    raw = {
        "archive": str(config.archive),
        "runtime": str(config.runtime),
        "base_url": config.base_url,
        "allow_http": config.allow_http,
        "object_format": config.object_format,
        "limits": asdict(config.limits),
    }
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def make_spec(
    config_path: Path, config: Config, unit_name: str, port: int
) -> tuple[HostSpec, list[str]]:
    if sys.platform != "linux":
        raise HostError("unsupported_host", "Host setup requires Linux")
    if not _UNIT_NAME.fullmatch(unit_name):
        raise HostError(
            "invalid_unit", "Unit name must be html-publish or html-publish-<lowercase-name>"
        )
    if port < 1 or port > 65535:
        raise HostError("invalid_port", "Persistent host port must be between 1 and 65535")
    if not config_path.is_absolute():
        raise HostError("invalid_config", "Selected config path must be absolute")
    unit_path = (
        _xdg_path("XDG_CONFIG_HOME", Path.home() / ".config")
        / "systemd"
        / "user"
        / f"{unit_name}.service"
    )
    record_path = (
        _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state")
        / "html-publish"
        / "hosts"
        / f"{unit_name}.json"
    )
    for path in (unit_path, record_path):
        _safe_path(path)
    executable, environment, executable_error = _durable_executable()
    fingerprint = _config_fingerprint(config)
    unit = None
    if executable is not None:
        unit = "\n".join(
            (
                "[Unit]",
                "Description=html-publish private pages",
                "",
                "[Service]",
                "Type=simple",
                "ExecStart="
                + " ".join(
                    (
                        _unit_arg(str(executable)),
                        "--config",
                        _unit_arg(str(config_path)),
                        "host",
                        "serve",
                        "--bind",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    )
                ),
                "Restart=on-failure",
                "RestartSec=2",
                "NoNewPrivileges=true",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            )
        )
    return HostSpec(
        config_path,
        fingerprint,
        _package_hash(),
        config.runtime / "public",
        unit_name + ".service",
        unit_path,
        record_path,
        executable,
        environment,
        "127.0.0.1",
        port,
        unit,
    ), ([executable_error] if executable_error else [])


def _systemd(unit_name: str) -> dict[str, str]:
    result = _run(
        [
            "systemctl",
            "--user",
            "show",
            unit_name,
            "--property=LoadState,FragmentPath,DropInPaths,UnitFileState,ActiveState,MainPID",
        ],
        check=False,
    )
    if result.returncode and not result.stdout.strip():
        raise HostError(
            "systemd_unavailable", result.stderr.strip() or "systemd user manager unavailable"
        )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def observe(spec: HostSpec) -> Observation:
    record = None
    if spec.record_path.exists():
        if not spec.record_path.is_file() or spec.record_path.stat().st_uid != os.geteuid():
            raise HostError(
                "record_collision", f"Host record is not owned by this user: {spec.record_path}"
            )
        try:
            value = json.loads(spec.record_path.read_text())
        except (OSError, ValueError) as error:
            raise HostError("record_corrupt", f"Cannot read host record: {error}") from error
        if not isinstance(value, dict):
            raise HostError("record_corrupt", "Host record is not an object")
        record = cast(dict[str, object], value)
        if record.get("schema_version") != 1 or not isinstance(record.get("installation_id"), str):
            raise HostError(
                "record_corrupt", "Host record has an unknown format or no installation ID"
            )
    unit_bytes = spec.unit_path.read_text() if spec.unit_path.exists() else None
    unit_mode = stat.S_IMODE(spec.unit_path.stat().st_mode) if unit_bytes is not None else None
    if unit_bytes is not None and spec.unit_path.stat().st_uid != os.geteuid():
        raise HostError("unit_collision", f"Unit is not owned by this user: {spec.unit_path}")
    manager = _systemd(spec.unit_name)
    return Observation(record, unit_bytes, unit_mode, manager)


def _blockers(spec: HostSpec, observed: Observation, prerequisites: list[str]) -> list[str]:
    blockers = list(prerequisites)
    record = observed.record
    manager = observed.manager
    load = manager.get("LoadState", "not-found")
    fragment = manager.get("FragmentPath", "")
    if manager.get("DropInPaths"):
        blockers.append("Unit has drop-ins")
    if load not in {"not-found", "loaded"} or manager.get("UnitFileState") in {
        "masked",
        "linked",
        "alias",
        "linked-runtime",
    }:
        blockers.append(f"Unit manager state is {load}/{manager.get('UnitFileState')}")
    if fragment and fragment != str(spec.unit_path):
        blockers.append(f"Effective unit fragment is foreign: {fragment}")
    if record is None:
        if observed.unit_bytes is not None or load != "not-found":
            blockers.append("Unit exists without this installation's ownership record")
    else:
        if load != "loaded" and record.get("pending") != "unit":
            blockers.append(f"Owned unit is not loaded by the user manager: {load}")
        expected = {
            "uid": os.geteuid(),
            "config_path": str(spec.config_path),
            "fingerprint": spec.fingerprint,
            "unit_path": str(spec.unit_path),
            "listen_port": spec.port,
        }
        for key, value in expected.items():
            if record.get(key) != value:
                blockers.append(f"Owned host record {key} differs from selected configuration")
        pending_unit = record.get("pending") == "unit"
        if pending_unit:
            previous = record.get("unit")
            unit_ok = observed.unit_bytes in (previous, spec.unit)
            if previous == spec.unit:
                unit_ok = unit_ok or observed.unit_bytes is None
            mode_ok = observed.unit_bytes is None or observed.unit_mode == 0o644
        else:
            unit_ok = observed.unit_bytes == record.get("unit")
            mode_ok = observed.unit_mode == 0o644
        if not unit_ok or not mode_ok:
            blockers.append("Owned unit bytes or mode have drifted")
        if record.get("enabled") is True and manager.get("UnitFileState") != "enabled":
            blockers.append("Owned user unit enablement has drifted")
    return blockers


def preview(spec: HostSpec, prerequisites: list[str]) -> dict[str, object]:
    try:
        observed = observe(spec)
        blockers = _blockers(spec, observed, prerequisites)
    except HostError as error:
        observed = None
        blockers = [str(error), *prerequisites]
    record = observed.record if observed else None
    same = bool(
        record
        and observed
        and spec.unit == observed.unit_bytes
        and record.get("package_hash") == spec.package_hash
        and observed.manager.get("ActiveState") == "active"
        and not blockers
        and record.get("pending") is None
    )
    service_effects = (
        []
        if same or blockers
        else ["record", "unit", "daemon_reload", "enable", "start", "loopback_probe"]
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "operation": "host.setup",
        "outcome": "blocked" if blockers else "unchanged" if same else "planned",
        "config": str(spec.config_path),
        "fingerprint": spec.fingerprint,
        "package_hash": spec.package_hash,
        "public": str(spec.public),
        "unit_name": spec.unit_name,
        "unit_path": str(spec.unit_path),
        "record_path": str(spec.record_path),
        "directories_to_create": [
            str(path)
            for path in (spec.unit_path.parent, spec.record_path.parent)
            if not path.exists()
        ],
        "executable": str(spec.executable) if spec.executable else None,
        "environment": str(spec.environment) if spec.environment else None,
        "version": __version__,
        "unit": spec.unit,
        "listen": f"{spec.bind}:{spec.port}",
        "prerequisites": {
            "durable_uv_tool": spec.executable is not None,
            "systemd_user": observed is not None,
        },
        "observation": {
            "record": record,
            "unit_bytes": observed.unit_bytes if observed else None,
            "unit_mode": observed.unit_mode if observed else None,
            "manager": observed.manager if observed else None,
        },
        "blockers": blockers,
        "proposed_effects": service_effects,
    }
    return report


def _service_blocker(error: HostError) -> ServiceBlocker:
    return ServiceBlocker(error.code, str(error), error.next_action)


def _read_loopback(port: int, timeout: float) -> tuple[int, bytes]:
    expires = time.monotonic() + timeout
    request = (
        b"GET /_html-publish-health HTTP/1.0\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Accept-Encoding: identity\r\n"
        b"Connection: close\r\n\r\n"
    )

    def remaining() -> float:
        remaining = expires - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("loopback health deadline expired")
        return remaining

    with socket.create_connection(("127.0.0.1", port), timeout=remaining()) as connection:
        connection.settimeout(remaining())
        connection.sendall(request)
        response = bytearray()
        header_end = -1
        while header_end < 0:
            connection.settimeout(remaining())
            chunk = connection.recv(4096)
            if not chunk:
                raise OSError("loopback health response ended before its headers")
            response.extend(chunk)
            header_end = response.find(b"\r\n\r\n")
            if header_end < 0 and len(response) > 8192:
                raise OSError("loopback health headers exceed 8192 bytes")
        if header_end > 8192:
            raise OSError("loopback health headers exceed 8192 bytes")
        raw_headers = bytes(response[:header_end])
        body = bytearray(response[header_end + 4 : header_end + 8])
        lines = raw_headers.split(b"\r\n")
        status_parts = lines[0].split(b" ", 2)
        if (
            len(status_parts) < 2
            or status_parts[0] not in {b"HTTP/1.0", b"HTTP/1.1"}
            or len(status_parts[1]) != 3
            or not status_parts[1].isdigit()
        ):
            raise OSError("loopback health returned a malformed HTTP status")
        content_lengths = [
            value.strip()
            for line in lines[1:]
            if b":" in line
            for name, value in (line.split(b":", 1),)
            if name.strip().lower() == b"content-length"
        ]
        if len(content_lengths) > 1 or (content_lengths and not content_lengths[0].isdigit()):
            raise OSError("loopback health returned an invalid Content-Length")
        content_length = int(content_lengths[0]) if content_lengths else None
        body_limit = min(content_length, 4) if content_length is not None else 4
        del body[body_limit:]
        while len(body) < body_limit:
            connection.settimeout(remaining())
            chunk = connection.recv(body_limit - len(body))
            if not chunk:
                break
            body.extend(chunk)
        return int(status_parts[1]), bytes(body)


def _check_loopback(port: int) -> None:
    url = f"http://127.0.0.1:{port}/_html-publish-health"
    try:
        status, body = _read_loopback(port, 1)
        if status != 200 or body != b"ok\n":
            raise HostError(
                "health_failed",
                f"Loopback health response did not match at {url}",
                "repair_owned_service",
            )
    except (TimeoutError, OSError) as error:
        raise HostError(
            "health_failed",
            f"Loopback health failed at {url}: {error}",
            "repair_owned_service",
        ) from error


def inspect_owned_service(config_path: Path, config: Config, unit_name: str) -> ServiceInspection:
    selected: dict[str, object] = {
        "config_path": str(config_path),
        "unit_name": f"{unit_name}.service",
    }
    blockers: list[ServiceBlocker] = []
    healthy: HealthyOwnedService | None = None
    manager: dict[str, str] | None = None
    health = "not_checked"
    try:
        if sys.platform != "linux":
            raise HostError("unsupported_host", "Host route setup requires Linux", "use_linux_host")
        if not _UNIT_NAME.fullmatch(unit_name):
            raise HostError(
                "invalid_unit",
                "Unit name must be html-publish or html-publish-<lowercase-name>",
                "select_owned_service",
            )
        record_path = (
            _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state")
            / "html-publish"
            / "hosts"
            / f"{unit_name}.json"
        )
        unit_path = (
            _xdg_path("XDG_CONFIG_HOME", Path.home() / ".config")
            / "systemd"
            / "user"
            / f"{unit_name}.service"
        )
        for path in (record_path, unit_path):
            _safe_path(path)
        executable, environment, _ = _durable_executable()
        selected.update(
            {
                "record_path": str(record_path),
                "config_fingerprint": _config_fingerprint(config),
                "package_hash": _package_hash(),
                "executable": str(executable) if executable else None,
                "environment": str(environment) if environment else None,
                "unit_path": str(unit_path),
            }
        )
        if not record_path.exists():
            manager = _systemd(f"{unit_name}.service")
            raise HostError(
                "service_not_owned",
                f"Owned host record does not exist: {record_path}",
                "run_host_setup_apply",
            )
        if not record_path.is_file() or record_path.stat().st_uid != os.geteuid():
            raise HostError(
                "record_collision",
                f"Host record is not owned by this user: {record_path}",
                "inspect_service_record",
            )
        try:
            raw_record = json.loads(record_path.read_text())
        except (OSError, ValueError) as error:
            raise HostError(
                "record_corrupt",
                f"Cannot read host record: {error}",
                "inspect_service_record",
            ) from error
        if not isinstance(raw_record, dict):
            raise HostError(
                "record_corrupt", "Host record is not an object", "inspect_service_record"
            )
        initial_record = cast(dict[str, object], raw_record)
        listen_port = initial_record.get("listen_port")
        if not isinstance(listen_port, int) or isinstance(listen_port, bool):
            raise HostError(
                "record_corrupt",
                "Host record has no valid listen port",
                "inspect_service_record",
            )
        spec, prerequisites = make_spec(config_path, config, unit_name, listen_port)
        selected.update(
            {
                "config_fingerprint": spec.fingerprint,
                "package_hash": spec.package_hash,
                "executable": str(spec.executable) if spec.executable else None,
                "environment": str(spec.environment) if spec.environment else None,
                "unit_path": str(spec.unit_path),
                "listen": f"{spec.bind}:{spec.port}",
            }
        )
        observed = observe(spec)
        manager = observed.manager
        record = observed.record
        if record != initial_record:
            blockers.append(
                ServiceBlocker(
                    "service_drift",
                    "Owned host record changed during service inspection",
                    "inspect_service_record",
                )
            )
        if record is None:
            blockers.append(
                ServiceBlocker(
                    "service_drift",
                    "Owned host record disappeared during service inspection",
                    "inspect_service_record",
                )
            )
            return ServiceInspection(selected, manager, health, tuple(blockers), None)
        for message in _blockers(spec, observed, prerequisites):
            blockers.append(ServiceBlocker("service_drift", message, "repair_owned_service"))
        if spec.unit != observed.unit_bytes or record.get("unit") != spec.unit:
            blockers.append(
                ServiceBlocker(
                    "service_drift",
                    "Owned unit does not match the selected executable and configuration",
                    "run_host_setup_apply",
                )
            )
        if record.get("package_hash") != spec.package_hash:
            blockers.append(
                ServiceBlocker(
                    "service_drift",
                    "Owned host package differs from the selected installed package",
                    "run_host_setup_apply",
                )
            )
        if record.get("pending") is not None:
            blockers.append(
                ServiceBlocker(
                    "service_pending",
                    f"Owned host setup has pending step {record.get('pending')}",
                    "inspect_service_setup",
                )
            )
        if record.get("enabled") is not True or observed.manager.get("UnitFileState") != "enabled":
            blockers.append(
                ServiceBlocker(
                    "service_drift",
                    "Owned host record and user manager must both show the unit enabled",
                    "repair_owned_service",
                )
            )
        if observed.manager.get("ActiveState") != "active":
            blockers.append(
                ServiceBlocker(
                    "service_inactive",
                    "Owned user service is not active",
                    "repair_owned_service",
                )
            )
        if not blockers:
            try:
                _check_loopback(spec.port)
                health = "passed"
            except HostError as error:
                blockers.append(_service_blocker(error))
                health = "failed"
        if not blockers:
            confirmed = observe(spec)
            if confirmed != observed:
                blockers.append(
                    ServiceBlocker(
                        "service_drift",
                        "Owned service changed during loopback health inspection",
                        "inspect_service_state",
                    )
                )
        if not blockers:
            installation_id = record.get("installation_id")
            uid = record.get("uid")
            if (
                not isinstance(installation_id, str)
                or not isinstance(uid, int)
                or isinstance(uid, bool)
                or spec.executable is None
                or spec.environment is None
                or spec.unit is None
            ):
                raise HostError(
                    "record_corrupt",
                    "Owned host record or selected installation identity is incomplete",
                    "inspect_service_record",
                )
            healthy = HealthyOwnedService(
                installation_id=installation_id,
                uid=uid,
                config_path=spec.config_path,
                config_fingerprint=spec.fingerprint,
                executable=spec.executable,
                environment=spec.environment,
                package_hash=spec.package_hash,
                unit_name=spec.unit_name,
                unit_path=spec.unit_path,
                unit_digest=hashlib.sha256(spec.unit.encode()).hexdigest(),
                listen_port=spec.port,
            )
    except HostError as error:
        blockers.append(_service_blocker(error))
    return ServiceInspection(selected, manager, health, tuple(blockers), healthy)


def _write(path: Path, content: str, mode: int) -> None:
    _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".html-publish-", delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _save_record(spec: HostSpec, record: dict[str, object]) -> None:
    _write(spec.record_path, json.dumps(record, sort_keys=True, indent=2) + "\n", 0o600)


def _require_selected(spec: HostSpec) -> None:
    try:
        _, config = read_document("publisher", spec.config_path)
    except PublishError as error:
        raise HostError("config_drift", str(error)) from error
    if not isinstance(config, Config):
        raise HostError("config_drift", "Selected configuration is no longer a publisher")
    current, _ = make_spec(
        spec.config_path,
        config,
        spec.unit_name.removesuffix(".service"),
        spec.port,
    )
    if current != spec:
        raise HostError(
            "host_drift", "Selected config or installed executable changed during setup"
        )


def _probe_loopback(spec: HostSpec) -> None:
    url = f"http://127.0.0.1:{spec.port}/_html-publish-health"
    expires = time.monotonic() + 10
    last_error = "listener did not answer"
    while True:
        remaining = expires - time.monotonic()
        if remaining <= 0:
            raise HostError(
                "health_failed", f"Loopback health did not become ready at {url}: {last_error}"
            )
        try:
            status, body = _read_loopback(spec.port, min(1, remaining))
            if status != 200 or body != b"ok\n":
                raise HostError("health_failed", f"Loopback health response did not match at {url}")
            return
        except (TimeoutError, OSError) as error:
            last_error = str(error)
        time.sleep(min(0.2, max(0, expires - time.monotonic())))


@contextmanager
def _lock(spec: HostSpec) -> Generator[None, None, None]:
    lock = spec.record_path.parent / ".setup.lock"
    _safe_path(lock)
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock.open("a+") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HostError("setup_busy", f"Another host setup holds {lock}") from error
        yield


def apply(spec: HostSpec, prerequisites: list[str]) -> dict[str, object]:
    effects: dict[str, str] = {
        step: "not_started"
        for step in (
            "record",
            "unit",
            "daemon_reload",
            "enable",
            "start",
            "loopback_probe",
        )
    }
    with _lock(spec):
        _require_selected(spec)
        plan = preview(spec, prerequisites)
        if plan["blockers"]:
            return {**plan, "outcome": "blocked", "effects": effects}
        if plan["outcome"] == "unchanged":
            return {**plan, "effects": effects}
        observed = observe(spec)
        record = observed.record or {
            "schema_version": 1,
            "installation_id": str(uuid.uuid4()),
            "uid": os.geteuid(),
            "config_path": str(spec.config_path),
            "fingerprint": spec.fingerprint,
            "package_hash": spec.package_hash,
            "unit_path": str(spec.unit_path),
            "listen_port": spec.port,
            "unit": spec.unit,
            "enabled": False,
            "pending": "unit",
        }
        try:
            _require_selected(spec)
            _save_record(spec, record)
            effects["record"] = "changed"
            refresh_service = (
                observed.unit_bytes != spec.unit
                or record.get("pending") == "unit"
                or record.get("package_hash") != spec.package_hash
            )
            if refresh_service:
                _require_selected(spec)
                fresh = observe(spec)
                if _blockers(spec, fresh, prerequisites):
                    raise HostError("host_drift", "Host resources changed before unit write")
                record["pending"] = "unit"
                _save_record(spec, record)
                if observed.unit_bytes != spec.unit:
                    _write(spec.unit_path, spec.unit or "", 0o644)
                    effects["unit"] = "changed"
                else:
                    effects["unit"] = "unchanged"
                record["unit"] = spec.unit
                _save_record(spec, record)
                _require_selected(spec)
                fresh = observe(spec)
                if _blockers(spec, fresh, prerequisites):
                    raise HostError("host_drift", "Host resources changed before daemon reload")
                effects["daemon_reload"] = "unknown"
                _run(["systemctl", "--user", "daemon-reload"])
                effects["daemon_reload"] = "changed"
                record["unit"] = spec.unit
                record["pending"] = None
                _save_record(spec, record)
            else:
                effects["unit"] = "unchanged"
            current = _systemd(spec.unit_name)
            if current.get("UnitFileState") != "enabled":
                _require_selected(spec)
                fresh = observe(spec)
                if _blockers(spec, fresh, prerequisites):
                    raise HostError("host_drift", "Host resources changed before enable")
                record["pending"] = "enable"
                _save_record(spec, record)
                effects["enable"] = "unknown"
                _run(["systemctl", "--user", "enable", spec.unit_name])
                effects["enable"] = "changed"
                record["enabled"] = True
                record["pending"] = None
                _save_record(spec, record)
            else:
                effects["enable"] = "unchanged"
                record["enabled"] = True
                if record.get("pending") == "enable":
                    record["pending"] = None
                    _save_record(spec, record)
            action = "restart" if current.get("ActiveState") == "active" else "start"
            if refresh_service or current.get("ActiveState") != "active":
                _require_selected(spec)
                fresh = observe(spec)
                if _blockers(spec, fresh, prerequisites):
                    raise HostError("host_drift", "Host resources changed before start")
                record["pending"] = action
                _save_record(spec, record)
                effects["start"] = "unknown"
                _run(["systemctl", "--user", action, spec.unit_name])
                effects["start"] = "changed"
                record["pending"] = "probe"
                _save_record(spec, record)
            else:
                effects["start"] = "unchanged"
                if record.get("pending") in {"start", "restart"}:
                    record["pending"] = "probe"
                    _save_record(spec, record)
            if _systemd(spec.unit_name).get("ActiveState") != "active":
                raise HostError("service_failed", "Owned user service is not active after start")
            record["pending"] = "probe"
            _save_record(spec, record)
            effects["loopback_probe"] = "unknown"
            try:
                _probe_loopback(spec)
            except HostError:
                effects["loopback_probe"] = "failed"
                raise
            effects["loopback_probe"] = "unchanged"
            record["package_hash"] = spec.package_hash
            record["pending"] = None
            _save_record(spec, record)
            return {
                **plan,
                "outcome": "applied",
                "effects": effects,
                "verification": {"loopback": "passed", "private_https": "not_checked"},
                "observation_after": _systemd(spec.unit_name),
            }
        except (HostError, OSError) as error:
            return {
                **plan,
                "outcome": "error",
                "effects": effects,
                "error": {
                    "code": error.code if isinstance(error, HostError) else "setup_failed",
                    "message": str(error),
                    "next_action": (
                        f"Inspect {spec.record_path}, {spec.unit_path}, "
                        "and selected service state before retry"
                    ),
                },
            }
