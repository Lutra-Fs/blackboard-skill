from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from blackboard.safe_fetch import (
    DownloadOutcome,
    FetchError,
    HttpFetcher,
    _LimitedRedirectHandler,
    filename_from_content_disposition,
)


class _FakeResponse:
    """Minimal stand-in for a urllib HTTPResponse."""

    def __init__(
        self,
        *,
        body: bytes = b"",
        status: int = 200,
        url: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self.status = status
        self._url = url
        self.headers = _HeaderMap(headers or {})

    def geturl(self) -> str:
        return self._url

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data

    def close(self) -> None:
        pass


class _HeaderMap(dict):
    """Case-insensitive header lookup mirroring urllib's http.client.HTTPMessage."""

    def get(self, key: str, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _fetcher_with_response(response: _FakeResponse, *, cookie_store=None) -> HttpFetcher:
    """Build an HttpFetcher whose opener returns the scripted response."""
    fetcher = HttpFetcher(cookie_store=cookie_store)
    opener = MagicMock()
    opener.open.return_value = response
    fetcher._opener = opener
    return fetcher


class DownloadCookieAndHeaderTests(unittest.TestCase):
    def test_download_streams_in_chunks_and_enforces_max_bytes(self) -> None:
        """A response larger than max_bytes aborts and leaves no target file."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "big.pdf"
            resp = _FakeResponse(body=b"x" * 4096, url="https://cdn.example.com/big")
            fetcher = HttpFetcher(max_bytes=100)
            fetcher._opener = MagicMock()
            fetcher._opener.open.return_value = resp
            with self.assertRaises(FetchError):
                fetcher.download("https://example.com/big", dest)
            self.assertFalse(dest.exists())

    def test_download_rejects_redirect_to_non_public_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.pdf"
            resp = _FakeResponse(body=b"x", url="http://internal.local/x")  # non-public
            fetcher = _fetcher_with_response(resp)
            with self.assertRaises(FetchError):
                fetcher.download("https://example.com/start", dest)

    def test_download_injects_cookie_only_when_store_present(self) -> None:
        disposition = "inline; filename*=UTF-8''MATH3015%20Week%201%20Exercises.pdf"
        body = b"%PDF-1.7\nfixture\n%%EOF\n"
        for store in (MagicMock(), None):
            with self.subTest(store=store), tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "out.pdf"
                if store is not None:
                    store.cookie_header.return_value = "JSESSIONID=abc; BbRouter=xyz"
                resp = _FakeResponse(
                    body=body, status=200, url="https://cdn.example.com/x",
                    headers={"Content-Disposition": disposition, "Content-Length": str(len(body))},
                )
                fetcher = _fetcher_with_response(resp, cookie_store=store)
                outcome = fetcher.download("https://learn.example.edu/xid-1", dest)
                self.assertIsInstance(outcome, DownloadOutcome)
                self.assertEqual(outcome.effective_url, "https://cdn.example.com/x")
                self.assertEqual(outcome.content_disposition, disposition)
                self.assertEqual(dest.read_bytes(), body)
                self.assertEqual(
                    filename_from_content_disposition(outcome.content_disposition),
                    "MATH3015 Week 1 Exercises.pdf",
                )
                # The request handed to opener.open carries the Cookie header
                # only when a cookie store was supplied.
                request = fetcher._opener.open.call_args[0][0]
                expected = "JSESSIONID=abc; BbRouter=xyz" if store is not None else None
                self.assertEqual(request.get_header("Cookie"), expected)


class ProbeTests(unittest.TestCase):
    def test_range_probe_uses_content_range_total_not_one_byte_length(self) -> None:
        resp = _FakeResponse(
            body=b"%", status=206, url="https://cdn.example.com/file",
            headers={"Content-Length": "1", "Content-Range": "bytes 0-0/12345"},
        )
        fetcher = _fetcher_with_response(resp)
        result = fetcher.probe("https://example.com/start")
        request = fetcher._opener.open.call_args[0][0]
        self.assertEqual(result["content_length"], 12345)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Range"), "bytes=0-0")


class EncodedDownloadTests(unittest.TestCase):
    def test_gzip_transfer_encoding_is_decoded_before_file_validation(self) -> None:
        body = b"%PDF-1.4\nfixture\n%%EOF\n"
        compressed = gzip.compress(body)
        resp = _FakeResponse(
            body=compressed, status=200, url="https://cdn.example.com/file.pdf",
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
                "Content-Type": "application/pdf",
            },
        )
        fetcher = _fetcher_with_response(resp)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "file.pdf"
            fetcher.download("https://example.com/start", dest)
            self.assertEqual(dest.read_bytes(), body)


class ContentDispositionParserTests(unittest.TestCase):
    def test_filename_from_content_disposition(self) -> None:
        cases = [
            ("inline; filename*=UTF-8''MATH3015%20Lecture%201%20Sets.pdf", "MATH3015 Lecture 1 Sets.pdf"),
            ('attachment; filename="My File.docx"', "My File.docx"),
            ("attachment; filename=report.pdf", "report.pdf"),
            ('inline; filename="token.pdf"; filename*=UTF-8\'\'Real%20Name.pdf', "Real Name.pdf"),
            ("inline; filename*=UTF-8''Lecture%201%20-%20annotated%20%28draft%29.pdf", "Lecture 1 - annotated (draft).pdf"),
            (None, None),
            ("", None),
            ("inline", None),
        ]
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(filename_from_content_disposition(header), expected)


class FetchJsonTests(unittest.TestCase):
    def test_fetch_json_raises_on_non_json(self) -> None:
        resp = _FakeResponse(
            body=b"<html>error</html>", status=200,
            url="https://learn.example.edu/api",
        )
        fetcher = _fetcher_with_response(resp)
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch_json("https://learn.example.edu/api")
        self.assertIn("non-JSON", str(ctx.exception))

    def test_fetch_json_raises_on_http_error_with_status(self) -> None:
        from urllib.error import HTTPError
        fetcher = HttpFetcher()
        opener = MagicMock()
        # HTTPError needs url, code, msg, hdrs, fp positional args.
        opener.open.side_effect = HTTPError(
            "https://x", 401, "Unauthorized", {}, None,
        )
        fetcher._opener = opener
        with self.assertRaises(FetchError) as ctx:
            fetcher.fetch_json("https://learn.example.edu/api")
        self.assertEqual(ctx.exception.http_status, 401)

    def test_fetch_json_includes_cookie_when_store_present(self) -> None:
        store = MagicMock()
        store.cookie_header.return_value = "BbRouter=xyz"
        resp = _FakeResponse(body=b'{"id": "_1_1"}', url="https://learn.example.edu/api")
        fetcher = _fetcher_with_response(resp, cookie_store=store)
        result = fetcher.fetch_json("https://learn.example.edu/api")
        self.assertEqual(result, {"id": "_1_1"})
        request = fetcher._opener.open.call_args[0][0]
        self.assertEqual(request.get_header("Cookie"), "BbRouter=xyz")
        # Accept: application/json is always set for fetch_json.
        self.assertEqual(request.get_header("Accept"), "application/json")


class CrossOriginCookieIsolationTests(unittest.TestCase):

    def test_redirect_to_same_host_keeps_cookie(self) -> None:
        """A redirect within the same host keeps the cookie (Leganto LTI)."""
        from urllib.request import Request
        handler = _LimitedRedirectHandler()
        origin = Request("https://example.leganto.exlibrisgroup.com/start",
                         headers={"Cookie": "session=abc"})
        same_host = "https://example.leganto.exlibrisgroup.com/export"
        new_req = handler.redirect_request(origin, None, 302, "Found", {}, same_host)
        self.assertIsNotNone(new_req)
        self.assertEqual(new_req.get_header("Cookie"), "session=abc")


if __name__ == "__main__":
    unittest.main()
