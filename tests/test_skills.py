from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib import resources
from pathlib import Path
from typing import Any, cast

from html_publish import __version__, guides

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "html_publish", *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class SkillsCliTest(unittest.TestCase):
    def test_registered_guides_exist_and_match_the_executable_version(self) -> None:
        guide_root = resources.files("html_publish").joinpath("guides")
        for guide in guides.GUIDES:
            with self.subTest(guide=guide.name):
                resource = guide_root.joinpath(guide.filename)
                self.assertTrue(resource.is_file())
                self.assertIn(f"html-publish {__version__}", resource.read_text(encoding="utf-8"))

    def test_list_reports_ordered_guide_metadata_and_exact_byte_lengths(self) -> None:
        result = run_cli("skills", "list")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        payload = cast(dict[str, Any], json.loads(result.stdout))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["executable"], "html-publish")
        self.assertEqual(payload["version"], __version__)
        inventory = cast(list[dict[str, Any]], payload["guides"])
        self.assertEqual([item["name"] for item in inventory], ["core", "recovery"])
        expected_bytes = {
            guide.name: len(guides.read(guide.name).encode("utf-8")) for guide in guides.GUIDES
        }
        self.assertEqual({str(item["name"]): item["bytes"] for item in inventory}, expected_bytes)

    def test_get_core_prints_plain_guide_text(self) -> None:
        result = run_cli("skills", "get", "core")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("# Core guide", result.stdout)
        self.assertIn(f"html-publish {__version__}", result.stdout)

    def test_get_recovery_can_emit_json(self) -> None:
        result = run_cli("skills", "get", "recovery", "--json")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        payload = cast(dict[str, Any], json.loads(result.stdout))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["version"], __version__)
        self.assertEqual(payload["name"], "recovery")
        self.assertIn(f"html-publish {__version__}", str(payload["content"]))

    def test_unknown_guide_is_a_usage_error(self) -> None:
        plain = run_cli("skills", "get", "bogus")

        self.assertEqual(plain.returncode, 2)
        self.assertEqual(plain.stdout, "")
        self.assertTrue(plain.stderr.strip())

        structured = run_cli("skills", "get", "bogus", "--json")
        self.assertEqual(structured.returncode, 2)
        self.assertEqual(structured.stderr, "")
        payload = cast(dict[str, Any], json.loads(structured.stdout))
        self.assertEqual(payload["outcome"], "error")
        self.assertEqual(payload["operation"], "usage")
        self.assertEqual(cast(dict[str, Any], payload["error"])["code"], "invalid_usage")

    def test_list_does_not_read_configuration(self) -> None:
        result = run_cli("skills", "list", "--config", "/nonexistent/x.json")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["schema_version"], 1)

    def test_remote_executable_does_not_expose_skills(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "html_publish.remote", "skills", "get", "core"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        payload = cast(dict[str, Any], json.loads(result.stdout))
        self.assertEqual(payload["outcome"], "error")
        self.assertEqual(payload["operation"], "usage")
        self.assertEqual(cast(dict[str, Any], payload["error"])["code"], "invalid_usage")

    def test_documented_setup_and_artifact_restore_commands(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        setup = readme.split("## Set up a target", 1)[1].split("```sh\n", 1)[1].split("\n```", 1)[0]
        core = run_cli("skills", "get", "core")
        self.assertEqual(core.returncode, 0, core.stderr)
        restore = (
            core.stdout.split("Start a guarded restore through the receipt:", 1)[1]
            .split("```sh\n", 1)[1]
            .split("\n```", 1)[0]
            .replace("COMMIT", "0" * 40)
        )
        environment = os.environ.copy()
        environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment["PATH"]

        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            configured = subprocess.run(
                ["bash", "-eu", "-c", setup],
                cwd=workdir,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
            publisher = json.loads((workdir / "publisher.json").read_text(encoding="utf-8"))
            client = json.loads((workdir / "client.json").read_text(encoding="utf-8"))
            self.assertEqual(publisher["archive"], str(workdir / "archive.git"))
            self.assertEqual(publisher["runtime"], str(workdir / "runtime"))
            self.assertEqual(
                client["execution"]["publisher_config"], str(workdir / "publisher.json")
            )

            restored = subprocess.run(
                ["bash", "-eu", "-c", restore],
                cwd=workdir,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(restored.returncode, 1, restored.stdout + restored.stderr)
            self.assertEqual(json.loads(restored.stdout)["error"]["code"], "receipt_missing")


if __name__ == "__main__":
    unittest.main()
