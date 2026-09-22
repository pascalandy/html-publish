from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import tempfile
import time
import uuid
from collections.abc import Generator
from pathlib import Path, PurePosixPath
from typing import Literal

from html_publish import _git
from html_publish.artifact import capture, warnings_for
from html_publish.delivery import (
    publication_url,
    reachability_probe,
    resolve_host,
    verify,
)
from html_publish.model import (
    CapturedSite,
    Commit,
    Config,
    Deadline,
    Effects,
    Failure,
    FileEntry,
    HistoryEntry,
    LocalState,
    Name,
    PublishError,
    RelativePath,
    Report,
    Revision,
    SavedPage,
    Selection,
    StatusEntry,
    StoredSite,
    Verification,
)

ARCHIVE_REF = "refs/heads/published"
_PublishingOperation = Literal["publish", "restore"]
_DIFF_CAP_BYTES = 64 * 1024
_PublicationDecision = Literal["create", "update", "unchanged"]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _contains(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right)


def _decide_publication(
    state: LocalState,
    requested_revision: Revision,
    expected_revision: Revision | None,
) -> _PublicationDecision | Failure:
    selection = state.selection
    if selection.kind == "degraded":
        return Failure(
            "state_degraded",
            "guard",
            selection.detail or "The selected export is degraded",
            "inspect",
        )
    if selection.kind == "unobserved":
        return Failure(
            "state_unobserved",
            "guard",
            selection.detail or "Selected state could not be observed",
            "inspect",
        )
    if selection.kind == "selected":
        active_revision = selection.revision
        assert active_revision is not None
        if active_revision == requested_revision:
            return "unchanged"
        if expected_revision == active_revision:
            return "update"
        return Failure(
            "revision_conflict",
            "guard",
            "The active publication does not match the expected revision",
            "review_conflict",
            ("expected_revision", "active_revision", "requested_revision"),
        )
    if expected_revision is not None:
        return Failure(
            "revision_conflict",
            "guard",
            "An expectation cannot create an absent active publication",
            "inspect",
            ("active_revision",),
        )
    if state.saved is None or state.saved.site.revision == requested_revision:
        return "create"
    return Failure(
        "revision_conflict",
        "guard",
        "Different content is saved while the active publication is absent",
        "review_conflict",
        ("archived_revision", "requested_revision"),
    )


class PublicationStore:
    def __init__(self, config: Config, deadline: Deadline) -> None:
        self.config = config
        self.deadline = deadline

    def _check_target(self, target: str) -> None:
        if target != self.config.base_url:
            raise PublishError(
                "target_mismatch",
                "validate",
                f"The requested target does not match configuration: {target}",
                "rebind",
                ("target",),
            )

    def _check_source_separation(self, source: Path) -> None:
        resolved = source.resolve(strict=True)
        for private_path in (self.config.archive, self.config.runtime):
            if _contains(resolved, private_path) or _contains(private_path, resolved):
                raise PublishError(
                    "path_overlap",
                    "validate",
                    f"The source overlaps publisher storage: {source}",
                    "move_input",
                )

    def _archive_format(self) -> str:
        if not self.config.archive.exists():
            return self.config.object_format
        if not self.config.archive.is_dir():
            raise PublishError(
                "archive_failure",
                "archive",
                "The configured archive path is not a directory",
                "fix_storage",
            )
        current = _git.object_format(self.config.archive, self.deadline)
        if current != self.config.object_format:
            raise PublishError(
                "archive_failure",
                "archive",
                f"The archive uses {current}, configuration declares {self.config.object_format}",
                "fix_config",
            )
        return current

    def _ensure_runtime(self) -> None:
        for path in (
            self.config.runtime,
            self.config.runtime / "public",
            self.config.runtime / "releases",
            self.config.runtime / "staging",
        ):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise PublishError(
                    "runtime_failure",
                    "initialize",
                    f"A runtime directory could not be created: {path}: {error}",
                    "fix_storage",
                ) from error
            if path.is_symlink() or not path.is_dir():
                raise PublishError(
                    "runtime_failure",
                    "initialize",
                    f"A runtime path is not a real directory: {path}",
                    "fix_storage",
                )
            os.chmod(path, 0o700 if path.name == "staging" else 0o755)
        devices = {
            (self.config.runtime / child).stat().st_dev
            for child in ("public", "releases", "staging")
        }
        if len(devices) != 1:
            raise PublishError(
                "runtime_failure",
                "initialize",
                "Runtime public, releases, and staging paths must share one filesystem",
                "fix_storage",
            )
        lock_path = self.config.runtime / ".publish.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        _fsync_directory(self.config.runtime)

    @contextlib.contextmanager
    def _lock(self, *, create: bool) -> Generator[None, None, None]:
        lock_path = self.config.runtime / ".publish.lock"
        if create:
            self._ensure_runtime()
        elif not lock_path.exists():
            if not self.config.runtime.exists() and self._head() is None:
                yield
                return
            raise PublishError(
                "runtime_failure",
                "lock",
                "Publisher state exists without its lock file",
                "inspect",
            )
        descriptor = os.open(lock_path, os.O_RDWR)
        lock_deadline = time.monotonic() + min(
            self.config.limits.lock_seconds,
            self.deadline.remaining(),
        )
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= lock_deadline:
                        raise PublishError(
                            "lock_timeout",
                            "lock",
                            "The publication lock did not become available in time",
                            "retry",
                        ) from None
                    time.sleep(min(0.05, self.deadline.remaining()))
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _ensure_archive(self) -> None:
        if self.config.archive.exists():
            self._archive_format()
            return
        _git.init_bare(self.config.archive, self.config.object_format, self.deadline)
        _fsync_directory(self.config.archive.parent)

    def _head(self) -> str | None:
        if not self.config.archive.exists():
            return None
        output = (
            _git.command(
                self.config.archive,
                ["rev-parse", "--verify", "--quiet", ARCHIVE_REF],
                self.deadline,
                allowed_returncodes=frozenset({0, 1}),
            )
            .decode()
            .strip()
        )
        return output or None

    def _page_tree_at(self, commit: str, name: Name) -> str | None:
        root_entries = _git.read_tree(self.config.archive, f"{commit}^{{tree}}", self.deadline)
        name_entry = next((entry for entry in root_entries if entry.name == str(name)), None)
        if name_entry is None or name_entry.kind != "tree":
            return None
        name_entries = _git.read_tree(self.config.archive, name_entry.object_id, self.deadline)
        site_entry = next((entry for entry in name_entries if entry.name == "site"), None)
        if site_entry is None or site_entry.kind != "tree":
            return None
        return site_entry.object_id

    def _site(self, revision: str) -> StoredSite:
        output = _git.command(
            self.config.archive,
            ["ls-tree", "-r", "-l", "-z", revision],
            self.deadline,
        )
        entries: list[FileEntry] = []
        for record in output.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, object_id, raw_size = metadata.split(b" ", 3)
            if mode != b"100644" or kind != b"blob":
                raise PublishError(
                    "archive_corrupt",
                    "archive",
                    f"The saved site contains an unsupported Git entry: {raw_path!r}",
                    "inspect",
                )
            try:
                path = raw_path.decode("utf-8", "strict")
                size = int(raw_size)
            except (UnicodeDecodeError, ValueError) as error:
                raise PublishError(
                    "archive_corrupt",
                    "archive",
                    "The saved site contains invalid path or size data",
                    "inspect",
                ) from error
            entries.append(FileEntry(RelativePath(path), size, object_id.decode("ascii")))
        return StoredSite(Revision(revision), tuple(entries))

    def _saved(self, name: Name) -> SavedPage | None:
        head = self._head()
        if head is None:
            return None
        revision = self._page_tree_at(head, name)
        if revision is None:
            return None
        commit_output = (
            _git.command(
                self.config.archive,
                ["log", "-1", "--format=%H", head, "--", f"{name}/site"],
                self.deadline,
            )
            .decode()
            .strip()
        )
        if not commit_output:
            raise PublishError(
                "archive_corrupt",
                "archive",
                f"The saved page has no reachable history entry: {name}",
                "inspect",
            )
        return SavedPage(self._site(revision), Commit(commit_output))

    def _reachable(self, name: Name, revision: Revision) -> bool:
        head = self._head()
        if head is None:
            return False
        commits = (
            _git.command(
                self.config.archive,
                ["rev-list", head, "--", f"{name}/site"],
                self.deadline,
            )
            .decode()
            .splitlines()
        )
        return any(self._page_tree_at(commit, name) == str(revision) for commit in commits)

    def _page_commits(self, name: Name) -> tuple[str, ...]:
        head = self._head()
        if head is None:
            return ()
        output = (
            _git.command(
                self.config.archive,
                ["rev-list", head, "--", f"{name}/site"],
                self.deadline,
            )
            .decode()
            .splitlines()
        )
        return tuple(output)

    def _tree_paths(self, revision: str) -> dict[str, str]:
        return {str(entry.path): entry.blob for entry in self._site(revision).entries}

    def _release_paths(self, root: Path) -> tuple[str, ...]:
        paths: list[str] = []

        def walk(directory: Path, relative: PurePosixPath) -> None:
            with os.scandir(directory) as scan:
                entries = list(scan)
            for entry in entries:
                child = relative / entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise PublishError(
                        "export_corruption",
                        "export",
                        f"The release contains a symbolic link: {child}",
                        "inspect",
                    )
                if stat.S_ISDIR(info.st_mode):
                    walk(Path(entry.path), child)
                elif stat.S_ISREG(info.st_mode):
                    paths.append(child.as_posix())
                else:
                    raise PublishError(
                        "export_corruption",
                        "export",
                        f"The release contains a special file: {child}",
                        "inspect",
                    )

        walk(root, PurePosixPath())
        return tuple(sorted(paths, key=lambda path: path.encode("utf-8")))

    def _validate_release(self, release: Path, site: StoredSite) -> None:
        if release.is_symlink() or not release.is_dir():
            raise PublishError(
                "export_corruption",
                "export",
                f"The release is not a real directory: {release}",
                "inspect",
            )
        actual_paths = self._release_paths(release)
        expected_paths = tuple(str(entry.path) for entry in site.entries)
        if actual_paths != expected_paths:
            raise PublishError(
                "export_corruption",
                "export",
                "The release path set differs from the archive",
                "inspect",
            )
        for entry in site.entries:
            path = release.joinpath(*PurePosixPath(str(entry.path)).parts)
            if path.stat().st_size != entry.size:
                raise PublishError(
                    "export_corruption",
                    "export",
                    f"The release size differs from the archive: {entry.path}",
                    "inspect",
                )
            object_id = _git.hash_file(self.config.archive, path, self.deadline, write=False)
            if object_id != entry.blob:
                raise PublishError(
                    "export_corruption",
                    "export",
                    f"The release bytes differ from the archive: {entry.path}",
                    "inspect",
                )

    def _selection(self, name: Name, *, full: bool) -> Selection:
        link = self.config.runtime / "public" / str(name)
        try:
            info = link.lstat()
        except FileNotFoundError:
            return Selection("absent")
        if not stat.S_ISLNK(info.st_mode):
            return Selection("degraded", detail="The public entry is not a symbolic link")
        raw_target = Path(os.readlink(link))
        if raw_target.is_absolute():
            return Selection("degraded", detail="The public link uses an absolute target")
        try:
            resolved = (link.parent / raw_target).resolve(strict=True)
            releases = (self.config.runtime / "releases").resolve(strict=True)
        except OSError as error:
            return Selection("degraded", detail=f"The public link is dangling: {error}")
        if resolved.parent != releases or resolved.is_symlink() or not resolved.is_dir():
            return Selection("degraded", detail="The public link escapes the release directory")
        revision = Revision(resolved.name)
        if not self.config.archive.exists() or not self._reachable(name, revision):
            return Selection(
                "degraded",
                revision=revision,
                release=resolved,
                detail="The selected revision is not reachable for this name",
            )
        if full:
            self._validate_release(resolved, self._site(str(revision)))
        return Selection("selected", revision, resolved, full)

    def _state(self, name: Name, *, full: bool) -> LocalState:
        return LocalState(self._saved(name), self._selection(name, full=full))

    def _observe_after_error(self, name: Name) -> LocalState:
        try:
            return self._state(name, full=False)
        except (OSError, PublishError) as error:
            return LocalState(None, Selection("unobserved", detail=str(error)))

    def _save(self, name: Name, captured: CapturedSite, expected_head: str | None) -> SavedPage:
        self._ensure_archive()
        file_blobs: list[tuple[PurePosixPath, str]] = []
        for entry in captured.entries:
            source = captured.root.joinpath(*PurePosixPath(str(entry.path)).parts)
            blob = _git.hash_file(self.config.archive, source, self.deadline, write=True)
            if blob != entry.blob:
                raise PublishError(
                    "archive_failure",
                    "archive",
                    f"Git produced a different blob identity for {entry.path}",
                    "inspect",
                )
            file_blobs.append((PurePosixPath(str(entry.path)), blob))
        site_tree = _git.make_site_tree(self.config.archive, file_blobs, self.deadline)
        if site_tree != str(captured.revision):
            raise PublishError(
                "archive_failure",
                "archive",
                "The archive produced a different site revision from capture",
                "inspect",
            )

        root_entries: list[_git.TreeEntry] = []
        name_entries: list[_git.TreeEntry] = []
        if expected_head is not None:
            root_entries = list(
                _git.read_tree(self.config.archive, f"{expected_head}^{{tree}}", self.deadline)
            )
            existing_name = next((entry for entry in root_entries if entry.name == str(name)), None)
            if existing_name is not None:
                if existing_name.kind != "tree":
                    raise PublishError(
                        "archive_corrupt",
                        "archive",
                        f"The archive record for {name} is not a tree",
                        "inspect",
                    )
                name_entries = list(
                    _git.read_tree(self.config.archive, existing_name.object_id, self.deadline)
                )
        name_entries = [entry for entry in name_entries if entry.name != "site"]
        name_entries.append(_git.TreeEntry("040000", "tree", site_tree, "site"))
        name_tree = _git.make_tree(self.config.archive, name_entries, self.deadline)
        root_entries = [entry for entry in root_entries if entry.name != str(name)]
        root_entries.append(_git.TreeEntry("040000", "tree", name_tree, str(name)))
        root_tree = _git.make_tree(self.config.archive, root_entries, self.deadline)

        commit_args = ["commit-tree", root_tree]
        if expected_head is not None:
            commit_args.extend(["-p", expected_head])
        commit = (
            _git.command(
                self.config.archive,
                commit_args,
                self.deadline,
                input_bytes=f"publish {name} {captured.revision}\n".encode(),
            )
            .decode()
            .strip()
        )
        zero = "0" * (40 if self.config.object_format == "sha1" else 64)
        _git.command(
            self.config.archive,
            ["update-ref", ARCHIVE_REF, commit, expected_head or zero],
            self.deadline,
        )
        return SavedPage(
            StoredSite(Revision(site_tree), captured.entries),
            Commit(commit),
        )

    def _materialize(self, site: StoredSite) -> Path:
        release = self.config.runtime / "releases" / str(site.revision)
        if release.exists() or release.is_symlink():
            self._validate_release(release, site)
            return release
        stage = self.config.runtime / "staging" / f"release-{uuid.uuid4().hex}"
        stage.mkdir(mode=0o700)
        try:
            for entry in site.entries:
                destination = stage.joinpath(*PurePosixPath(str(entry.path)).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _git.export_blob(self.config.archive, entry.blob, destination, self.deadline)
                os.chmod(destination, 0o644)
            directories = [path for path in stage.rglob("*") if path.is_dir()]
            for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
                _fsync_directory(directory)
            _fsync_directory(stage)
            self._validate_release(stage, site)
            os.rename(stage, release)
            _fsync_directory(release.parent)
        except Exception as error:
            if isinstance(error, PublishError):
                raise
            usage = sum(
                item.stat(follow_symlinks=False).st_size
                for item in stage.rglob("*")
                if item.is_file()
            )
            raise PublishError(
                "export_failure",
                "export",
                f"The committed release could not be materialized in {stage}"
                f" ({usage} bytes): {error}",
                "inspect",
            ) from error
        return release

    def _diff(self, before: StoredSite | None, after: CapturedSite) -> dict[str, object]:
        old = {str(entry.path): entry.blob for entry in before.entries} if before else {}
        new = {str(entry.path): entry.blob for entry in after.entries}
        return {
            "added": sorted(new.keys() - old.keys()),
            "changed": sorted(path for path in new.keys() & old.keys() if new[path] != old[path]),
            "deleted": sorted(old.keys() - new.keys()),
        }

    def _verify(
        self,
        name: Name,
        revision: Revision,
        release: Path,
        site: StoredSite,
        removed: tuple[str, ...] = (),
    ) -> Verification:
        try:
            return verify(
                self.config.base_url,
                name,
                revision,
                release,
                site,
                self.deadline,
                self.config.limits.verification_seconds,
                removed=removed,
            )
        except PublishError as error:
            if error.failure.phase == "verify":
                raise
            raise PublishError(
                error.failure.code,
                "verify",
                error.failure.message,
                error.failure.next_action,
                error.failure.required_inputs,
            ) from error

    def _publish_captured(
        self,
        operation: _PublishingOperation,
        name: Name,
        captured: CapturedSite,
        target: str,
        expected_revision: Revision | None,
        request_id: str | None,
    ) -> Report:
        effects = Effects()
        verification = Verification()
        try:
            with self._lock(create=True):
                state = self._state(name, full=True)
                decision = _decide_publication(state, captured.revision, expected_revision)
                if isinstance(decision, Failure):
                    raise PublishError(
                        decision.code,
                        decision.phase,
                        decision.message,
                        decision.next_action,
                        decision.required_inputs,
                    )
                previous_paths: set[str] = set()
                if (
                    decision == "update"
                    and state.selection.kind == "selected"
                    and state.selection.revision is not None
                ):
                    previous_paths = set(self._tree_paths(str(state.selection.revision)))
                if decision == "unchanged":
                    assert state.selection.kind == "selected"
                    assert state.selection.release is not None
                    site = self._site(str(captured.revision))
                    verification = self._verify(
                        name,
                        captured.revision,
                        state.selection.release,
                        site,
                    )
                    return Report(
                        operation,
                        "unchanged",
                        target,
                        name,
                        publication_url(target, name),
                        request_id,
                        expected_revision,
                        captured.revision,
                        state,
                        effects,
                        verification,
                        captured.warnings,
                    )
                saved = state.saved
                if saved is None or saved.site.revision != captured.revision:
                    expected_head = self._head()
                    saved = self._save(name, captured, expected_head)
                    effects = Effects(True, False)
                release = self._materialize(saved.site)
                staged_link = self.config.runtime / "staging" / f"link-{uuid.uuid4().hex}"
                public_link = self.config.runtime / "public" / str(name)
                os.symlink(f"../releases/{saved.site.revision}", staged_link)
                _fsync_directory(staged_link.parent)
                try:
                    os.replace(staged_link, public_link)
                except OSError as error:
                    raise PublishError(
                        "activation_failure",
                        "activate",
                        f"The public selection could not be replaced: {error}",
                        "inspect",
                    ) from error
                effects = Effects(effects.archive_advanced, True)
                try:
                    _fsync_directory(public_link.parent)
                except OSError as error:
                    raise PublishError(
                        "persistence_failure",
                        "activate",
                        f"The selected publication could not be persisted: {error}",
                        "inspect",
                    ) from error
                selected = self._state(name, full=True)
                active_paths = {str(entry.path) for entry in saved.site.entries}
                removed = tuple(
                    path
                    for path in sorted(previous_paths - active_paths)
                    if not any(new_path.startswith(f"{path}/") for new_path in active_paths)
                )
                verification = self._verify(
                    name,
                    saved.site.revision,
                    release,
                    saved.site,
                    removed=removed,
                )
                return Report(
                    operation,
                    "published",
                    target,
                    name,
                    publication_url(target, name),
                    request_id,
                    expected_revision,
                    captured.revision,
                    selected,
                    effects,
                    verification,
                    captured.warnings,
                )
        except (OSError, PublishError) as error:
            failure = (
                error.failure
                if isinstance(error, PublishError)
                else Failure("operation_failure", "unknown", str(error), "inspect")
            )
            state = self._observe_after_error(name)
            if failure.code == "git_timeout" and failure.phase == "archive":
                effects = Effects(None, effects.activated)
            if failure.phase == "verify":
                verification = Verification(
                    "failed",
                    state.selection.revision,
                    probe_location="host",
                    detail=failure.message,
                )
            return Report(
                operation,
                "error",
                target,
                name,
                publication_url(target, name),
                request_id,
                expected_revision,
                captured.revision,
                state,
                effects,
                verification,
                captured.warnings,
                failure,
            )

    def plan(
        self,
        name: Name,
        source: Path,
        target: str,
        expected_revision: Revision | None = None,
    ) -> Report:
        captured: CapturedSite | None = None
        state: LocalState | None = None
        try:
            self._check_target(target)
            self._check_source_separation(source)
            object_format = self._archive_format()
            with tempfile.TemporaryDirectory(prefix="html-publish-plan-") as temporary:
                captured = capture(
                    source,
                    Path(temporary),
                    object_format,
                    self.config.limits,
                    self.deadline,
                )
                with self._lock(create=False):
                    state = self._state(name, full=True)
                    selected_site = (
                        self._site(str(state.selection.revision))
                        if state.selection.kind == "selected" and state.selection.revision
                        else state.saved.site
                        if state.saved
                        else None
                    )
                    decision = _decide_publication(
                        state,
                        captured.revision,
                        expected_revision,
                    )
                prediction = "conflict" if isinstance(decision, Failure) else decision
                details = {
                    "prediction": prediction,
                    "file_count": len(captured.entries),
                    "byte_count": captured.total_bytes,
                    "differences": self._diff(selected_site, captured),
                    "limits": {
                        "max_bytes": self.config.limits.max_bytes,
                        "max_files": self.config.limits.max_files,
                    },
                }
                return Report(
                    "plan",
                    "planned",
                    target,
                    name,
                    publication_url(target, name),
                    expected_revision=expected_revision,
                    requested_revision=captured.revision,
                    state=state,
                    warnings=captured.warnings,
                    details=details,
                )
        except (OSError, PublishError) as error:
            failure = (
                error.failure
                if isinstance(error, PublishError)
                else Failure("validation_failure", "validate", str(error), "fix_input")
            )
            return Report(
                "plan",
                "error",
                target,
                name,
                publication_url(target, name),
                expected_revision=expected_revision,
                requested_revision=captured.revision if captured else None,
                state=state,
                error=failure,
            )

    def publish(
        self,
        name: Name,
        source: Path,
        target: str,
        expected_revision: Revision | None,
        request_id: str | None,
    ) -> Report:
        captured: CapturedSite | None = None
        try:
            self._check_target(target)
            self._check_source_separation(source)
            object_format = self._archive_format()
        except (OSError, PublishError) as error:
            failure = (
                error.failure
                if isinstance(error, PublishError)
                else Failure("operation_failure", "unknown", str(error), "inspect")
            )
            return Report(
                "publish",
                "error",
                target,
                name,
                publication_url(target, name),
                request_id,
                expected_revision,
                error=failure,
            )
        with tempfile.TemporaryDirectory(prefix="html-publish-capture-") as temporary:
            try:
                captured = capture(
                    source,
                    Path(temporary),
                    object_format,
                    self.config.limits,
                    self.deadline,
                )
            except (OSError, PublishError) as error:
                failure = (
                    error.failure
                    if isinstance(error, PublishError)
                    else Failure("operation_failure", "unknown", str(error), "inspect")
                )
                return Report(
                    "publish",
                    "error",
                    target,
                    name,
                    publication_url(target, name),
                    request_id,
                    expected_revision,
                    captured.revision if captured else None,
                    error=failure,
                )
            return self._publish_captured(
                "publish",
                name,
                captured,
                target,
                expected_revision,
                request_id,
            )

    def verify_page(self, name: Name) -> Report:
        state: LocalState | None = None
        verification = Verification()
        delivery_attempted = False
        try:
            with self._lock(create=False):
                state = self._state(name, full=True)
                selection = state.selection
                if selection.kind == "degraded":
                    raise PublishError(
                        "state_degraded",
                        "verify",
                        selection.detail or "The selected export is degraded",
                        "inspect",
                    )
                if selection.kind == "unobserved":
                    raise PublishError(
                        "state_unobserved",
                        "verify",
                        selection.detail or "Selected state could not be observed",
                        "inspect",
                    )
                if (
                    selection.kind != "selected"
                    or selection.release is None
                    or selection.revision is None
                ):
                    raise PublishError(
                        "nothing_selected",
                        "verify",
                        "No active publication is selected for this name",
                        "publish",
                    )
                site = self._site(str(selection.revision))
                delivery_attempted = True
                verification = self._verify(name, selection.revision, selection.release, site)
                return Report(
                    "verify",
                    "verified",
                    self.config.base_url,
                    name,
                    publication_url(self.config.base_url, name),
                    state=state,
                    verification=verification,
                )
        except (OSError, PublishError) as error:
            failure = (
                error.failure
                if isinstance(error, PublishError)
                else Failure("observation_failure", "verify", str(error), "inspect")
            )
            if state is None:
                state = self._observe_after_error(name)
            if delivery_attempted:
                verification = Verification(
                    "failed",
                    state.selection.revision,
                    probe_location="host",
                    scope=("local_export",),
                    detail=failure.message,
                )
            return Report(
                "verify",
                "error",
                self.config.base_url,
                name,
                publication_url(self.config.base_url, name),
                state=state,
                verification=verification,
                error=failure,
            )

    def _entry_changes(
        self,
        name: Name,
        newer: str,
        older: str | None,
    ) -> dict[str, list[str]]:
        new_paths = self._tree_paths(newer)
        old_paths = self._tree_paths(older) if older is not None else {}
        return {
            "added": sorted(new_paths.keys() - old_paths.keys()),
            "changed": sorted(
                path
                for path in new_paths.keys() & old_paths.keys()
                if new_paths[path] != old_paths[path]
            ),
            "deleted": sorted(old_paths.keys() - new_paths.keys()),
        }

    def history(
        self,
        name: Name,
        limit: int,
        after: str | None,
        diff_revision: str | None,
    ) -> Report:
        try:
            commits = self._page_commits(name)
            if after is not None:
                if after not in commits:
                    raise PublishError(
                        "unreachable_revision",
                        "history",
                        "The continuation commit is not in the reachable page history",
                        "inspect",
                        ("archive_commit",),
                    )
                commits = commits[commits.index(after) + 1 :]
            window = commits[: limit + 1]
            truncated = len(window) > limit
            shown = window[:limit]
            entries: list[HistoryEntry] = []
            for index, commit in enumerate(shown):
                revision = self._page_tree_at(commit, name)
                if revision is None:
                    raise PublishError(
                        "archive_corrupt",
                        "history",
                        f"The page tree is missing at {commit}",
                        "inspect",
                    )
                older_commit = window[index + 1] if index + 1 < len(window) else None
                older = self._page_tree_at(older_commit, name) if older_commit is not None else None
                changes = self._entry_changes(name, revision, older)
                entries.append(HistoryEntry(Commit(commit), Revision(revision), changes))
            details: dict[str, object] = {
                "total": len(commits),
                "truncated": truncated,
                "continuation": shown[-1] if truncated and shown else None,
                "entries": [
                    {
                        "archive_commit": entry.archive_commit,
                        "archived_revision": entry.revision,
                        "changes": dict(entry.changes),
                    }
                    for entry in entries
                ],
            }
            if diff_revision is not None:
                head = self._head()
                if head is None or not self._reachable(name, Revision(diff_revision)):
                    raise PublishError(
                        "unreachable_revision",
                        "history",
                        "The diff revision is not reachable for this name",
                        "inspect",
                        ("archived_revision",),
                    )
                latest = self._page_tree_at(head, name)
                if latest is None:
                    raise PublishError(
                        "archive_corrupt",
                        "history",
                        f"The page tree is missing at {head}",
                        "inspect",
                    )
                raw = _git.command(
                    self.config.archive,
                    ["diff", "--no-color", "--unified=3", diff_revision, latest],
                    self.deadline,
                )
                text = raw.decode("utf-8", "replace")
                encoded_text = text.encode("utf-8")
                truncated_diff = len(encoded_text) > _DIFF_CAP_BYTES
                if truncated_diff:
                    text = encoded_text[:_DIFF_CAP_BYTES].decode("utf-8", "ignore")
                details["diff"] = {
                    "from": diff_revision,
                    "to": latest,
                    "truncated": truncated_diff,
                    "text": text,
                }
            return Report(
                "history",
                "observed",
                self.config.base_url,
                name,
                publication_url(self.config.base_url, name),
                details=details,
            )
        except (OSError, PublishError) as error:
            failure = (
                error.failure
                if isinstance(error, PublishError)
                else Failure("observation_failure", "history", str(error), "inspect")
            )
            return Report(
                "history",
                "error",
                self.config.base_url,
                name,
                publication_url(self.config.base_url, name),
                error=failure,
            )

    def restore(
        self,
        name: Name,
        archive_commit: str,
        target: str,
        expected_revision: Revision | None,
        request_id: str | None,
    ) -> Report:
        captured: CapturedSite | None = None
        try:
            self._check_target(target)
            commits = self._page_commits(name)
            if archive_commit not in commits:
                raise PublishError(
                    "unreachable_revision",
                    "restore",
                    "The restore commit is not reachable in the page history for this name",
                    "inspect",
                    ("archive_commit",),
                )
            revision = self._page_tree_at(archive_commit, name)
            if revision is None:
                raise PublishError(
                    "unreachable_revision",
                    "restore",
                    "The restore commit has no site for this name",
                    "inspect",
                    ("archive_commit",),
                )
        except (OSError, PublishError) as error:
            failure = (
                error.failure
                if isinstance(error, PublishError)
                else Failure("operation_failure", "restore", str(error), "inspect")
            )
            return Report(
                "restore",
                "error",
                target,
                name,
                publication_url(target, name),
                request_id,
                expected_revision,
                error=failure,
            )
        site = self._site(revision)
        with tempfile.TemporaryDirectory(prefix="html-publish-restore-") as temporary:
            try:
                root = Path(temporary) / "site"
                root.mkdir()
                total_bytes = 0
                for entry in site.entries:
                    destination = root.joinpath(*PurePosixPath(str(entry.path)).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _git.export_blob(self.config.archive, entry.blob, destination, self.deadline)
                    os.chmod(destination, 0o644)
                    total_bytes += entry.size
                captured = CapturedSite(
                    root,
                    site.entries,
                    Revision(revision),
                    total_bytes,
                    warnings_for(root / "index.html"),
                )
            except (OSError, PublishError) as error:
                failure = (
                    error.failure
                    if isinstance(error, PublishError)
                    else Failure("operation_failure", "restore", str(error), "inspect")
                )
                return Report(
                    "restore",
                    "error",
                    target,
                    name,
                    publication_url(target, name),
                    request_id,
                    expected_revision,
                    Revision(revision),
                    error=failure,
                )
            return self._publish_captured(
                "restore",
                name,
                captured,
                target,
                expected_revision,
                request_id,
            )

    def _staging_usage(self) -> dict[str, object] | None:
        staging = self.config.runtime / "staging"
        if not staging.is_dir():
            return None
        items = [item for item in staging.iterdir() if not item.name.startswith(".")]
        if not items:
            return None
        return {
            "entries": len(items),
            "bytes": sum(
                item.stat(follow_symlinks=False).st_size
                for item in staging.rglob("*")
                if item.is_file()
            ),
        }

    def _host_checks(
        self,
        name: Name | None,
        state: LocalState | None,
    ) -> tuple[dict[str, object], Verification, Failure | None]:
        checks: dict[str, object] = resolve_host(self.config.base_url)
        verification = Verification()
        if name is None:
            if "dns_error" in checks:
                checks["route"] = "unreachable"
                return checks, verification, None
            checks.update(
                reachability_probe(
                    self.config.base_url,
                    self.deadline,
                    self.config.limits.verification_seconds,
                )
            )
            checks["route"] = "ok" if "http_status" in checks else "unreachable"
            return checks, verification, None
        selection = state.selection if state is not None else Selection("absent")
        if selection.kind != "selected" or selection.release is None or selection.revision is None:
            checks["route"] = "not_checked"
            return checks, verification, None
        site = self._site(str(selection.revision))
        try:
            self._validate_release(selection.release, site)
        except PublishError as error:
            verification = Verification(
                "failed",
                selection.revision,
                probe_location="local",
                scope=("local_export",),
                detail=error.failure.message,
            )
            checks["route"] = "not_checked"
            return checks, verification, error.failure
        if "dns_error" in checks:
            checks["route"] = "unreachable"
            return checks, verification, None
        try:
            verification = self._verify(name, selection.revision, selection.release, site)
            checks["route"] = "ok"
        except PublishError as error:
            verification = Verification(
                "failed",
                selection.revision,
                probe_location="host",
                scope=("local_export",),
                detail=error.failure.message,
            )
            checks["route"] = "drift"
        return checks, verification, None

    def status(
        self,
        name: Name | None,
        after: Name | None,
        limit: int,
        host_check: bool = False,
    ) -> Report:
        try:
            with self._lock(create=False):
                staging = self._staging_usage()
                if name is not None:
                    state = self._state(name, full=False)
                    verification = Verification()
                    details: dict[str, object] = {}
                    status_error: Failure | None = None
                    selection = state.selection
                    if selection.kind == "degraded":
                        status_error = Failure(
                            "state_degraded",
                            "status",
                            selection.detail or "The selected export is degraded",
                            "inspect",
                        )
                    elif selection.kind == "unobserved":
                        status_error = Failure(
                            "state_unobserved",
                            "status",
                            selection.detail or "Selected state could not be observed",
                            "inspect",
                        )
                    if host_check:
                        checks, verification, host_failure = self._host_checks(name, state)
                        details["host_checks"] = checks
                        status_error = status_error or host_failure
                    if staging is not None:
                        details["staging"] = staging
                    return Report(
                        "status",
                        "observed",
                        self.config.base_url,
                        name,
                        publication_url(self.config.base_url, name),
                        state=state,
                        verification=verification,
                        error=status_error,
                        details=details,
                    )
                names: set[str] = set()
                head = self._head()
                if head is not None:
                    names.update(
                        entry.name
                        for entry in _git.read_tree(
                            self.config.archive,
                            f"{head}^{{tree}}",
                            self.deadline,
                        )
                        if entry.kind == "tree"
                    )
                public = self.config.runtime / "public"
                if public.is_dir():
                    names.update(entry.name for entry in os.scandir(public))
                ordered = sorted(
                    candidate for candidate in names if after is None or candidate > after
                )
                selected_names = ordered[:limit]
                entries = tuple(
                    StatusEntry(Name(candidate), self._state(Name(candidate), full=False))
                    for candidate in selected_names
                )
                details = {
                    "total": len(names),
                    "truncated": len(ordered) > limit,
                    "continuation": selected_names[-1] if len(ordered) > limit else None,
                    "staging": staging,
                }
                verification = Verification()
                if host_check:
                    checks, verification, _ = self._host_checks(None, None)
                    details["host_checks"] = checks
                return Report(
                    "status",
                    "observed",
                    self.config.base_url,
                    None,
                    None,
                    details=details,
                    status_entries=entries,
                    verification=verification,
                )
        except (OSError, PublishError) as error:
            failure = (
                error.failure
                if isinstance(error, PublishError)
                else Failure("observation_failure", "status", str(error), "inspect")
            )
            return Report(
                "status",
                "error",
                self.config.base_url,
                name,
                publication_url(self.config.base_url, name) if name else None,
                error=failure,
            )
