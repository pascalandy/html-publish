from __future__ import annotations

import contextlib
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from email.message import Message
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol, cast

from html_publish.model import Deadline, Name, PublishError, Revision, StoredSite, Verification


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        request: urllib.request.Request,
        file_pointer: BinaryIO,
        code: int,
        message: str,
        headers: Message,
        new_url: str,
    ) -> None:
        return None


class _Response(Protocol):
    status: int
    headers: Message

    def read(self, amount: int = -1) -> bytes: ...

    def read1(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


def publication_url(base_url: str, name: Name) -> str:
    return f"{base_url}{urllib.parse.quote(str(name), safe='')}/"


def _same_origin(left: urllib.parse.ParseResult, right: urllib.parse.ParseResult) -> bool:
    return (
        left.scheme.lower(),
        left.hostname,
        left.port,
    ) == (
        right.scheme.lower(),
        right.hostname,
        right.port,
    )


def _open(
    url: str,
    publication_root: str,
    deadline: Deadline,
    timeout_cap: float,
) -> tuple[_Response, int, Message, str]:
    opener = urllib.request.build_opener(_NoRedirect())
    original = urllib.parse.urlparse(publication_root)
    current = url
    for _ in range(6):
        request = urllib.request.Request(
            current,
            headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"},
        )
        try:
            response = cast(
                _Response,
                opener.open(request, timeout=deadline.remaining(timeout_cap)),
            )
        except urllib.error.HTTPError as error:
            response = cast(_Response, error)
        except (OSError, urllib.error.URLError) as error:
            raise PublishError(
                "delivery_failure",
                "verify",
                f"The delivery request failed for {current}: {error}",
                "inspect",
            ) from error

        status = response.status
        if status not in {301, 302, 303, 307, 308}:
            return response, status, response.headers, current
        location = response.headers.get("Location")
        response.close()
        if location is None:
            raise PublishError(
                "delivery_failure",
                "verify",
                f"A redirect from {current} omitted Location",
                "fix_route",
            )
        redirected = urllib.parse.urljoin(current, location)
        parsed = urllib.parse.urlparse(redirected)
        if not _same_origin(original, parsed) or not parsed.path.startswith(original.path):
            raise PublishError(
                "delivery_failure",
                "verify",
                f"A redirect escaped the publication boundary: {redirected}",
                "fix_route",
            )
        current = redirected
    raise PublishError(
        "delivery_failure",
        "verify",
        f"The delivery request exceeded the redirect limit: {url}",
        "fix_route",
    )


def _compare(response: _Response, expected: Path, deadline: Deadline) -> int:
    compared = 0
    try:
        with expected.open("rb") as source:
            while True:
                deadline.remaining()
                actual_chunk = response.read1(64 * 1024)
                deadline.remaining()
                if not actual_chunk:
                    if source.read(1):
                        raise PublishError(
                            "delivery_failure",
                            "verify",
                            f"Delivered bytes differ from the committed file: {expected.name}",
                            "inspect",
                        )
                    return compared
                expected_chunk = source.read(len(actual_chunk))
                if actual_chunk != expected_chunk:
                    raise PublishError(
                        "delivery_failure",
                        "verify",
                        f"Delivered bytes differ from the committed file: {expected.name}",
                        "inspect",
                    )
                compared += len(actual_chunk)
    except PublishError:
        raise
    except OSError as error:
        raise PublishError(
            "delivery_failure",
            "verify",
            f"The delivery response for {expected.name} could not be read: {error}",
            "retry",
        ) from error


def _path_url(root: str, relative: str) -> str:
    encoded = "/".join(
        urllib.parse.quote(component, safe="") for component in PurePosixPath(relative).parts
    )
    return urllib.parse.urljoin(root, encoded)


def verify(
    base_url: str,
    name: Name,
    revision: Revision,
    release: Path,
    site: StoredSite,
    deadline: Deadline,
    timeout_cap: float,
    removed: Sequence[str] = (),
) -> Verification:
    root_url = publication_url(base_url, name)
    bytes_checked = 0
    response, status, headers, final_url = _open(root_url, root_url, deadline, timeout_cap)
    with contextlib.closing(response):
        if status != 200:
            raise PublishError(
                "delivery_failure",
                "verify",
                f"The publication URL returned HTTP {status}: {final_url}",
                "inspect",
            )
        if headers.get_content_type() != "text/html":
            raise PublishError(
                "delivery_failure",
                "verify",
                f"The publication URL returned {headers.get_content_type()}, expected text/html",
                "fix_route",
            )
        bytes_checked += _compare(response, release / "index.html", deadline)

    for entry in site.entries:
        url = _path_url(root_url, str(entry.path))
        response, status, headers, final_url = _open(url, root_url, deadline, timeout_cap)
        with contextlib.closing(response):
            if status != 200:
                raise PublishError(
                    "delivery_failure",
                    "verify",
                    f"An expected file returned HTTP {status}: {final_url}",
                    "inspect",
                )
            if str(entry.path) == "index.html" and headers.get_content_type() != "text/html":
                raise PublishError(
                    "delivery_failure",
                    "verify",
                    f"index.html returned {headers.get_content_type()}, expected text/html",
                    "fix_route",
                )
            bytes_checked += _compare(
                response, release.joinpath(*PurePosixPath(str(entry.path)).parts), deadline
            )

    missing_url = urllib.parse.urljoin(root_url, f".html-publish-missing-{secrets.token_hex(8)}")
    response, status, _, final_url = _open(missing_url, root_url, deadline, timeout_cap)
    with contextlib.closing(response):
        if status != 404:
            raise PublishError(
                "delivery_failure",
                "verify",
                f"A missing path returned HTTP {status}, expected 404: {final_url}",
                "fix_route",
            )

    checked_removed = 0
    for path in removed:
        removed_url = _path_url(root_url, path)
        response, status, _headers, final_url = _open(removed_url, root_url, deadline, timeout_cap)
        with contextlib.closing(response):
            if status == 200:
                raise PublishError(
                    "delivery_failure",
                    "verify",
                    f"A removed path is still served: {final_url}",
                    "inspect",
                )
            if status not in {404, 410}:
                raise PublishError(
                    "delivery_failure",
                    "verify",
                    f"A removed path returned HTTP {status}: {final_url}",
                    "fix_route",
                )
            checked_removed += 1

    from datetime import UTC, datetime

    scope = ["local_export", "directory_url", "index_html", "all_files", "missing_path"]
    if checked_removed:
        scope.append("removed_paths")

    return Verification(
        result="passed",
        revision=revision,
        checked_at=datetime.now(UTC).isoformat(),
        probe_location="host",
        files_checked=len(site.entries),
        bytes_checked=bytes_checked,
        scope=tuple(scope),
    )


def resolve_host(base_url: str) -> dict[str, object]:
    parsed = urllib.parse.urlparse(base_url)
    hostname = parsed.hostname
    if not hostname:
        return {"dns_error": "The configured base URL has no hostname"}
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError as error:
        return {"dns_error": str(error)}
    return {"dns_resolved": sorted({info[4][0] for info in infos})}


def reachability_probe(base_url: str, deadline: Deadline, timeout_cap: float) -> dict[str, object]:
    request = urllib.request.Request(
        base_url,
        headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(request, timeout=deadline.remaining(timeout_cap)) as response:
            return {"http_status": response.status}
    except urllib.error.HTTPError as error:
        return {"http_status": error.code}
    except (OSError, urllib.error.URLError) as error:
        return {"http_error": str(error)}
