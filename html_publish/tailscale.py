from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import urlsplit

RouteState = Literal["absent", "equal", "changed", "collision", "unknown"]
_KNOWN_SECTIONS = frozenset({"Web", "TCP", "AllowFunnel", "Foreground", "Services"})


@dataclass(frozen=True)
class RouteIdentity:
    node: str
    https_port: int
    mount: str
    target: str


@dataclass(frozen=True)
class RouteBlocker:
    code: str
    message: str
    next_action: str


@dataclass(frozen=True)
class RouteInspection:
    selected: RouteIdentity | None
    node: dict[str, object] | None
    serve: dict[str, object] | None
    prerequisites: dict[str, bool]
    state: RouteState
    blockers: tuple[RouteBlocker, ...]


class _InspectionError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        next_action: str,
        *,
        command_available: bool = True,
    ) -> None:
        super().__init__(message)
        self.blocker = RouteBlocker(code, message, next_action)
        self.command_available = command_available


def _raise_unknown(message: str) -> None:
    raise _InspectionError("unknown_serve_state", message, "inspect_serve_state")


def _path(value: str, *, trailing_slash: bool) -> str:
    if (
        not value.startswith("/")
        or "//" in value
        or "\\" in value
        or "%" in value
        or any(segment in {".", ".."} for segment in value.split("/"))
        or any(ord(character) < 32 for character in value)
    ):
        _raise_unknown(f"Path has an ambiguous spelling: {value}")
    if trailing_slash and not value.endswith("/"):
        _raise_unknown(f"Path must end in a slash: {value}")
    return value


def _selected_route(base_url: str, listen_port: int) -> RouteIdentity:
    if not 1 <= listen_port <= 65535:
        raise _InspectionError(
            "invalid_route", "The loopback port must be between 1 and 65535", "fix_host_port"
        )
    try:
        parsed = urlsplit(base_url)
        https_port = parsed.port or 443
    except ValueError as error:
        raise _InspectionError(
            "invalid_route_url", f"The publisher URL is invalid: {error}", "fix_publisher_url"
        ) from error
    node = parsed.hostname
    if (
        parsed.scheme != "https"
        or node is None
        or "." not in node
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not 1 <= https_port <= 65535
    ):
        raise _InspectionError(
            "invalid_route_url",
            "Tailscale preview requires an HTTPS publisher URL with a DNS host and canonical path",
            "fix_publisher_url",
        )
    try:
        mount = _path(parsed.path or "/", trailing_slash=True)
    except _InspectionError as error:
        raise _InspectionError("invalid_route_url", str(error), "fix_publisher_url") from error
    return RouteIdentity(
        node.lower().rstrip("."),
        https_port,
        mount,
        f"http://127.0.0.1:{listen_port}",
    )


def _run_json(args: list[str]) -> dict[str, object]:
    command = " ".join(args)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except FileNotFoundError as error:
        raise _InspectionError(
            "tailscale_unavailable",
            "The tailscale executable is unavailable",
            "install_or_select_tailscale",
            command_available=False,
        ) from error
    except subprocess.TimeoutExpired as error:
        raise _InspectionError(
            "tailscale_inspection_failed",
            f"{command} exceeded its five second inspection limit",
            "inspect_tailscale",
        ) from error
    except OSError as error:
        raise _InspectionError(
            "tailscale_inspection_failed",
            f"Cannot run {command}: {error}",
            "inspect_tailscale",
            command_available=False,
        ) from error
    if result.returncode:
        detail = result.stderr.strip() or "no diagnostic output"
        raise _InspectionError(
            "tailscale_inspection_failed",
            f"{command} exited {result.returncode}: {detail}",
            "inspect_tailscale",
        )
    if len(result.stdout.encode()) > 1024 * 1024:
        raise _InspectionError(
            "tailscale_inspection_failed",
            f"{command} returned more than 1048576 bytes",
            "inspect_tailscale",
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise _InspectionError(
            "tailscale_inspection_failed",
            f"{command} returned invalid JSON: {error}",
            "inspect_tailscale",
        ) from error
    if not isinstance(payload, dict):
        raise _InspectionError(
            "tailscale_inspection_failed",
            f"{command} did not return a JSON object",
            "inspect_tailscale",
        )
    return cast(dict[str, object], payload)


def _node(payload: dict[str, object]) -> tuple[str, str]:
    self_value = payload.get("Self")
    if payload.get("BackendState") != "Running" or not isinstance(self_value, dict):
        raise _InspectionError(
            "node_unavailable",
            "Tailscale has no running authenticated node",
            "authenticate_tailscale",
        )
    self_node = cast(dict[str, object], self_value)
    node_id = self_node.get("ID")
    dns_name = self_node.get("DNSName")
    if (
        not isinstance(node_id, str)
        or not node_id
        or not isinstance(dns_name, str)
        or "." not in dns_name
        or any(c.isspace() for c in dns_name)
    ):
        raise _InspectionError(
            "node_unavailable",
            "Tailscale status has no valid Self.ID and Self.DNSName",
            "authenticate_tailscale",
        )
    return node_id, dns_name.lower().removesuffix(".")


def _section(payload: dict[str, object], name: str) -> dict[str, object]:
    value = payload.get(name, {})
    if not isinstance(value, dict):
        _raise_unknown(f"Tailscale Serve {name} must be an object")
    return cast(dict[str, object], value)


def _authority(value: str) -> tuple[str, int]:
    node, separator, raw_port = value.rpartition(":")
    if not separator or "." not in node or not _valid_port_text(raw_port):
        _raise_unknown(f"Tailscale Serve authority is malformed: {value}")
    port = int(raw_port)
    if port > 65535:
        _raise_unknown(f"Tailscale Serve authority is malformed: {value}")
    return node.lower().rstrip("."), port


def _valid_port_text(value: str) -> bool:
    return 1 <= len(value) <= 5 and value.isascii() and value.isdecimal() and value[0] != "0"


def _handler(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        _raise_unknown("Tailscale Serve handler has an unfamiliar shape")
    typed = cast(dict[object, object], value)
    if len(typed) != 1:
        _raise_unknown("Tailscale Serve handler has an unfamiliar shape")
    kind, target = next(iter(typed.items()))
    if kind not in {"Proxy", "Path", "Text"} or not isinstance(target, str):
        _raise_unknown("Tailscale Serve handler has an unfamiliar shape")
    return {cast(str, kind): cast(str, target)}


def _tcp_listener(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        _raise_unknown("Tailscale Serve TCP listener has an unfamiliar shape")
    listener = cast(dict[str, object], value)
    allowed = {"HTTPS", "HTTP", "TCPForward", "TerminateTLS"}
    if not listener or not set(listener) <= allowed:
        _raise_unknown("Tailscale Serve TCP listener has an unfamiliar shape")
    for name, setting in listener.items():
        expected = bool if name in {"HTTPS", "HTTP"} else str
        if not isinstance(setting, expected):
            _raise_unknown("Tailscale Serve TCP listener has an unfamiliar shape")
    return listener


def _paths_overlap(left: str, right: str) -> bool:
    left = left.rstrip("/")
    right = right.rstrip("/")
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _serve(
    payload: dict[str, object], selected: RouteIdentity
) -> tuple[dict[str, object], RouteState, tuple[RouteBlocker, ...]]:
    for key, value in payload.items():
        if key not in _KNOWN_SECTIONS and value not in (None, False, "", [], {}):
            _raise_unknown(f"Tailscale Serve returned an unfamiliar nonempty section: {key}")
    tcp = _section(payload, "TCP")
    web = _section(payload, "Web")
    funnel = _section(payload, "AllowFunnel")
    if _section(payload, "Foreground") or _section(payload, "Services"):
        _raise_unknown("Tailscale Serve has nonempty Foreground or Services state")

    selected_listener: dict[str, object] | None = None
    for raw_port, raw_listener in tcp.items():
        if not _valid_port_text(raw_port) or int(raw_port) > 65535:
            _raise_unknown(f"Tailscale Serve TCP port is malformed: {raw_port}")
        listener = _tcp_listener(raw_listener)
        if int(raw_port) == selected.https_port:
            selected_listener = listener

    blockers: list[RouteBlocker] = []
    selected_handler: dict[str, str] | None = None
    selected_authority_seen = False
    other_routes: list[dict[str, object]] = []
    for raw_authority, raw_host in sorted(web.items()):
        node, port = _authority(raw_authority)
        if not isinstance(raw_host, dict):
            _raise_unknown(f"Tailscale Serve Web entry is malformed: {raw_authority}")
        host = cast(dict[str, object], raw_host)
        if set(host) != {"Handlers"} or not isinstance(host["Handlers"], dict):
            _raise_unknown(f"Tailscale Serve Web entry is malformed: {raw_authority}")
        handlers = cast(dict[str, object], host["Handlers"])
        if not handlers:
            _raise_unknown(f"Tailscale Serve Web entry has no handlers: {raw_authority}")
        if node == selected.node and port == selected.https_port:
            selected_authority_seen = True
        if port == selected.https_port and node != selected.node and handlers:
            blockers.append(
                RouteBlocker(
                    "https_port_collision",
                    f"HTTPS port {selected.https_port} has handlers for another authority",
                    "choose_unclaimed_https_port",
                )
            )
        for raw_mount, raw_handler in sorted(handlers.items()):
            mount = _path(raw_mount, trailing_slash=False)
            handler = _handler(raw_handler)
            if node == selected.node and port == selected.https_port and mount == selected.mount:
                selected_handler = handler
            else:
                other_routes.append(
                    {"node": node, "https_port": port, "mount": mount, "handler": handler}
                )
            if (
                node == selected.node
                and port == selected.https_port
                and mount != selected.mount
                and _paths_overlap(mount, selected.mount)
            ):
                blockers.append(
                    RouteBlocker(
                        "route_overlap",
                        f"Tailscale Serve route {raw_mount} overlaps {selected.mount}",
                        "choose_unclaimed_mount",
                    )
                )

    funnel_entries: list[dict[str, object]] = []
    for raw_authority, enabled in sorted(funnel.items()):
        node, port = _authority(raw_authority)
        if not isinstance(enabled, bool):
            _raise_unknown(f"Tailscale Serve AllowFunnel entry is malformed: {raw_authority}")
        funnel_entries.append({"node": node, "https_port": port, "enabled": enabled})
        if enabled and port == selected.https_port:
            blockers.append(
                RouteBlocker(
                    "funnel_enabled",
                    f"Tailscale Funnel is enabled on HTTPS port {selected.https_port}",
                    "disable_funnel_or_choose_private_port",
                )
            )

    observation: dict[str, object] = {
        "https_listener": selected_listener == {"HTTPS": True},
        "selected_handler": selected_handler,
        "other_routes": other_routes,
        "funnel": funnel_entries,
    }
    if selected_authority_seen and selected_listener is None:
        _raise_unknown("The selected Web authority has no matching HTTPS TCP listener")
    if selected_listener is not None and selected_listener != {"HTTPS": True}:
        blockers.insert(
            0,
            RouteBlocker(
                "https_port_collision",
                f"Tailscale port {selected.https_port} is not an exclusive HTTPS listener",
                "choose_unclaimed_https_port",
            ),
        )
    if selected_handler == {"Proxy": selected.target} and not blockers:
        return observation, "equal", ()
    if selected_handler is not None and selected_handler != {"Proxy": selected.target}:
        changed = RouteBlocker(
            "route_changed",
            "The selected Tailscale Serve route has a different handler",
            "inspect_selected_route",
        )
        if not blockers:
            return observation, "changed", (changed,)
        blockers.insert(0, changed)
    return observation, "collision" if blockers else "absent", tuple(blockers)


def _unknown(
    selected: RouteIdentity | None,
    error: _InspectionError,
    *,
    node: dict[str, object] | None = None,
    serve: dict[str, object] | None = None,
    authenticated: bool = False,
    node_matches: bool = False,
    serve_inspected: bool = False,
) -> RouteInspection:
    return RouteInspection(
        selected,
        node,
        serve,
        {
            "command_available": error.command_available,
            "authenticated": authenticated,
            "node_matches": node_matches,
            "serve_inspected": serve_inspected,
        },
        "unknown",
        (error.blocker,),
    )


def inspect_route(base_url: str, listen_port: int) -> RouteInspection:
    try:
        selected = _selected_route(base_url, listen_port)
    except _InspectionError as error:
        return _unknown(None, error)
    try:
        node_id, authenticated_node = _node(
            _run_json(["tailscale", "status", "--json", "--peers=false"])
        )
    except _InspectionError as error:
        return _unknown(selected, error)

    matches = authenticated_node == selected.node
    node: dict[str, object] = {
        "id": node_id,
        "dns_name": authenticated_node,
        "matches_selected": matches,
    }
    try:
        serve, state, blockers = _serve(
            _run_json(["tailscale", "serve", "status", "--json"]), selected
        )
    except _InspectionError as error:
        return _unknown(
            selected,
            error,
            node=node,
            authenticated=True,
            node_matches=matches,
            serve_inspected=error.blocker.code == "unknown_serve_state",
        )
    if not matches:
        return _unknown(
            selected,
            _InspectionError(
                "node_mismatch",
                f"Publisher node {selected.node} does not match authenticated node "
                f"{authenticated_node}",
                "use_authenticated_node_url",
            ),
            node=node,
            serve=serve,
            authenticated=True,
            serve_inspected=True,
        )
    return RouteInspection(
        selected,
        node,
        serve,
        {
            "command_available": True,
            "authenticated": True,
            "node_matches": True,
            "serve_inspected": True,
        },
        state,
        blockers,
    )
