"""Run the bundled setup and receipt examples against an owned macOS instance."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path


def code_block_after(document: str, marker: str) -> str:
    return document.split(marker, 1)[1].split("```sh\n", 1)[1].split("\n```", 1)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()

    source = args.source.resolve()
    run = args.run.resolve()
    artifacts = run / "artifacts"
    instance = run / "instance"
    cli = instance / "venv/bin/html-publish"
    publisher_config = instance / "publisher.json"
    wheel = artifacts / "wheel/html_publish-0.1.0-py3-none-any.whl"
    url = json.loads(publisher_config.read_text())["base_url"].rstrip("/")
    environment = os.environ.copy()
    environment["PATH"] = str(cli.parent) + os.pathsep + environment["PATH"]
    records: list[dict[str, object]] = []

    def call(command: list[str], cwd: Path) -> str:
        result = subprocess.run(
            command, cwd=cwd, env=environment, text=True, capture_output=True, timeout=60
        )
        records.append(
            {
                "command": command,
                "cwd": str(cwd),
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{command} exited {result.returncode}: {result.stdout} {result.stderr}"
            )
        return result.stdout

    def check_http(expected: bytes) -> None:
        page_url = f"{url}/guide-proof/"
        with urllib.request.urlopen(page_url, timeout=3) as response:
            body = response.read()
            status = response.status
        records.append(
            {
                "http_url": page_url,
                "status": status,
                "body_utf8": body.decode("utf-8"),
                "body_sha256": hashlib.sha256(body).hexdigest(),
            }
        )
        assert status == 200 and body == expected, (status, body, expected)

    def receipt_state(work: Path) -> None:
        receipt = work / "page.html.publish/receipt.json"
        records.append({"receipt_path": str(receipt), "receipt": json.loads(receipt.read_text())})

    inventory = json.loads(call([str(cli), "skills", "list"], artifacts))
    core = call([str(cli), "skills", "get", "core"], artifacts)
    core_bytes = core.encode("utf-8")
    assert len(core_bytes) == inventory["guides"][0]["bytes"]
    assert core_bytes == (source / "html_publish/guides/core.md").read_bytes()
    assert f"html-publish {inventory['version']}" in core

    proof = artifacts / "macos-guide-proof"
    proof.mkdir()
    for label, document, marker in (
        ("readme", (source / "README.md").read_text(), "## Set up a target"),
        ("core", core, "## Configure a target"),
    ):
        work = proof / f"{label}-setup"
        work.mkdir()
        call(["bash", "-eu", "-c", code_block_after(document, marker)], work)
        publisher = json.loads((work / "publisher.json").read_text())
        client = json.loads((work / "client.json").read_text())
        assert publisher["archive"] == str(work / "archive.git")
        assert publisher["runtime"] == str(work / "runtime")
        assert client["execution"]["publisher_config"] == str(work / "publisher.json")

    work = proof / "receipt"
    work.mkdir()
    client_config = work / "client.json"
    call(
        [
            str(cli),
            "config",
            "init",
            "--role",
            "client",
            "--config",
            str(client_config),
            "--base-url",
            f"{url}/",
            "--target-id",
            "guide-proof",
            "--execution",
            "local",
            "--publisher-config",
            str(publisher_config),
        ],
        work,
    )
    page = work / "page.html"
    page_a = b"<html><body>A</body></html>"
    page_b = b"<html><body>B</body></html>"
    page.write_bytes(page_a)
    created = json.loads(
        call(
            [
                str(cli),
                "--config",
                str(client_config),
                "artifact",
                "publish",
                str(page),
                "--new",
                "guide-proof",
            ],
            work,
        )
    )
    assert created["outcome"] == "completed" and created["receipt_persisted"] is True
    check_http(page_a)
    receipt_state(work)
    call(
        [
            str(cli),
            "artifact",
            "status",
            "--receipt",
            str(work / "page.html.publish"),
            "--local-only",
        ],
        work,
    )

    page.write_bytes(page_b)
    updated = json.loads(
        call([str(cli), "--config", str(client_config), "artifact", "publish", str(page)], work)
    )
    assert updated["outcome"] == "completed" and updated["receipt_persisted"] is True
    assert updated["original_expectation"] == created["accepted_revision"]
    check_http(page_b)
    receipt_state(work)
    call(
        [
            str(cli),
            "artifact",
            "status",
            "--receipt",
            str(work / "page.html.publish"),
            "--local-only",
        ],
        work,
    )

    history = json.loads(
        call(
            [
                str(cli),
                "--config",
                str(publisher_config),
                "--json",
                "history",
                "--name",
                "guide-proof",
            ],
            work,
        )
    )
    assert len(history["entries"]) >= 2
    commit = history["entries"][-1]["archive_commit"]
    restore = code_block_after(core, "Start a guarded restore through the receipt:")
    restored = json.loads(call(["bash", "-eu", "-c", restore.replace("COMMIT", commit)], work))
    assert restored["outcome"] == "completed" and restored["receipt_persisted"] is True
    assert restored["original_expectation"] == updated["accepted_revision"]
    check_http(page_a)
    receipt_state(work)
    call(
        [
            str(cli),
            "artifact",
            "status",
            "--receipt",
            str(work / "page.html.publish"),
            "--local-only",
        ],
        work,
    )

    result = {
        "source_sha": args.source_sha,
        "source_archive_sha256": hashlib.sha256(
            (source.parent / f"{source.name}.tar").read_bytes()
        ).hexdigest(),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "installed_core_sha256": hashlib.sha256(core_bytes).hexdigest(),
        "installed_core_bytes": len(core_bytes),
        "macos_version": platform.mac_ver()[0],
        "python_version": sys.version.split()[0],
        "executable": str(cli),
        "url": f"{url}/guide-proof/",
        "records": records,
    }
    output = artifacts / "macos-guide-examples.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "records"}))


if __name__ == "__main__":
    main()
