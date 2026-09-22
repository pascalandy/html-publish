from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit

from html_publish import __version__
from html_publish.model import Config

_UNIT_NAME = re.compile(r"html-publish(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?\Z")


class HostError(Exception):
    def __init__(self, code: str, message: str, next_action: str = "inspect") -> None:
        super().__init__(message)
        self.code = code
        self.next_action = next_action


@dataclass(frozen=True)
class Route:
    host: str
    port: int
    mount: str
    proxy: str

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"


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
    route: Route | None
    unit: str | None


@dataclass(frozen=True)
class Observation:
    record: dict[str, object] | None
    unit_bytes: str | None
    unit_mode: int | None
    manager: dict[str, str]
    route_handler: str | None
    routes: dict[str, object] | None


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


def _route(base_url: str, port: int) -> Route:
    parsed = urlsplit(base_url)
    path = parsed.path
    decoded = unquote(path)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not path.endswith("/")
        or decoded != path
        or "//" in path
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise HostError(
            "invalid_route", "Tailscale setup needs an unambiguous HTTPS base URL ending in /"
        )
    return Route(parsed.hostname, parsed.port or 443, path, f"http://127.0.0.1:{port}")


def make_spec(
    config_path: Path, config: Config, unit_name: str, port: int, tailscale: bool
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
    raw = {
        "archive": str(config.archive),
        "runtime": str(config.runtime),
        "base_url": config.base_url,
        "allow_http": config.allow_http,
        "object_format": config.object_format,
        "limits": asdict(config.limits),
    }
    fingerprint = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    route = _route(config.base_url, port) if tailscale else None
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
        route,
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


def _tailscale(route: Route) -> dict[str, object]:
    try:
        status: object = json.loads(_run(["tailscale", "status", "--json"]).stdout)
    except json.JSONDecodeError as error:
        raise HostError("tailscale_state", f"Tailscale status is not JSON: {error}") from error
    if not isinstance(status, dict):
        raise HostError("tailscale_state", "Tailscale status is not an object")
    status = cast(dict[str, object], status)
    if status.get("BackendState") != "Running":
        raise HostError(
            "tailscale_unavailable", "Tailscale must already be running and authenticated"
        )
    self_node = status.get("Self")
    dns = cast(dict[str, object], self_node).get("DNSName") if isinstance(self_node, dict) else None
    if not isinstance(dns, str) or dns.rstrip(".").lower() != route.host.lower():
        raise HostError(
            "tailscale_identity",
            "Configured HTTPS host does not match the authenticated local node",
        )
    domains = status.get("CertDomains")
    if not isinstance(domains, list) or route.host not in domains:
        raise HostError(
            "tailscale_https_unavailable",
            "Tailscale does not report this host as an enabled HTTPS certificate domain",
        )
    try:
        serve: object = json.loads(_run(["tailscale", "serve", "status", "--json"]).stdout)
    except json.JSONDecodeError as error:
        raise HostError("tailscale_state", f"Serve status is not JSON: {error}") from error
    if not isinstance(serve, dict):
        raise HostError("tailscale_state", "Serve status is not an object")
    return cast(dict[str, object], serve)


def _route_handler(serve: dict[str, object], route: Route) -> str | None:
    allow_funnel: object = serve.get("AllowFunnel", {})
    if isinstance(allow_funnel, dict) and cast(dict[str, object], allow_funnel).get(route.key):
        raise HostError("route_collision", "Funnel is enabled on the selected HTTPS port")
    tcp: object = serve.get("TCP", {})
    if isinstance(tcp, dict) and cast(dict[str, object], tcp).get(str(route.port)):
        raise HostError("route_collision", "Selected port already has a TCP Serve handler")
    web: object = serve.get("Web", {})
    if not isinstance(web, dict):
        raise HostError("tailscale_state", "Serve Web state is malformed")
    web = cast(dict[str, object], web)
    for endpoint, value in web.items():
        if endpoint != route.key and endpoint.endswith(f":{route.port}"):
            raise HostError("route_collision", f"Selected HTTPS port has foreign host {endpoint}")
        if endpoint != route.key:
            continue
        if not isinstance(value, dict):
            raise HostError("tailscale_state", "Serve handler state is malformed")
        value = cast(dict[str, object], value)
        handlers_raw = value.get("Handlers", {})
        if not isinstance(handlers_raw, dict):
            raise HostError("tailscale_state", "Serve handler state is malformed")
        handlers = cast(dict[str, object], handlers_raw)
        for mount in handlers:
            if mount != route.mount and (
                mount.startswith(route.mount) or route.mount.startswith(mount)
            ):
                raise HostError("route_collision", f"Serve mount overlaps existing handler {mount}")
        handler = handlers.get(route.mount)
        if handler is None:
            return None
        if not isinstance(handler, dict):
            raise HostError("route_collision", "Selected Serve handler is not an HTTP proxy")
        handler = cast(dict[str, object], handler)
        if not isinstance(handler.get("Proxy"), str):
            raise HostError("route_collision", "Selected Serve handler is not an HTTP proxy")
        return cast(str, handler["Proxy"])
    return None


def _without_handler(serve: dict[str, object], route: Route) -> dict[str, object]:
    remaining = copy.deepcopy(serve)
    web = remaining.get("Web")
    if isinstance(web, dict):
        web = cast(dict[str, object], web)
        selected = web.get(route.key)
        if isinstance(selected, dict):
            selected = cast(dict[str, object], selected)
            handlers = selected.get("Handlers")
            if isinstance(handlers, dict):
                handlers = cast(dict[str, object], handlers)
                handlers.pop(route.mount, None)
                if not handlers:
                    web.pop(route.key, None)
        if not web:
            remaining.pop("Web", None)
    return remaining


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
    routes = _tailscale(spec.route) if spec.route else None
    handler = _route_handler(routes, spec.route) if routes is not None and spec.route else None
    return Observation(record, unit_bytes, unit_mode, manager, handler, routes)


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
        if observed.route_handler is not None:
            blockers.append("Serve handler exists without this installation's ownership record")
    else:
        if load != "loaded" and record.get("pending") != "unit":
            blockers.append(f"Owned unit is not loaded by the user manager: {load}")
        expected = {
            "uid": os.geteuid(),
            "config_path": str(spec.config_path),
            "fingerprint": spec.fingerprint,
            "unit_path": str(spec.unit_path),
            "listen_port": spec.port,
            "route": asdict(spec.route) if spec.route else None,
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
        if record.get("pending") == "route" and observed.route_handler is not None:
            blockers.append(
                "Pending Serve route appeared after an uncertain command; "
                "inspect ownership manually"
            )
        elif record.get("route_done") is True and observed.route_handler != (
            spec.route.proxy if spec.route else None
        ):
            blockers.append("Owned Serve handler has drifted")
        elif record.get("route_done") is not True and observed.route_handler is not None:
            blockers.append("Serve handler is present without completed ownership")
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
        and (spec.route is None or observed.route_handler == spec.route.proxy)
        and not blockers
        and record.get("pending") is None
    )
    return {
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
        "route": asdict(spec.route) if spec.route else None,
        "prerequisites": {
            "durable_uv_tool": spec.executable is not None,
            "systemd_user": observed is not None,
            "tailscale_https": spec.route is None
            or (observed is not None and observed.routes is not None),
        },
        "observation": {
            "record": record,
            "unit_bytes": observed.unit_bytes if observed else None,
            "unit_mode": observed.unit_mode if observed else None,
            "manager": observed.manager if observed else None,
            "route_handler": observed.route_handler if observed else None,
        },
        "blockers": blockers,
        "proposed_effects": []
        if same or blockers
        else ["record", "unit", "daemon_reload", "enable", "start", "loopback_probe"]
        + (["tailscale_serve"] if spec.route else []),
    }


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


@contextmanager
def _lock(spec: HostSpec) -> Generator[None, None, None]:
    lock = spec.record_path.with_suffix(".lock")
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
            "tailscale_serve",
        )
    }
    with _lock(spec):
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
            "route": asdict(spec.route) if spec.route else None,
            "unit": spec.unit,
            "enabled": False,
            "route_done": False,
            "pending": "unit",
        }
        try:
            _save_record(spec, record)
            effects["record"] = "changed"
            refresh_service = (
                observed.unit_bytes != spec.unit
                or record.get("pending") == "unit"
                or record.get("package_hash") != spec.package_hash
            )
            if refresh_service:
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
            with urllib.request.urlopen(
                f"http://127.0.0.1:{spec.port}/_html-publish-health", timeout=5
            ) as response:
                if response.status != 200 or response.read() != b"ok\n":
                    raise HostError("health_failed", "Loopback health response did not match")
            effects["loopback_probe"] = "unchanged"
            record["package_hash"] = spec.package_hash
            record["pending"] = None
            _save_record(spec, record)
            if spec.route:
                before = _tailscale(spec.route)
                current_handler = _route_handler(before, spec.route)
                if current_handler is not None and record.get("route_done") is not True:
                    raise HostError(
                        "route_collision", "Serve handler appeared before route mutation"
                    )
                if current_handler is None:
                    record["pending"] = "route"
                    _save_record(spec, record)
                    effects["tailscale_serve"] = "unknown"
                    _run(
                        [
                            "tailscale",
                            "serve",
                            "--bg",
                            f"--https={spec.route.port}",
                            f"--set-path={spec.route.mount}",
                            spec.route.proxy,
                        ]
                    )
                    effects["tailscale_serve"] = "changed"
                    after = _tailscale(spec.route)
                    if _route_handler(after, spec.route) != spec.route.proxy:
                        raise HostError(
                            "route_failed", "Selected Serve handler does not match after setup"
                        )
                    if _without_handler(before, spec.route) != _without_handler(after, spec.route):
                        raise HostError(
                            "route_concurrent_change",
                            "Unrelated Serve state changed during route setup",
                        )
                    record["route_done"] = True
                    record["pending"] = None
                    _save_record(spec, record)
                else:
                    effects["tailscale_serve"] = "unchanged"
            return {
                **plan,
                "outcome": "applied",
                "effects": effects,
                "verification": {"loopback": "passed", "private_https": "not_checked"},
                "observation_after": _systemd(spec.unit_name),
            }
        except (HostError, OSError, urllib.error.URLError) as error:
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
