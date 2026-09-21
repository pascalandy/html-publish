from __future__ import annotations

import os
import re
import stat
import unicodedata
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from html_publish import _git
from html_publish.model import (
    CapturedSite,
    Deadline,
    FileEntry,
    Limits,
    PublishError,
    RelativePath,
    Revision,
)


def _valid_component(component: str) -> bool:
    return (
        bool(component)
        and not component.startswith(".")
        and "\\" not in component
        and all(
            ord(character) >= 32
            and ord(character) != 127
            and unicodedata.category(character) != "Cs"
            for character in component
        )
    )


def _iter_source(source: Path) -> Iterator[tuple[Path, PurePosixPath]]:
    source_info = source.lstat()
    if stat.S_ISLNK(source_info.st_mode):
        raise PublishError(
            "unsafe_input",
            "capture",
            "The source is a symbolic link",
            "fix_input",
        )
    if stat.S_ISREG(source_info.st_mode):
        yield source, PurePosixPath("index.html")
        return
    if not stat.S_ISDIR(source_info.st_mode):
        raise PublishError(
            "unsafe_input",
            "capture",
            "The source is not a regular file or directory",
            "fix_input",
        )

    def walk(directory: Path, relative: PurePosixPath) -> Iterator[tuple[Path, PurePosixPath]]:
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda entry: entry.name.encode("utf-8"))
        for entry in entries:
            if not _valid_component(entry.name):
                raise PublishError(
                    "unsafe_input",
                    "capture",
                    f"The artifact contains an unsafe path component: {entry.name!r}",
                    "fix_input",
                )
            child_relative = relative / entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise PublishError(
                    "unsafe_input",
                    "capture",
                    f"The artifact contains a symbolic link: {child_relative}",
                    "fix_input",
                )
            if stat.S_ISDIR(info.st_mode):
                yield from walk(Path(entry.path), child_relative)
            elif stat.S_ISREG(info.st_mode):
                yield Path(entry.path), child_relative
            else:
                raise PublishError(
                    "unsafe_input",
                    "capture",
                    f"The artifact contains a special file: {child_relative}",
                    "fix_input",
                )

    yield from walk(source, PurePosixPath())


def _copy_stable(source: Path, destination: Path, deadline: Deadline, byte_budget: int) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise PublishError(
            "unsafe_input",
            "capture",
            f"The source file could not be opened safely: {source}: {error}",
            "retry",
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PublishError(
                "unsafe_input",
                "capture",
                f"The source changed type while opening: {source}",
                "retry",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        with os.fdopen(os.dup(descriptor), "rb") as input_file, destination.open("xb") as output:
            while chunk := input_file.read(1024 * 1024):
                deadline.remaining()
                copied += len(chunk)
                if copied > byte_budget:
                    raise PublishError(
                        "input_limit",
                        "capture",
                        "The artifact exceeds the configured byte limit",
                        "reduce_input",
                    )
                output.write(chunk)
        after = os.fstat(descriptor)
        current = source.lstat()
        fingerprint_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        fingerprint_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        fingerprint_current = (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        )
        if fingerprint_before != fingerprint_after or fingerprint_before != fingerprint_current:
            raise PublishError(
                "source_changed",
                "capture",
                f"The source changed during capture: {source}",
                "retry",
            )
        os.chmod(destination, 0o644)
        return copied
    finally:
        os.close(descriptor)


def _warnings(index: Path) -> tuple[str, ...]:
    text = index.read_bytes()[: 2 * 1024 * 1024].decode("utf-8", "replace")
    warnings: list[str] = []
    if re.search(r"(?:src|href)\s*=\s*['\"]/(?!/)", text, re.IGNORECASE):
        warnings.append("root_relative_reference")
    if re.search(r"(?:src|href)\s*=\s*['\"](?:https?:)?//", text, re.IGNORECASE):
        warnings.append("external_dependency")
    if re.search(r"serviceWorker|service-worker", text, re.IGNORECASE):
        warnings.append("service_worker")
    return tuple(warnings)


def capture(
    source: Path,
    workspace: Path,
    object_format: str,
    limits: Limits,
    deadline: Deadline,
) -> CapturedSite:
    site_root = workspace / "site"
    identity_repo = workspace / "identity.git"
    site_root.mkdir(parents=True)
    _git.init_bare(identity_repo, object_format, deadline)

    entries: list[FileEntry] = []
    total_bytes = 0
    for source_file, relative in _iter_source(source):
        if len(entries) >= limits.max_files:
            raise PublishError(
                "input_limit",
                "capture",
                "The artifact exceeds the configured file limit",
                "reduce_input",
            )
        destination = site_root.joinpath(*relative.parts)
        remaining_bytes = limits.max_bytes - total_bytes
        size = _copy_stable(source_file, destination, deadline, remaining_bytes)
        total_bytes += size
        blob = _git.hash_file(identity_repo, destination, deadline, write=True)
        entries.append(FileEntry(RelativePath(relative.as_posix()), size, blob))

    if not entries:
        raise PublishError(
            "invalid_input",
            "capture",
            "The artifact contains no files",
            "fix_input",
        )
    if not (site_root / "index.html").is_file():
        raise PublishError(
            "missing_index",
            "capture",
            "A directory artifact must contain index.html",
            "fix_input",
        )
    entries.sort(key=lambda entry: str(entry.path).encode("utf-8"))
    revision = _git.make_site_tree(
        identity_repo,
        [(PurePosixPath(str(entry.path)), entry.blob) for entry in entries],
        deadline,
    )
    return CapturedSite(
        site_root,
        tuple(entries),
        Revision(revision),
        total_bytes,
        _warnings(site_root / "index.html"),
    )
