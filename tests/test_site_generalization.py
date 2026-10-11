from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from blackboard import course_service as svc
from blackboard.cli_requests import FetchRequest, BrowserOptions  # noqa: E402
from blackboard import cli
from blackboard.browser_backend import SessionRequired
from blackboard.archive_io import ArchiveIntegrityError
from blackboard.cookie_store import CookieStore
from blackboard.resource_store import Store
from blackboard.site_config import SiteConfig
from site_fixture import EXAMPLE_SITE
from blackboard.sync_models import Attachment, Page
from blackboard.ultra_content import canonical_ultra_course_url, parse_course_id
from blackboard.url_policy import is_blackboard_url, is_trusted_attachment_source_url
from blackboard.content_rules import embedded_html_linked_download


CUSTOM = SiteConfig(
    origin="https://blackboard.example.edu",
    auth_mode="browser-session",
    trusted_hosts=("files.example.edu",),
    display_timezone="UTC",
)
CUSTOM_FILE = "https://files.example.edu/course/guide.pdf"
PORT_SITE = SiteConfig(
    origin="https://blackboard.example.edu:8443",
    auth_mode="browser-session",
    trusted_hosts=("files.example.edu",),
    display_timezone="UTC",
)


def _page(site= CUSTOM, attachment=True):
    return Page(
        "_1_1",
        "Week 1",
        None,
        None,
        0,
        ["Week 1"],
        f"{site.origin}/ultra/courses/_1_1/outline",
        "Source evidence",
        [Attachment("guide.pdf", CUSTOM_FILE, original_filename="guide.pdf")] if attachment else [],
    )


class SiteGeneralizationTests(unittest.TestCase):
    def test_site_config_rejects_ambiguous_suffix_and_accepts_ipv6_origin(self):
        with self.assertRaises(ValueError):
            SiteConfig(origin=CUSTOM.origin, trusted_host_suffixes=("com",))
        self.assertEqual(SiteConfig(origin="https://[2001:4860:4860::8888]").origin,
                         "https://[2001:4860:4860::8888]")

    def test_origin_matching_preserves_nondefault_port(self):
        matching = f"{PORT_SITE.origin}/ultra/courses/_1_1/outline"
        self.assertTrue(is_blackboard_url(matching, site_config=PORT_SITE))
        self.assertEqual(parse_course_id(matching, None, site_config=PORT_SITE), "_1_1")
        self.assertFalse(is_blackboard_url("https://blackboard.example.edu/ultra/courses/_1_1/outline", site_config=PORT_SITE))
        with self.assertRaises(RuntimeError):
            parse_course_id("https://blackboard.example.edu/ultra/courses/_1_1/outline", None, site_config=PORT_SITE)

    def test_custom_origin_is_explicit_and_allowlist_is_scoped(self):
        url = "https://blackboard.example.edu/ultra/courses/_1_1/outline"
        self.assertEqual(parse_course_id(url, None, site_config=CUSTOM), "_1_1")
        self.assertEqual(canonical_ultra_course_url("_1_1", site_config=CUSTOM), url)
        self.assertTrue(is_trusted_attachment_source_url(CUSTOM_FILE, site_config=CUSTOM))
        self.assertFalse(is_trusted_attachment_source_url("https://cdn.example.net/guide.pdf", site_config=CUSTOM))
        self.assertFalse(is_trusted_attachment_source_url("https://learn.example.edu/file", site_config=CUSTOM))

    def test_embedded_html_download_link_honors_site_trust_policy(self):
        wrapper = (
            '<div class="bb-embedded-html">'
            '<a href="{href}" aria-label="Download the file">Guide</a>'
            "</div>"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "wrapper.html"

            # Example deployment: a trusted example.edu host resolves.
            path.write_text(wrapper.format(href="https://documents.example.edu/guide.pdf"))
            linked = embedded_html_linked_download(path, site_config=EXAMPLE_SITE)
            self.assertEqual(linked, ("https://documents.example.edu/guide.pdf", "guide.pdf"))

            # A host outside the document suffixes is still rejected.
            path.write_text(wrapper.format(href="https://other.example.net/guide.pdf"))
            self.assertIsNone(embedded_html_linked_download(path, site_config=EXAMPLE_SITE))

            # A custom site accepts its trusted host and rejects an unlisted host.
            path.write_text(wrapper.format(href=CUSTOM_FILE))
            self.assertEqual(
                embedded_html_linked_download(path, site_config=CUSTOM),
                (CUSTOM_FILE, "guide.pdf"),
            )
            path.write_text(wrapper.format(href="https://documents.example.edu/guide.pdf"))
            self.assertIsNone(embedded_html_linked_download(path, site_config=CUSTOM))

    def test_capture_binds_site_and_course_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = svc.capture(
                root,
                "https://blackboard.example.edu/ultra/courses/_1_1/outline",
                None,
                site_config=CUSTOM,
                pages=[_page()],
                source_status={"rest": {"errors": []}},
            )
            self.assertTrue(result["published"])
            tree = json.loads((root / "_blackboard/course-tree.json").read_text())
            self.assertEqual(tree["site_origin"], CUSTOM.origin)
            with Store(root) as store:
                self.assertEqual(store.identity(), {"origin": CUSTOM.origin, "course_id": "_1_1"})
            with self.assertRaises(ArchiveIntegrityError):
                svc.capture(root, "_1_1", None, site_config=EXAMPLE_SITE, pages=[_page()], source_status={"rest": {}})

    def test_custom_session_reuses_authenticated_browser(self):
        for site in (CUSTOM, EXAMPLE_SITE):
            with self.subTest(origin=site.origin):
                fake_browser = MagicMock()
                with patch.object(svc, "BrowserSession", return_value=fake_browser):
                    session = svc.Session("_1_1", BrowserOptions(), site_config=site)
                    browser, fetcher = session.transport()
                    self.assertIs(browser, fake_browser)
                    self.assertIs(fetcher, session.fetcher)
                    self.assertEqual(session.transport(), (browser, fetcher))
                    fake_browser.open.assert_not_called()
                    fake_browser.eval_text.assert_not_called()
                    session.close()
                    fake_browser.close.assert_called_once()

    def test_custom_session_profile_includes_origin_port(self):
        fake_browser = MagicMock()
        fake_browser.session = "custom-session"
        fake_browser.backend = "kimi"
        fake_browser.open.return_value = "tab"
        fake_browser.eval_text.return_value = PORT_SITE.origin + "/ultra"
        with patch.object(svc, "BrowserSession", return_value=fake_browser) as browser_ctor:
            session = svc.Session("_1_1", BrowserOptions(), site_config=PORT_SITE)
            self.assertIn("blackboard-blackboard_example_edu_8443-1_1", browser_ctor.call_args.kwargs["session"])
            session.close()

    def test_invalid_stored_site_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            svc.capture(root, "_1_1", None, site_config=CUSTOM, pages=[_page()], source_status={"rest": {}})
            tree_path = root / "_blackboard/course-tree.json"
            tree = json.loads(tree_path.read_text())
            tree["site_config"]["display_timezone"] = "Not/AZone"
            tree_path.write_text(json.dumps(tree))
            with self.assertRaises(ArchiveIntegrityError):
                svc.resolve_site(None, tree)

    def test_fetch_rejects_state_identity_before_dry_run_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            svc.capture(root, "_1_1", None, site_config=CUSTOM, pages=[_page()], source_status={"rest": {}})
            state_path = root / "_blackboard/state.json"
            state = json.loads(state_path.read_text())
            state["archive_identity"]["origin"] = "https://other.example.edu"
            state_path.write_text(json.dumps(state))
            before = state_path.read_bytes()
            with self.assertRaises(ArchiveIntegrityError):
                svc.fetch(root, FetchRequest(dry_run=True), None)
            self.assertEqual((root / "_blackboard/state.json").read_bytes(), before)

    def test_stable_url_preserves_asset_identity_and_semantic_queries(self):
        from blackboard.redaction import stable_url
        from blackboard.resource_store import asset_id

        # Keep existing Blackboard asset identity byte-identical.
        self.assertEqual(
            stable_url("https://learn.example.edu/bbcswebdav/x.pdf?sig=abc&x=1"),
            "https://learn.example.edu/bbcswebdav/x.pdf",
        )
        self.assertEqual(
            stable_url("https://learn-xythos.content.blackboardcdn.com/f?X-Amz-Signature=s"),
            "https://learn-xythos.content.blackboardcdn.com/f",
        )
        self.assertEqual(
            asset_id("attachments", "https://learn.example.edu/bbcswebdav/x.pdf?sig=abc&x=1"),
            asset_id("attachments", "https://learn.example.edu/bbcswebdav/x.pdf"),
        )
        # Other URLs retain semantic query parameters.
        self.assertEqual(
            stable_url("https://files.example.edu/a.pdf?token=secret&version=2"),
            "https://files.example.edu/a.pdf?version=2",
        )
        # A custom CDN needs no special adapter for standard signature keys.
        self.assertEqual(
            stable_url("https://cdn.example.edu/f?signature=z&version=2"),
            "https://cdn.example.edu/f?version=2",
        )
        # bbcswebdav in the netloc alone never matched by default and still
        # does not: only the path marker or the host markers trigger.
        self.assertEqual(
            stable_url("https://bbcswebdav.example.edu/f?a=1&b=2"),
            "https://bbcswebdav.example.edu/f?a=1&b=2",
        )

    def test_cookie_filter_honors_host_only_and_path(self):
        # No fetch happens: the cached set is seeded directly, so the browser
        # argument is only the resolved-session the store delegates to.
        store = CookieStore(SimpleNamespace(backend="kimi"), domains=(CUSTOM.origin,))
        store._cached = [
            {"name": "host", "value": "1", "domain": "blackboard.example.edu", "hostOnly": True, "path": "/learn"},
            {"name": "domain", "value": "2", "domain": ".example.edu", "hostOnly": False, "path": "/"},
        ]
        store._cache_at = time.time()
        self.assertIn("host=1", store.cookie_header("https://blackboard.example.edu/learn/api"))
        self.assertIn("domain=2", store.cookie_header("https://blackboard.example.edu/learn/api"))
        self.assertNotIn("host=1", store.cookie_header("https://blackboard.example.edu/ultra"))
        self.assertNotIn("host=1", store.cookie_header("https://other.example.edu/learn/api"))
        self.assertIn("domain=2", store.cookie_header("https://other.example.edu/learn/api"))

if __name__ == "__main__":
    unittest.main()
