from __future__ import annotations

import os
import posixpath
import re
import stat
import unicodedata
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from html_publish import _git
from html_publish.model import (
    CapturedSite,
    Deadline,
    FileEntry,
    Limits,
    PublishError,
    RelativePath,
    Revision,
    WarningDetail,
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
            entries = sorted(scan, key=lambda entry: os.fsencode(entry.name))
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


RESOURCE_ATTRIBUTES = {
    "img": ("src",),
    "script": ("src",),
    "iframe": ("src",),
    "audio": ("src",),
    "video": ("src", "poster"),
    "source": ("src",),
    "track": ("src",),
    "embed": ("src",),
    "input": ("src",),
    "link": ("href",),
}
RESOURCE_LINK_RELS = frozenset(
    {"stylesheet", "icon", "apple-touch-icon", "mask-icon", "preload", "modulepreload", "manifest"}
)


class _References(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[tuple[str, bool]] = []
        self.base_href = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "base" and attributes.get("href") is not None:
            self.base_href = True
        resource_attributes = RESOURCE_ATTRIBUTES.get(tag, ())
        if tag == "input" and (attributes.get("type") or "").lower() != "image":
            resource_attributes = ()
        if tag == "link" and not RESOURCE_LINK_RELS.intersection(
            (attributes.get("rel") or "").lower().split()
        ):
            resource_attributes = ()
        for attribute in ("src", "href", "poster"):
            value = attributes.get(attribute)
            if value:
                self.references.append((value, attribute in resource_attributes))


def warnings_for(
    index: Path, entries: tuple[FileEntry, ...] = ()
) -> tuple[tuple[str, ...], tuple[WarningDetail, ...]]:
    with index.open("rb") as source:
        scanned = source.read(2 * 1024 * 1024 + 1)
    truncated = len(scanned) > 2 * 1024 * 1024
    text = scanned[: 2 * 1024 * 1024].decode("utf-8", "replace")
    parser = _References()
    parser.feed(text)
    codes: list[str] = []
    details: list[WarningDetail] = []
    seen_details: set[WarningDetail] = set()
    known_paths = {str(entry.path) for entry in entries}

    def add(code: str, reference: str = "", expected: str | None = None) -> None:
        if code not in codes:
            codes.append(code)
        item = WarningDetail(code, "index.html", reference, expected)
        if item not in seen_details:
            seen_details.add(item)
            details.append(item)

    for reference, is_resource in parser.references:
        try:
            parsed = urlsplit(reference.strip(" \t\n\r\f"))
        except ValueError:
            continue
        if parsed.scheme or parsed.netloc:
            if parsed.scheme in {"http", "https"} or reference.startswith("//"):
                add("external_dependency", reference)
            continue
        if parsed.path.startswith("/"):
            add("root_relative_reference", reference)
            continue
        if not is_resource or not parsed.path or parser.base_href or truncated:
            continue
        path = posixpath.normpath(unquote(parsed.path))
        if path == ".." or path.startswith("../") or path.startswith("/"):
            continue
        index_path = "index.html" if path == "." else f"{path}/index.html"
        if path not in known_paths and index_path not in known_paths:
            add("missing_relative_asset", reference, path)
    if re.search(r"serviceWorker|service-worker", text, re.IGNORECASE):
        add("service_worker")
    if parser.base_href:
        add("base_href_analysis_limited")
    if truncated:
        add("html_scan_truncated")
    return tuple(codes), tuple(details)


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

    staged: list[tuple[PurePosixPath, int]] = []
    total_bytes = 0
    for source_file, relative in _iter_source(source):
        if len(staged) >= limits.max_files:
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
        staged.append((relative, size))

    if not staged:
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
    blobs = _git.hash_files(identity_repo, site_root, [path for path, _ in staged], deadline)
    entries = [
        FileEntry(RelativePath(path.as_posix()), size, blob)
        for (path, size), blob in zip(staged, blobs, strict=True)
    ]
    entries.sort(key=lambda entry: str(entry.path).encode("utf-8"))
    revision = _git.make_site_tree(
        identity_repo,
        [(PurePosixPath(str(entry.path)), entry.blob) for entry in entries],
        deadline,
    )
    warnings, warning_details = warnings_for(site_root / "index.html", tuple(entries))
    return CapturedSite(
        site_root,
        tuple(entries),
        Revision(revision),
        total_bytes,
        warnings,
        warning_details,
    )
