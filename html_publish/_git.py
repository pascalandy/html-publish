from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, cast

from html_publish.model import Deadline, PublishError

MINIMUM_GIT_VERSION = (2, 36)


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    kind: str
    object_id: str
    name: str


def check_supported_version(deadline: Deadline) -> None:
    output = command(None, ["--version"], deadline, phase="version").decode("ascii", "replace")
    match = re.match(r"git version (\d+)\.(\d+)", output.strip())
    if match is None:
        raise PublishError(
            "git_unavailable",
            "version",
            f"Git reported an unrecognized version: {output.strip()}",
            "fix_host",
        )
    version = (int(match.group(1)), int(match.group(2)))
    if version < MINIMUM_GIT_VERSION:
        minimum = f"{MINIMUM_GIT_VERSION[0]}.{MINIMUM_GIT_VERSION[1]}"
        raise PublishError(
            "git_unsupported",
            "version",
            f"Git {version[0]}.{version[1]} is older than the supported minimum {minimum}",
            "fix_host",
        )


def _environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_AUTHOR_NAME": "html-publish",
        "GIT_AUTHOR_EMAIL": "html-publish@localhost",
        "GIT_COMMITTER_NAME": "html-publish",
        "GIT_COMMITTER_EMAIL": "html-publish@localhost",
    }
    if extra:
        env.update(extra)
    return env


def command(
    git_dir: Path | None,
    args: Sequence[str],
    deadline: Deadline,
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | int = subprocess.PIPE,
    extra_env: Mapping[str, str] | None = None,
    phase: str = "archive",
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> bytes:
    invocation = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgSign=false",
        "-c",
        "tag.gpgSign=false",
        "-c",
        "maintenance.auto=false",
        "-c",
        "gc.auto=0",
        "-c",
        "core.fsync=all",
        "-c",
        "core.fsyncMethod=fsync",
    ]
    if git_dir is not None:
        invocation.append(f"--git-dir={git_dir}")
    invocation.extend(args)
    try:
        completed = subprocess.run(
            invocation,
            input=input_bytes,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=_environment(extra_env),
            timeout=deadline.remaining(),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise PublishError(
            "git_timeout",
            phase,
            f"Git exceeded the command time budget while running {args[0]}",
            "inspect",
        ) from error
    except OSError as error:
        raise PublishError(
            "git_unavailable",
            phase,
            f"Git could not run: {error}",
            "fix_host",
        ) from error
    if completed.returncode not in allowed_returncodes:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise PublishError(
            "archive_failure",
            phase,
            f"Git {args[0]} failed: {detail}",
            "inspect",
        )
    return completed.stdout


def init_bare(path: Path, object_format: str, deadline: Deadline) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    command(
        None,
        ["init", "--bare", f"--object-format={object_format}", str(path)],
        deadline,
    )


def object_format(git_dir: Path, deadline: Deadline) -> str:
    return command(git_dir, ["rev-parse", "--show-object-format"], deadline).decode().strip()


def hash_file(git_dir: Path, path: Path, deadline: Deadline, *, write: bool) -> str:
    args = ["hash-object"]
    if write:
        args.append("-w")
    args.append("--stdin")
    with path.open("rb") as source:
        return command(git_dir, args, deadline, stdin=source).decode().strip()


def hash_files(
    git_dir: Path,
    root: Path,
    paths: Sequence[PurePosixPath],
    deadline: Deadline,
) -> tuple[str, ...]:
    if not paths:
        return ()
    if any(
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or "\n" in str(path)
        or "\r" in str(path)
        for path in paths
    ):
        raise PublishError(
            "unsafe_input",
            "capture",
            "A staged path cannot be sent to Git safely",
            "fix_input",
        )
    payload = b"\n".join(os.fsencode(path) for path in paths) + b"\n"
    output = command(
        git_dir,
        ["hash-object", "-w", "--stdin-paths", "--no-filters"],
        deadline,
        cwd=root,
        input_bytes=payload,
    )
    object_ids = output.splitlines()
    if len(object_ids) != len(paths) or any(
        re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", object_id) is None for object_id in object_ids
    ):
        raise PublishError(
            "archive_failure",
            "archive",
            "Git returned an invalid blob identity batch",
            "inspect",
        )
    return tuple(object_id.decode("ascii") for object_id in object_ids)


def make_tree(git_dir: Path, entries: Sequence[TreeEntry], deadline: Deadline) -> str:
    payload = bytearray()
    for entry in sorted(entries, key=lambda item: item.name.encode("utf-8")):
        payload.extend(f"{entry.mode} {entry.kind} {entry.object_id}\t{entry.name}".encode())
        payload.append(0)
    return command(git_dir, ["mktree", "-z"], deadline, input_bytes=bytes(payload)).decode().strip()


def make_site_tree(
    git_dir: Path,
    files: Sequence[tuple[PurePosixPath, str]],
    deadline: Deadline,
) -> str:
    root: dict[str, object] = {}
    for path, blob in files:
        node = root
        for part in path.parts[:-1]:
            existing = node.get(part)
            if existing is None:
                child: dict[str, object] = {}
                node[part] = child
            elif isinstance(existing, dict):
                child = cast(dict[str, object], existing)
            else:
                raise PublishError(
                    "invalid_path_set",
                    "capture",
                    f"A file and directory collide at {part}",
                    "fix_input",
                )
            node = child
        leaf = path.name
        if leaf in node:
            raise PublishError(
                "invalid_path_set",
                "capture",
                f"The artifact contains a duplicate path: {path}",
                "fix_input",
            )
        node[leaf] = blob

    def write_node(node: dict[str, object]) -> str:
        entries: list[TreeEntry] = []
        for name, value in node.items():
            if isinstance(value, dict):
                entries.append(
                    TreeEntry(
                        "040000",
                        "tree",
                        write_node(cast(dict[str, object], value)),
                        name,
                    )
                )
            else:
                entries.append(TreeEntry("100644", "blob", str(value), name))
        return make_tree(git_dir, entries, deadline)

    return write_node(root)


def read_tree(git_dir: Path, treeish: str, deadline: Deadline) -> tuple[TreeEntry, ...]:
    output = command(git_dir, ["ls-tree", "-z", treeish], deadline)
    entries: list[TreeEntry] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split(" ")
        entries.append(TreeEntry(mode, kind, object_id, raw_name.decode("utf-8", "strict")))
    return tuple(entries)


def export_blob(git_dir: Path, object_id: str, destination: Path, deadline: Deadline) -> None:
    with destination.open("xb") as output:
        command(git_dir, ["cat-file", "blob", object_id], deadline, stdout=output)
        output.flush()
        os.fsync(output.fileno())
