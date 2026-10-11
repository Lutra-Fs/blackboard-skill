from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Keep standalone (`python3 tests/test_cookie_store.py`) runs offline: any
# BrowserSession constructed here pins to the kimi transport (no probes, no
# CLIs). Cookie fetches are intercepted at the resolved-session seam — a
# FakeBrowser double or a patched backend impl — so no transport I/O happens.
os.environ.setdefault("BLACKBOARD_BROWSER_BACKEND", "kimi")

from blackboard import browser_backend
from blackboard import course_service as svc
from blackboard.browser_backend import BrowserSession
from blackboard.cookie_store import CookieStore
from site_fixture import EXAMPLE_SITE
from blackboard.cli_requests import BrowserOptions  # noqa: E402


EXAMPLE_DOMAINS = EXAMPLE_SITE.cookie_origins()


class FakeBrowser:
    """Minimal resolved-session double: records cookie fetches, never does I/O."""

    def __init__(self, cookies=None, error=None, backend="kimi"):
        self.backend = backend
        self.session = "blackboard-sync"
        self.fetched_urls: list[list[str]] = []
        self._cookies = list(cookies or [])
        self._error = error

    def cookies(self, urls):
        self.fetched_urls.append(list(urls))
        if self._error is not None:
            raise self._error
        return [dict(cookie) for cookie in self._cookies]


class CookieStoreTests(unittest.TestCase):
    def test_cookie_header_refetches_after_expiry(self) -> None:
        browser = FakeBrowser([{"name": "a", "value": "1"}])
        store = CookieStore(browser, domains=EXAMPLE_DOMAINS)
        clock = iter((1000.0, 1100.0, 2000.0))
        with patch("blackboard.cookie_store.time.time", side_effect=lambda: next(clock)):
            first = store.cookie_header(max_age_seconds=300)
            second = store.cookie_header(max_age_seconds=300)
            self.assertEqual(first, "a=1")
            self.assertEqual(first, second)
            self.assertEqual(len(browser.fetched_urls), 1)
            store.cookie_header(max_age_seconds=300)
        self.assertEqual(len(browser.fetched_urls), 2)

    def test_refresh_forces_refetch(self) -> None:
        browser = FakeBrowser([{"name": "a", "value": "1"}])
        store = CookieStore(browser, domains=EXAMPLE_DOMAINS)
        store.cookie_header()
        store.refresh()
        self.assertEqual(len(browser.fetched_urls), 2)

    def test_fetch_includes_httponly_cookies(self) -> None:
        store = CookieStore(FakeBrowser([
            {"name": "JSESSIONID", "value": "abc", "httpOnly": False, "domain": "x.example"},
            {"name": "BbRouter", "value": "long-value", "httpOnly": True, "domain": "y.example"},
            {"name": "samlCookie", "value": "saml", "httpOnly": True, "domain": "y.example"},
        ]), domains=EXAMPLE_DOMAINS)
        header = store.cookie_header()
        self.assertIn("JSESSIONID=abc", header)
        self.assertIn("BbRouter=long-value", header)
        self.assertIn("samlCookie=saml", header)

    def test_fetch_wraps_backend_failure_with_backend_name(self) -> None:
        browser = FakeBrowser(
            error=RuntimeError("kimi-webbridge daemon unreachable at http://127.0.0.1:10086/command: refused"),
            backend="kimi",
        )
        with self.assertRaises(RuntimeError) as ctx:
            CookieStore(browser, domains=EXAMPLE_DOMAINS).cookie_header()
        self.assertIn("unreachable", str(ctx.exception))
        self.assertIn("cookie fetch via kimi backend failed", str(ctx.exception))

    def test_multi_domain_passes_all_urls_to_backend(self) -> None:
        domains = (
            "https://learn.example.edu/",
            "https://example.leganto.exlibrisgroup.com/",
        )
        browser = FakeBrowser([{"name": "a", "value": "1"}])
        CookieStore(browser, domains=domains).cookie_header()
        self.assertEqual(browser.fetched_urls, [list(domains)])

    def test_skips_cookies_without_value(self) -> None:
        store = CookieStore(FakeBrowser([
            {"name": "good", "value": "v", "domain": "learn.example.edu"},
            {"name": "empty", "value": ""},
            {"value": "no-name"},
            {},
        ]), domains=EXAMPLE_DOMAINS)
        header = store.cookie_header()
        self.assertEqual(header, "good=v")

    def test_cookie_header_filters_by_origin(self) -> None:
        """A Blackboard request must not carry Leganto cookies (and vice versa)."""
        store = CookieStore(FakeBrowser([
            {"name": "BbRouter", "value": "bb-val", "domain": "learn.example.edu"},
            {"name": "JSESSIONID", "value": "js-val", "domain": "learn.example.edu"},
            {"name": "institute", "value": "leg-val", "domain": "example.leganto.exlibrisgroup.com"},
            {"name": "XSRF-TOKEN", "value": "xsrf-val", "domain": "example.leganto.exlibrisgroup.com"},
            {"name": "shared", "value": "s", "domain": ".example.edu"},
        ]), domains=EXAMPLE_DOMAINS)
        bb_header = store.cookie_header("https://learn.example.edu/learn/api/v1/courses")
        self.assertIn("BbRouter=bb-val", bb_header)
        self.assertIn("JSESSIONID=js-val", bb_header)
        self.assertIn("shared=s", bb_header)
        self.assertNotIn("institute=", bb_header)
        self.assertNotIn("XSRF-TOKEN=", bb_header)

        store.refresh()
        leg_header = store.cookie_header("https://example.leganto.exlibrisgroup.com/leganto/rl/files/ris/list/123")
        self.assertIn("institute=leg-val", leg_header)
        self.assertIn("XSRF-TOKEN=xsrf-val", leg_header)
        self.assertNotIn("BbRouter=", leg_header)
        self.assertNotIn("shared=", leg_header)

class SameBackendInvariantTests(unittest.TestCase):
    """Cookies always come from the browser the session resolved — never another one."""

    def test_service_session_store_reads_through_the_session_browser(self) -> None:
        session = svc.Session("_1_", BrowserOptions(browser_backend="kimi"), site_config=EXAMPLE_SITE)
        self.assertIs(session.cookies._browser, session.browser)
        self.assertEqual(session.browser.backend, "kimi")
        with patch.object(browser_backend._KimiImpl, "cookies",
                          return_value=[{"name": "BbRouter", "value": "x"}]) as kimi_cookies, \
                patch.object(browser_backend._EgoImpl, "cookies",
                             side_effect=AssertionError("ego must not be asked for cookies")):
            header = session.cookies.cookie_header()
        self.assertIn("BbRouter=x", header)
        kimi_cookies.assert_called_once()


if __name__ == "__main__":
    unittest.main()
