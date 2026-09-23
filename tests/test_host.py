from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from typing import cast


@unittest.skipUnless(sys.platform == "linux", "Linux user service setup")
class InstalledHostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.temp = tempfile.TemporaryDirectory(prefix="html publish % host-")
        cls.base = Path(cls.temp.name)
        uv_executable = shutil.which("uv", path="/usr/bin:/bin") or shutil.which("uv")
        if uv_executable is None:
            raise AssertionError("uv is required for installed host verification")
        wheel_dir = cls.base / "wheel"
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
state_path = pathlib.Path(os.environ['FAKE_SYSTEMCTL_STATE'])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
unit = pathlib.Path(os.environ['XDG_CONFIG_HOME']) / 'systemd/user/html-publish-test.service'
if 'show' in args:
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
    print(pathlib.Path(os.environ['FAKE_TAILSCALE_STATUS']).read_text())
    sys.exit(0)
if args == ['serve', 'status', '--json']:
    print(pathlib.Path(os.environ['FAKE_TAILSCALE_SERVE']).read_text())
    sys.exit(0)
print('unexpected tailscale command: ' + ' '.join(args), file=sys.stderr)
sys.exit(64)
""")
        tailscale.chmod(0o755)
        cls.env["PATH"] = str(fake_bin) + os.pathsep + cls.env["PATH"]
        cls.env["FAKE_SYSTEMCTL_STATE"] = str(cls.base / "systemctl.json")
        cls.env["FAKE_TAILSCALE_STATUS"] = str(cls.base / "tailscale-status.json")
        cls.env["FAKE_TAILSCALE_SERVE"] = str(cls.base / "tailscale-serve.json")
        cls.env["FAKE_TAILSCALE_LOG"] = str(cls.base / "tailscale.log")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
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
                    "Self": {"DNSName": "preview.test.ts.net."},
                }
            )
        )
        Path(self.env["FAKE_TAILSCALE_SERVE"]).write_text("{}")
        Path(self.env["FAKE_TAILSCALE_LOG"]).write_text("")
        for path in (
            self.base / "config-home/systemd/user/html-publish-test.service",
            self.base / "state-home/html-publish/hosts/html-publish-test.json",
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
        return self.command("--tailscale")

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

    def test_tailscale_preview_reports_absent_route_without_writes(self) -> None:
        serve_path = Path(self.env["FAKE_TAILSCALE_SERVE"])
        status_path = Path(self.env["FAKE_TAILSCALE_STATUS"])
        serve = {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                "preview.test.ts.net:443": {
                    "Handlers": {"/other": {"Proxy": "http://127.0.0.1:9000"}}
                }
            },
            "AllowFunnel": {"preview.test.ts.net:443": False},
        }
        serve_path.write_text(json.dumps(serve))
        before = (serve_path.read_bytes(), status_path.read_bytes())
        lock_path = self.base / "state-home/html-publish/hosts/.setup.lock"
        lock_existed = lock_path.exists()

        result, report = self.tailscale_preview(
            serve,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["outcome"], "planned")
        self.assertEqual(
            report["tailscale"],
            {
                "selected": {
                    "node": "preview.test.ts.net",
                    "https_port": 443,
                    "mount": "/pages/",
                    "target": f"http://127.0.0.1:{self.port}",
                },
                "node": {
                    "dns_name": "preview.test.ts.net",
                    "matches_selected": True,
                },
                "serve": {
                    "https_listener": True,
                    "selected_handler": None,
                    "other_routes": [
                        {
                            "node": "preview.test.ts.net",
                            "https_port": 443,
                            "mount": "/other",
                            "handler": {"Proxy": "http://127.0.0.1:9000"},
                        }
                    ],
                    "funnel": [
                        {
                            "node": "preview.test.ts.net",
                            "https_port": 443,
                            "enabled": False,
                        }
                    ],
                },
                "prerequisites": {
                    "command_available": True,
                    "authenticated": True,
                    "node_matches": True,
                    "serve_inspected": True,
                },
                "state": "absent",
                "blockers": [],
                "proposed_effects": ["tailscale_serve_route"],
                "private_https_verified": False,
            },
        )
        self.assertIn("tailscale_serve_route", cast(list[str], report["proposed_effects"]))
        self.assertEqual(
            self.tailscale_commands(),
            [
                ["status", "--json", "--peers=false"],
                ["serve", "status", "--json"],
            ],
        )
        self.assertEqual((serve_path.read_bytes(), status_path.read_bytes()), before)
        self.assertFalse(Path(str(report["record_path"])).exists())
        self.assertFalse(Path(str(report["unit_path"])).exists())
        self.assertEqual(lock_path.exists(), lock_existed)
        self.assertFalse((self.base / "systemctl.json").exists())
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.runtime.exists())

    def test_tailscale_preview_derives_root_and_custom_https_port(self) -> None:
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
        result, report = self.command("--tailscale")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tailscale = cast(dict[str, object], report["tailscale"])
        selected = cast(dict[str, object], tailscale["selected"])
        self.assertEqual(selected["node"], "preview.test.ts.net")
        self.assertEqual(selected["https_port"], 8444)
        self.assertEqual(selected["mount"], "/")
        self.assertEqual(selected["target"], f"http://127.0.0.1:{self.port}")

    def test_tailscale_preview_classifies_foreign_collisions_and_unknown_state(self) -> None:
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
                tailscale = cast(dict[str, object], report["tailscale"])
                self.assertEqual(tailscale["state"], state)
                blockers = cast(list[dict[str, str]], tailscale["blockers"])
                self.assertEqual(blockers[0]["code"], blocker_code)
                self.assertTrue(blockers[0]["message"])
                self.assertTrue(blockers[0]["next_action"])
                self.assertEqual(tailscale["proposed_effects"], [])
                self.assertEqual(report["proposed_effects"], [])
                self.assertEqual(len(self.tailscale_commands()), 2)

    def test_tailscale_preview_blocks_node_mismatch(self) -> None:
        result, report = self.tailscale_preview({}, hostname="other.test.ts.net")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        tailscale = cast(dict[str, object], report["tailscale"])
        self.assertEqual(tailscale["state"], "unknown")
        self.assertEqual(
            cast(list[dict[str, str]], tailscale["blockers"])[0]["code"],
            "node_mismatch",
        )
        self.assertEqual(tailscale["proposed_effects"], [])

    def test_tailscale_apply_is_rejected_before_config_or_inspection(self) -> None:
        self.config.write_text("not json")
        result, report = self.command("--tailscale", "--apply")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(report["outcome"], "error")
        self.assertEqual(cast(dict[str, object], report["error"])["code"], "invalid_usage")
        self.assertIn("issue #59", str(cast(dict[str, object], report["error"])["message"]))
        self.assertEqual(self.tailscale_commands(), [])
        self.assertFalse((self.base / "systemctl.json").exists())

    def test_tailscale_flag_is_discoverable(self) -> None:
        help_result = subprocess.run(
            [str(self.cli), "host", "setup", "--help"],
            cwd=self.base,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--tailscale", help_result.stdout)
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
        setup = next(item for item in host["commands"] if item["name"] == "setup")
        flags = [flag for option in setup["options"] for flag in option["flags"]]
        self.assertIn("--tailscale", flags)

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
