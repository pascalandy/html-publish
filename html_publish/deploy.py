from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse

DEFAULT_STATE_ROOT = Path("/home/pascal/.local/share/html-publish")
DEFAULT_CONFIG = Path("/home/pascal/.config/html-publish/publisher.json")
DEFAULT_UNIT = Path("/home/pascal/.config/systemd/user/html-publish.service")
DEFAULT_BASE_URL = "https://om1.donkey-arcturus.ts.net:8444/html-publish/"
DEFAULT_LISTEN_URL = "http://127.0.0.1:4177"
SERVE_PATH = "/html-publish"
UNIT_NAME = "html-publish.service"


class DeployError(Exception):
    pass


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def __call__(self, argv: Sequence[str]) -> CommandResult: ...


Probe = Callable[[str], tuple[bool, str]]


@dataclass(frozen=True)
class Layout:
    state_root: Path
    config: Path
    unit: Path
    base_url: str = DEFAULT_BASE_URL
    listen_url: str = DEFAULT_LISTEN_URL

    @property
    def archive(self) -> Path:
        return self.state_root / "archive.git"

    @property
    def runtime(self) -> Path:
        return self.state_root / "runtime"

    @property
    def incoming(self) -> Path:
        return self.state_root / "incoming"

    @property
    def releases(self) -> Path:
        return self.state_root / "app-releases"

    @property
    def current(self) -> Path:
        return self.state_root / "current"

    @property
    def previous(self) -> Path:
        return self.state_root / "previous"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _run(argv: Sequence[str]) -> CommandResult:
    try:
        result = subprocess.run(
            list(argv),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise DeployError(f"Could not run {argv[0]}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise DeployError(f"Command failed ({result.returncode}): {' '.join(argv)}: {detail}")
    return CommandResult(result.stdout, result.stderr)


def _probe(url: str) -> tuple[bool, str]:
    request = urllib.request.Request(
        url,
        headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"},
    )
    deadline = time.monotonic() + 5
    last_detail = "probe deadline expired"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, last_detail
        try:
            with urllib.request.urlopen(request, timeout=min(0.75, remaining)) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
            last_detail = f"HTTP {status}"
            if status not in {502, 503}:
                return False, last_detail
        except (OSError, urllib.error.URLError) as error:
            last_detail = str(error)
        else:
            if status not in {502, 503}:
                return status == 200, f"HTTP {status}"
            last_detail = f"HTTP {status}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, last_detail
        time.sleep(min(0.2, remaining))


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_symlink(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, path)


def _symlink_target(path: Path) -> Path | None:
    if not path.is_symlink():
        return None
    target = Path(os.readlink(path))
    return target if target.is_absolute() else path.parent / target


def _publisher_config(layout: Layout) -> str:
    payload = {
        "archive": str(layout.archive),
        "runtime": str(layout.runtime),
        "base_url": layout.base_url,
        "allow_http": False,
        "object_format": "sha1",
        "limits": {
            "command_seconds": 120,
            "lock_seconds": 30,
            "verification_seconds": 60,
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _ensure_config(layout: Layout) -> None:
    expected = json.loads(_publisher_config(layout))
    if layout.config.exists():
        try:
            current = json.loads(layout.config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DeployError(f"Existing publisher config is unreadable: {error}") from error
        required = ("archive", "runtime", "base_url")
        conflicts = [name for name in required if current.get(name) != expected[name]]
        if conflicts:
            raise DeployError(
                "Existing publisher config conflicts with this deployment: " + ", ".join(conflicts)
            )
        return
    _atomic_write(layout.config, _publisher_config(layout), 0o600)


def _unit_content(layout: Layout) -> str:
    python = layout.current / ".venv" / "bin" / "python"
    public = layout.runtime / "public"
    return (
        "[Unit]\n"
        "Description=html-publish static publication server\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={python} -m html_publish.server --port 4177 --bind 127.0.0.1 "
        f"--directory {public}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _release_name(wheel: Path) -> str:
    digest = hashlib.sha256()
    with wheel.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256-{digest.hexdigest()}"


def _build_wheel(source: Path, runner: Runner, output: Path) -> Path:
    if not source.is_dir():
        raise DeployError(f"Source checkout is not a directory: {source}")
    runner(("uv", "build", "--wheel", "--out-dir", str(output), str(source)))
    wheels = tuple(output.glob("*.whl"))
    if len(wheels) != 1:
        raise DeployError(f"Expected one wheel from {source}, found {len(wheels)}")
    return wheels[0]


def _install_release(layout: Layout, wheel: Path, runner: Runner) -> Path:
    release = layout.releases / _release_name(wheel)
    marker = release / ".ready"
    if marker.is_file():
        return release
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(parents=True)
    python = release / ".venv" / "bin" / "python"
    try:
        runner(("uv", "venv", "--python", sys.executable, str(release / ".venv")))
        runner(
            (
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                str(wheel),
            )
        )
        runner((str(python), "-m", "html_publish", "--help"))
        runner((str(python), "-m", "html_publish.deploy", "--help"))
        _atomic_write(marker, _release_name(wheel) + "\n", 0o644)
    except Exception:
        shutil.rmtree(release, ignore_errors=True)
        raise
    return release


def _serve_payload(runner: Runner) -> dict[str, object]:
    raw = runner(("tailscale", "serve", "status", "--json")).stdout
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise DeployError(f"Tailscale Serve returned invalid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise DeployError("Tailscale Serve status must be a JSON object")
    return cast(dict[str, object], payload)


def _route_state(layout: Layout, payload: dict[str, object]) -> str:
    parsed = urlparse(layout.base_url)
    if parsed.hostname is None or parsed.port is None:
        raise DeployError(
            f"Deployment base URL requires an explicit host and port: {layout.base_url}"
        )
    host_port = f"{parsed.hostname}:{parsed.port}"
    raw_web = payload.get("Web", {})
    raw_tcp = payload.get("TCP", {})
    if not isinstance(raw_web, dict) or not isinstance(raw_tcp, dict):
        raise DeployError("Tailscale Serve status has invalid Web or TCP data")
    web = cast(dict[str, object], raw_web)
    tcp = cast(dict[str, object], raw_tcp)
    port = tcp.get(str(parsed.port))
    if port is not None:
        if not isinstance(port, dict):
            return "collision"
        port_config = cast(dict[str, object], port)
        if port_config.get("HTTPS") is not True:
            return "collision"
    host = web.get(host_port)
    if host is None:
        return "absent"
    if not isinstance(host, dict):
        raise DeployError(f"Tailscale Serve has invalid handler data for {host_port}")
    host_config = cast(dict[str, object], host)
    if not isinstance(host_config.get("Handlers"), dict):
        raise DeployError(f"Tailscale Serve has invalid handler data for {host_port}")
    handlers = cast(dict[str, object], host_config["Handlers"])
    handler = handlers.get(SERVE_PATH)
    if handler is None:
        return "absent"
    if isinstance(handler, dict):
        handler_config = cast(dict[str, object], handler)
        if handler_config.get("Proxy") == layout.listen_url:
            return "exact"
    return "collision"


def _ensure_route(layout: Layout, runner: Runner) -> None:
    state = _route_state(layout, _serve_payload(runner))
    if state == "collision":
        raise DeployError(
            f"Tailscale Serve route {SERVE_PATH} on port 8444 is owned by another target"
        )
    if state == "exact":
        return
    runner(
        (
            "tailscale",
            "serve",
            "--bg",
            "--yes",
            "--https=8444",
            f"--set-path={SERVE_PATH}",
            layout.listen_url,
        )
    )
    if _route_state(layout, _serve_payload(runner)) != "exact":
        raise DeployError("Tailscale Serve did not install the requested route")


def _release_id(path: Path | None, releases: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(releases.resolve()).name
    except ValueError:
        return None


def health(layout: Layout, runner: Runner = _run, probe: Probe = _probe) -> dict[str, object]:
    current_target = _symlink_target(layout.current)
    release = _release_id(current_target, layout.releases)
    checks = [
        Check(
            "current_release",
            release is not None
            and current_target is not None
            and (current_target / ".ready").is_file(),
            release or "current is absent or does not select an installed release",
        )
    ]
    try:
        service = runner(("systemctl", "--user", "is-active", UNIT_NAME)).stdout.strip()
        checks.append(Check("service", service == "active", service or "no status"))
    except DeployError as error:
        checks.append(Check("service", False, str(error)))
    try:
        route = _route_state(layout, _serve_payload(runner))
        checks.append(Check("tailscale_route", route == "exact", route))
    except DeployError as error:
        checks.append(Check("tailscale_route", False, str(error)))
    loopback_ok, loopback_detail = probe(f"{layout.listen_url}/_html-publish-health")
    checks.append(Check("loopback", loopback_ok, loopback_detail))
    https_ok, https_detail = probe(f"{layout.base_url}_html-publish-health")
    checks.append(Check("https", https_ok, https_detail))
    return {
        "operation": "health",
        "healthy": all(check.ok for check in checks),
        "release": release,
        "base_url": layout.base_url,
        "checks": [asdict(check) for check in checks],
    }


def _restore_pointers(layout: Layout, current: Path | None, previous: Path | None) -> None:
    for pointer, target in ((layout.current, current), (layout.previous, previous)):
        if target is None:
            pointer.unlink(missing_ok=True)
        else:
            _atomic_symlink(pointer, target)


def _recover_release(
    layout: Layout,
    current: Path,
    previous: Path | None,
    runner: Runner,
) -> None:
    with contextlib.suppress(Exception):
        _restore_pointers(layout, current, previous)
    with contextlib.suppress(Exception):
        runner(("systemctl", "--user", "restart", UNIT_NAME))


def install(
    layout: Layout,
    source: Path,
    runner: Runner = _run,
    probe: Probe = _probe,
) -> dict[str, object]:
    layout.state_root.mkdir(parents=True, exist_ok=True)
    layout.incoming.mkdir(exist_ok=True)
    layout.releases.mkdir(exist_ok=True)
    _ensure_config(layout)

    with tempfile.TemporaryDirectory(prefix="html-publish-wheel-") as directory:
        wheel = _build_wheel(source.resolve(), runner, Path(directory))
        release = _install_release(layout, wheel, runner)

    original_current = _symlink_target(layout.current)
    original_previous = _symlink_target(layout.previous)
    changed = original_current is None or original_current.resolve() != release.resolve()
    if changed:
        _atomic_symlink(layout.previous, original_current or release)
        _atomic_symlink(layout.current, release)
    elif original_previous is None:
        _atomic_symlink(layout.previous, release)
    try:
        _atomic_write(layout.unit, _unit_content(layout), 0o644)
        runner(("systemctl", "--user", "daemon-reload"))
        runner(("systemctl", "--user", "enable", "--now", UNIT_NAME))
        runner(("systemctl", "--user", "restart", UNIT_NAME))
        _ensure_route(layout, runner)
        report = health(layout, runner, probe)
        if not report["healthy"]:
            raise DeployError(
                "Post-install health check failed: " + json.dumps(report, sort_keys=True)
            )
    except Exception:
        if changed and original_current is not None:
            _recover_release(layout, original_current, original_previous, runner)
        raise
    return {
        "operation": "install",
        "outcome": "updated" if changed else "unchanged",
        "release": release.name,
        "previous": _release_id(_symlink_target(layout.previous), layout.releases),
        "base_url": layout.base_url,
        "health": report,
    }


def rollback(
    layout: Layout,
    release_id: str | None,
    runner: Runner = _run,
    probe: Probe = _probe,
) -> dict[str, object]:
    original_current = _symlink_target(layout.current)
    original_previous = _symlink_target(layout.previous)
    if original_current is None:
        raise DeployError("No active release is installed")
    target = layout.releases / release_id if release_id is not None else original_previous
    if target is None:
        raise DeployError("No previous release is installed")
    if target.parent.resolve() != layout.releases.resolve() or not (target / ".ready").is_file():
        raise DeployError(f"Release is not installed: {release_id or target.name}")
    if target.resolve() != original_current.resolve():
        _atomic_symlink(layout.previous, original_current)
        _atomic_symlink(layout.current, target)
    try:
        runner(("systemctl", "--user", "restart", UNIT_NAME))
        report = health(layout, runner, probe)
        if not report["healthy"]:
            raise DeployError(
                "Post-rollback health check failed: " + json.dumps(report, sort_keys=True)
            )
    except Exception:
        _recover_release(layout, original_current, original_previous, runner)
        raise
    return {
        "operation": "rollback",
        "outcome": "rolled_back",
        "release": target.name,
        "previous": original_current.name,
        "base_url": layout.base_url,
        "health": report,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m html_publish.deploy",
        description="Install, check, or roll back the controlled om1 deployment",
    )
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--unit", type=Path, default=DEFAULT_UNIT)
    commands = parser.add_subparsers(dest="operation", required=True)
    install_parser = commands.add_parser("install", help="build and activate a source checkout")
    install_parser.add_argument("--source", type=Path, default=Path.cwd())
    commands.add_parser("health", help="check the release, service, route, and HTTP endpoints")
    rollback_parser = commands.add_parser("rollback", help="activate a prior installed release")
    rollback_parser.add_argument("--release", help="release ID; defaults to previous")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = _run,
    probe: Probe = _probe,
) -> int:
    arguments = _parser().parse_args(argv)
    layout = Layout(arguments.state_root, arguments.config, arguments.unit)
    try:
        if arguments.operation == "install":
            payload = install(layout, arguments.source, runner, probe)
        elif arguments.operation == "rollback":
            payload = rollback(layout, arguments.release, runner, probe)
        else:
            payload = health(layout, runner, probe)
            if not payload["healthy"]:
                print(json.dumps(payload, sort_keys=True))
                return 1
    except DeployError as error:
        print(
            json.dumps(
                {"operation": arguments.operation, "outcome": "error", "error": str(error)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
