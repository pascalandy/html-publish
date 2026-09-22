from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".agents/skills/verify-html-publish/scripts/instance.sh"


class InstalledConfigurationTest(unittest.TestCase):
    def test_configuration_and_doctor_leave_publisher_state_untouched(self) -> None:
        run_id = f"config-{os.getpid()}-{time.time_ns()}"

        def lifecycle(action: str) -> dict[str, str]:
            result = subprocess.run(
                ["bash", str(HELPER), action, run_id],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

        try:
            values = lifecycle("start")
            lifecycle("doctor")
            artifacts = Path(values["ARTIFACTS"])
            home = artifacts / "home"
            xdg_config = artifacts / "xdg-config"
            xdg_data = artifacts / "xdg-data"
            publisher = xdg_config / "html-publish/publisher.json"
            client = xdg_config / "html-publish/client.json"
            remote_client = artifacts / "remote-client.json"
            fingerprint_client = artifacts / "fingerprint-client.json"
            legacy_client = artifacts / "legacy-client.json"
            calls = artifacts / "forbidden-calls.txt"
            binary = artifacts / "bin"
            binary.mkdir()
            for program in ("ssh", "scp", "ssh-keyscan", "systemctl", "tailscale"):
                probe = binary / program
                probe.write_text('#!/bin/sh\nprintf \'%s\\n\' "$0 $*" >> "$CALL_LOG"\nexit 97\n')
                probe.chmod(0o755)
            executable_path = os.pathsep.join(
                (str(binary), str(Path(values["CLI"]).parent), os.environ["PATH"])
            )
            environment = os.environ | {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(xdg_config),
                "XDG_DATA_HOME": str(xdg_data),
                "PATH": executable_path,
                "CALL_LOG": str(calls),
            }
            evidence = artifacts / "config-cli.jsonl"

            def command(executable: str, *args: str, exit_code: int = 0) -> dict[str, object]:
                argv = [executable, *args]
                result = subprocess.run(
                    argv,
                    cwd=artifacts,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                with evidence.open("a") as output:
                    output.write(
                        json.dumps(
                            {
                                "argv": argv,
                                "exit": result.returncode,
                                "stdout": result.stdout,
                                "stderr": result.stderr,
                            }
                        )
                        + "\n"
                    )
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")
                return cast(dict[str, object], json.loads(result.stdout))

            cli = values["CLI"]
            remote = str(Path(cli).with_name("html-publish-remote"))
            target = values["URL"] + "/"
            self.assertEqual(
                command(
                    cli,
                    "config",
                    "init",
                    "--role",
                    "publisher",
                    "--config",
                    str(publisher),
                    "--base-url",
                    target,
                    "--allow-http",
                    "--json",
                )["outcome"],
                "config_written",
            )
            first_bytes = publisher.read_bytes()
            first_stat = publisher.stat()
            self.assertEqual(
                command(
                    cli,
                    "--json",
                    "--config",
                    str(publisher),
                    "config",
                    "init",
                    "--role",
                    "publisher",
                    "--base-url",
                    target,
                    "--allow-http",
                )["outcome"],
                "unchanged",
            )
            self.assertEqual(publisher.read_bytes(), first_bytes)
            self.assertEqual(publisher.stat().st_mtime_ns, first_stat.st_mtime_ns)
            self.assertEqual(publisher.stat().st_mode, first_stat.st_mode)
            alias = artifacts / "publisher-alias.json"
            alias.symlink_to(publisher)
            symlink_result = command(
                cli,
                "config",
                "init",
                "--role",
                "publisher",
                "--config",
                str(alias),
                "--base-url",
                target,
                "--allow-http",
                "--json",
                exit_code=1,
            )
            self.assertEqual(
                cast(dict[str, object], symlink_result["error"])["code"], "config_exists"
            )
            self.assertEqual(
                cast(
                    dict[str, object],
                    command(cli, "config", "show", "--role", "publisher", "--json")["config"],
                )["source"],
                "user_default",
            )
            shown = command(
                cli,
                "config",
                "--config",
                str(publisher),
                "--json",
                "show",
                "--role",
                "publisher",
                "--command-seconds",
                "7",
            )
            self.assertEqual(
                cast(dict[str, object], cast(dict[str, object], shown["values"])["limits"])[
                    "command_seconds"
                ],
                7,
            )
            self.assertEqual(
                cast(dict[str, object], shown["origins"])["limits.command_seconds"], "argument"
            )
            self.assertEqual(
                command(cli, "config", "validate", "--role", "publisher", "--json")["outcome"],
                "valid",
            )
            doctor = command(cli, "doctor", "--role", "publisher", "--json")
            checks = cast(list[dict[str, str]], doctor["checks"])
            self.assertEqual(doctor["outcome"], "diagnosed")
            self.assertEqual(
                next(check["status"] for check in checks if check["id"] == "archive"), "warning"
            )
            self.assertEqual(
                next(check["status"] for check in checks if check["id"] == "network"), "skipped"
            )
            self.assertFalse(xdg_data.exists())
            self.assertFalse(calls.exists())
            environment["XDG_CONFIG_HOME"] = "relative/config"
            relative_xdg = command(
                cli, "config", "show", "--role", "publisher", "--json", exit_code=1
            )
            self.assertEqual(
                cast(dict[str, object], relative_xdg["error"])["code"], "invalid_config"
            )
            environment["XDG_CONFIG_HOME"] = str(xdg_config)

            self.assertEqual(
                command(
                    cli,
                    "config",
                    "init",
                    "--role",
                    "client",
                    "--config",
                    str(client),
                    "--execution",
                    "local",
                    "--target-id",
                    "local-review",
                    "--base-url",
                    target,
                    "--publisher-config",
                    str(publisher),
                    "--json",
                )["outcome"],
                "config_written",
            )
            client_bytes = client.read_bytes()
            local = command(cli, "config", "show", "--role", "client", "--json")
            self.assertIsInstance(cast(dict[str, object], local["values"])["fingerprint"], str)
            local_doctor = command(cli, "doctor", "--role", "client", "--json")
            local_checks = cast(list[dict[str, str]], local_doctor["checks"])
            self.assertEqual(
                next(
                    check["status"] for check in local_checks if check["id"] == "publisher_target"
                ),
                "pass",
            )
            self.assertEqual(client.read_bytes(), client_bytes)
            self.assertFalse(calls.exists())
            self.assertEqual(client.stat().st_mode & 0o777, 0o600)

            command(
                cli,
                "config",
                "init",
                "--role",
                "client",
                "--config",
                str(remote_client),
                "--execution",
                "remote",
                "--target-id",
                "host-review",
                "--base-url",
                target,
                "--host",
                "operator@example.test",
                "--remote-executable",
                "/opt/html-publish/bin/html-publish",
                "--remote-config",
                "/etc/html-publish/publisher.json",
                "--incoming-root",
                "/srv/html-publish/incoming",
                "--json",
            )
            shown_remote = command(
                cli, "config", "show", "--role", "client", "--config", str(remote_client), "--json"
            )
            execution = cast(
                dict[str, object], cast(dict[str, object], shown_remote["values"])["execution"]
            )
            self.assertEqual(execution["incoming_root"], "/srv/html-publish/incoming")
            self.assertEqual(execution["host"], "operator@example.test")
            command(
                cli,
                "config",
                "init",
                "--role",
                "client",
                "--config",
                str(fingerprint_client),
                "--execution",
                "remote",
                "--target-id",
                "stable-review",
                "--base-url",
                "https://review.example/pages/",
                "--host",
                "operator@example.test",
                "--remote-executable",
                "/opt/html-publish/bin/html-publish",
                "--remote-config",
                "/etc/html-publish/publisher.json",
                "--incoming-root",
                "/srv/html-publish/incoming",
                "--json",
            )
            fingerprint = command(
                cli,
                "config",
                "show",
                "--role",
                "client",
                "--config",
                str(fingerprint_client),
                "--json",
            )
            self.assertEqual(
                cast(dict[str, object], fingerprint["values"])["fingerprint"],
                "03017328146237a41159e406ce32fa56fa1edabe33b439f860ff49520e7e0721",
            )
            legacy_client.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "target": {"id": "legacy", "base_url": "http://intranet.example/pages/"},
                        "execution": {
                            "kind": "remote",
                            "command": ["html-publish-remote"],
                            "host": "operator@example.test",
                        },
                        "limits": {},
                    }
                )
            )
            legacy = command(
                cli, "config", "show", "--role", "client", "--config", str(legacy_client), "--json"
            )
            self.assertEqual(
                cast(dict[str, object], legacy["values"])["fingerprint"],
                "14471749cdb83a4f180c6bfa57d4056fa2853a3d5cee66376b543422888455eb",
            )
            incomplete = command(
                remote, "--config", str(legacy_client), "status", "--json", exit_code=1
            )
            self.assertEqual(cast(dict[str, object], incomplete["error"])["code"], "invalid_config")
            self.assertFalse(calls.exists())
            refused = command(
                cli,
                "config",
                "init",
                "--role",
                "publisher",
                "--config",
                str(publisher),
                "--base-url",
                "https://different.example/pages/",
                "--json",
                exit_code=1,
            )
            self.assertEqual(cast(dict[str, object], refused["error"])["code"], "config_exists")
            self.assertEqual(publisher.read_bytes(), first_bytes)
            self.assertEqual(
                cast(dict[str, object], command(remote, "status", "--json", exit_code=1)["error"])[
                    "code"
                ],
                "invalid_config",
            )
            environment["XDG_CONFIG_HOME"] = str(artifacts / "empty-config")
            self.assertEqual(
                cast(dict[str, object], command(remote, "status", "--json", exit_code=2)["error"])[
                    "code"
                ],
                "invalid_usage",
            )
            self.assertFalse(calls.exists())
            fixture_network = command(
                cli,
                "doctor",
                "--role",
                "client",
                "--config",
                str(remote_client),
                "--network",
                "--json",
                exit_code=1,
            )
            self.assertEqual(fixture_network["outcome"], "error")
            invocations = calls.read_text().splitlines()
            self.assertEqual(len(invocations), 2)
            self.assertTrue(all("ssh " in invocation for invocation in invocations))
            self.assertTrue(any("--version" in invocation for invocation in invocations))
            self.assertTrue(
                any("doctor --role publisher" in invocation for invocation in invocations)
            )
            self.assertTrue(
                all(
                    " publish " not in invocation and " restore " not in invocation
                    for invocation in invocations
                )
            )
            calls.unlink()
            schema = command(cli, "schema")
            self.assertEqual(
                command(cli, "config", "show", "--version", "--json")["executable"], "html-publish"
            )
            groups = cast(list[dict[str, object]], schema["commands"])
            nested = next(group for group in groups if group["name"] == "config")
            self.assertEqual(
                [entry["name"] for entry in cast(list[dict[str, object]], nested["commands"])],
                ["init", "show", "validate"],
            )
            network = command(
                cli,
                "doctor",
                "--role",
                "publisher",
                "--config",
                str(publisher),
                "--network",
                "--json",
            )
            self.assertEqual(network["scope"], "network")
            self.assertTrue(
                any(
                    check["id"] == "http_reachability"
                    for check in cast(list[dict[str, str]], network["checks"])
                )
            )
            self.assertFalse(calls.exists())
            self.assertEqual(publisher.read_bytes(), first_bytes)
            self.assertEqual(publisher.stat().st_mtime_ns, first_stat.st_mtime_ns)
            self.assertEqual(publisher.stat().st_mode, first_stat.st_mode)
            self.assertFalse(xdg_data.exists())
        finally:
            lifecycle("stop")
            lifecycle("stop")
