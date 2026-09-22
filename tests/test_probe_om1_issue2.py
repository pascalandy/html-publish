from __future__ import annotations

# pyright: reportPrivateUsage=false
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts.probe_om1_issue2 import (
    CLI_PYTHON,
    ProbeFailure,
    ProbeState,
    _assert_process,
    _fingerprint,
    _preflight,
    _process_identity,
    _route_state,
    _strip_owned_route,
)


def probe_state(baseline: dict[str, object]) -> ProbeState:
    return ProbeState(
        schema_version=1,
        owner_token="owner-token",
        run_id="run-id",
        expected_host="om1",
        observed_host="om1",
        dns_name="om1.example.ts.net",
        name="issue2-probe-run-id",
        listen_port=41001,
        https_port=9443,
        route_path="/html-publish-issue2-probe",
        route_host_port="om1.example.ts.net:9443",
        route_target="http://127.0.0.1:41001",
        base_url="https://om1.example.ts.net:9443/html-publish-issue2-probe/",
        root="/home/pascal/.local/state/html-publish-probes/run-id",
        evidence_root="/home/pascal/.local/state/html-publish-probe-evidence/run-id",
        state_path="/home/pascal/.local/state/html-publish-probe-evidence/run-id/probe.json",
        cli_python=str(CLI_PYTHON),
        phase="prepared",
        baseline_serve=baseline,
        baseline_serve_fingerprint=_fingerprint(baseline),
        baseline_observations={},
        fixtures={},
        validators={},
    )


class ProbeMutationSafetyTest(unittest.TestCase):
    def test_owned_route_can_be_removed_without_masking_unrelated_routes(self) -> None:
        baseline: dict[str, object] = {
            "TCP": {"8444": {"HTTPS": True}},
            "Web": {
                "om1.example.ts.net:8444": {
                    "Handlers": {"/html-publish": {"Proxy": "http://127.0.0.1:4177"}}
                }
            },
        }
        state = probe_state(baseline)
        installed: dict[str, object] = {
            "TCP": {"8444": {"HTTPS": True}, "9443": {"HTTPS": True}},
            "Web": {
                "om1.example.ts.net:8444": {
                    "Handlers": {"/html-publish": {"Proxy": "http://127.0.0.1:4177"}}
                },
                "om1.example.ts.net:9443": {
                    "Handlers": {"/html-publish-issue2-probe": {"Proxy": "http://127.0.0.1:41001"}}
                },
            },
        }

        route, fingerprint = _route_state(state, installed)

        self.assertEqual(route, "exact")
        self.assertEqual(
            fingerprint,
            "bc013d1ff8819b69577475272d6707397bc94687c29c60b2ae8424c122ab7e48",
        )
        self.assertEqual(_strip_owned_route(state, installed), baseline)

    def test_foreign_route_target_is_a_collision(self) -> None:
        baseline: dict[str, object] = {"TCP": {}, "Web": {}}
        state = probe_state(baseline)
        foreign: dict[str, object] = {
            "TCP": {"9443": {"HTTPS": True}},
            "Web": {
                "om1.example.ts.net:9443": {
                    "Handlers": {"/html-publish-issue2-probe": {"Proxy": "http://127.0.0.1:49999"}}
                }
            },
        }

        route, _ = _route_state(state, foreign)

        self.assertEqual(route, "collision")

    def test_process_identity_detects_command_and_owner_changes(self) -> None:
        environment = dict(os.environ)
        environment["HTML_PUBLISH_PROBE_OWNER"] = "owner-token"
        process = subprocess.Popen(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            env=environment,
        )
        try:
            start_ticks, command, owner = _process_identity(process.pid)
            state = replace(
                probe_state({"TCP": {}, "Web": {}}),
                pid=process.pid,
                process_start_ticks=start_ticks,
                process_cmd=command,
            )
            self.assertEqual(owner, "owner-token")
            assert state.pid is not None
            self.assertEqual(_process_identity(state.pid)[1], state.process_cmd)
            with self.assertRaises(ProbeFailure):
                _assert_process(replace(state, process_cmd=("wrong",)))
        finally:
            process.terminate()
            process.wait(timeout=2)

    def test_wrong_host_preflight_creates_no_probe_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "probe.json"
            with (
                mock.patch("scripts.probe_om1_issue2.socket.gethostname", return_value="other"),
                self.assertRaisesRegex(ProbeFailure, "host identity mismatch"),
            ):
                _preflight(output)

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
