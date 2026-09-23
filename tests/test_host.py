from __future__ import annotations

import fcntl
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
        cls.env["PATH"] = str(fake_bin) + os.pathsep + cls.env["PATH"]
        cls.env["FAKE_SYSTEMCTL_STATE"] = str(cls.base / "systemctl.json")

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
