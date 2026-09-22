from __future__ import annotations

# pyright: reportPrivateUsage=false
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
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
            start_new_session=True,
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
            _assert_process(state)
            with self.assertRaises(ProbeFailure):
                _assert_process(replace(state, process_cmd=("wrong",)))
            with self.assertRaises(ProbeFailure):
                _assert_process(replace(state, owner_token="foreign-owner"))
            with self.assertRaises(ProbeFailure):
                _assert_process(replace(state, process_start_ticks="0"))
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


class ProbeRepairTest(unittest.TestCase):
    def test_complete_listener_census_skips_occupied_candidate(self) -> None:
        from scripts import probe_om1_issue2 as probe

        listener = 'LISTEN 0 128 127.0.0.1:9443 0.0.0.0:* users:(("owned",pid=1,fd=3))\n'
        with mock.patch.object(
            probe, "_run", return_value=subprocess.CompletedProcess([], 0, listener, "")
        ):
            snapshot = probe._listener_snapshot()
        self.assertEqual(snapshot, [listener.strip()])
        self.assertEqual(probe._https_port({"TCP": {}, "Web": {}}, snapshot), 9444)

    def test_timeout_and_output_limit_stop_owned_descendants(self) -> None:
        import time

        from scripts import probe_om1_issue2 as probe

        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "escaped"
            child = (
                "import time;from pathlib import Path;time.sleep(1.3);"
                f"Path({str(marker)!r}).touch()"
            )
            parent = (
                f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(10)"
            )
            with self.assertRaisesRegex(ProbeFailure, "exceeded 1 seconds"):
                probe._run((sys.executable, "-c", parent), timeout=1)
            time.sleep(0.5)
            self.assertFalse(marker.exists())
            with self.assertRaisesRegex(ProbeFailure, "output exceeded"):
                probe._run(
                    (sys.executable, "-c", "import os,time;os.write(1,b'x'*1000000);time.sleep(10)")
                )

    def test_checkpoint_retains_completed_and_failed_response(self) -> None:
        import json

        from scripts import probe_om1_issue2 as probe

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = replace(
                probe_state({"TCP": {}, "Web": {}}),
                evidence_root=str(root),
                state_path=str(root / "probe.json"),
            )

            def fail(current: ProbeState, label: str, rows: list[dict[str, object]]) -> None:
                rows.extend(
                    [
                        {"id": "first", "ok": True},
                        {
                            "id": "second",
                            "ok": False,
                            "response": {"status": 304, "sha256": "wrong"},
                        },
                    ]
                )
                raise ProbeFailure("failed conditional")

            with (
                mock.patch.object(probe, "_checkpoint_checks", side_effect=fail),
                self.assertRaisesRegex(ProbeFailure, "failed conditional"),
            ):
                probe._checkpoint(state, "A")
            evidence = json.loads(next(root.glob("checkpoint-*-failed-*.json")).read_text())
            self.assertEqual(evidence["rows"][1]["response"]["status"], 304)
            self.assertEqual(len(evidence["rows"]), 2)
            self.assertTrue(json.loads(Path(state.state_path).read_text())["failed"])

    def test_real_prepare_checkpoints_restore_and_recoverable_cleanup(self) -> None:
        import json
        import shutil
        import time

        from scripts import probe_om1_issue2 as probe

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            shutil.copytree(
                Path(__file__).parents[1] / "html_publish",
                source / "html_publish",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            for args in (
                ("init", "-q"),
                ("add", "."),
                (
                    "-c",
                    "user.name=Probe",
                    "-c",
                    "user.email=probe@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ),
            ):
                probe._run(("git", "-C", str(source), *args))
            commit = probe._run(("git", "-C", str(source), "rev-parse", "HEAD")).stdout.strip()
            provenance = probe._source_provenance(source, commit, Path(sys.executable))
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.assertEqual(
                    probe._source_provenance(source, commit, Path(sys.executable)), provenance
                )
            finally:
                os.chdir(old_cwd)
            evidence = root / "evidence"
            evidence.mkdir(mode=0o700)
            port = probe._free_loopback_port()
            baseline: dict[str, object] = {"TCP": {}, "Web": {}}
            state = replace(
                probe_state(baseline),
                phase="preflight",
                root=str(root / "owned"),
                evidence_root=str(evidence),
                state_path=str(evidence / "probe.json"),
                listen_port=port,
                route_target=f"http://127.0.0.1:{port}",
                base_url=f"http://127.0.0.1:{port}/",
                cli_python=sys.executable,
                source_root=str(source),
                source_commit=commit,
                provenance=provenance,
            )
            served: dict[str, object] = baseline
            original_run = probe._run
            original_config = probe._publisher_config
            install_states: list[dict[str, object]] = []

            def command(
                argv: tuple[str, ...], *, timeout: int = probe.COMMAND_TIMEOUT
            ) -> subprocess.CompletedProcess[str]:
                nonlocal served
                if argv[0] != "tailscale":
                    return original_run(argv, timeout=timeout)
                if argv[-1] == "off":
                    served = baseline
                else:
                    install_states.append(json.loads(Path(state.state_path).read_text()))
                    served = {
                        "TCP": {"9443": {"HTTPS": True}},
                        "Web": {
                            state.route_host_port: {
                                "Handlers": {state.route_path: {"Proxy": state.route_target}}
                            }
                        },
                    }
                return subprocess.CompletedProcess(argv, 0, "", "")

            def config(current: ProbeState) -> dict[str, object]:
                return {**original_config(current), "allow_http": True}

            with (
                mock.patch.object(probe, "_host_guard", return_value="om1"),
                mock.patch.object(probe, "_assert_baseline_unchanged"),
                mock.patch.object(probe, "_https_port", return_value=9443),
                mock.patch.object(probe, "_listener_snapshot", return_value=[]),
                mock.patch.object(probe, "_serve_status", side_effect=lambda: served),
                mock.patch.object(probe, "_observations", return_value={}),
                mock.patch.object(probe, "_publisher_config", side_effect=config),
                mock.patch.object(probe, "_run", side_effect=command),
            ):
                try:
                    state, _ = probe._prepare(state)
                    self.assertEqual(
                        install_states[0]["route_fingerprint"],
                        probe._fingerprint({"Proxy": state.route_target}),
                    )
                    state, a = probe._checkpoint(state, "A")
                    state, _ = probe._activate_b(state)
                    state, b = probe._checkpoint(state, "B")
                    state, _ = probe._restore_a(state)
                    state, restored = probe._checkpoint(state, "restored-A")
                    self.assertEqual(a["served_files"], restored["served_files"])
                    for relative in ("index.html", "assets/state.css"):
                        self.assertEqual(
                            cast(dict[str, dict[str, object]], a["served_files"])[relative][
                                "mtime_ns"
                            ],
                            cast(dict[str, dict[str, object]], b["served_files"])[relative][
                                "mtime_ns"
                            ],
                        )
                    conditional = [
                        row
                        for row in cast(list[dict[str, object]], b["rows"])
                        if row.get("request_headers")
                    ]
                    self.assertEqual(len(conditional), 2)
                    self.assertTrue(
                        all(
                            set(cast(dict[str, str], row["request_headers"]))
                            == {"If-Modified-Since"}
                            for row in conditional
                        )
                    )
                    recorded_pid = state.pid
                    self.assertIsNotNone(recorded_pid)
                    recovered = probe._recover_process(
                        replace(state, pid=None, process_start_ticks=None)
                    )
                    self.assertEqual(recovered.pid, recorded_pid)
                    asset = (
                        state.root_path
                        / "runtime/releases"
                        / str(state.revision_a)
                        / "assets/state.css"
                    )
                    asset.write_bytes(b"incorrect served CSS")
                    with self.assertRaisesRegex(ProbeFailure, "P2-css failed"):
                        probe._checkpoint(state, "restored-A")
                    failure = json.loads(
                        next(evidence.glob("checkpoint-restored-A-failed-*.json")).read_text()
                    )
                    self.assertTrue(failure["rows"][0]["ok"])
                    self.assertFalse(failure["rows"][-1]["ok"])
                    self.assertTrue(json.loads(Path(state.state_path).read_text())["failed"])
                    state = replace(state, failed=True)
                    with (
                        mock.patch.object(probe, "_observations", return_value={"drift": True}),
                        self.assertRaisesRegex(ProbeFailure, "C1 failed"),
                    ):
                        probe._cleanup(state)
                    stopped = json.loads(Path(state.state_path).read_text())
                    self.assertIsNone(stopped["pid"])
                    state = replace(state, pid=None, process_start_ticks=None, process_cmd=())
                    state, cleanup = probe._cleanup(state)
                    self.assertTrue(cleanup["root_retained"])
                    self.assertEqual(state.phase, "cleaned")
                    self.assertEqual(served, baseline)
                    assert recorded_pid is not None
                    self.assertFalse(probe._process_running(recorded_pid))
                    state, again = probe._cleanup(state)
                    self.assertTrue(again["unchanged"])
                    (source / "html_publish/__init__.py").write_text("changed source\n")
                    with self.assertRaisesRegex(ProbeFailure, "source must be clean"):
                        probe._source_provenance(source, commit, Path(sys.executable))
                finally:
                    if state.process_cmd:
                        state = probe._recover_process(state)
                    elif (state.root_path / "server.identity.json").exists():
                        record = json.loads((state.root_path / "server.identity.json").read_text())
                        state = replace(
                            state,
                            pid=record["pid"],
                            process_start_ticks=record["process_start_ticks"],
                            process_cmd=tuple(record["process_cmd"]),
                        )
                    if state.pid is not None and probe._process_running(state.pid):
                        os.killpg(state.pid, 15)
                        time.sleep(0.1)

    def test_interrupted_route_install_cleanup_preserves_unrelated_drift(self) -> None:
        from scripts import probe_om1_issue2 as probe

        for drift in (False, True):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                owned_root = root / "owned"
                owned_root.mkdir(mode=0o700)
                evidence = root / "evidence"
                evidence.mkdir(mode=0o700)
                baseline: dict[str, object] = {"TCP": {}, "Web": {}}
                state = replace(
                    probe_state(baseline),
                    phase="preflight",
                    root=str(owned_root),
                    evidence_root=str(evidence),
                    state_path=str(evidence / "probe.json"),
                    route_fingerprint=probe._fingerprint({"Proxy": "http://127.0.0.1:41001"}),
                    failed=drift,
                )
                (owned_root / ".probe-owner").write_text(state.owner_token)
                unrelated: dict[str, object] = {"TCP": {}, "Web": {}}
                if drift:
                    unrelated["Web"] = {"foreign:9555": {"Handlers": {"/": {"Text": "keep"}}}}
                current = {
                    "TCP": {"9443": {"HTTPS": True}},
                    "Web": {
                        **cast(dict[str, object], unrelated["Web"]),
                        state.route_host_port: {
                            "Handlers": {state.route_path: {"Proxy": state.route_target}}
                        },
                    },
                }
                with (
                    mock.patch.object(probe, "_host_guard"),
                    mock.patch.object(probe, "_serve_status", side_effect=[current, unrelated]),
                    mock.patch.object(probe, "_observations", return_value={}),
                    mock.patch.object(probe, "_run") as mutation,
                ):
                    if drift:
                        with self.assertRaisesRegex(ProbeFailure, "C1 failed"):
                            probe._cleanup(state)
                    else:
                        _, result = probe._cleanup(state)
                        self.assertFalse(result["root_retained"])
                    self.assertEqual(
                        mutation.call_args.args[0],
                        (
                            "tailscale",
                            "serve",
                            "--yes",
                            "--https=9443",
                            "--set-path=/html-publish-issue2-probe",
                            "off",
                        ),
                    )
                self.assertEqual(owned_root.exists(), drift)

    def test_startup_record_failure_stops_locally_owned_child(self) -> None:
        from scripts import probe_om1_issue2 as probe

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            evidence.mkdir(mode=0o700)
            state = replace(
                probe_state({"TCP": {}, "Web": {}}),
                phase="preflight",
                root=str(root / "owned"),
                evidence_root=str(evidence),
                state_path=str(evidence / "probe.json"),
                source_root=str(Path(__file__).parents[1]),
            )
            original_popen = subprocess.Popen
            children: list[subprocess.Popen[bytes]] = []

            def start(
                argv: tuple[str, ...],
                *,
                stdout: int,
                stderr: int,
                env: dict[str, str],
                start_new_session: bool,
            ) -> subprocess.Popen[bytes]:
                child = original_popen(
                    argv, stdout=stdout, stderr=stderr, env=env, start_new_session=start_new_session
                )
                children.append(child)
                return child

            with (
                mock.patch.object(probe, "_host_guard"),
                mock.patch.object(probe, "_assert_source"),
                mock.patch.object(probe, "_assert_baseline_unchanged"),
                mock.patch.object(
                    probe, "_recover_process", side_effect=ProbeFailure("injected record failure")
                ),
                mock.patch.object(probe.subprocess, "Popen", side_effect=start),
                self.assertRaisesRegex(ProbeFailure, "injected record failure"),
            ):
                probe._prepare(state)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())

    def test_each_resource_replays_its_own_observed_validators(self) -> None:
        from scripts import probe_om1_issue2 as probe

        state = replace(
            probe_state({"TCP": {}, "Web": {}}),
            phase="b-active",
            validators={
                "A": {
                    "index": {"last-modified": "index-date", "etag": '"index-tag"'},
                    "css": {"last-modified": "css-date", "etag": '"css-tag"'},
                }
            },
        )
        requests: list[tuple[str, dict[str, str]]] = []

        def response(
            rows: list[dict[str, object]],
            row: str,
            url: str,
            statuses: set[int],
            expected: dict[str, object] | None = None,
            headers: dict[str, str] | None = None,
            *,
            content_type: str,
        ) -> dict[str, object]:
            if row == "P2-path":
                raise ProbeFailure("conditional section complete")
            requests.append((row, headers or {}))
            return {"status": 200, "headers": {"last-modified": "current-date"}}

        with (
            mock.patch.object(probe, "_assert_owned_live"),
            mock.patch.object(probe, "_fixture_file", return_value={"sha256": "expected"}),
            mock.patch.object(probe, "_write_state"),
            mock.patch.object(probe, "_expect_response", side_effect=response),
            self.assertRaisesRegex(ProbeFailure, "conditional section complete"),
        ):
            probe._checkpoint_checks(state, "B", [])
        self.assertEqual(
            [headers for _, headers in requests if headers],
            [
                {"If-Modified-Since": "index-date"},
                {"If-None-Match": '"index-tag"'},
                {"If-Modified-Since": "index-date", "If-None-Match": '"index-tag"'},
                {"If-Modified-Since": "css-date"},
                {"If-None-Match": '"css-tag"'},
                {"If-Modified-Since": "css-date", "If-None-Match": '"css-tag"'},
            ],
        )

    def test_candidate_port_is_rechecked_before_prepare(self) -> None:
        from scripts import probe_om1_issue2 as probe

        baseline: dict[str, object] = {"TCP": {}, "Web": {}}
        with (
            mock.patch.object(probe, "_serve_status", return_value=baseline),
            mock.patch.object(
                probe, "_listener_snapshot", return_value=["LISTEN 0 128 127.0.0.1:9443 0.0.0.0:*"]
            ),
            self.assertRaisesRegex(ProbeFailure, "HTTPS port"),
        ):
            probe._assert_baseline_unchanged(probe_state(baseline))


class ProbeCancellationTest(unittest.TestCase):
    def test_real_signals_at_owned_creation_boundaries(self) -> None:
        for mode in ("before-spawn", "before-identity", "after-identity", "checkpoint"):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    (
                        sys.executable,
                        "-B",
                        "-c",
                        "from tests.test_probe_om1_issue2 import cancellation_case;"
                        f"cancellation_case({mode!r})",
                    ),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("cancelled by SIGTERM", result.stderr)

    def test_bounded_command_sigterm_stops_descendants_and_restores_handlers(self) -> None:
        result = subprocess.run(
            (
                sys.executable,
                "-B",
                "-c",
                "from tests.test_probe_om1_issue2 import cancelled_command;cancelled_command()",
            ),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def cancelled_command() -> None:
    import signal
    import time

    from scripts import probe_om1_issue2 as probe

    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    with tempfile.TemporaryDirectory() as temporary:
        marker = Path(temporary) / "escaped"
        child = (
            f"import time;from pathlib import Path;time.sleep(0.8);Path({str(marker)!r}).touch()"
        )
        command = (
            "import subprocess,sys,os,signal,time;"
            f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
            "os.kill(os.getppid(),signal.SIGTERM);time.sleep(30)"
        )
        try:
            probe._run((sys.executable, "-B", "-c", command))
        except ProbeFailure as error:
            assert str(error) == "probe cancelled by SIGTERM"
        else:
            raise AssertionError("cancelled command returned success")
        time.sleep(1)
        assert not marker.exists(), "descendant escaped cancellation"
    assert {number: signal.getsignal(number) for number in previous} == previous


def cancellation_case(mode: str) -> None:
    import json
    import signal
    import time

    from scripts import probe_om1_issue2 as probe

    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        evidence = root / "evidence"
        evidence.mkdir(mode=0o700)
        state = replace(
            probe_state({"TCP": {}, "Web": {}}),
            phase="preflight",
            root=str(root / "owned"),
            evidence_root=str(evidence),
            state_path=str(evidence / "probe.json"),
            source_root=str(Path(__file__).parents[1]),
        )
        probe._write_state(state)
        popen = subprocess.Popen
        write_state = probe._write_state
        children: list[subprocess.Popen[bytes]] = []
        signalled = False

        def read_state(path: Path) -> ProbeState:
            values = json.loads(path.read_text())
            values["process_cmd"] = tuple(values["process_cmd"])
            return ProbeState(**values)

        def write(current: ProbeState) -> None:
            nonlocal signalled
            write_state(current)
            if mode == "before-spawn" and current.process_cmd and not signalled:
                signalled = True
                os.kill(os.getpid(), signal.SIGTERM)

        def start(
            argv: tuple[str, ...],
            *,
            stdout: int,
            stderr: int,
            env: dict[str, str],
            start_new_session: bool,
        ) -> subprocess.Popen[bytes]:
            child = popen(
                argv, stdout=stdout, stderr=stderr, env=env, start_new_session=start_new_session
            )
            children.append(child)
            identity = state.root_path / "server.identity.json"
            if mode == "after-identity":
                deadline = time.monotonic() + 3
                while not identity.exists():
                    assert time.monotonic() < deadline, "missing child identity"
                    time.sleep(0.01)
            else:
                assert not identity.exists(), "signal did not precede child identity"
            os.kill(os.getpid(), signal.SIGTERM)
            return child

        try:
            with (
                mock.patch.object(probe, "_load_state", side_effect=read_state),
                mock.patch.object(probe, "_host_guard"),
                mock.patch.object(probe, "_assert_source"),
                mock.patch.object(probe, "_assert_baseline_unchanged"),
                mock.patch.object(probe, "_serve_status", return_value={"TCP": {}, "Web": {}}),
                mock.patch.object(probe, "_observations", return_value={}),
            ):
                if mode == "checkpoint":
                    state.root_path.mkdir(mode=0o700)
                    probe._owner_file(state).write_text(state.owner_token)
                    child = popen(
                        (sys.executable, "-c", "import time;time.sleep(30)"),
                        env={**os.environ, "HTML_PUBLISH_PROBE_OWNER": state.owner_token},
                        start_new_session=True,
                    )
                    children.append(child)
                    ticks, command, _ = probe._process_identity(child.pid)
                    state = replace(
                        state,
                        phase="prepared",
                        pid=child.pid,
                        process_start_ticks=ticks,
                        process_cmd=command,
                    )
                    write_state(state)

                    def checkpoint(current: ProbeState, expected: str) -> None:
                        probe._run(
                            (
                                sys.executable,
                                "-c",
                                "import os,signal,time;os.kill(os.getppid(),signal.SIGTERM);"
                                "time.sleep(30)",
                            )
                        )

                    with mock.patch.object(probe, "_checkpoint", side_effect=checkpoint):
                        result = probe.main(
                            ["checkpoint", "--state", state.state_path, "--expect", "A"]
                        )
                    assert child.poll() is None, "checkpoint killed persistent server"
                    probe._assert_process(state)
                else:
                    with (
                        mock.patch.object(probe, "_write_state", side_effect=write),
                        mock.patch.object(probe.subprocess, "Popen", side_effect=start),
                        mock.patch.object(
                            probe, "BOOTSTRAP", "import time;time.sleep(0.15)\n" + probe.BOOTSTRAP
                        ),
                    ):
                        result = probe.main(["prepare", "--state", state.state_path])
                    assert len(children) == (0 if mode == "before-spawn" else 1)
                    assert all(child.poll() is not None for child in children)
                assert result == 1
                latest = read_state(Path(state.state_path))
                assert latest.failed
                assert list(evidence.glob("operation-failed-*.json"))
                if mode == "before-spawn":
                    assert not latest.process_cmd and latest.pid is None
                else:
                    assert latest.pid == children[0].pid and latest.process_start_ticks
                cleaned, payload = probe._cleanup(latest)
                assert payload["phase"] == "cleaned" and payload["root_retained"]
                assert probe._cleanup(cleaned)[1]["unchanged"]
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
    assert {number: signal.getsignal(number) for number in previous} == previous
