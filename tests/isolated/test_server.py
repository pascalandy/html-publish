"""The catalogue follows the effective address of its bound listener.

Failure modes:
F1: server: a wildcard or non-loopback bind exposes catalogue history and reads the archive
F2: server: an IPv6 loopback address is rejected by numeric loopback classification
"""

from __future__ import annotations

import functools
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from html_publish.model import Config, Limits
from html_publish.server import PublicationHTTPServer, PublicationRequestHandler
from html_publish.store import PublicationStore


class CatalogueListenerBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="html-publish-listener-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.public = self.root / "runtime" / "public"
        page = self.public / "witness" / "index.html"
        page.parent.mkdir(parents=True)
        self.expected_page = b"synthetic stable page\n"
        page.write_bytes(self.expected_page)
        self.config = Config(
            archive=self.root / "archive.git",
            runtime=self.root / "runtime",
            base_url="http://127.0.0.1/",
            allow_http=True,
            object_format="sha1",
            limits=Limits(),
        )
        handler = functools.partial(
            PublicationRequestHandler,
            directory=str(self.public),
            publisher=self.config,
        )
        self.server = PublicationHTTPServer(("127.0.0.1", 0), handler)
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.worker.join(timeout=5)
        self.server.server_close()

    def _request(self, path: str) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_non_loopback_effective_addresses_keep_static_files_and_skip_catalogue(self) -> None:
        """Proves F1 with loopback transport and simulated post-bind addresses."""
        self.assertEqual(self.server.socket.getsockname()[0], "127.0.0.1")
        for effective_host in ("0.0.0.0", "192.0.2.10", "::"):
            with self.subTest(effective_host=effective_host):
                # Simulate the address reported by the bound listener; the real socket stays
                # bound to IPv4 loopback, so this test opens no shared-network listener.
                self.server.server_address = (effective_host, self.server.server_port)
                with patch.object(
                    PublicationStore, "catalogue", autospec=True, return_value=[]
                ) as catalogue_read:
                    self.assertEqual(self._request("/witness/"), (200, self.expected_page))
                    self.assertEqual(self._request("/_catalogue/")[0], 404)
                    self.assertEqual(self._request("/_catalogue")[0], 404)
                    catalogue_read.assert_not_called()

    def test_ipv6_loopback_effective_address_keeps_catalogue_route(self) -> None:
        """Proves F2 through the handler using the IPv4 loopback transport."""
        self.server.server_address = ("::1", self.server.server_port, 0, 0)
        with patch.object(PublicationStore, "catalogue", autospec=True, return_value=[]) as read:
            self.assertEqual(self._request("/_catalogue/")[0], 200)
            read.assert_called_once()


if __name__ == "__main__":
    unittest.main()
