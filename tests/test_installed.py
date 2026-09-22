from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".agents/skills/verify-html-publish/scripts/instance.sh"


class InstalledWorkflowTest(unittest.TestCase):
    def test_installed_wheel_publishes_updates_retries_and_restores(self) -> None:
        run_id = f"installed-{os.getpid()}-{time.time_ns()}"
        run = Path("/tmp/html-publish-verify") / run_id
        artifacts = run / "artifacts"

        def lifecycle(command: str) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                ["bash", str(HELPER), command, run_id],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result

        try:
            started = lifecycle("start")
            values = dict(line.split("=", 1) for line in started.stdout.splitlines())
            (artifacts / "launch.txt").write_text(started.stdout + started.stderr)
            doctor = lifecycle("doctor")
            (artifacts / "doctor.txt").write_text(doctor.stdout + doctor.stderr)
            sources = dict(line.split("=", 1) for line in lifecycle("sources").stdout.splitlines())
            evidence = artifacts / "installed-workflow.jsonl"

            def record(value: dict[str, object]) -> None:
                with evidence.open("a") as output:
                    output.write(json.dumps({"run_id": run_id, **value}) + "\n")

            def cli(*args: str, exit_code: int = 0) -> dict[str, object]:
                command = [values["CLI"], "--config", values["CONFIG"], "--json", *args]
                result = subprocess.run(
                    command, cwd=artifacts, capture_output=True, text=True, timeout=30
                )
                record(
                    {
                        "command": command,
                        "exit_code": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                )
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                return cast(dict[str, object], json.loads(result.stdout))

            remote_executable = str(Path(values["CLI"]).with_name("html-publish-remote"))

            def discovery(
                executable: str, *args: str, exit_code: int = 0
            ) -> subprocess.CompletedProcess[str]:
                command = [executable, *args]
                result = subprocess.run(
                    command, cwd=artifacts, capture_output=True, text=True, timeout=30
                )
                record(
                    {
                        "feature": "#33-help-discovery",
                        "command": command,
                        "exit_code": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                )
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                return result

            for executable in (values["CLI"], remote_executable):
                schema_result = discovery(executable, "schema")
                self.assertEqual(schema_result.stderr, "")
                schema = cast(dict[str, object], json.loads(schema_result.stdout))
                self.assertEqual(
                    json.loads(discovery(executable, "schema", "--json").stdout), schema
                )
                self.assertEqual(schema["schema_version"], 1)
                self.assertEqual(schema["executable"], Path(executable).name)
                commands = cast(list[dict[str, object]], schema["commands"])
                self.assertEqual(
                    [command["name"] for command in commands],
                    ["plan", "publish", "status", "verify", "history", "restore"]
                    + (["artifact"] if executable == values["CLI"] else [])
                    + ["schema"]
                    + (["config", "doctor"] if executable == values["CLI"] else []),
                )
                plan = next(command for command in commands if command["name"] == "plan")
                plan_expected = next(
                    option
                    for option in cast(list[dict[str, object]], plan["options"])
                    if "--expected-revision" in cast(list[str], option["flags"])
                )
                self.assertIn("prediction", str(plan_expected["help"]))
                self.assertNotIn("unguarded", str(plan_expected["help"]))
                history = next(command for command in commands if command["name"] == "history")
                diff_option = next(
                    option
                    for option in cast(list[dict[str, object]], history["options"])
                    if "--diff" in cast(list[str], option["flags"])
                )
                self.assertIn("HEAD", str(diff_option["help"]))
                self.assertIn("64 KiB", str(diff_option["help"]))
                root_help = discovery(executable, "--help")
                self.assertEqual(root_help.stderr, "")
                for option in cast(list[dict[str, object]], schema["global_options"]):
                    for flag in cast(list[str], option["flags"]):
                        self.assertIn(flag, root_help.stdout)
                for command in commands:
                    name = str(command["name"])
                    help_result = discovery(executable, "--json", name, "--help")
                    self.assertEqual(help_result.stderr, "")
                    self.assertIn("Effects:", help_result.stdout)
                    self.assertIn("Examples:", help_result.stdout)
                    for example_text in cast(list[str], command["examples"]):
                        self.assertIn(example_text, help_result.stdout)
                    for option in cast(list[dict[str, object]], command["options"]):
                        for flag in cast(list[str], option["flags"]):
                            self.assertIn(flag, help_result.stdout)
                version = cast(
                    dict[str, object],
                    json.loads(discovery(executable, "--json", "--version").stdout),
                )
                self.assertEqual(version["version"], schema["version"])
                self.assertEqual(
                    json.loads(discovery(executable, "publish", "--version", "--json").stdout),
                    version,
                )
                self.assertEqual(
                    json.loads(
                        discovery(
                            executable,
                            "--json",
                            "--version",
                            "status",
                            "--name",
                            "notes",
                        ).stdout
                    ),
                    version,
                )
                self.assertEqual(
                    json.loads(
                        discovery(
                            executable,
                            "status",
                            "--name",
                            "notes",
                            "--version",
                            "--json",
                        ).stdout
                    ),
                    version,
                )
                self.assertEqual(
                    discovery(executable, "--version").stdout.strip(),
                    f"{Path(executable).name} {schema['version']}",
                )
                self.assertEqual(
                    json.loads(discovery(executable, "--command-seconds", "30", "schema").stdout),
                    schema,
                )
                self.assertEqual(
                    json.loads(discovery(executable, "schema", "--command-seconds", "30").stdout),
                    schema,
                )
                invalid = discovery(executable, "--json", "status", "--nam", "notes", exit_code=2)
                self.assertEqual(invalid.stderr, "")
                self.assertEqual(json.loads(invalid.stdout)["error"]["code"], "invalid_usage")
                bad_budget = discovery(
                    executable, "--json", "status", "--command-seconds", "0", exit_code=2
                )
                self.assertEqual(bad_budget.stderr, "")
                self.assertEqual(json.loads(bad_budget.stdout)["error"]["code"], "invalid_usage")
                plain = discovery(executable, "status", "--nam", "notes", exit_code=2)
                if executable == values["CLI"]:
                    self.assertEqual(plain.stdout, "")
                    self.assertIn("unrecognized arguments", plain.stderr)
                else:
                    self.assertEqual(json.loads(plain.stdout)["error"]["code"], "invalid_usage")

            local_schema = cast(
                dict[str, object], json.loads(discovery(values["CLI"], "schema").stdout)
            )
            local_commands = cast(list[dict[str, object]], local_schema["commands"])

            def run_example(name: str, replacements: dict[str, str]) -> dict[str, object]:
                command = next(item for item in local_commands if item["name"] == name)
                source = cast(list[str], command["examples"])[0]
                arguments = [replacements.get(part, part) for part in shlex.split(source)][1:]
                return cast(
                    dict[str, object], json.loads(discovery(values["CLI"], *arguments).stdout)
                )

            def fetch(path: str) -> tuple[int, bytes]:
                try:
                    with urllib.request.urlopen(values["URL"] + path, timeout=3) as response:
                        status, body = response.status, response.read()
                except urllib.error.HTTPError as error:
                    status, body = error.code, error.read()
                record({"path": path, "status": status, "body": body.decode("utf-8", "replace")})
                return status, body

            target = values["URL"] + "/"
            plan = cli("plan", "--name", "notes", "--source", sources["PAGE_A"], "--target", target)
            self.assertEqual(plan["prediction"], "create")
            self.assertFalse((run / "instance/archive.git").exists())
            self.assertFalse((run / "instance/runtime").exists())
            example_values = {
                "publisher.json": values["CONFIG"],
                "./page.html": sources["PAGE_A"],
                "https://host.example/pages/": target,
            }
            self.assertEqual(run_example("plan", example_values)["prediction"], "create")
            example_published = run_example("publish", example_values)
            self.assertEqual(example_published["outcome"], "published")
            self.assertEqual(run_example("status", example_values)["outcome"], "observed")
            self.assertEqual(run_example("verify", example_values)["outcome"], "verified")
            self.assertEqual(run_example("history", example_values)["outcome"], "observed")
            example_updated = cli(
                "publish",
                "--name",
                "release-notes",
                "--source",
                sources["PAGE_B"],
                "--target",
                target,
                "--expected-revision",
                str(example_published["active_revision"]),
            )
            self.assertEqual(example_updated["outcome"], "published")
            example_values["COMMIT"] = str(example_published["archive_commit"])
            example_values["REVISION"] = str(example_updated["active_revision"])
            self.assertEqual(
                run_example("restore", example_values)["active_revision"],
                example_published["active_revision"],
            )
            self.assertEqual(fetch("/release-notes/"), (200, Path(sources["PAGE_A"]).read_bytes()))
            self.assertEqual(fetch("/release-notes/missing.html")[0], 404)
            redirect_command = [
                "curl",
                "-sS",
                "-o",
                os.devnull,
                "-w",
                "%{http_code}",
                values["URL"] + "/release-notes",
            ]
            redirect = subprocess.run(
                redirect_command, cwd=artifacts, capture_output=True, text=True, timeout=5
            )
            record(
                {
                    "feature": "#33-help-discovery",
                    "command": redirect_command,
                    "exit_code": redirect.returncode,
                    "stdout": redirect.stdout,
                    "stderr": redirect.stderr,
                }
            )
            self.assertEqual(redirect.returncode, 0, redirect.stderr)
            self.assertEqual(redirect.stdout, "301")
            before = cli("status", "--name", "release-notes")
            after = cast(
                dict[str, object],
                json.loads(
                    discovery(
                        values["CLI"],
                        "status",
                        "--name",
                        "release-notes",
                        "--config",
                        values["CONFIG"],
                        "--json",
                        "--command-seconds",
                        "30",
                    ).stdout
                ),
            )
            self.assertEqual(after["active_revision"], before["active_revision"])
            before_placement = cast(
                dict[str, object],
                json.loads(
                    discovery(
                        values["CLI"],
                        "--command-seconds",
                        "30",
                        "--config",
                        values["CONFIG"],
                        "--json",
                        "status",
                        "--name",
                        "release-notes",
                    ).stdout
                ),
            )
            self.assertEqual(before_placement["active_revision"], before["active_revision"])
            a = cli("publish", "--name", "notes", "--source", sources["PAGE_A"], "--target", target)
            self.assertEqual(a["outcome"], "published")
            revision_a = str(a["active_revision"])
            self.assertEqual(fetch("/notes/"), (200, Path(sources["PAGE_A"]).read_bytes()))
            self.assertEqual(cli("status", "--name", "notes")["active_revision"], revision_a)
            b = cli(
                "publish",
                "--name",
                "notes",
                "--source",
                sources["PAGE_B"],
                "--target",
                target,
                "--expected-revision",
                revision_a,
            )
            revision_b = str(b["active_revision"])
            self.assertNotEqual(revision_a, revision_b)
            self.assertEqual(b["url"], a["url"])
            self.assertEqual(fetch("/notes/"), (200, Path(sources["PAGE_B"]).read_bytes()))
            retry = cli(
                "publish",
                "--name",
                "notes",
                "--source",
                sources["PAGE_B"],
                "--target",
                target,
                "--expected-revision",
                revision_a,
            )
            self.assertEqual(retry["outcome"], "unchanged")
            self.assertEqual(retry["archive_commit"], b["archive_commit"])
            self.assertEqual(retry["effects"], {"archive_advanced": False, "activated": False})
            conflict = cli(
                "publish",
                "--name",
                "notes",
                "--source",
                sources["PAGE_A"],
                "--target",
                target,
                "--expected-revision",
                revision_a,
                exit_code=1,
            )
            self.assertEqual(
                cast(dict[str, object], conflict["error"])["code"], "revision_conflict"
            )
            self.assertEqual(cli("status", "--name", "notes")["active_revision"], revision_b)
            self.assertEqual(cli("verify", "--name", "notes")["outcome"], "verified")
            history = cli("history", "--name", "notes")
            self.assertEqual(len(cast(list[object], history["entries"])), 2)
            restored = cli(
                "restore",
                "--name",
                "notes",
                "--archive-commit",
                str(a["archive_commit"]),
                "--target",
                target,
                "--expected-revision",
                revision_b,
            )
            self.assertEqual(restored["active_revision"], revision_a)
            self.assertEqual(fetch("/notes/"), (200, Path(sources["PAGE_A"]).read_bytes()))
            self.assertEqual(
                len(cast(list[object], cli("history", "--name", "notes")["entries"])), 3
            )
            site = run / "instance/site with spaces"
            site.mkdir()
            (site / "index.html").write_bytes(Path(sources["PAGE_A"]).read_bytes())
            asset = site / "café.txt"
            asset.write_bytes(b"asset\n")
            directory = cli("publish", "--name", "site", "--source", str(site), "--target", target)
            self.assertEqual(fetch("/site/caf%C3%A9.txt"), (200, b"asset\n"))
            asset.unlink()
            removed = cli(
                "publish",
                "--name",
                "site",
                "--source",
                str(site),
                "--target",
                target,
                "--expected-revision",
                str(directory["active_revision"]),
            )
            self.assertEqual(removed["outcome"], "published")
            self.assertEqual(fetch("/site/caf%C3%A9.txt")[0], 404)
            self.assertEqual(fetch("/site/missing.txt")[0], 404)
            lifecycle("offline")
            self.assertEqual(cli("status", "--name", "notes")["active_revision"], revision_a)
            failed = cli("verify", "--name", "notes", exit_code=1)
            self.assertEqual(cast(dict[str, object], failed["error"])["code"], "delivery_failure")
        finally:
            lifecycle("stop")
        self.assertTrue(evidence.is_file())
        self.assertTrue((artifacts / "doctor.txt").is_file())
        self.assertTrue((artifacts / "server.log").is_file())
        self.assertFalse((run / "instance").exists())
        lifecycle("stop")
