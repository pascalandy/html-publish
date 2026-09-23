from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT / ".agents/skills/verify-html-publish/scripts/instance.sh"


class InstalledReportsTest(unittest.TestCase):
    def test_warning_detail_summary_and_receipt_cap_through_wheel(self) -> None:
        run_id = f"reports-{os.getpid()}-{time.time_ns()}"
        run = Path("/tmp/html-publish-verify") / run_id
        artifacts = run / "artifacts"

        def lifecycle(action: str) -> str:
            result = subprocess.run(
                ["bash", str(INSTANCE), action, run_id],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

        try:
            values = dict(line.split("=", 1) for line in lifecycle("start").splitlines())
            lifecycle("doctor")
            cli = values["CLI"]
            target = values["URL"] + "/"
            publisher_config = values["CONFIG"]
            source = run / "instance/report-source"
            source.mkdir()
            (source / "present.css").write_bytes(b"body{color:white}\n")
            (source / "present.png").write_bytes(b"PNG")
            child = source / "child"
            child.mkdir()
            (child / "index.html").write_bytes(b"child page\n")
            first_html = (
                b'<!doctype html><link href="present.css?v=1#top">'
                b'<iframe src="child"></iframe><img src="present.png ">'
                b'<link rel="next" href="page2.html">'
                b'<img src="missing.png?size=1#view">'
                b'<link href="/global.css"><script src="https://cdn.example/a.js"></script>'
                b"<script>navigator.serviceWorker.register('/sw.js')</script>"
            )
            (source / "index.html").write_bytes(first_html)
            client = run / "instance/client.json"
            receipt = run / "report.publish"
            archive = json.loads(Path(publisher_config).read_text())["archive"]
            client_data = {
                "schema_version": 1,
                "target": {"id": "loopback", "base_url": target},
                "execution": {
                    "kind": "local",
                    "command": [cli],
                    "publisher_config": publisher_config,
                },
            }
            client.write_text(json.dumps(client_data))
            small_client = run / "instance/small-client.json"
            small_client.write_text(json.dumps(client_data | {"limits": {"output_bytes": 1024}}))
            evidence = artifacts / "reports-installed.jsonl"

            def call(config: Path | str, *args: str, code: int = 0) -> dict[str, Any]:
                command = [cli, "--config", str(config), "--json", *args]
                result = subprocess.run(
                    command, cwd=artifacts, capture_output=True, text=True, timeout=45
                )
                with evidence.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "command": command,
                                "exit_code": result.returncode,
                                "stdout_bytes": len(result.stdout.encode()),
                                "stdout": result.stdout,
                                "stderr": result.stderr,
                            }
                        )
                        + "\n"
                    )
                self.assertEqual(result.returncode, code, result.stdout[:1000] + result.stderr)
                return cast(dict[str, Any], json.loads(result.stdout))

            def root(*args: str, code: int = 0) -> dict[str, Any]:
                return call(publisher_config, *args, code=code)

            def artifact(config: Path, *args: str, code: int = 0) -> dict[str, Any]:
                return call(config, "artifact", *args, code=code)

            def head() -> str:
                return subprocess.run(
                    ["git", "--git-dir", archive, "rev-parse", "refs/heads/published"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            for executable in (cli, str(Path(cli).with_name("html-publish-remote"))):
                discovered = subprocess.run(
                    [executable, "schema"], check=True, capture_output=True, text=True
                )
                commands = json.loads(discovered.stdout)["commands"]
                status = next(item for item in commands if item["name"] == "status")
                option = next(item for item in status["options"] if "--report" in item["flags"])
                self.assertEqual(option["choices"], ["detail", "summary"])
                self.assertIn("summary", option["help"])

            plan_args = ("plan", "--name", "reports", "--source", str(source), "--target", target)
            detailed = root(*plan_args)
            summary = root(*plan_args, "--report", "summary")
            self.assertEqual(detailed["report"]["mode"], "detail")
            self.assertEqual(summary["report"]["mode"], "summary")
            self.assertEqual(
                detailed["warnings"],
                [
                    "missing_relative_asset",
                    "root_relative_reference",
                    "external_dependency",
                    "service_worker",
                ],
            )
            self.assertIn(
                {
                    "code": "missing_relative_asset",
                    "source_path": "index.html",
                    "reference": "missing.png?size=1#view",
                    "expected_path": "missing.png",
                },
                detailed["warning_details"],
            )
            self.assertEqual(len(detailed["warning_details"]), 4)
            self.assertEqual(summary["warning_details"], [])
            self.assertEqual(
                summary["report"]["collections"]["/warning_details"]["omitted"],
                len(detailed["warning_details"]),
            )
            self.assertEqual(
                summary["report"]["collections"]["/differences/added"]["omitted"],
                len(detailed["differences"]["added"]),
            )
            for field in (
                "target",
                "name",
                "url",
                "expected_revision",
                "requested_revision",
                "effects",
                "verification",
                "error",
                "outcome",
            ):
                self.assertEqual(summary[field], detailed[field])

            rejected_new = artifact(
                small_client,
                "publish",
                str(source),
                "--new",
                "too-small",
                "--receipt",
                str(run / "small.publish"),
                code=1,
            )
            self.assertEqual(rejected_new["error"]["code"], "output_limit_too_small")
            self.assertEqual(rejected_new["publisher_calls"], 0)
            self.assertFalse((run / "small.publish" / "receipt.json").exists())

            first = artifact(
                client, "publish", str(source), "--new", "reports", "--receipt", str(receipt)
            )
            self.assertEqual(first["outcome"], "completed")
            with urllib.request.urlopen(values["URL"] + "/reports/", timeout=3) as response:
                self.assertEqual(response.read(), first_html)
            with urllib.request.urlopen(
                values["URL"] + "/reports/present.css", timeout=3
            ) as response:
                self.assertEqual(response.read(), b"body{color:white}\n")
            with urllib.request.urlopen(values["URL"] + "/reports/child", timeout=3) as response:
                self.assertEqual(response.url, values["URL"] + "/reports/child/")
                self.assertEqual(response.read(), b"child page\n")
            with urllib.request.urlopen(
                values["URL"] + "/reports/present.png", timeout=3
            ) as response:
                self.assertEqual(response.read(), b"PNG")
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(values["URL"] + "/reports/missing.png", timeout=3)
            self.assertEqual(missing.exception.code, 404)
            history = root("history", "--name", "reports")
            first_commit = history["entries"][0]["archive_commit"]

            large_html = b"<!doctype html>" + b"".join(
                f'<img src="missing-{index:05d}.png?view={index}">'.encode()
                for index in range(18_000)
            )
            (source / "present.css").unlink()
            (source / "new.txt").write_bytes(b"new asset\n")
            (source / "index.html").write_bytes(large_html)
            full_large = root(*plan_args)
            self.assertGreater(len(json.dumps(full_large).encode()), 1024 * 1024)
            self.assertEqual(len(full_large["warning_details"]), 18_000)
            short_large = root(*plan_args, "--report", "summary")
            self.assertLess(len(json.dumps(short_large).encode()), 1024 * 1024)
            self.assertEqual(
                short_large["report"]["collections"]["/warning_details"],
                {"total": 18_000, "included": 0, "omitted": 18_000},
            )
            for key in ("added", "changed", "deleted"):
                self.assertEqual(
                    short_large["report"]["collections"][f"/differences/{key}"],
                    {"total": 1, "included": 0, "omitted": 1},
                )

            before_receipt = (receipt / "receipt.json").read_bytes()
            before_head = head()
            rejected = artifact(
                small_client, "publish", str(source), "--receipt", str(receipt), code=1
            )
            self.assertEqual(rejected["error"]["code"], "output_limit_too_small")
            self.assertEqual(rejected["publisher_calls"], 0)
            self.assertEqual((receipt / "receipt.json").read_bytes(), before_receipt)
            self.assertEqual(head(), before_head)
            with urllib.request.urlopen(values["URL"] + "/reports/", timeout=3) as response:
                self.assertEqual(response.read(), first_html)
            accepted = artifact(client, "publish", str(source), "--receipt", str(receipt))
            self.assertEqual(accepted["outcome"], "completed")
            self.assertEqual(accepted["publisher_calls"], 1)
            with urllib.request.urlopen(values["URL"] + "/reports/", timeout=3) as response:
                self.assertEqual(response.read(), large_html)
            self.assertEqual(
                artifact(client, "status", "--receipt", str(receipt), "--local-only")[
                    "accepted_revision"
                ],
                accepted["accepted_revision"],
            )
            page = root("history", "--name", "reports", "--report", "summary")
            self.assertEqual(page["entries"][0]["changes"]["added"], [])
            self.assertEqual(
                page["report"]["collections"]["/entries/0/changes/changed"]["omitted"],
                1,
            )
            before_restore = (receipt / "receipt.json").read_bytes()
            before_head = head()
            rejected_restore = artifact(
                small_client,
                "restore",
                "--receipt",
                str(receipt),
                "--archive-commit",
                first_commit,
                code=1,
            )
            self.assertEqual(rejected_restore["error"]["code"], "output_limit_too_small")
            self.assertEqual(rejected_restore["publisher_calls"], 0)
            self.assertEqual((receipt / "receipt.json").read_bytes(), before_restore)
            self.assertEqual(head(), before_head)
            with urllib.request.urlopen(values["URL"] + "/reports/", timeout=3) as response:
                self.assertEqual(response.read(), large_html)

            (source / "index.html").write_bytes(b"conflicting content")
            conflict = root(
                "publish",
                "--name",
                "reports",
                "--source",
                str(source),
                "--target",
                target,
                "--expected-revision",
                "0" * 40,
                "--report",
                "summary",
                code=1,
            )
            self.assertEqual(conflict["error"]["code"], "revision_conflict")
            self.assertEqual(conflict["error"]["next_action"]["kind"], "review_conflict")
            self.assertEqual(conflict["expected_revision"], "0" * 40)
            self.assertEqual(conflict["effects"], {"archive_advanced": False, "activated": False})

            restored = root(
                "restore",
                "--name",
                "reports",
                "--archive-commit",
                first_commit,
                "--target",
                target,
                "--expected-revision",
                accepted["accepted_revision"],
            )
            self.assertEqual(restored["outcome"], "published")
            self.assertIn(
                {
                    "code": "missing_relative_asset",
                    "source_path": "index.html",
                    "reference": "missing.png?size=1#view",
                    "expected_path": "missing.png",
                },
                restored["warning_details"],
            )
            with urllib.request.urlopen(values["URL"] + "/reports/", timeout=3) as response:
                self.assertEqual(response.read(), first_html)

            (source / "index.html").write_bytes(b"offline publication")
            lifecycle("offline")
            failed = root(
                "publish",
                "--name",
                "reports",
                "--source",
                str(source),
                "--target",
                target,
                "--expected-revision",
                restored["active_revision"],
                "--report",
                "summary",
                code=1,
            )
            self.assertEqual(failed["error"]["code"], "delivery_failure")
            self.assertEqual(failed["verification"]["result"], "failed")
            self.assertEqual(failed["effects"], {"archive_advanced": True, "activated": True})
            self.assertEqual(failed["active_revision"], failed["requested_revision"])
        finally:
            if run.exists():
                lifecycle("stop")
