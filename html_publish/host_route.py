from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from html_publish.configuration import read_document
from html_publish.host import (
    HealthyOwnedService,
    HostError,
    ServiceInspection,
    host_setup_lock,
    inspect_owned_service,
    write_host_file,
)
from html_publish.model import Config, PublishError
from html_publish.tailscale import RouteIdentity, RouteInspection, inspect_route, serve_route

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
        "state": inspected.state,
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
    ownership["binding"] = expected

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
                "choose_unclaimed_mount",
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


def _apply_decision(report: dict[str, object]) -> tuple[str, list[dict[str, str]]]:
    blockers = list(cast(list[dict[str, str]], report["blockers"]))
    tailscale = cast(dict[str, object], report["tailscale"])
    serve = cast(dict[str, object] | None, tailscale.get("serve"))
    selected = cast(
        dict[str, object] | None, cast(dict[str, object], report["selected"]).get("route")
    )
    if serve is not None and selected is not None:
        port = selected["https_port"]
        if any(
            entry["https_port"] == port for entry in cast(list[dict[str, object]], serve["funnel"])
        ):
            blockers.append(
                _blocker(
                    "funnel_entry",
                    f"Tailscale Funnel has an entry on HTTPS port {port}; Serve would remove it",
                    "remove_funnel_entry_or_choose_private_port",
                )
            )
    route_state = report["route_state"]
    if route_state == "owned" and not blockers:
        return "unchanged", []
    if route_state == "planned" and not blockers:
        return "create", []
    if route_state == "pending":
        blockers = [blocker for blocker in blockers if blocker["code"] != "route_pending"]
        ownership = cast(dict[str, object], report["ownership"])
        record = cast(dict[str, object] | None, ownership.get("record"))
        if (
            not blockers
            and record is not None
            and tailscale.get("state") == "absent"
            and serve is not None
            and record.get("port_state_digest") == serve.get("selected_port_digest")
        ):
            return "retry", []
        if tailscale.get("state") != "absent":
            blockers.append(
                _blocker(
                    "route_pending",
                    "The pending attempt no longer has an absent selected route",
                    "inspect_pending_route",
                )
            )
        elif not blockers:
            blockers.append(
                _blocker(
                    "pending_baseline_drift",
                    "The selected HTTPS port differs from the pending attempt baseline",
                    "inspect_pending_route",
                )
            )
    return "blocked", blockers


def _blocked(report: dict[str, object], blockers: list[dict[str, str]]) -> dict[str, object]:
    report["outcome"] = "blocked"
    report["blockers"] = blockers
    report["proposed_effects"] = []
    return report


def _config_drift(config_path: Path, selected: Config) -> str | None:
    try:
        _, current = read_document("publisher", config_path)
    except (PublishError, OSError) as error:
        return f"Selected publisher configuration cannot be reloaded: {error}"
    if current != selected:
        return "Selected publisher configuration changed during route setup"
    return None


def _surrounding(serve: dict[str, object]) -> tuple[object, object, object]:
    return serve["other_routes"], serve["other_listeners"], serve["funnel"]


def _uncertain_report(
    report: dict[str, object],
    path: Path,
    pending: dict[str, object],
    effects: dict[str, str],
    code: str,
    message: str,
) -> dict[str, object]:
    try:
        observed = _read_record(path)
    except (OSError, ValueError):
        observed = None
    record = observed.values if observed else None
    ownership = cast(dict[str, object], report["ownership"])
    ownership["record"] = record
    binding = cast(dict[str, object] | None, ownership.get("binding"))
    ownership["matches_selected"] = bool(
        record is not None
        and binding is not None
        and all(record.get(key) == value for key, value in binding.items())
    )
    still_pending = record == pending
    report["outcome"] = "pending" if still_pending else "blocked"
    report["route_state"] = "pending" if still_pending else "unknown"
    report["blockers"] = [_blocker(code, message, "inspect_pending_route")]
    report["proposed_effects"] = []
    report["effects"] = effects
    return report


def apply(config_path: Path, config: Config, unit_name: str) -> dict[str, object]:
    effects = {name: "not_started" for name in _EFFECT_NAMES}
    with host_setup_lock():
        drift = _config_drift(config_path, config)
        plan = preview(config_path, config, unit_name)
        if drift is not None:
            plan["effects"] = effects
            return _blocked(plan, [_blocker("config_drift", drift, "inspect_publisher_config")])
        decision, blockers = _apply_decision(plan)
        if decision == "blocked":
            plan["effects"] = effects
            return _blocked(plan, blockers)
        if decision == "unchanged":
            plan["effects"] = {name: "unchanged" for name in _EFFECT_NAMES}
            return plan

        drift = _config_drift(config_path, config)
        fresh = preview(config_path, config, unit_name)
        fresh_decision, fresh_blockers = _apply_decision(fresh)
        if drift is not None:
            fresh_blockers.append(_blocker("config_drift", drift, "inspect_publisher_config"))
        if (
            fresh_decision != decision
            or fresh["selected"] != plan["selected"]
            or fresh["tailscale"] != plan["tailscale"]
            or fresh["ownership"] != plan["ownership"]
        ):
            fresh_blockers.append(
                _blocker(
                    "route_preflight_drift",
                    "Selected service, node, route, or Serve state changed before apply",
                    "inspect_route_state",
                )
            )
        if fresh_blockers:
            fresh["effects"] = effects
            return _blocked(fresh, fresh_blockers)

        selected = cast(dict[str, object], fresh["selected"])
        route = cast(dict[str, object], selected["route"])
        route_identity = RouteIdentity(
            node=cast(str, route["node"]),
            https_port=cast(int, route["https_port"]),
            mount=cast(str, route["mount"]),
            target=cast(str, route["target"]),
        )
        tailscale = cast(dict[str, object], fresh["tailscale"])
        baseline = cast(dict[str, object], tailscale["serve"])
        ownership = cast(dict[str, object], fresh["ownership"])
        path = Path(cast(str, fresh["record_path"]))
        if decision == "create":
            binding = cast(dict[str, object], ownership["binding"])
            pending = {
                "schema_version": 1,
                "status": "pending",
                **binding,
                "attempt_id": str(uuid.uuid4()),
                "observed_absent": True,
                "port_state_digest": baseline["selected_port_digest"],
            }
            try:
                write_host_file(path, json.dumps(pending, sort_keys=True, indent=2) + "\n", 0o600)
            except (HostError, OSError) as error:
                effects["route_intent"] = "unknown"
                return _uncertain_report(
                    fresh,
                    path,
                    pending,
                    effects,
                    "route_intent_uncertain",
                    f"Pending route intent could not be durably saved: {error}",
                )
            effects["route_intent"] = "completed"
        else:
            pending = cast(dict[str, object], ownership["record"])
            effects["route_intent"] = "unchanged"

        drift = _config_drift(config_path, config)
        try:
            intent = preview(config_path, config, unit_name)
        except OSError as error:
            return _uncertain_report(
                fresh,
                path,
                pending,
                effects,
                "route_preflight_inspection_failed",
                f"Cannot inspect the route after pending intent: {error}",
            )
        intent_decision, intent_blockers = _apply_decision(intent)
        intent_ownership = cast(dict[str, object], intent["ownership"])
        if drift is not None:
            intent_blockers.append(_blocker("config_drift", drift, "inspect_publisher_config"))
        if (
            intent_decision != "retry"
            or intent["selected"] != fresh["selected"]
            or intent["tailscale"] != fresh["tailscale"]
            or intent_ownership.get("record") != pending
            or intent_ownership.get("binding") != ownership.get("binding")
        ):
            intent_blockers.append(
                _blocker(
                    "route_preflight_drift",
                    "Selected service, node, route, or Serve state changed after pending intent",
                    "inspect_pending_route",
                )
            )
        if intent_blockers:
            intent["effects"] = effects
            return _blocked(intent, intent_blockers)

        effects["tailscale_serve_route"] = "unknown"
        command_error = serve_route(route_identity)
        try:
            post = preview(config_path, config, unit_name)
        except OSError as error:
            return _uncertain_report(
                intent,
                path,
                pending,
                effects,
                "route_post_inspection_failed",
                f"Cannot inspect the route after Tailscale Serve: {error}",
            )
        post_tailscale = cast(dict[str, object], post["tailscale"])
        post_serve = cast(dict[str, object] | None, post_tailscale.get("serve"))
        post_ownership = cast(dict[str, object], post["ownership"])
        exact_observation = (
            post_tailscale.get("state") == "equal"
            and post_serve is not None
            and post_serve.get("https_listener") is True
            and _surrounding(post_serve) == _surrounding(baseline)
            and post["selected"] == fresh["selected"]
            and post_tailscale.get("node") == tailscale.get("node")
            and post_ownership.get("record") == pending
            and post_ownership.get("matches_selected") is True
            and _config_drift(config_path, config) is None
            and not [
                blocker
                for blocker in cast(list[dict[str, str]], post["blockers"])
                if blocker["code"] != "route_pending"
            ]
        )
        if exact_observation:
            effects["tailscale_serve_route"] = "completed"
        if command_error is None and exact_observation:
            owned = {
                key: value
                for key, value in pending.items()
                if key not in {"attempt_id", "observed_absent", "port_state_digest"}
            }
            owned["status"] = "owned"
            try:
                write_host_file(path, json.dumps(owned, sort_keys=True, indent=2) + "\n", 0o600)
            except (HostError, OSError) as error:
                effects["route_completion"] = "unknown"
                return _uncertain_report(
                    post,
                    path,
                    pending,
                    effects,
                    "route_completion_uncertain",
                    f"Route matched, but ownership could not be durably completed: {error}",
                )
            else:
                effects["route_completion"] = "completed"
                post["outcome"] = "applied"
                post["route_state"] = "owned"
                post["ownership"] = {**post_ownership, "record": owned}
                post["blockers"] = []
                post["proposed_effects"] = list(_EFFECT_NAMES)
                post["effects"] = effects
                return post
        post["outcome"] = "pending"
        post["route_state"] = "pending"
        post["proposed_effects"] = []
        post["blockers"] = [
            *cast(list[dict[str, str]], post["blockers"]),
            _blocker(
                "serve_command_uncertain" if command_error else "serve_postcheck_failed",
                command_error
                or "Tailscale Serve did not leave the exact selected route and surrounding state",
                "inspect_pending_route",
            ),
        ]
        post["effects"] = effects
        return post
