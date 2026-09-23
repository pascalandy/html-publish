from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast


@unittest.skipUnless(sys.platform == "linux", "Linux user service setup")
class InstalledHostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        run_id = f"route-preview-{time.time_ns()}-{os.getpid()}"
        cls.run_root = Path("/tmp/html-publish-verify") / run_id
        cls.artifacts = cls.run_root / "artifacts"
        cls.artifacts.mkdir(parents=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="html publish % host-", dir=cls.run_root)
        cls.base = Path(cls.temp.name)
        uv_executable = shutil.which("uv", path="/usr/bin:/bin") or shutil.which("uv")
        if uv_executable is None:
            raise AssertionError("uv is required for installed host verification")
        wheel_dir = cls.artifacts / "wheel"
        installation = subprocess.run(
            [uv_executable, "build", "--wheel", "--out-dir", str(wheel_dir)],
            cwd=cls.root,
            capture_output=True,
            text=True,
        )
        if installation.returncode:
            raise AssertionError(installation.stderr)
        wheel = next(wheel_dir.glob("*.whl"))
        cls.wheel = wheel
        source_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cls.root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
        (cls.artifacts / "provenance.json").write_text(
            json.dumps(
                {
                    "classification": "controlled",
                    "source_head": source_head,
                    "wheel": str(wheel),
                    "wheel_sha256": wheel_sha256,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        print(
            f"installed-host-evidence source_head={source_head} wheel_sha256={wheel_sha256}",
            file=sys.stderr,
        )
        cls.env = os.environ.copy()
        cls.env.update(
            {
                "UV_TOOL_DIR": str(cls.base / "tools"),
                "UV_TOOL_BIN_DIR": str(cls.base / "tool-bin"),
                "XDG_CONFIG_HOME": str(cls.base / "config-home"),
                "XDG_STATE_HOME": str(cls.base / "state-home"),
                "PATH": f"{Path(uv_executable).parent}:/usr/bin:/bin",
            }
        )
        installation = subprocess.run(
            [
                uv_executable,
                "tool",
                "install",
                "--from",
                str(wheel),
                "html-publish",
                "--python",
                sys.executable,
            ],
            env=cls.env,
            capture_output=True,
            text=True,
        )
        if installation.returncode:
            raise AssertionError(installation.stderr)
        cls.cli = cls.base / "tool-bin" / "html-publish"
        fake_bin = cls.base / "fake-bin"
        fake_bin.mkdir()
        systemctl = fake_bin / "systemctl"
        systemctl.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ['FAKE_SYSTEMCTL_LOG']).open('a') as log:
    log.write(json.dumps(args, separators=(',', ':')) + '\\n')
state_path = pathlib.Path(os.environ['FAKE_SYSTEMCTL_STATE'])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
unit = pathlib.Path(os.environ['XDG_CONFIG_HOME']) / 'systemd/user/html-publish-test.service'
if 'show' in args:
    if os.environ.get('FAKE_BLOCK_ROUTES_AFTER_INTENT') == 'yes':
        route_record = (
            pathlib.Path(os.environ['XDG_STATE_HOME'])
            / 'html-publish/routes/html-publish-test.json'
        )
        if route_record.exists():
            route_record.parent.chmod(0)
    if os.environ.get('FAKE_DRIFT_HOST_RECORD_ON_SHOW') == 'yes':
        record_path = (
            pathlib.Path(os.environ['XDG_STATE_HOME'])
            / 'html-publish/hosts/html-publish-test.json'
        )
        if record_path.exists():
            record = json.loads(record_path.read_text())
            record['package_hash'] = 'concurrent-record-change'
            record_path.write_text(json.dumps(record))
    fragment = state.get('foreign_fragment') or (str(unit) if unit.exists() else '')
    print('LoadState=' + ('loaded' if fragment else 'not-found'))
    print('FragmentPath=' + fragment)
    print('DropInPaths=' + state.get('dropins', ''))
    print('UnitFileState=' + ('enabled' if state.get('enabled') else 'disabled'))
    print('ActiveState=' + ('active' if state.get('active') else 'inactive'))
    print('MainPID=0')
    sys.exit(0)
if 'enable' in args:
    state['enabled'] = True
    state_path.write_text(json.dumps(state))
    if os.environ.get('FAKE_DRIFT_CONFIG_ON_ENABLE') == 'yes':
        config_path = pathlib.Path(os.environ['FAKE_CONFIG'])
        config = json.loads(config_path.read_text())
        config['limits'] = {'command_seconds': 121}
        config_path.write_text(json.dumps(config))
elif 'start' in args or 'restart' in args:
    if os.environ.get('FAKE_START_MODE') == 'success':
        state['active'] = True
        state_path.write_text(json.dumps(state))
        sys.exit(0)
    print('simulated start failure', file=sys.stderr)
    sys.exit(23)
sys.exit(0)
""")
        systemctl.chmod(0o755)
        tailscale = fake_bin / "tailscale"
        tailscale.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
log_path = pathlib.Path(os.environ['FAKE_TAILSCALE_LOG'])
with log_path.open('a') as log:
    log.write(json.dumps(args, separators=(',', ':')) + '\\n')
if args == ['status', '--json', '--peers=false']:
    if os.environ.get('FAKE_DRIFT_CONFIG_ON_TAILSCALE_STATUS') == 'yes':
        config_path = pathlib.Path(os.environ['FAKE_CONFIG'])
        config = json.loads(config_path.read_text())
        config['limits'] = {'command_seconds': 121}
        config_path.write_text(json.dumps(config))
        os.environ.pop('FAKE_DRIFT_CONFIG_ON_TAILSCALE_STATUS')
    print(pathlib.Path(os.environ['FAKE_TAILSCALE_STATUS']).read_text())
    sys.exit(0)
if args == ['serve', 'status', '--json']:
    path = pathlib.Path(os.environ['FAKE_TAILSCALE_SERVE'])
    drift_on = os.environ.get('FAKE_DRIFT_SERVE_ON_STATUS')
    if drift_on is not None:
        commands = [json.loads(line) for line in log_path.read_text().splitlines()]
        if commands.count(['serve', 'status', '--json']) == int(drift_on):
            state = json.loads(path.read_text())
            state.setdefault('TCP', {})['9443'] = {'HTTPS': True}
            path.write_text(json.dumps(state))
    print(path.read_text())
    sys.exit(0)
if (len(args) == 5 and args[:2] == ['serve', '--bg']
        and args[2].startswith('--https=') and args[3].startswith('--set-path=')):
    mode = os.environ.get('FAKE_SERVE_MODE', 'success')
    if mode != 'fail_before':
        path = pathlib.Path(os.environ['FAKE_TAILSCALE_SERVE'])
        state = json.loads(path.read_text())
        if mode != 'no_write':
            port = args[2].removeprefix('--https=')
            mount = args[3].removeprefix('--set-path=')
            authority = 'preview.test.ts.net:' + port
            state.setdefault('TCP', {})[port] = {'HTTPS': True}
            handlers = state.setdefault('Web', {}).setdefault(
                authority, {'Handlers': {}}
            )['Handlers']
            handlers[mount] = {'Proxy': args[4]}
            if mode == 'mutate_unrelated':
                state.setdefault('TCP', {})['9443'] = {'HTTPS': True}
            path.write_text(json.dumps(state))
    if mode in {'fail_before', 'fail_after'}:
        print('simulated Serve failure', file=sys.stderr)
        sys.exit(23)
    sys.exit(0)
print('unexpected tailscale command: ' + ' '.join(args), file=sys.stderr)
sys.exit(64)
""")
        tailscale.chmod(0o755)
        cls.env["PATH"] = str(fake_bin) + os.pathsep + cls.env["PATH"]
        cls.env["FAKE_SYSTEMCTL_STATE"] = str(cls.base / "systemctl.json")
        cls.env["FAKE_SYSTEMCTL_LOG"] = str(cls.base / "systemctl.log")
        cls.env["FAKE_TAILSCALE_STATUS"] = str(cls.base / "tailscale-status.json")
        cls.env["FAKE_TAILSCALE_SERVE"] = str(cls.base / "tailscale-serve.json")
        cls.env["FAKE_TAILSCALE_LOG"] = str(cls.base / "tailscale.log")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.env.pop("FAKE_SERVE_MODE", None)
        self.env.pop("FAKE_DRIFT_SERVE_ON_STATUS", None)
        self.env.pop("FAKE_DRIFT_CONFIG_ON_TAILSCALE_STATUS", None)
        self.env.pop("FAKE_BLOCK_ROUTES_AFTER_INTENT", None)
        self.archive = self.base / f"{self._testMethodName}.git"
        self.runtime = self.base / f"{self._testMethodName}-runtime"
        self.config = self.base / f"{self._testMethodName}.json"
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            self.port = available.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": self.url,
                    "allow_http": True,
                }
            )
        )
        self.env["FAKE_CONFIG"] = str(self.config)
        state = self.base / "systemctl.json"
        if state.exists():
            state.unlink()
        Path(self.env["FAKE_TAILSCALE_STATUS"]).write_text(
            json.dumps(
                {
                    "BackendState": "Running",
                    "Self": {"ID": "node-controlled", "DNSName": "preview.test.ts.net."},
                }
            )
        )
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text("{}")
        Path(self.env["FAKE_TAILSCALE_LOG"]).write_text("")
        Path(self.env["FAKE_SYSTEMCTL_LOG"]).write_text("")
        for path in (
            self.base / "config-home/systemd/user/html-publish-test.service",
            self.base / "state-home/html-publish/hosts/html-publish-test.json",
            self.base / "state-home/html-publish/routes/html-publish-test.json",
        ):
            if path.exists():
                path.unlink()

    def command(self, *args: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        result = subprocess.run(
            [
                str(self.cli),
                "--config",
                str(self.config),
                "--json",
                "host",
                "setup",
                "--unit-name",
                "html-publish-test",
                "--port",
                str(self.port),
                *args,
            ],
            cwd=self.base,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=25,
        )
        return result, json.loads(result.stdout)

    def tailscale_commands(self) -> list[list[str]]:
        return [
            cast(list[str], json.loads(line))
            for line in Path(self.env["FAKE_TAILSCALE_LOG"]).read_text().splitlines()
        ]

    def systemctl_commands(self) -> list[list[str]]:
        return [
            cast(list[str], json.loads(line))
            for line in Path(self.env["FAKE_SYSTEMCTL_LOG"]).read_text().splitlines()
        ]

    def state_manifest(self) -> dict[str, object]:
        paths = {
            "config": self.config,
            "unit": self.base / "config-home/systemd/user/html-publish-test.service",
            "service_record": self.base / "state-home/html-publish/hosts/html-publish-test.json",
            "route_record": self.base / "state-home/html-publish/routes/html-publish-test.json",
            "service_state": Path(self.env["FAKE_SYSTEMCTL_STATE"]),
            "route_state": Path(self.env["FAKE_TAILSCALE_SERVE"]),
            "node_state": Path(self.env["FAKE_TAILSCALE_STATUS"]),
            "archive_sentinel": self.archive / "sentinel",
            "runtime_sentinel": self.runtime / "sentinel",
            "receipt": self.base / f"{self._testMethodName}.receipt.json",
            "setup_lock": self.base / "state-home/html-publish/hosts/.setup.lock",
        }
        manifest: dict[str, object] = {}
        for name, path in paths.items():
            if path.is_file():
                manifest[name] = {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "mode": oct(path.stat().st_mode & 0o777),
                }
            else:
                manifest[name] = {"path": str(path), "exists": path.exists()}
        manifest["archive_tree"] = self.tree_manifest(self.archive)
        manifest["runtime_tree"] = self.tree_manifest(self.runtime)
        manifest["receipt_tree"] = [
            self.path_manifest(path) for path in sorted(self.base.glob("*.receipt.json"))
        ]
        return manifest

    @staticmethod
    def path_manifest(path: Path) -> dict[str, object]:
        info = path.lstat()
        item: dict[str, object] = {"path": str(path), "mode": oct(info.st_mode & 0o777)}
        if path.is_symlink():
            item["kind"] = "symlink"
            item["target"] = os.readlink(path)
        elif path.is_file():
            item["kind"] = "file"
            item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            item["kind"] = "directory"
        return item

    @classmethod
    def tree_manifest(cls, root: Path) -> list[dict[str, object]]:
        if not root.exists():
            return []
        return [cls.path_manifest(path) for path in (root, *sorted(root.rglob("*")))]

    def route_command(
        self, *args: str, assert_unchanged: bool = True
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        argv = [
            str(self.cli),
            "--config",
            str(self.config),
            "--json",
            "host",
            "route",
            "setup",
            "--unit-name",
            "html-publish-test",
            *args,
        ]
        before = self.state_manifest()
        result = subprocess.run(
            argv,
            cwd=self.base,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=25,
        )
        after = self.state_manifest()
        artifact = {
            "classification": "controlled",
            "test": self._testMethodName,
            "argv": argv,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "before": before,
            "after": after,
            "systemctl_commands": self.systemctl_commands(),
            "tailscale_commands": self.tailscale_commands(),
        }
        evidence_path = self.artifacts / f"{self._testMethodName}.jsonl"
        with evidence_path.open("a") as evidence:
            evidence.write(json.dumps(artifact, sort_keys=True, separators=(",", ":")) + "\n")
        if assert_unchanged:
            self.assertEqual(before, after)
        return result, json.loads(result.stdout)

    def seed_preserved_state(self) -> None:
        self.archive.mkdir(exist_ok=True)
        (self.archive / "sentinel").write_bytes(b"archive-preserved")
        self.runtime.mkdir(exist_ok=True)
        (self.runtime / "sentinel").write_bytes(b"runtime-preserved")
        (self.base / f"{self._testMethodName}.receipt.json").write_bytes(b"receipt-preserved")

    def seed_owned_service(self) -> dict[str, object]:
        result, plan = self.command()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        unit_path = Path(str(plan["unit_path"]))
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(str(plan["unit"]))
        record_path = Path(str(plan["record_path"]))
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "installation_id": "owned-test-installation",
                    "uid": os.geteuid(),
                    "config_path": str(self.config),
                    "fingerprint": plan["fingerprint"],
                    "package_hash": plan["package_hash"],
                    "unit_path": str(unit_path),
                    "listen_port": self.port,
                    "unit": plan["unit"],
                    "enabled": True,
                    "pending": None,
                }
            )
        )
        Path(self.env["FAKE_SYSTEMCTL_STATE"]).write_text('{"enabled": true, "active": true}')
        return plan

    @contextmanager
    def controlled_http(
        self,
        port: int,
        *,
        status: int,
        body: bytes = b"",
        location: str | None = None,
    ) -> Generator[tuple[int, list[str]], None, None]:
        requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append(self.path)
                self.send_response(status)
                if location is not None:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @contextmanager
    def healthy_owned_service(self) -> Generator[dict[str, object], None, None]:
        plan = self.seed_owned_service()
        server = subprocess.Popen(
            [
                str(self.cli),
                "--config",
                str(self.config),
                "host",
                "serve",
                "--port",
                str(self.port),
            ],
            cwd=self.base,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            for _ in range(50):
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/_html-publish-health", timeout=1
                    ) as response:
                        if response.read() == b"ok\n":
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("owned service fixture did not answer health")
            yield plan
        finally:
            server.send_signal(signal.SIGTERM)
            self.assertEqual(server.wait(timeout=5), 0)

    def seed_route_record(
        self,
        report: dict[str, object],
        status: str,
        **changes: object,
    ) -> Path:
        selected = cast(dict[str, object], report["selected"])
        service = cast(dict[str, object], selected["service"])
        healthy = cast(dict[str, object], service["healthy_owned_service"])
        route = cast(dict[str, object], selected["route"])
        tailscale = cast(dict[str, object], report["tailscale"])
        node = cast(dict[str, object], tailscale["node"])
        record: dict[str, object] = {
            "schema_version": 1,
            "status": status,
            "service_installation_id": healthy["installation_id"],
            "uid": healthy["uid"],
            "config_path": healthy["config_path"],
            "config_fingerprint": healthy["config_fingerprint"],
            "executable": healthy["executable"],
            "package_hash": healthy["package_hash"],
            "unit_name": healthy["unit_name"],
            "unit_path": healthy["unit_path"],
            "unit_digest": healthy["unit_digest"],
            "listen_port": healthy["listen_port"],
            "node_id": node["id"],
            "node_dns_name": node["dns_name"],
            "https_host": route["node"],
            "https_port": route["https_port"],
            "mount": route["mount"],
            "target": route["target"],
        }
        if status == "pending":
            record.update(
                {
                    "attempt_id": "controlled-pending-attempt",
                    "observed_absent": True,
                    "port_state_digest": "controlled-port-state-digest",
                }
            )
        record.update(changes)
        path = Path(str(report["record_path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        return path

    def tailscale_preview(
        self, serve: object, *, hostname: str = "preview.test.ts.net"
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": f"https://{hostname}/pages/",
                    "allow_http": False,
                }
            )
        )
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text(json.dumps(serve))
        self.seed_preserved_state()
        with self.healthy_owned_service():
            return self.route_command()

    def test_preview_is_read_only_and_foreign_equal_unit_blocks(self) -> None:
        result, preview = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(preview["outcome"], "planned")
        self.assertIn("%%", str(preview["unit"]))
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.runtime.exists())
        self.assertFalse(Path(str(preview["record_path"])).exists())
        unit_path = Path(str(preview["unit_path"]))
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(str(preview["unit"]))
        blocked, report = self.command("--apply")
        self.assertEqual(blocked.returncode, 1)
        self.assertEqual(report["outcome"], "blocked")
        self.assertIn("without this installation", " ".join(cast(list[str], report["blockers"])))
        self.assertEqual(unit_path.read_text(), preview["unit"])
        self.assertFalse(Path(str(preview["record_path"])).exists())
        self.assertNotIn("tailscale", preview)
        self.assertEqual(self.tailscale_commands(), [])

    def test_route_preview_reports_planned_without_writes(self) -> None:
        serve = {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                "preview.test.ts.net:443": {
                    "Handlers": {"/other": {"Proxy": "http://127.0.0.1:9000"}}
                }
            },
            "AllowFunnel": {"preview.test.ts.net:443": False},
        }
        result, report = self.tailscale_preview(serve)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["outcome"], "planned")
        self.assertEqual(report["route_state"], "planned")
        selected = cast(dict[str, object], report["selected"])
        route = cast(dict[str, object], selected["route"])
        self.assertEqual(route["target"], f"http://127.0.0.1:{self.port}")
        self.assertEqual(
            report["proposed_effects"],
            ["route_intent", "tailscale_serve_route", "route_completion"],
        )
        self.assertEqual(
            report["effects"],
            {
                "route_intent": "not_started",
                "tailscale_serve_route": "not_started",
                "route_completion": "not_started",
            },
        )
        self.assertEqual(report["verification"], {"private_https": "not_checked"})
        self.assertEqual(
            self.tailscale_commands(),
            [
                ["status", "--json", "--peers=false"],
                ["serve", "status", "--json"],
            ],
        )
        self.assertFalse(Path(str(report["record_path"])).exists())
        service = cast(dict[str, object], selected["service"])
        healthy = cast(dict[str, object], service["healthy_owned_service"])
        self.assertEqual(healthy["listen_port"], self.port)

    def test_route_preview_derives_root_and_custom_https_port(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net:8444/",
                    "allow_http": False,
                }
            )
        )
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text(
            json.dumps(
                {
                    "TCP": {"8444": {"HTTPS": True}},
                    "AllowFunnel": {"preview.test.ts.net:8444": False},
                }
            )
        )
        self.seed_preserved_state()
        with self.healthy_owned_service():
            result, report = self.route_command()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        selected = cast(dict[str, object], report["selected"])
        route = cast(dict[str, object], selected["route"])
        self.assertEqual(route["node"], "preview.test.ts.net")
        self.assertEqual(route["https_port"], 8444)
        self.assertEqual(route["mount"], "/")
        self.assertEqual(route["target"], f"http://127.0.0.1:{self.port}")

    def test_route_preview_classifies_foreign_collisions_and_unknown_state(self) -> None:
        cases = (
            (
                "equal",
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        "preview.test.ts.net:443": {
                            "Handlers": {"/pages/": {"Proxy": f"http://127.0.0.1:{self.port}"}}
                        }
                    },
                },
                "foreign",
                "route_foreign",
            ),
            (
                "slash-alias",
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        "preview.test.ts.net:443": {
                            "Handlers": {"/pages": {"Proxy": f"http://127.0.0.1:{self.port}"}}
                        }
                    },
                },
                "collision",
                "route_overlap",
            ),
            (
                "overlap",
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        "preview.test.ts.net:443": {
                            "Handlers": {"/pages/child": {"Proxy": "http://127.0.0.1:9000"}}
                        }
                    },
                },
                "collision",
                "route_overlap",
            ),
            (
                "port",
                {"TCP": {"443": {"TCPForward": "127.0.0.1:9000"}}},
                "collision",
                "https_port_collision",
            ),
            (
                "missing-tcp",
                {
                    "Web": {
                        "preview.test.ts.net:443": {
                            "Handlers": {"/other": {"Proxy": "http://127.0.0.1:9000"}}
                        }
                    }
                },
                "unknown",
                "unknown_serve_state",
            ),
            (
                "unicode-tcp-port",
                {"TCP": {"²": {"HTTPS": True}}},
                "unknown",
                "unknown_serve_state",
            ),
            (
                "unicode-funnel-port",
                {"AllowFunnel": {"preview.test.ts.net:²": True}},
                "unknown",
                "unknown_serve_state",
            ),
            (
                "funnel",
                {"AllowFunnel": {"preview.test.ts.net:443": True}},
                "collision",
                "funnel_enabled",
            ),
            (
                "unsupported-handler",
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        "preview.test.ts.net:443": {
                            "Handlers": {"/other": {"Redirect": "https://example.com/"}}
                        }
                    },
                },
                "unknown",
                "unknown_serve_state",
            ),
            (
                "unknown",
                {"Web": {}, "FutureRoutes": {"enabled": True}},
                "unknown",
                "unknown_serve_state",
            ),
            (
                "services",
                {"Services": {"svc:example": {"TCP": 443}}},
                "unknown",
                "unknown_serve_state",
            ),
        )
        for name, serve, state, blocker_code in cases:
            with self.subTest(name=name):
                Path(self.env["FAKE_TAILSCALE_LOG"]).write_text("")
                result, report = self.tailscale_preview(serve)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(report["route_state"], state)
                blockers = cast(list[dict[str, str]], report["blockers"])
                matching = [blocker for blocker in blockers if blocker["code"] == blocker_code]
                self.assertEqual(len(matching), 1)
                self.assertTrue(matching[0]["message"])
                self.assertTrue(matching[0]["next_action"])
                self.assertEqual(report["proposed_effects"], [])
                self.assertEqual(len(self.tailscale_commands()), 2)

    def test_route_preview_blocks_node_mismatch(self) -> None:
        result, report = self.tailscale_preview({}, hostname="other.test.ts.net")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(report["route_state"], "unknown")
        self.assertEqual(
            cast(list[dict[str, str]], report["blockers"])[0]["code"],
            "node_mismatch",
        )
        self.assertEqual(report["proposed_effects"], [])

    def test_route_apply_creates_and_repeats_without_serve_write(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        unrelated = {"Proxy": "http://127.0.0.1:9000"}
        serve = {
            "TCP": {"8444": {"HTTPS": True}},
            "Web": {"preview.test.ts.net:8444": {"Handlers": {"/other": unrelated}}},
        }
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text(json.dumps(serve))
        self.seed_preserved_state()
        with self.healthy_owned_service():
            before = self.state_manifest()
            result, report = self.route_command("--apply", assert_unchanged=False)
            after = self.state_manifest()
            command_count = len(self.tailscale_commands())
            repeat_result, repeat = self.route_command("--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["outcome"], "applied")
        self.assertEqual(report["route_state"], "owned")
        self.assertEqual(
            report["effects"],
            {
                "route_intent": "completed",
                "tailscale_serve_route": "completed",
                "route_completion": "completed",
            },
        )
        record_path = Path(str(report["record_path"]))
        self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(record_path.read_text())["status"], "owned")
        self.assertEqual(
            self.tailscale_commands()[6],
            [
                "serve",
                "--bg",
                "--https=443",
                "--set-path=/pages/",
                f"http://127.0.0.1:{self.port}",
            ],
        )
        self.assertEqual(len(self.tailscale_commands()), command_count + 2)
        self.assertEqual(repeat_result.returncode, 0)
        self.assertEqual(repeat["outcome"], "unchanged")
        self.assertEqual(
            json.loads(Path(self.env["FAKE_TAILSCALE_SERVE"]).read_text())["Web"][
                "preview.test.ts.net:8444"
            ]["Handlers"]["/other"],
            unrelated,
        )
        for key in (
            "config",
            "unit",
            "service_record",
            "service_state",
            "archive_sentinel",
            "runtime_sentinel",
            "receipt",
        ):
            self.assertEqual(before[key], after[key], key)

    def test_route_apply_blocks_foreign_collision_and_funnel_entries(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        cases = (
            (
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        "preview.test.ts.net:443": {
                            "Handlers": {"/pages/": {"Proxy": f"http://127.0.0.1:{self.port}"}}
                        }
                    },
                },
                "route_foreign",
            ),
            (
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        "preview.test.ts.net:443": {
                            "Handlers": {"/pages/child": {"Proxy": "http://127.0.0.1:9000"}}
                        }
                    },
                },
                "route_overlap",
            ),
            (
                {"TCP": {"443": {"HTTPS": True}}, "AllowFunnel": {"preview.test.ts.net:443": True}},
                "funnel_entry",
            ),
            (
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "AllowFunnel": {"preview.test.ts.net:443": False},
                },
                "funnel_entry",
            ),
            (
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "AllowFunnel": {"preview.test.ts.net:443": "maybe"},
                },
                "unknown_serve_state",
            ),
        )
        self.seed_preserved_state()
        with self.healthy_owned_service():
            for serve, code in cases:
                with self.subTest(code=code, serve=serve):
                    Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text(json.dumps(serve))
                    Path(self.env["FAKE_TAILSCALE_LOG"]).write_text("")
                    result, report = self.route_command("--apply")
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(report["outcome"], "blocked")
                    self.assertIn(
                        code,
                        {item["code"] for item in cast(list[dict[str, str]], report["blockers"])},
                    )
                    self.assertFalse(
                        any(
                            command[:2] == ["serve", "--bg"]
                            for command in self.tailscale_commands()
                        )
                    )
                    self.assertFalse(Path(str(report["record_path"])).exists())

    def test_route_apply_recovers_only_matching_absent_pending_attempt(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text("{}")
        self.seed_preserved_state()
        with self.healthy_owned_service():
            self.env["FAKE_SERVE_MODE"] = "fail_before"
            failed_result, failed = self.route_command("--apply", assert_unchanged=False)
            record_path = Path(str(failed["record_path"]))
            pending = json.loads(record_path.read_text())
            self.env.pop("FAKE_SERVE_MODE")
            retry_result, retry = self.route_command("--apply", assert_unchanged=False)
        self.assertEqual(failed_result.returncode, 1)
        self.assertEqual(failed["outcome"], "pending")
        self.assertEqual(
            failed["effects"],
            {
                "route_intent": "completed",
                "tailscale_serve_route": "unknown",
                "route_completion": "not_started",
            },
        )
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(retry_result.returncode, 0, retry_result.stdout + retry_result.stderr)
        self.assertEqual(retry["outcome"], "applied")
        self.assertEqual(cast(dict[str, str], retry["effects"])["route_intent"], "unchanged")
        self.assertNotIn("attempt_id", json.loads(record_path.read_text()))

    def test_route_apply_keeps_pending_after_unacknowledged_write(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text("{}")
        self.seed_preserved_state()
        with self.healthy_owned_service():
            self.env["FAKE_SERVE_MODE"] = "fail_after"
            failed_result, failed = self.route_command("--apply", assert_unchanged=False)
            self.env.pop("FAKE_SERVE_MODE")
            command_count = len(self.tailscale_commands())
            retry_result, retry = self.route_command("--apply")
        self.assertEqual(failed_result.returncode, 1)
        self.assertEqual(failed["outcome"], "pending")
        self.assertEqual(
            cast(dict[str, str], failed["effects"])["tailscale_serve_route"], "completed"
        )
        self.assertEqual(
            json.loads(Path(str(failed["record_path"])).read_text())["status"], "pending"
        )
        self.assertEqual(retry_result.returncode, 1)
        self.assertEqual(retry["outcome"], "blocked")
        self.assertEqual(len(self.tailscale_commands()), command_count + 2)

    def test_route_apply_blocks_preflight_and_config_drift(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text("{}")
        self.seed_preserved_state()
        with self.healthy_owned_service():
            self.env["FAKE_DRIFT_SERVE_ON_STATUS"] = "2"
            route_result, route_report = self.route_command("--apply", assert_unchanged=False)
            self.env.pop("FAKE_DRIFT_SERVE_ON_STATUS")
            Path(self.env["FAKE_TAILSCALE_LOG"]).write_text("")
            self.env["FAKE_DRIFT_CONFIG_ON_TAILSCALE_STATUS"] = "yes"
            config_result, config_report = self.route_command("--apply", assert_unchanged=False)
            self.env.pop("FAKE_DRIFT_CONFIG_ON_TAILSCALE_STATUS")
        self.assertEqual(route_result.returncode, 1)
        self.assertIn(
            "route_preflight_drift",
            {item["code"] for item in cast(list[dict[str, str]], route_report["blockers"])},
        )
        self.assertEqual(config_result.returncode, 1)
        self.assertIn(
            "config_drift",
            {item["code"] for item in cast(list[dict[str, str]], config_report["blockers"])},
        )
        self.assertFalse(
            any(command[:2] == ["serve", "--bg"] for command in self.tailscale_commands())
        )
        self.assertFalse(Path(str(route_report["record_path"])).exists())

    def test_route_apply_leaves_pending_when_surrounding_state_changes(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text("{}")
        self.seed_preserved_state()
        with self.healthy_owned_service():
            self.env["FAKE_SERVE_MODE"] = "mutate_unrelated"
            result, report = self.route_command("--apply", assert_unchanged=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["outcome"], "pending")
        self.assertIn(
            "serve_postcheck_failed",
            {item["code"] for item in cast(list[dict[str, str]], report["blockers"])},
        )
        self.assertEqual(
            json.loads(Path(str(report["record_path"])).read_text())["status"], "pending"
        )
        self.assertEqual(
            cast(dict[str, str], report["effects"])["tailscale_serve_route"], "unknown"
        )

    def test_route_apply_rechecks_after_pending_intent_before_serve(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text("{}")
        self.seed_preserved_state()
        with self.healthy_owned_service():
            self.env["FAKE_DRIFT_SERVE_ON_STATUS"] = "3"
            result, report = self.route_command("--apply", assert_unchanged=False)
            self.env.pop("FAKE_DRIFT_SERVE_ON_STATUS")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["outcome"], "blocked")
        self.assertIn(
            "route_preflight_drift",
            {item["code"] for item in cast(list[dict[str, str]], report["blockers"])},
        )
        self.assertEqual(
            cast(dict[str, str], report["effects"]),
            {
                "route_intent": "completed",
                "tailscale_serve_route": "not_started",
                "route_completion": "not_started",
            },
        )
        self.assertEqual(
            json.loads(Path(str(report["record_path"])).read_text())["status"], "pending"
        )
        self.assertFalse(
            any(command[:2] == ["serve", "--bg"] for command in self.tailscale_commands())
        )

    def test_route_apply_reports_post_intent_inspection_error(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text("{}")
        self.seed_preserved_state()
        routes = self.base / "state-home/html-publish/routes"
        with self.healthy_owned_service():
            argv = [
                str(self.cli),
                "--config",
                str(self.config),
                "--json",
                "host",
                "route",
                "setup",
                "--unit-name",
                "html-publish-test",
                "--apply",
            ]
            before = self.state_manifest()
            self.env["FAKE_BLOCK_ROUTES_AFTER_INTENT"] = "yes"
            try:
                result = subprocess.run(
                    argv,
                    cwd=self.base,
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=25,
                )
            finally:
                self.env.pop("FAKE_BLOCK_ROUTES_AFTER_INTENT")
                routes.chmod(0o700)
            after = self.state_manifest()
            with (self.artifacts / f"{self._testMethodName}.jsonl").open("a") as evidence:
                evidence.write(
                    json.dumps(
                        {
                            "classification": "controlled",
                            "test": self._testMethodName,
                            "argv": argv,
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                            "exit_code": result.returncode,
                            "before": before,
                            "after": after,
                            "systemctl_commands": self.systemctl_commands(),
                            "tailscale_commands": self.tailscale_commands(),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(report["outcome"], "blocked")
        self.assertIn(
            "route_preflight_inspection_failed",
            {item["code"] for item in cast(list[dict[str, str]], report["blockers"])},
        )
        self.assertEqual(
            cast(dict[str, str], report["effects"]),
            {
                "route_intent": "completed",
                "tailscale_serve_route": "not_started",
                "route_completion": "not_started",
            },
        )
        self.assertEqual(
            json.loads((routes / "html-publish-test.json").read_text())["status"], "pending"
        )
        self.assertFalse(
            any(command[:2] == ["serve", "--bg"] for command in self.tailscale_commands())
        )

    def test_route_apply_rejects_changed_pending_baseline(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text("{}")
        with self.healthy_owned_service():
            self.env["FAKE_SERVE_MODE"] = "fail_before"
            _, failed = self.route_command("--apply", assert_unchanged=False)
            self.env.pop("FAKE_SERVE_MODE")
            Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text(
                json.dumps({"TCP": {"443": {"HTTPS": True}}})
            )
            Path(self.env["FAKE_TAILSCALE_LOG"]).write_text("")
            result, report = self.route_command("--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "pending_baseline_drift",
            {item["code"] for item in cast(list[dict[str, str]], report["blockers"])},
        )
        self.assertEqual(
            json.loads(Path(str(failed["record_path"])).read_text())["status"], "pending"
        )
        self.assertFalse(
            any(command[:2] == ["serve", "--bg"] for command in self.tailscale_commands())
        )

    def test_route_command_is_parser_discoverable(self) -> None:
        help_result = subprocess.run(
            [str(self.cli), "host", "route", "setup", "--help"],
            cwd=self.base,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--unit-name", help_result.stdout)
        self.assertIn("--apply", help_result.stdout)
        self.assertNotIn("--port", help_result.stdout)
        schema_result = subprocess.run(
            [str(self.cli), "schema"],
            cwd=self.base,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(schema_result.returncode, 0, schema_result.stderr)
        schema = json.loads(schema_result.stdout)
        host = next(item for item in schema["commands"] if item["name"] == "host")
        service_setup = next(item for item in host["commands"] if item["name"] == "setup")
        service_flags = [flag for option in service_setup["options"] for flag in option["flags"]]
        self.assertNotIn("--tailscale", service_flags)
        route = next(item for item in host["commands"] if item["name"] == "route")
        setup = next(item for item in route["commands"] if item["name"] == "setup")
        flags = [flag for option in setup["options"] for flag in option["flags"]]
        self.assertIn("--unit-name", flags)
        self.assertIn("--apply", flags)
        self.assertNotIn("--port", flags)

    def test_route_preview_recognizes_owned_exact_mapping(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        serve = {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                "preview.test.ts.net:443": {
                    "Handlers": {"/pages/": {"Proxy": f"http://127.0.0.1:{self.port}"}}
                }
            },
            "AllowFunnel": {"preview.test.ts.net:443": False},
        }
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text(json.dumps(serve))
        self.seed_preserved_state()
        with self.healthy_owned_service():
            foreign_result, foreign = self.route_command()
            self.assertEqual(foreign_result.returncode, 1)
            self.assertEqual(foreign["route_state"], "foreign")
            self.seed_route_record(foreign, "owned")
            owned_result, owned = self.route_command()
            serve["Web"] = {
                "preview.test.ts.net:443": {
                    "Handlers": {
                        "/pages/": {"Proxy": f"http://127.0.0.1:{self.port}"},
                        "/pages/child": {"Proxy": "http://127.0.0.1:9000"},
                    }
                }
            }
            Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text(json.dumps(serve))
            collision_result, collision = self.route_command()
            serve["Web"] = {
                "preview.test.ts.net:443": {
                    "Handlers": {"/pages/": {"Proxy": "http://127.0.0.1:9001"}}
                }
            }
            Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text(json.dumps(serve))
            changed_result, changed = self.route_command()
        self.assertEqual(owned_result.returncode, 0, owned_result.stdout + owned_result.stderr)
        self.assertEqual(owned["outcome"], "unchanged")
        self.assertEqual(owned["route_state"], "owned")
        self.assertEqual(owned["blockers"], [])
        self.assertEqual(owned["proposed_effects"], [])
        self.assertEqual(collision_result.returncode, 1)
        self.assertEqual(collision["route_state"], "collision")
        self.assertEqual(changed_result.returncode, 1)
        self.assertEqual(changed["route_state"], "drift")
        changed_codes = {
            blocker["code"] for blocker in cast(list[dict[str, str]], changed["blockers"])
        }
        self.assertEqual(changed_codes, {"route_changed", "owned_route_drift"})

    def test_route_preview_reports_pending_and_binding_drift(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        serve = {
            "TCP": {"443": {"HTTPS": True}},
            "AllowFunnel": {"preview.test.ts.net:443": False},
        }
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text(json.dumps(serve))
        self.seed_preserved_state()
        with self.healthy_owned_service():
            planned_result, planned = self.route_command()
            self.assertEqual(planned_result.returncode, 0)
            record_path = self.seed_route_record(planned, "pending")
            pending_result, pending = self.route_command()
            self.assertEqual(pending_result.returncode, 1)
            self.assertEqual(pending["route_state"], "pending")
            record = json.loads(record_path.read_text())
            record["listen_port"] = self.port + 1
            record_path.write_text(json.dumps(record))
            drift_result, drift = self.route_command()
            record["status"] = []
            record_path.write_text(json.dumps(record))
            malformed_result, malformed = self.route_command()
        self.assertEqual(drift_result.returncode, 1)
        self.assertEqual(drift["route_state"], "drift")
        self.assertEqual(drift["proposed_effects"], [])
        self.assertEqual(malformed_result.returncode, 1)
        self.assertEqual(malformed["route_state"], "unknown")
        malformed_codes = {
            blocker["code"] for blocker in cast(list[dict[str, str]], malformed["blockers"])
        }
        self.assertIn("route_record_invalid", malformed_codes)

    def test_route_preview_blocks_missing_and_unhealthy_service(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        missing_result, missing = self.route_command()
        self.assertEqual(missing_result.returncode, 1)
        self.assertEqual(missing["route_state"], "unknown")
        self.assertEqual(
            cast(list[dict[str, str]], missing["blockers"])[0]["code"],
            "service_not_owned",
        )
        self.assertEqual(self.tailscale_commands(), [])

        self.seed_owned_service()
        Path(self.env["FAKE_TAILSCALE_LOG"]).write_text("")
        unhealthy_result, unhealthy = self.route_command()
        self.assertEqual(unhealthy_result.returncode, 1)
        self.assertEqual(unhealthy["route_state"], "unknown")
        codes = {blocker["code"] for blocker in cast(list[dict[str, str]], unhealthy["blockers"])}
        self.assertIn("health_failed", codes)
        self.assertEqual(self.tailscale_commands(), [])

    def test_route_preview_rejects_redirected_health(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        self.seed_owned_service()
        with (
            self.controlled_http(0, status=200, body=b"ok\n") as (
                target_port,
                target_requests,
            ),
            self.controlled_http(
                self.port,
                status=302,
                location=f"http://127.0.0.1:{target_port}/_html-publish-health",
            ) as (_, redirect_requests),
        ):
            result, report = self.route_command()
        self.assertEqual(result.returncode, 1)
        codes = {blocker["code"] for blocker in cast(list[dict[str, str]], report["blockers"])}
        self.assertIn("health_failed", codes)
        self.assertEqual(redirect_requests, ["/_html-publish-health"])
        self.assertEqual(target_requests, [])

    def test_route_preview_bounds_trickling_health_headers(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        self.seed_owned_service()

        class TrickleHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                self.request.recv(4096)
                response = b"HTTP/1.0 200 OK\r\nContent-Length: 3\r\n\r\nok\n"
                for byte in response:
                    try:
                        self.request.sendall(bytes((byte,)))
                    except OSError:
                        return
                    time.sleep(0.1)

        class TrickleServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = TrickleServer(("127.0.0.1", self.port), TrickleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            result, report = self.route_command()
        finally:
            elapsed = time.monotonic() - started
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(result.returncode, 1)
        codes = {blocker["code"] for blocker in cast(list[dict[str, str]], report["blockers"])}
        self.assertIn("health_failed", codes)
        self.assertLess(elapsed, 2.5)
        self.assertEqual(self.tailscale_commands(), [])

    def test_route_preview_rejects_unit_that_matches_only_the_old_record(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        with self.healthy_owned_service() as plan:
            unit_path = Path(str(plan["unit_path"]))
            record_path = Path(str(plan["record_path"]))
            record = json.loads(record_path.read_text())
            record["unit"] = "old selected unit\n"
            record_path.write_text(json.dumps(record))
            unit_path.write_text("old selected unit\n")
            result, report = self.route_command()
        self.assertEqual(result.returncode, 1)
        messages = " ".join(
            blocker["message"] for blocker in cast(list[dict[str, str]], report["blockers"])
        )
        self.assertIn("selected executable and configuration", messages)
        self.assertEqual(self.tailscale_commands(), [])

    def test_route_preview_detects_record_replacement_during_health(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://preview.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        with self.healthy_owned_service():
            before = self.state_manifest()
            self.env["FAKE_DRIFT_HOST_RECORD_ON_SHOW"] = "yes"
            try:
                result, report = self.route_command(assert_unchanged=False)
            finally:
                self.env.pop("FAKE_DRIFT_HOST_RECORD_ON_SHOW")
            after = self.state_manifest()
        self.assertEqual(result.returncode, 1)
        codes = {blocker["code"] for blocker in cast(list[dict[str, str]], report["blockers"])}
        self.assertIn("service_drift", codes)
        before_record = before.pop("service_record")
        after_record = after.pop("service_record")
        self.assertNotEqual(before_record, after_record)
        self.assertEqual(before, after)
        self.assertEqual(self.tailscale_commands(), [])

    def test_route_preview_blocks_funnel_and_unknown_exposure(self) -> None:
        cases = (
            (
                "enabled",
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "AllowFunnel": {"preview.test.ts.net:443": True},
                },
                "collision",
                "funnel_enabled",
            ),
            (
                "unknown",
                {
                    "TCP": {"443": {"HTTPS": True}},
                    "AllowFunnel": {"preview.test.ts.net:443": "maybe"},
                },
                "unknown",
                "unknown_serve_state",
            ),
        )
        for name, serve, state, code in cases:
            with self.subTest(name=name):
                Path(self.env["FAKE_TAILSCALE_LOG"]).write_text("")
                result, report = self.tailscale_preview(serve)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(report["route_state"], state)
                codes = {
                    blocker["code"] for blocker in cast(list[dict[str, str]], report["blockers"])
                }
                self.assertIn(code, codes)
                self.assertEqual(report["proposed_effects"], [])

    def test_failed_service_start_retains_completed_effects(self) -> None:
        self.archive.mkdir()
        (self.archive / "sentinel").write_bytes(b"archive-preserved")
        receipt = self.base / "receipt.json"
        receipt.write_bytes(b"receipt-preserved")
        result, report = self.command("--apply")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["outcome"], "error")
        effects = cast(dict[str, str], report["effects"])
        self.assertEqual(effects["unit"], "changed")
        self.assertEqual(effects["enable"], "changed")
        self.assertEqual(effects["start"], "unknown")
        record = json.loads(Path(str(report["record_path"])).read_text())
        self.assertEqual(record["pending"], "start")
        self.assertTrue(record["enabled"])
        self.assertEqual((self.archive / "sentinel").read_bytes(), b"archive-preserved")
        self.assertEqual(receipt.read_bytes(), b"receipt-preserved")
        self.assertFalse(self.runtime.exists())

    def test_config_change_after_enable_stops_before_start(self) -> None:
        self.env["FAKE_DRIFT_CONFIG_ON_ENABLE"] = "yes"
        try:
            result, report = self.command("--apply")
        finally:
            self.env.pop("FAKE_DRIFT_CONFIG_ON_ENABLE")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["outcome"], "error")
        effects = cast(dict[str, str], report["effects"])
        self.assertEqual(effects["unit"], "changed")
        self.assertEqual(effects["enable"], "changed")
        self.assertEqual(effects["start"], "not_started")
        self.assertIn("changed during setup", cast(dict[str, str], report["error"])["message"])

    def test_different_unit_names_share_one_setup_lock(self) -> None:
        previewed, plan = self.command()
        self.assertEqual(previewed.returncode, 0)
        lock_path = Path(str(plan["record_path"])).parent / ".setup.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(
                [
                    str(self.cli),
                    "--config",
                    str(self.config),
                    "--json",
                    "host",
                    "setup",
                    "--unit-name",
                    "html-publish-other",
                    "--port",
                    str(self.port),
                    "--apply",
                ],
                cwd=self.base,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=25,
            )
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report["error"]["code"], "setup_busy")
        self.assertFalse(
            (self.base / "config-home/systemd/user/html-publish-other.service").exists()
        )

    def test_foreground_serves_published_and_updated_bytes(self) -> None:
        server = subprocess.Popen(
            [
                str(self.cli),
                "--config",
                str(self.config),
                "host",
                "serve",
                "--port",
                str(self.port),
            ],
            cwd=self.base,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _ in range(50):
                try:
                    with urllib.request.urlopen(
                        self.url + "_html-publish-health", timeout=1
                    ) as response:
                        if response.read() == b"ok\n":
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("installed host serve did not answer health")
            source = self.base / "page.html"
            source.write_bytes(b"<h1>first host page</h1>\n")
            first = subprocess.run(
                [
                    str(self.cli),
                    "--config",
                    str(self.config),
                    "--json",
                    "publish",
                    "--name",
                    "hostpage",
                    "--source",
                    str(source),
                    "--target",
                    self.url,
                ],
                cwd=self.base,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            first_report = json.loads(first.stdout)
            with urllib.request.urlopen(self.url + "hostpage/", timeout=2) as response:
                self.assertEqual(response.read(), b"<h1>first host page</h1>\n")
            source.write_bytes(b"<h1>updated host page</h1>\n")
            second = subprocess.run(
                [
                    str(self.cli),
                    "--config",
                    str(self.config),
                    "--json",
                    "publish",
                    "--name",
                    "hostpage",
                    "--source",
                    str(source),
                    "--target",
                    self.url,
                    "--expected-revision",
                    str(first_report["active_revision"]),
                ],
                cwd=self.base,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            with urllib.request.urlopen(self.url + "hostpage/", timeout=2) as response:
                self.assertEqual(response.read(), b"<h1>updated host page</h1>\n")
        finally:
            server.send_signal(signal.SIGTERM)
            self.assertEqual(server.wait(timeout=5), 0)
            if server.stderr:
                server.stderr.close()

    def test_delayed_listener_apply(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://host.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        self.env["FAKE_START_MODE"] = "success"
        servers: list[subprocess.Popen[str]] = []

        def start_later() -> None:
            time.sleep(0.7)
            servers.append(
                subprocess.Popen(
                    [
                        str(self.cli),
                        "--config",
                        str(self.config),
                        "host",
                        "serve",
                        "--port",
                        str(self.port),
                    ],
                    cwd=self.base,
                    env=self.env.copy(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            )

        starter = threading.Thread(target=start_later)
        starter.start()
        try:
            applied, report = self.command("--apply")
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            self.assertEqual(report["outcome"], "applied")
            self.assertEqual(
                report["verification"], {"loopback": "passed", "private_https": "not_checked"}
            )
            repeat, report = self.command("--apply")
            self.assertEqual(repeat.returncode, 0, repeat.stdout + repeat.stderr)
            self.assertEqual(report["outcome"], "unchanged")
        finally:
            starter.join(timeout=3)
            for server in servers:
                server.send_signal(signal.SIGTERM)
                self.assertEqual(server.wait(timeout=5), 0)
            self.env.pop("FAKE_START_MODE")

    def test_service_apply_rejects_redirected_health(self) -> None:
        self.env["FAKE_START_MODE"] = "success"
        try:
            with (
                self.controlled_http(0, status=200, body=b"ok\n") as (
                    target_port,
                    target_requests,
                ),
                self.controlled_http(
                    self.port,
                    status=302,
                    location=f"http://127.0.0.1:{target_port}/_html-publish-health",
                ) as (_, redirect_requests),
            ):
                result, report = self.command("--apply")
        finally:
            self.env.pop("FAKE_START_MODE")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["outcome"], "error")
        self.assertEqual(
            cast(dict[str, str], report["error"])["code"],
            "health_failed",
        )
        self.assertEqual(redirect_requests, ["/_html-publish-health"])
        self.assertEqual(target_requests, [])

    def test_transient_uvx_refuses_setup(self) -> None:
        env = {**self.env, "UV_TOOL_DIR": str(self.base / "other-tools")}
        result = subprocess.run(
            [
                "uvx",
                "--no-cache",
                "--from",
                str(self.wheel),
                "html-publish",
                "--config",
                str(self.config),
                "--json",
                "host",
                "setup",
                "--unit-name",
                "html-publish-test",
                "--port",
                str(self.port),
            ],
            cwd=self.base,
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertIn("uv tool install", " ".join(cast(list[str], report["blockers"])))
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.runtime.exists())

    def test_invalid_host_config_keeps_host_error_envelope(self) -> None:
        self.config.write_text("{}")
        result, report = self.command()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["operation"], "host.setup")
        self.assertEqual(report["outcome"], "error")
        self.assertEqual(cast(dict[str, object], report["error"])["code"], "invalid_config")

    def test_owned_unit_and_config_drift_block(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://host.test.ts.net/pages/",
                    "allow_http": False,
                }
            )
        )
        result, plan = self.command()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        unit_path = Path(str(plan["unit_path"]))
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(str(plan["unit"]))
        (self.base / "systemctl.json").write_text('{"enabled": true, "active": true}')
        record_path = Path(str(plan["record_path"]))
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "installation_id": "owned-test-installation",
                    "uid": os.geteuid(),
                    "config_path": str(self.config),
                    "fingerprint": plan["fingerprint"],
                    "package_hash": plan["package_hash"],
                    "unit_path": str(unit_path),
                    "listen_port": self.port,
                    "unit": plan["unit"],
                    "enabled": True,
                    "pending": None,
                }
            )
        )
        unchanged, report = self.command("--apply")
        self.assertEqual(unchanged.returncode, 0, unchanged.stdout + unchanged.stderr)
        self.assertEqual(report["outcome"], "unchanged")
        unit_path.write_text(str(plan["unit"]) + "\n")
        drift, report = self.command()
        self.assertEqual(drift.returncode, 1)
        self.assertIn("unit bytes", " ".join(cast(list[str], report["blockers"])))
        unit_path.write_text(str(plan["unit"]))
        self.config.write_text(
            json.dumps(
                {
                    "archive": str(self.archive),
                    "runtime": str(self.runtime),
                    "base_url": "https://host.test.ts.net/other/",
                    "allow_http": False,
                }
            )
        )
        drift, report = self.command()
        self.assertEqual(drift.returncode, 1)
        self.assertIn("fingerprint", " ".join(cast(list[str], report["blockers"])))


if __name__ == "__main__":
    unittest.main()
