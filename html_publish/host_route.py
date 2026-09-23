from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from html_publish.host import HealthyOwnedService, ServiceInspection, inspect_owned_service
from html_publish.model import Config
from html_publish.tailscale import RouteIdentity, RouteInspection, inspect_route

RouteDecision = Literal["planned", "foreign", "owned", "drift", "pending", "collision", "unknown"]
_EFFECT_NAMES = ("route_intent", "tailscale_serve_route", "route_completion")
_UNIT_NAME = re.compile(r"html-publish(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?\Z")


@dataclass(frozen=True)
class RouteRecord:
    status: Literal["pending", "owned"]
    values: dict[str, object]


def _record_path(unit_name: str) -> Path:
    if not _UNIT_NAME.fullmatch(unit_name):
        raise ValueError("Unit name must be html-publish or html-publish-<lowercase-name>")
    root_value = os.environ.get("XDG_STATE_HOME")
    root = Path(root_value).expanduser() if root_value else Path.home() / ".local" / "state"
    if not root.is_absolute():
        raise ValueError("XDG_STATE_HOME must be absolute")
    path = root / "html-publish" / "routes" / f"{unit_name}.json"
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Symlink in route record path: {current}")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Non-directory route record ancestor: {current}")
    return path


def _read_record(path: Path) -> RouteRecord | None:
    if not path.exists():
        return None
    if (
        not path.is_file()
        or path.stat().st_uid != os.geteuid()
        or stat.S_IMODE(path.stat().st_mode) != 0o600
    ):
        raise ValueError(f"Route record is not an owned regular mode-0600 file: {path}")
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read route record: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("Route record is not an object")
    values = cast(dict[str, object], raw)
    status = values.get("status")
    if (
        values.get("schema_version") != 1
        or not isinstance(status, str)
        or status not in {"pending", "owned"}
    ):
        raise ValueError("Route record has an unknown format")
    common = {
        "service_installation_id": str,
        "uid": int,
        "config_path": str,
        "config_fingerprint": str,
        "executable": str,
        "package_hash": str,
        "unit_name": str,
        "unit_path": str,
        "unit_digest": str,
        "listen_port": int,
        "node_id": str,
        "node_dns_name": str,
        "https_host": str,
        "https_port": int,
        "mount": str,
        "target": str,
    }
    if any(
        not isinstance(values.get(name), expected)
        or (expected is int and isinstance(values.get(name), bool))
        for name, expected in common.items()
    ):
        raise ValueError("Route record binding is incomplete")
    if status == "pending" and (
        not isinstance(values.get("attempt_id"), str)
        or values.get("observed_absent") is not True
        or not isinstance(values.get("port_state_digest"), str)
    ):
        raise ValueError("Pending route record is incomplete")
    return RouteRecord(cast(Literal["pending", "owned"], status), values)


def _binding(
    service: HealthyOwnedService, route: RouteIdentity, node: dict[str, object]
) -> dict[str, object]:
    return {
        "service_installation_id": service.installation_id,
        "uid": service.uid,
        "config_path": str(service.config_path),
        "config_fingerprint": service.config_fingerprint,
        "executable": str(service.executable),
        "package_hash": service.package_hash,
        "unit_name": service.unit_name,
        "unit_path": str(service.unit_path),
        "unit_digest": service.unit_digest,
        "listen_port": service.listen_port,
        "node_id": node["id"],
        "node_dns_name": node["dns_name"],
        "https_host": route.node,
        "https_port": route.https_port,
        "mount": route.mount,
        "target": route.target,
    }


def _service_json(inspection: ServiceInspection) -> dict[str, object]:
    healthy = asdict(inspection.healthy) if inspection.healthy else None
    if healthy is not None:
        for key in ("config_path", "executable", "environment", "unit_path"):
            healthy[key] = str(healthy[key])
    return {
        "selected": inspection.selected,
        "manager": inspection.manager,
        "health": inspection.health,
        "healthy_owned_service": healthy,
    }


def _blocker(code: str, message: str, next_action: str) -> dict[str, str]:
    return {"code": code, "message": message, "next_action": next_action}


def _inspection_blockers(inspection: RouteInspection) -> list[dict[str, str]]:
    return [asdict(blocker) for blocker in inspection.blockers]


def _empty_tailscale() -> dict[str, object]:
    return {
        "selected": None,
        "node": None,
        "serve": None,
        "prerequisites": {
            "command_available": False,
            "authenticated": False,
            "node_matches": False,
            "serve_inspected": False,
        },
    }


def preview(config_path: Path, config: Config, unit_name: str) -> dict[str, object]:
    service = inspect_owned_service(config_path, config, unit_name)
    effects = {name: "not_started" for name in _EFFECT_NAMES}
    blockers = [asdict(blocker) for blocker in service.blockers]
    try:
        record_path = _record_path(unit_name)
    except ValueError as error:
        record_path = None
        blockers.append(_blocker("route_record_invalid", str(error), "fix_state_path"))
    report: dict[str, object] = {
        "schema_version": 1,
        "operation": "host.route.setup",
        "outcome": "blocked",
        "selected": {
            "executable": service.selected.get("executable"),
            "configuration": {
                "path": service.selected.get("config_path"),
                "fingerprint": service.selected.get("config_fingerprint"),
            },
            "service": _service_json(service),
            "route": None,
        },
        "record_path": str(record_path) if record_path else None,
        "ownership": {"record": None, "matches_selected": False},
        "tailscale": _empty_tailscale(),
        "prerequisites": {
            "owned_service": service.healthy is not None,
            "healthy_service": service.healthy is not None,
            "authenticated_node": False,
            "matching_node": False,
            "serve_inspected": False,
            "private_funnel": False,
        },
        "route_state": "unknown",
        "blockers": blockers,
        "proposed_effects": [],
        "effects": effects,
        "verification": {"private_https": "not_checked"},
    }
    if service.healthy is None or record_path is None:
        return report

    record_error: str | None = None
    try:
        record = _read_record(record_path)
    except ValueError as error:
        record = None
        record_error = str(error)
    ownership = cast(dict[str, object], report["ownership"])
    ownership["record"] = record.values if record else None

    inspected = inspect_route(config.base_url, service.healthy.listen_port)
    report["tailscale"] = {
        "selected": asdict(inspected.selected) if inspected.selected else None,
        "node": inspected.node,
        "serve": inspected.serve,
        "prerequisites": inspected.prerequisites,
    }
    prerequisites = cast(dict[str, bool], report["prerequisites"])
    prerequisites.update(
        {
            "authenticated_node": inspected.prerequisites["authenticated"],
            "matching_node": inspected.prerequisites["node_matches"],
            "serve_inspected": inspected.prerequisites["serve_inspected"],
            "private_funnel": inspected.state != "unknown"
            and not any(blocker.code == "funnel_enabled" for blocker in inspected.blockers),
        }
    )
    blockers.extend(_inspection_blockers(inspected))
    selected = cast(dict[str, object], report["selected"])
    if inspected.selected is not None:
        selected["route"] = asdict(inspected.selected)
    if record_error is not None:
        blockers.append(_blocker("route_record_invalid", record_error, "inspect_route_record"))
    if inspected.selected is None or inspected.node is None or inspected.state == "unknown":
        report["blockers"] = blockers
        return report

    expected = _binding(service.healthy, inspected.selected, inspected.node)
    binding_matches = bool(
        record and all(record.values.get(key) == value for key, value in expected.items())
    )
    ownership["matches_selected"] = binding_matches

    decision: RouteDecision
    if record_error is not None:
        decision = "unknown"
    elif record is not None and not binding_matches:
        decision = "drift"
        blockers.append(
            _blocker(
                "route_binding_drift",
                "Route ownership record differs from the selected service, node, or route",
                "inspect_route_binding",
            )
        )
    elif record is not None and record.status == "pending":
        decision = "pending"
        blockers.append(
            _blocker(
                "route_pending",
                "Route setup has a pending attempt",
                "inspect_pending_route",
            )
        )
    elif inspected.state == "collision":
        decision = "collision"
    elif record is not None and inspected.state == "changed":
        decision = "drift"
        blockers.append(
            _blocker(
                "owned_route_drift",
                "The owned route differs from its recorded mapping",
                "inspect_owned_route",
            )
        )
    elif record is not None and inspected.state == "equal":
        decision = "owned"
    elif record is not None:
        decision = "drift"
        blockers.append(
            _blocker(
                "owned_route_drift",
                "The owned route is missing or differs from its recorded mapping",
                "inspect_owned_route",
            )
        )
    elif inspected.state == "equal":
        decision = "foreign"
        blockers.append(
            _blocker(
                "route_foreign",
                "The selected route exists without this installation's ownership record",
                "choose_unclaimed_mount_or_wait_for_issue_59",
            )
        )
    elif inspected.state == "changed":
        decision = "collision"
    else:
        decision = "planned"

    report["route_state"] = decision
    report["blockers"] = blockers
    if decision == "planned" and not blockers:
        report["outcome"] = "planned"
        report["proposed_effects"] = list(_EFFECT_NAMES)
    elif decision == "owned" and not blockers:
        report["outcome"] = "unchanged"
    return report
