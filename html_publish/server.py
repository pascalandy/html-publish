from __future__ import annotations

import argparse
import functools
import http.server
import io
import os
import socket
import socketserver
import stat
import sys
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_CONDITIONAL_HEADERS = (
    "If-Match",
    "If-Modified-Since",
    "If-None-Match",
    "If-Range",
    "If-Unmodified-Since",
)
_HEALTH_PATH = "/_html-publish-health"
_HEALTH_BODY = b"ok\n"


@dataclass(frozen=True)
class ServerConfig:
    directory: Path
    bind: str
    port: int


class PublicationHTTPServer(http.server.ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        if not isinstance(host, str):
            raise TypeError("server bind address must be text")
        self.server_name = host
        self.server_port = port


class PublicationRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: socketserver.BaseServer,
        *,
        directory: str,
    ) -> None:
        self.root = Path(directory)
        super().__init__(request, client_address, server, directory=str(self.root))

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def list_directory(self, path: str | os.PathLike[str]) -> io.BytesIO | None:
        self.send_error(404, "File not found")
        return None

    def send_head(self) -> io.BytesIO | BinaryIO | None:
        if self.path == _HEALTH_PATH:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(_HEALTH_BODY)))
            self.end_headers()
            return io.BytesIO(_HEALTH_BODY)

        if not self._path_is_publishable():
            self.send_error(404, "File not found")
            return None
        if self._redirect_directory():
            return None

        removed_headers: dict[str, list[str]] = {}
        for name in _CONDITIONAL_HEADERS:
            values = self.headers.get_all(name)
            if values:
                removed_headers[name] = values
                del self.headers[name]
        try:
            return super().send_head()
        finally:
            for name, values in removed_headers.items():
                for value in values:
                    self.headers.add_header(name, value)

    def _redirect_directory(self) -> bool:
        url = urllib.parse.urlsplit(self.path)
        if url.path.endswith("/"):
            return False
        translated = Path(super().translate_path(self.path))
        if not translated.is_dir():
            return False
        location = f"{url.path.rsplit('/', 1)[-1]}/"
        if url.query:
            location = f"{location}?{url.query}"
        self.send_response(301)
        self.send_header("Location", location)
        self.end_headers()
        return True

    def _path_is_publishable(self) -> bool:
        translated = Path(super().translate_path(self.path))
        try:
            relative = translated.relative_to(self.root)
        except ValueError:
            return False

        try:
            resolved = translated.resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        if resolved == self.root or resolved.is_relative_to(self.root):
            return True
        if not relative.parts:
            return False

        selection = self.root / relative.parts[0]
        if not selection.is_symlink():
            return False
        try:
            target = Path(os.readlink(selection))
        except OSError:
            return False
        if (
            target.is_absolute()
            or len(target.parts) != 3
            or target.parts[:2] != ("..", "releases")
            or target.parts[2] in {"", ".", ".."}
        ):
            return False

        try:
            releases = (self.root.parent / "releases").resolve(strict=False)
            release = (releases / target.parts[2]).resolve(strict=False)
            selection_target = selection.resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        if selection_target != release:
            return False
        return resolved == release or resolved.is_relative_to(release)


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def _directory(value: str) -> Path:
    directory = Path(os.path.abspath(value))
    if sys.platform == "darwin" and len(directory.parts) > 1:
        alias = Path(directory.anchor) / directory.parts[1]
        expected = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var")}
        if alias in expected and alias.is_symlink() and alias.resolve() == expected[alias]:
            directory = expected[alias].joinpath(*directory.parts[2:])
    current = Path(directory.anchor)
    for component in (None, *directory.parts[1:]):
        if component is not None:
            current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        except OSError as error:
            raise argparse.ArgumentTypeError(f"directory is unavailable: {error}") from error
        if stat.S_ISLNK(mode):
            raise argparse.ArgumentTypeError(
                f"directory cannot contain an existing symlink: {current}"
            )
        if not stat.S_ISDIR(mode):
            raise argparse.ArgumentTypeError(f"directory ancestor is not a directory: {current}")
    return directory


def _parse_args(argv: Sequence[str] | None) -> ServerConfig:
    parser = argparse.ArgumentParser(description="Serve html-publish releases over HTTP")
    parser.add_argument("--directory", required=True, type=_directory)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=_port)
    arguments = parser.parse_args(argv)

    directory = arguments.directory
    if not isinstance(directory, Path):
        parser.error("directory must be a path")
    bind = arguments.bind
    port = arguments.port
    if not isinstance(bind, str) or not isinstance(port, int):
        parser.error("invalid server address")
    return ServerConfig(directory, bind, port)


def main(argv: Sequence[str] | None = None) -> int:
    config = _parse_args(argv)
    handler = functools.partial(PublicationRequestHandler, directory=str(config.directory))
    server = PublicationHTTPServer((config.bind, config.port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
