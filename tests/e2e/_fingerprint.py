"""Identify the source files an E2E run tested.

The recorder in tests/e2e/__main__.py stores this fingerprint before and after the suite, and
scripts/check_e2e_artifacts.py compares it with the checkout it audits. It covers the working
tree, not HEAD: every tracked file and every untracked file Git does not ignore, by
repository-relative path, file type, executable bit, and content. A symlink contributes its
target without being followed. Git internals and ignored output such as __pycache__/ stay out,
while a tracked file stays in even when an ignore pattern matches it. Timestamps never count.

This is source identity, not a hermetic record of the environment the suite ran in.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path


def source_fingerprint(root: Path) -> str:
    """sha256 over the sorted inventory of source files in the working tree at `root`.

    Raises OSError or subprocess.CalledProcessError when Git cannot list the files.
    """
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
    ).stdout
    inventory = hashlib.sha256()
    # An unmerged path is listed once per stage
    for name in sorted(set(listed.split(b"\0")) - {b""}):
        inventory.update(name + b"\0" + describe(root / os.fsdecode(name)) + b"\0")
    return inventory.hexdigest()


def describe(path: Path) -> bytes:
    """The type, executable bit, and content digest of one listed path."""
    try:
        status = path.lstat()
    except FileNotFoundError:
        return b"missing"
    if stat.S_ISLNK(status.st_mode):
        target = os.fsencode(os.readlink(path))
        return b"symlink " + hashlib.sha256(target).hexdigest().encode()
    if stat.S_ISREG(status.st_mode):
        kind = "executable" if status.st_mode & stat.S_IXUSR else "file"
        with path.open("rb") as handle:
            content = hashlib.file_digest(handle, "sha256").hexdigest()
        return f"{kind} {content}".encode()
    return b"directory" if stat.S_ISDIR(status.st_mode) else b"other"
